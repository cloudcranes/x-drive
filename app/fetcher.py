"""下载层：HTTP 直链（httpx 流式）与磁力/BT（aria2c）。

只处理用户自己提供的链接或磁力，不做资源搜索。
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

log = logging.getLogger("x-drive.fetcher")

VIDEO_EXT = {".mp4", ".mkv", ".avi", ".wmv", ".mov", ".m4v", ".ts", ".flv", ".rmvb"}
SKIP_PARTS = {".aria2", ".part", ".tmp"}
DOWNLOAD_TIMEOUT = httpx.Timeout(120.0, connect=15.0, write=30.0)


def sources(text: str) -> list[str]:
    """消息里所有可下载来源（磁力/直链），去重保序。"""
    from .core import extract_urls

    return extract_urls(text)


def is_magnet(url: str) -> bool:
    return url.lower().startswith("magnet:")


def filename_from(url: str) -> str:
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or "download.bin"
    return name[:150]


async def download_http(
    client: httpx.AsyncClient, url: str, dest_dir: Path, max_bytes: int
) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename_from(url)
    if dest.suffix.lower() not in VIDEO_EXT:
        dest = dest.with_suffix(".mp4")
    total = 0
    async with client.stream("GET", url, timeout=DOWNLOAD_TIMEOUT) as resp:
        resp.raise_for_status()
        declared = int(resp.headers.get("content-length") or 0)
        if declared and declared > max_bytes:
            raise RuntimeError(f"{declared // 1048576}MB 超过上限 {max_bytes // 1048576}MB")
        with open(dest, "wb") as fh:
            async for chunk in resp.aiter_bytes(1 << 20):
                total += len(chunk)
                if total > max_bytes:
                    dest.unlink(missing_ok=True)
                    raise RuntimeError(f"超过上限 {max_bytes // 1048576}MB")
                fh.write(chunk)
    if total == 0:
        dest.unlink(missing_ok=True)
        raise RuntimeError("空文件")
    log.info("直链下载完成 %s（%.1fMB）", dest.name, total / 1048576)
    return dest


async def download_magnet(
    aria, client, magnet: str, dest_dir: Path, progress=None
) -> None:
    """把磁力提交给 aria2-pro 容器的 RPC，轮询到完成。

    aria2-pro 与 bot 共用同一个 downloads 卷，所以只要给它一个容器内路径即可。
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    gid = await aria.add_magnet(client, magnet, str(dest_dir))
    try:
        status = await aria.wait(client, gid, progress=progress)
        log.info("磁力下载完成 gid=%s 完成 %s 字节", gid, status.get("completedLength"))
    finally:
        await aria.shutdown(client, gid)


def media_files(directory: Path, min_size: int = 1 << 20) -> list[Path]:
    """目录里的视频文件，按大小降序；太小的是样本/广告，丢掉。"""
    files = [
        p for p in directory.rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_EXT
        and p.suffix.lower() not in SKIP_PARTS and p.stat().st_size >= min_size
    ]
    return sorted(files, key=lambda p: p.stat().st_size, reverse=True)


def flatten(directory: Path) -> None:
    """BT 常见多层目录：把视频挪到顶层，方便按番号归档。"""
    for path in media_files(directory):
        if path.parent != directory:
            target = directory / path.name
            if target.exists():
                target = directory / f"{path.stem}-{path.parent.name}{path.suffix}"
            shutil.move(str(path), str(target))
    # 清掉空目录
    for sub in sorted(directory.rglob("*"), key=lambda p: -len(p.parts)):
        if sub.is_dir() and not any(sub.iterdir()):
            sub.rmdir()
