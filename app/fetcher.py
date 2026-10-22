"""下载层：把任务投给迅雷容器，跟任务状态等完成，再校验落盘；另有目录摊平与文件筛选。

只处理用户自己提供的链接或磁力，不做资源搜索。
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import workspace, xunlei

log = logging.getLogger("x-drive.fetcher")

VIDEO_EXT = {
    ".mp4", ".mkv", ".avi", ".wmv", ".mov", ".m4v", ".ts", ".flv", ".rmvb",
}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
# 写真图包只有图片，不能因为「没视频」就判失败
MEDIA_EXT = VIDEO_EXT | IMAGE_EXT
SKIP_PARTS = {".part", ".tmp", ".xltd", ".downloading"}

def sources(text: str) -> list[str]:
    """消息里所有可下载来源（磁力/直链），去重保序。"""
    from .core import extract_urls

    return extract_urls(text)

def is_magnet(url: str) -> bool:
    return url.lower().startswith("magnet:")

def filename_from(url: str) -> str:
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or "download.bin"
    return name[:150]

def staging(tmp_root: str, key: str) -> Path:
    """任务目录 `<TMP_ROOT>/<key>`，同时是给迅雷的任务名（迅雷按名字建同名子目录）。"""
    return workspace.staging(tmp_root, key)

def _same_source(task: dict, url: str, infohash: str = "") -> bool:
    """面板里的任务是不是同一个来源：infohash 优先，解析前用 url 兜底。"""
    params = task.get("params") or {}
    got = (params.get("info_hash") or "").lower()
    want = (infohash or xunlei.info_hash_of(url) or "").lower()
    if want and got == want:
        return True
    return not got and (params.get("url") or "") == url


async def download(
    xl: xunlei.Xunlei,
    client,
    source: str,
    dest: Path,
    poll: float = 10.0,
    timeout: float = 7200.0,
    stall_timeout: float = 900.0,
    settle: float = 30.0,
    size: int = 0,
    progress=None,
    on_task=None,
    session: dict | None = None,
) -> dict:
    """投递任务、等到迅雷报告完成，再确认文件真的落在自己的目录里。

    返回 {"task", "dir", "waited", "reused"}：
      dir     迅雷上报的实际目录（重名时可能带 -1 后缀，必须用它而不是 dest）
      reused  平台里已有同种子的任务，我们只是接过来跟进度
    非本任务目录会直接报错，绝不去猜一个「大概是」的路径。

    session 是「本任务上次投递留下的证据」（task / infohash）：调用方把它从任务表
    里读出来传进来。有证据且对得上的任务是我们自己投的，直接续接；对不上才按
    别人的任务处理 —— 以前不区分，重启后自己的任务也会被当成外人的而拒绝续传。
    """
    # 目录归属用任务键（去掉 - _ 等），与 workspace.key_of 一致
    key = workspace.key_of(dest.name)
    btih = xunlei.info_hash_of(source)
    prev = session or {}
    prev_task = str(prev.get("task") or "")
    prev_hash = str(prev.get("infohash") or "").lower()
    # 只有本任务自己留下的证据才算数：infohash 对得上，或上次记的就是这条任务
    ours = bool(prev_hash and prev_hash == (btih or "").lower())
    workspace.claim(dest, key, source, infohash=btih)
    reused = await xl.find(client, source)
    task = ""
    if reused:
        task_id = str(reused.get("id") or "")
        if prev_task and task_id == prev_task:
            log.info("续接本任务上次投递的迅雷任务 %s", task_id)
            task = task_id
        elif ours and _same_source(reused, source, btih):
            log.info("按本任务记录的 infohash 续接迅雷任务 %s", task_id)
            task = task_id
        else:
            # 复用别人的任务：只跟进度，绝不打标记、绝不清理它的目录。
            # 以前这里照样 adopt()，等于把外部任务的目录认成自己的，后面就会删掉它。
            raise RuntimeError(
                f"迅雷里已有同一个种子在下载（任务 {task_id or '?'}），"
                f"那是平台里别人提交的任务，本程序不接管它的目录；"
                f"等它跑完或在面板里删掉后再提交"
            )
    else:
        task = await xl.add(client, source, name=dest.name, size=size)
    if on_task:
        on_task(task, {"reused": False, "id": task})
    started = time.monotonic()
    info = await xl.wait(client, task, url=source, poll=poll,
                         timeout=timeout, stall_timeout=stall_timeout, progress=progress)

    # 迅雷的路径即真相：重名时 real_path 会变成 `<名字>-1`
    real = (info.get("params") or {}).get("real_path") or ""
    path = xunlei.local_path(real, xl.tmp_root)
    if path is None:
        raise RuntimeError(
            f"迅雷把文件下到了 {real or '（未上报路径）'}，不在 TMP_ROOT={xl.tmp_root} 下；"
            "检查 XUNLEI_DOWNLOADS_HOST 与迅雷容器的 /downloads 是否同一个挂载点"
        )
    # 形态是「我们的目录」才继续：dest 本身，或迅雷重名时自己建的 `<键>-N`
    if not workspace.shape_of(path, dest):
        # 复用了别人手工建的同名任务：那是人家的目录，不接管
        raise RuntimeError(
            f"迅雷落盘目录是 {path}，不属于本次任务 {dest}；"
            "请先在迅雷面板处理那个同名任务"
        )
    # 到了这里已确认：任务是我们投的（reused 为假）、路径形态对。
    # 只有迅雷重名新建的目录才需要补标记，落地证据写进标记便于事后对账。
    if not workspace.owned(path, key):
        workspace.adopt(path, key, source, task_id=task, infohash=btih)
    while not xunlei.done(path, min_age=settle):
        if time.monotonic() > started + timeout:
            raise RuntimeError(f"迅雷报告完成，但 {path} 里没有可用视频（等了 {int(timeout)}s）")
        await asyncio.sleep(poll)
    return {"task": task, "dir": path, "waited": time.monotonic() - started,
            "reused": bool(reused)}

def media_files(directory: Path, min_size: int = 1 << 20,
                types: set[str] = VIDEO_EXT) -> list[Path]:
    """目录里的媒体文件，按大小降序；太小的是样本/广告，丢掉。

    types 默认只取视频；写真图包传 MEDIA_EXT。
    """
    files = [
        p for p in directory.rglob("*")
        if p.is_file() and p.suffix.lower() in types
        and p.suffix.lower() not in SKIP_PARTS and p.stat().st_size >= min_size
    ]
    return sorted(files, key=lambda p: p.stat().st_size, reverse=True)

def probe_seconds(path: Path, timeout: float = 20.0) -> float:
    """ffprobe 取时长（秒）。取不到返回 0，绝不抛。

    容器里装了 ffmpeg 就是为了这一步：靠体积猜主体经常猜错 ——
    4K 预告片能比正片大，而正片往往是时长最长的那个。
    """
    try:
        done = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, timeout=timeout,
        )
        return float(done.stdout.decode(errors="replace").strip() or 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0

def pick_media(files: list[Path], trailer_seconds: float = 300.0,
               feature_seconds: float = 1200.0) -> tuple[list[Path], list[Path]]:
    """挑主体文件：有长片（≥20 分钟）时，丢掉 5 分钟以下的预告/样片。

    返回 (保留, 丢弃)。时长都取不到就按体积来，一个都不丢。
    """
    if len(files) < 2:
        return files, []
    ranked = [(f, probe_seconds(f)) for f in files]
    known = [(f, d) for f, d in ranked if d > 0]
    if len(known) < 2:
        return files, []
    longest = max(d for _, d in known)
    if longest < feature_seconds:
        return [f for f, _ in sorted(ranked, key=lambda x: (-x[1], -x[0].stat().st_size))], []
    keep = [f for f, d in ranked if d <= 0 or d >= trailer_seconds]
    drop = [f for f, d in ranked if 0 < d < trailer_seconds]
    keep.sort(key=lambda f: -probe_seconds(f))
    return keep or files, drop

def flatten(directory: Path) -> None:
    """BT 常见多层目录：把视频挪到顶层，方便按番号归档。"""
    for path in media_files(directory):
        if path.parent != directory:
            target = directory / path.name
            if target.exists():
                target = directory / f"{path.stem}-{path.parent.name}{path.suffix}"
            shutil.move(str(path), str(target))
    for sub in sorted(directory.rglob("*"), key=lambda p: -len(p.parts)):
        if sub.is_dir() and not any(sub.iterdir()):
            sub.rmdir()
