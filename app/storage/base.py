"""存储后端协议：把「建目录 / 上传 / 分享 / 改名 / 删除 / 列目录」抽成一层。

为什么要有这层：以前只有 rclone 一条路，代码里到处是
`f"{RCLONE_REMOTE}:{DRIVE_ROOT}/{code}"` 和 subprocess 拼参数。要接 OpenList
（原生 REST）或 CloudDrive2（gRPC）时，如果各写一遍上传/记账流程，三条链路
（网页、bot、补刮）迟早行为不一致。所以先定协议，实现只负责翻译。

路径约定（重要，别再各写一套）：
  · 协议里的 path 一律是「后端内相对路径」，形如 `X-Drive/SSIS-555`；
    不带 `remote:` 前缀，也不带开头的 `/`；
  · rclone 适配器内部拼成 `gdrive:X-Drive/SSIS-555`，
    OpenList / CloudDrive2 内部拼成 `/X-Drive/SSIS-555`；
  · 目录名以服务端返回的为准：CloudDrive2 会把 `*?:/\\<>|"` 转成全角并削掉
    尾部空白与点号，所以 `real_folder()` 要能问出「实际落地叫什么」。

上传语义：`upload_dir` 返回时，文件必须**已经真的在云端**。
CloudDrive2 的写入只是塞进上传队列，适配器必须轮询到任务完成才算数 —— 否则
清单里会记上一个还不存在的文件，之后的校核与补刮全会错。

错误只分两类，够 retry 分层用：
  StorageError —— 可重试（网络抖动、5xx、超时、连接断）
  AuthError    —— 不重试（账号密码错、只读、权限不足），命中 retry 的 config 类
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable


class StorageError(RuntimeError):
    """可重试的存储错误。文案里要带后端名，方便在任务时间线上看出是谁出的问题。"""


class AuthError(StorageError):
    """配置/权限问题：重试一万次也一样，直接进 retry 的 config 类。"""


class NotFoundError(StorageError):
    """路径不存在。

    与「读失败」要分开：读一个**还不存在**的目录是正常情况（提交前的
    快照就是这样），不是故障；读失败（网络/权限/缓存）才是问题。
    """


@dataclass(frozen=True)
class FileEntry:
    """远端一个文件：名字 + 字节数。校核只比这两项。"""

    name: str
    size: int = 0


@dataclass(frozen=True)
class Quota:
    """云盘容量：总共多少、用了多少、还剩多少。字节为单位。

    为什么要有这个：本地下载取消后（2026-10-25）文件全程在云盘里，
    「还剩多少空间」是唯一有意义的空间指标 —— 报本地磁盘剩余空间
    对用户毫无参考价值，甚至会误导（以为本机磁盘满了就不能下了）。
    """

    total: int = 0
    used: int = 0
    free: int = 0

    @property
    def known(self) -> bool:
        """有没有真的问到数。拿不到时各字段为 0，别把 0 当成「用满了」。"""
        return self.total > 0 or self.used > 0 or self.free > 0


Progress = Callable[[float, str], None]
"""进度回调：(0..1, 一句人话)。没有可信进度的后端可以不回调。"""


@runtime_checkable
class Backend(Protocol):
    """一个存储后端要能做到的十件事。"""

    kind: str

    # ── 目录
    def ensure_dir(self, path: str) -> str:
        """确保目录存在，返回**实际落地**的目录名（可能被服务端归一化）。"""

    def list_dirs(self, path: str) -> list[str]:
        """列子目录名（改名查重用）。"""

    def list_files(self, path: str) -> list[FileEntry]:
        """列目录里的文件（名字 + 大小）。目录不存在应抛 StorageError。"""

    # ── 写入
    def upload_dir(self, local: Path, dest: str, *, transfers: int = 4,
                   chunk: str = "64M", progress: Progress | None = None) -> int:
        """把本地目录里的文件推进 dest，返回总字节数。返回即代表云端已落地。"""

    def upload_file(self, local: Path, dest: str) -> None:
        """推单个文件到 dest（dest 是完整文件路径）。"""

    # ── 分享
    def link(self, dest: str) -> str | None:
        """给目录要一个分享链接；没有这个能力的后端返回 None。"""

    # ── 变更
    def purge(self, dest: str) -> str:
        """递归删除目录，返回一句回执（例如「已删除」/「已移入回收站」）。"""

    def remove_dir(self, dest: str) -> str:
        """删除**空**目录（非递归）。目录非空时必须报错，不许连带删内容。

        为什么需要它：整理完源目录只剩空壳时，整目录 purge 也能删，但 purge 是
        递归的 —— 万一删除前有别的任务刚好往里落了文件，那份数据会跟着消失。
        非递归删除只对「确实是空的」生效，是这里唯一安全的做法。

        后端不支持时抛 StorageError（调用方据此保留目录壳）。
        """

    def rename_dir(self, src: str, new_name: str) -> None:
        """同父目录改名（归档改名只用这一种）。"""

    def move_file(self, src: str, dst: str) -> None:
        """把一个文件从 src 移到 dst（dst 是完整文件路径，含新名字）。

        云端整理用它：落地目录里的文件要按规范名搬进归档目录，全程远端，
        不下载。目标已存在时应抛错而不是覆盖。
        """

    # ── 自检
    def check(self, probe_path: str = "") -> tuple[bool, str]:
        """(可用, 说明)。说明里带上版本/配额之类有用信息。

        probe_path 是「在归档根目录下探测写权限」——有些后端（CloudDrive2）
        的 "/" 是聚合根，不允许建目录，只能在某个云盘目录里试。
        """

    # ── 容量
    def quota(self, probe_path: str = "") -> Quota:
        """问云盘还剩多少空间。拿不到就返回空的 Quota（known=False）。

        probe_path 是「问哪一层的容量」：CloudDrive2 的 "/" 是聚合根，
        报出来的数字离谱（各家云盘容量简单相加，还含未挂载的占位），
        必须问归档根目录那一层。
        """
