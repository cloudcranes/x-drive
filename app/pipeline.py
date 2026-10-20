"""编排层：把「下载 → 整理 → 上传 Drive」拆成可复用的几个函数。

bot 与 web 后台共用这一层，这样网页上下的任务和 TG 发的任务行为一致。
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path

from . import aria2, codes, core, fetcher, whatslink

log = logging.getLogger("x-drive.pipeline")


class QuotaExceeded(RuntimeError):
    """今日上传量已达上限。"""


def job_dir(tmp_root: str, key: str) -> Path:
    """每个任务独立目录，键只保留字母数字。"""
    safe = "".join(ch for ch in key if ch.isalnum()) or "job"
    return Path(tmp_root) / safe


async def probe(client, source: str) -> dict:
    """提交前查元信息（只有磁力会查，直链直接跳过）。"""
    return await whatslink.lookup(client, source)


async def download(
    aria,
    client,
    source: str,
    work: Path,
    progress=None,
) -> None:
    await fetcher.download_rpc(aria, client, source, work, progress=progress)


def organize(work: Path, code: str) -> tuple[str, list[Path]]:
    """摊平目录 + 按番号重命名，返回 (目录名, 文件列表)。"""
    fetcher.flatten(work)
    files = fetcher.media_files(work)
    if not files:
        raise RuntimeError("下载完成但没有找到视频文件")

    folder = codes.safe_folder(code) if code else ""
    if not folder or len(folder) < 3:
        folder = f"unclassified-{datetime.now():%Y%m%d-%H%M%S}"

    if len(files) == 1:
        target = work / f"{folder}{files[0].suffix.lower()}"
        if files[0] != target:
            if target.exists():
                target.unlink()
            files[0].rename(target)
        files = [target]
    else:
        renamed: list[Path] = []
        for i, f in enumerate(files, start=1):
            target = work / f"{folder}-{i}{f.suffix.lower()}"
            if f != target:
                if target.exists():
                    target.unlink()
                f.rename(target)
            renamed.append(target)
        files = renamed
    return folder, files


def total_size(files: list[Path]) -> int:
    return sum(f.stat().st_size for f in files)


def cleanup(work: Path) -> None:
    shutil.rmtree(work, ignore_errors=True)
