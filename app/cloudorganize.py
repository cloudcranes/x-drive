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

VIDEO_EXT = {".mp4", ".mkv", ".avi", ".wmv", ".mov", ".m4v", ".ts", ".flv", ".rmvb", ".iso",
             # BD 原盘的容器很常见（.m2ts）—— 不认识它会让整个任务失败，
             # 而且报错文案说「只有广告？」，极具误导性（实际是格式没认出来）
             ".m2ts", ".m2t", ".vob", ".mpg", ".mpeg", ".divx", ".asf", ".m2p", ".ogm"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
# 副产物：字幕 / 封面 / 清单 —— 它们是**正片的附属**，跟着正片一起留。
# 以前它们被当成广告 purge 掉了（字幕没了、封面也没了）。
# 注意：**不含 .txt** —— 种子里的 .txt 绝大多数是广告（"最新地址获取…"），
# 不是字幕。字幕用 .srt/.ass/.ssa/.sub/.vtt 这些正经扩展名。
SIDECAR_EXT = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup", ".smi"}
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
    """取扩展名（小写）。

    先 strip：CD2 会归一化目录名，但文件名不一定 —— `movie.mp4 `（尾随空格）
    的 suffix 是 '.mp4 '，不匹配 VIDEO_EXT，那个文件会被当成广告丢掉。
    """
    return PurePosixPath((name or "").strip()).suffix.lower().strip()


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

# **多集/图包场景**用的严格线：小于最大文件的 1/10 才当样片丢。
#
# 0.25 那条线在多集发行上会误杀：一集 4K + 若干 1080p，1080p 一集的体积
# 约 4K 的 1/5，正好落在 0.25 线上 —— 三集正片被当样片**永久删除**，
# 源目录被清空，站内只剩一集的记录，用户完全无从察觉。
#
# 实测推广视频与正片差**三个数量级**（14MB vs 4.4GB），所以 1/10 这条线
# 既能分清推广视频，又不会碰到多集。
SAMPLE_RATIO_STRICT = 0.1


def pick_main(files: list[dict]) -> list[dict]:
    """从落地目录里挑出要归档的文件。

    **以体积为准，不以名字为准**：实测那个种子的正片叫
    `489155.com@MIMK-290.mp4` —— 名字里带推广域名，但它是唯一的正片；
    而叫得像样的 `社 區 最 新 情 報.mp4` 只有 15MB，是广告。
    所以名字里的推广标记只用来解释「为什么丢掉」，不用来判定「要不要留」。

    **但体积比例只用于「一堆视频里挑主体」这一种场景**。多集发行与图包
    不能这么判：一集 4K + 若干 1080p 是**同一部片的多集**，1080p 那一集的
    体积约 4K 的 1/5，正好落在 SAMPLE_RATIO 线上 —— 用比例判会把三集正片
    当成样片**永久删除**（源目录还被清空，站内只剩一集的记录，用户完全
    无从察觉）。图包同理：100 张照片只留 1 张、其余全 purge。

    所以先看**结构**：同扩展名的多个文件 = 多集/多图/分卷，全留。
    只有「同扩展名、且体积差一个数量级」时才可能是「主体 + 样片」。
    """
    videos = [f for f in files if _suffix(str(f.get("name") or "")) in VIDEO_EXT]
    if videos:
        return _pick_by_structure(videos)
    images = [f for f in files if _suffix(str(f.get("name") or "")) in IMAGE_EXT]
    if images:
        return _pick_by_structure(images)
    return []


def _pick_by_structure(items: list[dict]) -> list[dict]:
    """同类文件里挑主体：结构优先，体积只在「明显是样片」时才用。

    规则：
      · **同扩展名且有多个** → 多集/多图/分卷，**全留**。
        （这是最要紧的一条：按体积挑主体会误杀多集发行。）
      · **同扩展名只有一个** → 它就是主体，留它；其余（字幕/封面/广告）
        按各自规则处理。
      · 同扩展名多个、但体积差一个数量级 → 可能是「主体 + 样片」。
        这时才用比例挑 —— 但只在**数量少**（≤4）时才敢这么判，
        数量多就更像多集而不是样片。
    """
    by_ext: dict[str, list[dict]] = {}
    for f in items:
        by_ext.setdefault(_suffix(str(f.get("name") or "")), []).append(f)
    # 取最大的那一组（同扩展名的才算同类）
    ext, group = max(by_ext.items(), key=lambda kv: (len(kv[1]), kv[0]))
    if len(group) == 1:
        return list(group)
    # 同扩展名的多个文件，默认**全留** —— 多集/图包/分卷都比「主体+样片」常见。
    #
    # 只有「同扩展名、且有一个小到明显不是正片」时才可能是样片。判据要**很严**：
    # 以前用的 0.25 会在多集上误杀（一集 4K + 若干 1080p，1080p 约 4K 的
    # 1/5，正好落在线上），三集正片被永久删除、源目录清空、站内只剩一集。
    # 现在要小到 **1/10 以下**（推广视频 14MB vs 正片 4.4GB 是三个数量级）
    # 才当样片丢。
    biggest = max(int(f.get("size") or 0) for f in group)
    kept = [f for f in group
            if int(f.get("size") or 0) >= max(biggest * SAMPLE_RATIO_STRICT, 1)]
    # 全被滤掉 = 判据不适用这个组合 → 全留（宁可多留，不要误删）
    return kept or list(group)


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
        # 分清两种情况：目录是空的 vs 文件都在但格式不认识 ——
        # 后者报错说「只有广告？」会让人以为是种子的错，其实是格式没认出来
        names = [str(f.get("name") or "") for f in files if f.get("name")]
        if names:
            raise OrganizeError(
                f"{source or '收件目录'} 里的文件格式都不认识：{names[:5]}")
        raise OrganizeError(f"{source or '收件目录'} 是空的，没有可归档的文件")
    kept_names = [str(f.get("name") or "") for f in keep if f.get("name")]
    if len(set(kept_names)) != len(kept_names):
        raise OrganizeError(f"{source or '收件目录'} 里有同名文件，云端无法区分：{kept_names}")
    # 副产物跟着正片一起留 —— 它们是正片的附属，不是广告。
    # 字幕：一律跟着留（扩展名够明确）。
    # 图片（封面/海报）：**只在有视频主体时**才跟着留 —— 图包里的 .jpg
    # 是照片本身，不是封���。
    have_video = any(_suffix(n) in VIDEO_EXT for n in kept_names)
    sidecars = []
    for f in files:
        name = str(f.get("name") or "")
        if not name or name in kept_names:
            continue
        ext = _suffix(name)
        if ext in SIDECAR_EXT:
            sidecars.append(name)
        elif have_video and ext in IMAGE_EXT:
            sidecars.append(name)
    kept_names = kept_names + [n for n in sidecars if n not in kept_names]
    drop = [str(f.get("name") or "") for f in files
            if str(f.get("name") or "") and str(f.get("name") or "") not in kept_names]
    target = (folder_code or "").strip()
    if target and _BAD_NAME.search(target):
        # 在计划阶段就拒：推到执行阶段才报错（"目录名不安全，拒绝执行"）
        # 会让人以为是云端的问题，其实是这个番号不合法。
        raise OrganizeError(f"番号不合法，拒绝整理：{folder_code!r}")
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
    # 已经归档过的（目标目录里已有同名同大小文件）→ 源文件是冗余，
    # 不搬、只把它交给清理（drop）。
    #
    # 以前这种情况直接抛错 → 任务 failed → 源文件留在原地 → 重跑再撞同一个
    # 已存在的目录再失败 —— **死循环**（cloud_submit 只在成功后才清）。
    repeated: list[str] = []
    for old, new in mapping:
        try:
            if old != new or src != dst:
                backend.move_file(f"{src}/{old}", f"{dst}/{new}")
        except Exception as exc:                # noqa: BLE001
            if _is_already_archived(backend, dst, new, old, plan_obj):
                # 目标里已经有同样的文件了：这次是重复下载，源文件是冗余
                repeated.append(old)
                continue
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
            "dropped": plan_obj.drop,
            # 目标里本来就有的（重复下载）：这些源文件是冗余，交给清理
            "repeated": repeated}


def _is_already_archived(backend, dst: str, new_name: str, old_name: str,
                         plan_obj: Plan) -> bool:
    """目标目录里是不是已经有「同样内容」的文件了（= 重复下载）。

    判据要**两个都成立**：同名**且**同大小。只是一同名就当重复的话，
    换版重发（同名不同内容）会被误判成冗余而丢掉新版本。

    读不出来就返回 False（不当重复）—— 那就照旧报错，让流程停下来，
    总比悄悄丢掉一个文件强。
    """
    try:
        entries = {str(e.name): int(e.size or 0) for e in backend.list_files(dst)}
    except Exception:                          # noqa: BLE001 读不出来 = 不确定
        return False
    if new_name not in entries:
        return False
    want = int(plan_obj.sizes.get(old_name) or 0)
    have = int(entries.get(new_name) or 0)
    if want and have and want != have:
        return False                            # 同名不同内容 = 换版，不是重复
    return True


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
