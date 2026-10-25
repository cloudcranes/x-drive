"""云端整理：把云盘里刚落地的那个目录，改成规范名并只留正片。

与本地链路（`pipeline.organize`）的差别：**没有本地文件可搬**，所有动作都是
远端的 `ensure_dir` / `move` / `rename` / `purge`。所以：

  · 选正片的规则沿用本地的（`fetcher` 里那套「有正片就丢样片」），只是决定
    「搬哪些」而不是「删哪些」；
  · 搬运是「目标目录里建好 → 逐个 move → 回读核验 → 归档记账」；
  · 记账后只逐项清理计划中的 drop；未知内容保留并报告，源目录外壳不删。
    清理路径只允许 inbox 与已确认的条目，不允许 `..` / 分隔符。

实测（CD2 1.0.18 + 迅雷云盘）：建目录、改名、移动、删除都可用；同网盘内一次
只能移动一个文件（CD2 的接口本身就是单文件循环，不受影响）。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

log = logging.getLogger("x-drive.cloudorganize")

VIDEO_EXT = {".mp4", ".mkv", ".avi", ".wmv", ".mov", ".m4v", ".ts", ".flv", ".rmvb", ".iso"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
# 广告/推广文件的名字特征：实测种子里夹着 `489155.com@xxx.mp4`、`最新地址获取…txt`
_AD_MARKS = ("489155.com", "最新地址", "最新位址", "获取", "直播", "收藏不迷路",
             "社区最新", "聚合全网", "推广", "广告")

# 路径安全：目录名不允许出现这些，避免拼出别的路径
_BAD_NAME = re.compile(r"[/\\]|\.\.")


class OrganizeError(RuntimeError):
    """整理失败（路径不安全、目标已存在、云端操作报错）。"""


@dataclass
class Plan:
    """一次云端整理的计划。"""

    source: str                      # inbox 下的落地目录名
    target: str                      # 目标目录名（规范名）
    move: list[str]                  # 要搬过去的文件名
    drop: list[str]                  # 留在原目录、随 purge 一起消失的
    size: int = 0
    sizes: dict = field(default_factory=dict)   # 文件名 → 落地时观测的字节数

    @property
    def kept(self) -> list[str]:
        return list(self.move)


def _suffix(name: str) -> str:
    return PurePosixPath(name).suffix.lower()


def is_ad(name: str) -> bool:
    """广告/推广文件的判断：名字里带推广标记，或者不是媒体文件。"""
    lower = (name or "").lower()
    if _suffix(name) in (VIDEO_EXT | IMAGE_EXT):
        return any(mark.lower() in lower for mark in _AD_MARKS)
    # 非媒体：txt / html / url 这些一律当广告（影像包也要靠扩展名留）
    return True


# 小于最大文件这个比例的，按样片/广告丢掉。0.25 是实测值：真片 4.4GB、
# 推广视频 14MB —— 差三个数量级，这条线画在哪都安全。
SAMPLE_RATIO = 0.25


def pick_main(files: list[dict]) -> list[dict]:
    """从落地目录里挑出要归档的文件。

    **以体积为准，不以名字为准**：实测那个种子的正片叫
    `489155.com@MIMK-290.mp4` —— 名字里带推广域名，但它是唯一的正片；
    而叫得像样的 `社 區 最 新 情 報.mp4` 只有 15MB，是广告。
    所以名字里的推广标记只用来解释「为什么丢掉」，不用来判定「要不要留」。
    """
    videos = [f for f in files if _suffix(str(f.get("name") or "")) in VIDEO_EXT]
    if videos:
        biggest = max(int(f.get("size") or 0) for f in videos)
        return [f for f in videos
                if int(f.get("size") or 0) >= max(biggest * SAMPLE_RATIO, 1)]
    images = [f for f in files if _suffix(str(f.get("name") or "")) in IMAGE_EXT]
    if images:
        biggest = max(int(f.get("size") or 0) for f in images)
        return [f for f in images
                if int(f.get("size") or 0) >= max(biggest * SAMPLE_RATIO, 1)]
    return []


def target_name(folder: str, files: list[dict]) -> str:
    """目标目录名：能认出番号就用番号，否则保留落地目录名（清洗后）。

    folder 为空 = 文件直接躺在收件目录根下（单文件种子实测如此），
    这时没有「落地目录名」可用，只能从**文件名**里认番号
    （MIMK-267ch.mp4 → MIMK-267）。
    """
    from . import codes

    base = codes.canonical(folder) or codes.canonical(
        " ".join(str(f.get("name") or "") for f in files[:4]))
    if base:
        return base
    safe = re.sub(r"[^0-9A-Za-z\u3040-\u30ff\u4e00-\u9fff._ -]+", "-", folder or "")
    safe = safe.strip(" .-")[:80]
    return safe or "unsorted"


def plan(source: str, files: list[dict], *, rename: bool = True,
         folder_code: str = "") -> Plan:
    """给落地目录出整理计划。纯函数，不发任何请求。

    folder_code 是**任务里的番号**（如 MIMK-267）—— 目标目录名优先用它。
    单文件种子落地时没有「落地目录名」（文件直接躺在收件目录根下），
    只能靠番号定名：从文件名认番号认不出来（`MIMK-267ch.mp4` 认不出）。
    """
    if _BAD_NAME.search(source or ""):
        raise OrganizeError(f"落地目录名不安全，拒绝整理：{source!r}")
    keep = pick_main(files)
    if not keep:
        raise OrganizeError(f"{source or '收件目录'} 里没有可归档的媒体文件（只有广告？）")
    kept_names = [str(f.get("name") or "") for f in keep if f.get("name")]
    if len(set(kept_names)) != len(kept_names):
        raise OrganizeError(f"{source or '收件目录'} 里有同名文件，云端无法区分：{kept_names}")
    drop = [str(f.get("name") or "") for f in files
            if str(f.get("name") or "") and str(f.get("name") or "") not in kept_names]
    target = (folder_code or "").strip()
    if not target:
        target = target_name(source, keep) if rename else source
    return Plan(source=source,
                target=target,
                move=kept_names,
                drop=drop,
                size=sum(int(f.get("size") or 0) for f in keep),
                sizes={str(f.get("name")): int(f.get("size") or 0) for f in keep})


def renamed_files(plan_obj: Plan, folder_code: str) -> list[tuple[str, str]]:
    """(原文件名, 新文件名)。单个文件用 `<番号><后缀>`，多个用 `<番号>-N<后缀>`。

    与本地 `pipeline.organize` 的命名保持一致，这样两条链路产出的归档目录
    结构相同，前端与校核逻辑不用分叉。
    """
    stem = re.sub(r"[^0-9A-Za-z-]+", "-", folder_code or "").strip("-") or "file"
    out: list[tuple[str, str]] = []
    if len(plan_obj.move) == 1:
        src = plan_obj.move[0]
        out.append((src, f"{stem}{_suffix(src) or '.mp4'}"))
        return out
    for index, src in enumerate(plan_obj.move, start=1):
        out.append((src, f"{stem}-{index}{_suffix(src) or '.mp4'}"))
    return out


def apply(backend, src_root: str, plan_obj: Plan, folder_code: str = "",
          dst_root: str = "") -> dict:
    """执行整理：建目标目录 → 逐个 move → **回读核验**。

    `src_root` 是落地目录所在的父目录（inbox），`dst_root` 是归档根目录。
    **两者必须分开传**：第一版把 dst 也算在 inbox 下面，结果文件只是原地改了名、
    广告也留着，而记账里写的却是归档路径 —— 账实不符。dst_root 省略时退回
    src_root（原地整理，仅用于「归档根就是落地目录」的部署）。

    本函数**不再删源目录**：曾经「move 完就 purge」，而观测只看了一层、
    记账又发生在清理之后 —— 源目录里有未观测的子文件/子目录会被一并删掉，
    归档写入失败时原始现场也已经没了。清理交给 `cleanup_source`，在
    **归档记录落盘之后**作为独立可重试步骤执行。

    返回 {target, folder, files, size}。任何一步失败都抛 OrganizeError，
    并**不动原目录**（除了已经搬走的那些文件 —— 这是远端的固有限制，
    所以 move 失败时会尽力搬回去，见 `_rollback`）。
    """
    if _BAD_NAME.search(plan_obj.source or "") or _BAD_NAME.search(plan_obj.target or ""):
        raise OrganizeError("目录名不安全，拒绝执行")
    src_base = src_root.strip("/")               # 后端内相对路径，不带前导斜杠
    dst_base = (dst_root or src_root).strip("/")
    # source 为空 = 文件直接躺在收件目录根下（单文件种子实测如此）
    src = f"{src_base}/{plan_obj.source}" if plan_obj.source else src_base
    dst = f"{dst_base}/{plan_obj.target}"
    if not src_base or not dst_base:
        raise OrganizeError("落地目录或归档根为空，拒绝执行")
    mapping = renamed_files(plan_obj, folder_code or plan_obj.target)
    if len({new for _old, new in mapping}) != len(mapping):
        raise OrganizeError(f"改名后出现重名：{mapping}")

    try:
        backend.ensure_dir(dst)
    except Exception as exc:                    # noqa: BLE001
        raise OrganizeError(f"建目标目录失败：{exc}") from exc

    moved: list[tuple[str, str]] = []
    for old, new in mapping:
        try:
            if old != new or src != dst:
                backend.move_file(f"{src}/{old}", f"{dst}/{new}")
        except Exception as exc:                # noqa: BLE001
            _rollback(backend, moved, dst, src)
            raise OrganizeError(f"移动 {old} 失败：{exc}") from exc
        moved.append((new, old))                # 记下反方向，便于回滚

    # 回读核验：目标目录里的每个文件，名字与大小都要对得上。
    # 不核验就记账，曾经出现过「云端没有/不完整，站内却显示已归档」的假账。
    files_out: list[str] = []
    try:
        actual = {str(e.name): int(e.size or 0) for e in backend.list_files(dst)}
    except Exception as exc:
        raise OrganizeError(f"核验目标目录失败（列 {dst} 出错）：{exc}") from exc
    for old, new in mapping:
        if new not in actual:
            raise OrganizeError(f"核验失败：{dst}/{new} 不在目标目录里")
        want_size = int(plan_obj.sizes.get(old) or 0)
        if want_size and actual[new] != want_size:
            raise OrganizeError(
                f"核验失败：{new} 大小不符（远端 {actual[new]}，落地时 {want_size}）")
        files_out.append(new)

    return {"target": plan_obj.target, "folder": dst,
            "files": files_out,
            "size": plan_obj.size,
            "dropped": plan_obj.drop}


def cleanup_source(backend, src_root: str, plan_obj: Plan, dst_root: str = "") -> str:
    """清理整理后已知的丢弃文件，保留源目录。归档落盘之后才允许调用。

    返回回执字符串。目标目录与源目录同名时拒绝 —— 那会把刚搬好的文件一起删掉。
    """
    if _BAD_NAME.search(plan_obj.source or "") or _BAD_NAME.search(plan_obj.target or ""):
        raise OrganizeError("目录名不安全，拒绝清理")
    src_base = src_root.strip("/")
    # source 为空 = 文件直接躺在收件目录根下（单文件种子实测如此）
    src = f"{src_base}/{plan_obj.source}" if plan_obj.source else src_base
    dst = f"{(dst_root or src_root).strip('/')}/{plan_obj.target}"
    if src == dst:
        raise OrganizeError(f"源目录与目标目录同名（{src}），拒绝清理")
    if not src_base:
        raise OrganizeError("落地目录为空，保留源目录")
    try:
        # 落地观察只读一层，子目录从未进入归档清单。清理前重新读取，
        # 未知文件、迟到的媒体和任何子目录都保留，让调用方挂 cleanup_pending。
        dirs = list(backend.list_dirs(src))
        files = list(backend.list_files(src))
        allowed = set(plan_obj.drop)
        unknown = [f"{name}/" for name in dirs]
        unknown.extend(str(entry.name) for entry in files if entry.name not in allowed)
        if unknown:
            raise OrganizeError(
                f"保留源目录 {src}：存在未归档或未知内容 {', '.join(unknown[:10])}")
        # 普通目录和 loose 都只逐项删除明确的 drop；后端没有非递归 rmdir
        # 契约，保留空目录外壳，杜绝回读后迟到文件被整目录 purge 连带删除。
        # 媒体已经 move，原路径通常不存在；删除失败向上抛，让残留被持久化。
        for entry in files:
            if _BAD_NAME.search(str(entry.name)):
                raise OrganizeError(f"清理文件名不安全：{entry.name!r}")
            backend.purge(f"{src}/{entry.name}")
        remaining_dirs = list(backend.list_dirs(src))
        remaining_files = list(backend.list_files(src))
        if remaining_dirs or remaining_files:
            raise OrganizeError(f"保留源目录 {src}：清理后仍有内容，需人工核对")
        # 源目录现在是空壳了。**不**在这里删：整目录 purge 是递归的，
        # 万一删除前有别的任务刚好往里落了文件，那份数据会跟着消失。
        # 空壳由调用方用非递归删除收掉（cloudadd.remove_empty_shells）。
        return "已清理已知丢弃文件，保留源目录外壳"
    except Exception as exc:
        raise OrganizeError(f"清理源目录 {src} 失败：{exc}") from exc


def source_shell_path(src_root: str, plan_obj: Plan) -> str:
    """这次落地留下的空壳目录在哪。

    两种形态：
      · 普通落地：源是 `<收件目录>/<种子目录名>`，壳在种子目录那一层
      · 单文件落地（loose）：源就是收件目录本身，壳在收件目录那一层
    """
    src_base = (src_root or "").strip("/")
    return f"{src_base}/{plan_obj.source}" if plan_obj.source else src_base


def _rollback(backend, moved: list[tuple[str, str]], dst: str, src: str) -> None:
    """把已经搬过去的文件搬回原目录（尽力而为，失败只记日志）。"""
    for new, old in moved:
        try:
            backend.move_file(f"{dst}/{new}", f"{src}/{old}")
        except Exception as exc:                # noqa: BLE001
            log.warning("回滚 %s 失败（需要人工处理）：%s", new, exc)
