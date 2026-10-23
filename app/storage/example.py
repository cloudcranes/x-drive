"""自写后端骨架：照这十一个方法填完，就能当第四个后端用。

接入步骤（以「自家的网盘 API」为例）：
  1. 复制本文件为 `app/storage/mycloud.py`，类名改成 MyCloud，`kind = "mycloud"`；
  2. 在 `app/storage/__init__.py` 的 `KINDS` 里加上 "mycloud"，
     `_build()` 里加一个分支，`_fingerprint()` 里列出它依赖的设置键；
  3. 在 `app/settings.py` 的「存储」分组里加上它需要的键（密码类标 secret: True）；
  4. `app/webui.html` 不用改 —— 后端下拉框与「测试连接」是从设置项自动渲染的；
  5. 补一份 `tests/test_storage_mycloud.py`（照着 test_storage_cd2.py 的假服务端写法）。

三条硬要求（不满足就会在别处炸）：
  · **路径是后端内相对路径**，形如 `X-Drive/SSIS-555`，不要自己拼 `remote:`；
  · **upload_dir 返回即代表云端已落地**。若你的 API 是异步任务，必须在这里
    轮询到完成（参考 clouddrive2.py 的 _wait_upload），不能让上层以为传完了；
  · **错误分两类**：网络抖动/5xx 抛 StorageError（会被退避重试），
    账号密码错/只读/权限不足抛 AuthError（立刻失败，不浪费重试）。
"""
from __future__ import annotations

from pathlib import Path

from .base import AuthError, Backend, FileEntry, Progress, StorageError


class MyCloud(Backend):
    kind = "mycloud"

    def __init__(self, base: str = "", token: str = "", timeout: float = 30.0):
        if not base:
            raise AuthError("mycloud：没配 MYCLOUD_URL")
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    # ── 目录
    def ensure_dir(self, path: str) -> str:
        """建目录，返回实际落地的目录名（服务端若改名，以它为准）。"""
        raise StorageError("mycloud：ensure_dir 还没实现")

    def list_dirs(self, path: str) -> list[str]:
        raise StorageError("mycloud：list_dirs 还没实现")

    def list_files(self, path: str) -> list[FileEntry]:
        raise StorageError("mycloud：list_files 还没实现")

    # ── 写入
    def upload_file(self, local: Path, dest: str) -> None:
        raise StorageError("mycloud：upload_file 还没实现")

    def upload_dir(self, local: Path, dest: str, *, transfers: int = 4,
                   chunk: str = "64M", progress: Progress | None = None) -> int:
        self.ensure_dir(dest)
        files = sorted(p for p in Path(local).rglob("*") if p.is_file())
        total = sum(p.stat().st_size for p in files) or 1
        done = 0
        for index, path in enumerate(files, start=1):
            self.upload_file(path, f"{dest.rstrip('/')}/{path.name}")
            done += path.stat().st_size
            if progress:
                progress(min(done / total, 1.0), f"{index}/{len(files)} {path.name}")
        return done

    # ── 分享 / 变更
    def link(self, dest: str) -> str | None:
        return None                      # 没有分享能力就返回 None，界面会显示路径

    def purge(self, dest: str) -> str:
        raise StorageError("mycloud：purge 还没实现")

    def rename_dir(self, src: str, new_name: str) -> None:
        raise StorageError("mycloud：rename_dir 还没实现")

    # ── 自检
    def check(self) -> tuple[bool, str]:
        return False, "mycloud：check 还没实现"
