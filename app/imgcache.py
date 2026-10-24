"""图片本地缓存：取到过的封面 / 截图落盘，之后不再请求外网。

为什么要单独一个模块：图床（`www.javbus.com`）实测握手很不稳 —— 同一张图连打
5 次会有 2 次超时，容器里更差。媒体墙一屏十几张，不缓存就是一半「无封面」，
而且每张都要白等一次超时。缓存之后一张图只需成功取到一次。

存法（刻意选「人能看懂」的）：
  `<缓存目录>/<sha16>-<原文件名>.<ext>`
  例如 `6f3a9c...-cmio.jpg`。哈希保证不同 URL 不撞车（同一图不同域名算两份，
  这比按内容去重简单，也不会因为 CDN 参数不同而误判同一张）；文件名保留原样
  是为了出事时能直接 `ls` 出来看是哪张图，而不是对着一堆 `.bin` 猜。

容量：按「总字节上限」做 LRU（按 mtime 从旧到新删）。上限来自设置
`IMG_CACHE_MB`，0 表示不限。修剪只在写入后触发，且是尽力而为 ——
删不掉（权限、正在被读）就跳过，绝不让缓存维护把取图搞挂。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger("x-drive.imgcache")

DEFAULT_DIR = os.getenv("COVER_CACHE", "")
# ctype → 扩展名。认不出的按 jpg 存（图床绝大多数是 jpeg）
EXT_BY_TYPE = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/webp": ".webp", "image/gif": ".gif", "image/avif": ".avif",
    "image/svg+xml": ".svg", "image/bmp": ".bmp",
}
_SAFE = re.compile(r"[^0-9A-Za-z._-]+")
_lock = threading.Lock()


def root() -> Path:
    """缓存目录。每次现取：设置/环境变量可能在进程运行中变（测试也会换目录）。

    优先级：网页设置 IMG_CACHE_DIR > 环境变量 COVER_CACHE > <data>/covers。
    """
    try:
        from . import settings as settings_mod
        chosen = str(settings_mod.hot("IMG_CACHE_DIR", "") or "").strip()
    except Exception:
        chosen = ""
    configured = chosen or os.getenv("COVER_CACHE") or DEFAULT_DIR
    if configured:
        return Path(configured)
    state = os.getenv("STATE_PATH", "/data/state.json")
    return Path(state).parent / "covers"


def ext_for(ctype: str, url: str = "") -> str:
    """先看 Content-Type，再退回 URL 后缀，最后 jpg。"""
    ctype = (ctype or "").split(";")[0].strip().lower()
    if ctype in EXT_BY_TYPE:
        return EXT_BY_TYPE[ctype]
    suffix = Path(urlsplit(url).path).suffix.lower()
    if suffix in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp"):
        return ".jpg" if suffix == ".jpeg" else suffix
    return ".jpg"


def type_for(path: Path) -> str:
    """反查 ctype（缓存命中时用，省一份 sidecar 文件）。"""
    ext = path.suffix.lower()
    for ctype, known in EXT_BY_TYPE.items():
        if known == ext:
            return ctype
    return "image/jpeg"


def _basename(url: str) -> str:
    """URL 里的文件名，只留安全字符、长度封顶；取不到就叫 image。"""
    name = Path(urlsplit(url).path).name or "image"
    name = _SAFE.sub("-", name).strip("-.") or "image"
    stem = Path(name).stem[:48] or "image"
    return stem


def path_for(url: str, ext: str = ".jpg") -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return root() / f"{digest}-{_basename(url)}{ext}"


def find(url: str) -> Path | None:
    """这个 URL 有没有缓存。扩展名事先不知道，按目录前缀找。"""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    directory = root()
    try:
        for entry in directory.glob(digest + "-*"):
            if entry.is_file() and entry.stat().st_size > 0:
                return entry
    except OSError:
        return None
    return None


def get(url: str) -> tuple[bytes, str, Path] | None:
    """命中就返回 (字节, ctype, 路径)。**任何异常都当没命中**：缓存坏了不该让图挂掉。"""
    path = find(url)
    if path is None:
        return None
    try:
        blob = path.read_bytes()
    except OSError:
        return None
    if not blob:
        return None
    # 读一次就摸一下 mtime：LRU 按「最近用过」淘汰，而不是「最早写入」
    try:
        os.utime(path, None)
    except OSError:
        pass
    return blob, type_for(path), path


def put(url: str, data: bytes, ctype: str = "image/jpeg") -> Path | None:
    """写盘。失败只记日志（拿不到图比存不进缓存严重得多），返回落地路径。"""
    if not data:
        return None
    purge_legacy()
    path = path_for(url, ext_for(ctype, url))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
    except OSError as exc:
        log.warning("写图片缓存失败：%s", exc)
        return None
    return path


def stats() -> dict:
    """给面板用：多少张、占多大、目录在哪。

    递归数：老布局是两级目录（`ab/hash.bin`），只 glob 顶层会显示 0 张，
    看着像「缓存没生效」，其实盘上有一堆。
    """
    directory = root()
    files = 0
    total = 0
    newest = 0.0
    try:
        for entry in directory.rglob("*"):
            if not entry.is_file() or entry.name.endswith(".tmp"):
                continue
            try:
                info = entry.stat()
            except OSError:
                continue
            files += 1
            total += info.st_size
            newest = max(newest, info.st_mtime)
    except OSError:
        pass
    return {"dir": str(directory), "files": files, "bytes": total,
            "newest": newest}


def trim(max_bytes: int) -> int:
    """超过上限就从最久没用的开始删，返回删了几个。0 = 不限，直接返回。"""
    if max_bytes <= 0:
        return 0
    directory = root()
    entries = []
    try:
        for entry in directory.rglob("*"):
            if not entry.is_file() or entry.name.endswith(".tmp"):
                continue
            try:
                entries.append((entry.stat().st_mtime, entry.stat().st_size, entry))
            except OSError:
                continue
    except OSError:
        return 0
    total = sum(size for _mtime, size, _entry in entries)
    if total <= max_bytes:
        return 0
    removed = 0
    for _mtime, size, entry in sorted(entries):
        if total <= max_bytes:
            break
        try:
            entry.unlink()
            total -= size
            removed += 1
        except OSError:
            continue
    if removed:
        log.info("图片缓存超出上限，清掉 %d 张（剩 %.1fMB）", removed, total / 1048576)
    return removed


def clear() -> dict:
    """清空缓存，返回删了多少。只动缓存目录里的普通文件（含老布局的两级目录）。"""
    directory = root()
    removed = 0
    freed = 0
    try:
        entries = [e for e in directory.rglob("*") if e.is_file()]
    except OSError:
        return {"removed": 0, "bytes": 0, "dir": str(directory)}
    for entry in entries:
        try:
            size = entry.stat().st_size
            entry.unlink()
            removed += 1
            freed += size
        except OSError:
            continue
    # 顺手收掉空的两级目录（老布局）
    try:
        for sub in directory.iterdir():
            if sub.is_dir() and not any(sub.iterdir()):
                sub.rmdir()
    except OSError:
        pass
    log.info("清空图片缓存：%d 个文件，释放 %.1fMB", removed, freed / 1048576)
    return {"removed": removed, "bytes": freed, "dir": str(directory)}


_legacy_checked = False


def purge_legacy() -> int:
    """删掉老布局（`ab/abcd….bin` + `.type`）的残留，返回删了几个。

    老布局只存了 URL 的哈希、没存 URL 本身，**没法迁移**（不知道对应哪张图），
    留着只会占地方。所以第一次用到缓存时清一次；这是本模块自己产出的文件，
    删掉不影响任何用户数据。
    """
    global _legacy_checked
    if _legacy_checked:
        return 0
    _legacy_checked = True
    directory = root()
    removed = 0
    try:
        for sub in directory.iterdir():
            if not sub.is_dir() or not re.fullmatch(r"[0-9a-f]{2}", sub.name):
                continue
            for entry in sub.glob("*"):
                if entry.suffix in (".bin", ".type", ".tmp") and entry.is_file():
                    try:
                        entry.unlink()
                        removed += 1
                    except OSError:
                        continue
            try:
                if not any(sub.iterdir()):
                    sub.rmdir()
            except OSError:
                pass
    except OSError:
        pass
    if removed:
        log.info("清掉 %d 个老布局的图片缓存文件（无法迁移）", removed)
    return removed


def sweep_tmp(max_age: float = 3600.0) -> int:
    """清掉中断留下的 .tmp（进程被杀时会有）。"""
    removed = 0
    cutoff = time.time() - max_age
    try:
        for entry in root().glob("*.tmp"):
            try:
                if entry.stat().st_mtime < cutoff:
                    entry.unlink()
                    removed += 1
            except OSError:
                continue
    except OSError:
        pass
    return removed
