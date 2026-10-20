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


def listing(work: Path, limit: int = 8) -> str:
    """目录里到底有什么：报错时带上，否则只看到「找不到视频文件」无从排查。"""
    try:
        items = sorted(work.rglob("*"))[:limit]
    except OSError:
        return "（无法读取目录）"
    if not items:
        return "（目录为空）"
    bits = []
    for p in items:
        try:
            if p.is_dir():
                bits.append(f"{p.relative_to(work)}/")
            else:
                bits.append(f"{p.relative_to(work)} ({p.stat().st_size // 1024}KB)")
        except OSError:
            bits.append(f"{p.name} (?)")
    return "、".join(bits)


def organize(work: Path, code: str) -> tuple[str, list[Path]]:
    """摊平目录 + 按番号重命名，返回 (目录名, 文件列表)。"""
    fetcher.flatten(work)
    files = fetcher.media_files(work)
    if not files:
        raise RuntimeError(f"下载完成但没有找到视频文件：{listing(work)}")

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
    prune(work, files)
    return folder, files


def prune(work: Path, keep: list[Path]) -> list[Path]:
    """删掉归档目录里的非视频文件（BT 种子常夹带 .url/.txt 广告和样本）。

    上传走 `rclone copy work`，如果不清掉，广告文件会跟着传到 Drive 的番号目录里。
    """
    keep_set = {p.resolve() for p in keep}
    removed = []
    for path in sorted(work.rglob("*"), key=lambda p: -len(p.parts)):
        try:
            if path.is_file() and path.resolve() not in keep_set:
                path.unlink(missing_ok=True)
                removed.append(path.name)
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        except OSError:
            pass
    if removed:
        log.info("清掉 %d 个非视频文件：%s", len(removed), "、".join(removed[:5]))
    return keep


def total_size(files: list[Path]) -> int:
    return sum(f.stat().st_size for f in files)


def cleanup(work: Path) -> None:
    shutil.rmtree(work, ignore_errors=True)


def dir_size(path: Path) -> int:
    total = 0
    try:
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return total


def trim_failed(root: str, keep: Path, limit_bytes: int = 20 * 1024**3) -> list[str]:
    """失败任务的目录先留着以便续传，但整体不能无限涨。

    超出 limit_bytes 时，从最旧的开始删（keep 指定的当前任务永不删）。
    """
    base = Path(root)
    try:
        dirs = [p for p in base.iterdir() if p.is_dir() and p.resolve() != keep.resolve()]
    except OSError:
        return []
    dirs.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0)

    def total() -> int:
        return sum(dir_size(p) for p in base.iterdir() if p.is_dir()) if base.exists() else 0

    removed: list[str] = []
    while dirs and total() > limit_bytes:
        victim = dirs.pop(0)
        size = dir_size(victim)
        log.info("清理旧的失败任务目录 %s（%.1fGB）", victim.name, size / 1024**3)
        shutil.rmtree(victim, ignore_errors=True)
        removed.append(victim.name)
    return removed
