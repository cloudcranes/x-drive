"""分块并发下载 —— 把一个大文件拆成几段同时拉。

**为什么需要**：单连接拉 5GB 的片子太慢。实测（2026-10-25，迅雷云盘）：
  · CD2 内网 `/static` 路径：单连接 1.4~2.4 MB/s → 5.25GB 要 40~60 分钟
  · 迅雷外网直链：      单连接 4.4 MB/s → 13 分钟
    4 并发：           14.5 MB/s → 6 分钟
    8 并发：           16.8 MB/s，但开始出现 503（CDN 限流）

服务端支持 Range（206 + Content-Range），所以拆段并行拉是合法的。
8 并发实测会吃到 503，默认取 4 —— 稳妥且已经拿到 6 倍提升。

失败退避：某段失败就重试，重试次数用尽就整段放弃（**不**把残缺的文件
当成品传回去 —— 那会毁掉归档）。
"""
from __future__ import annotations

import logging
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("x-drive.fastfetch")

DEFAULT_CONCURRENCY = 4
DEFAULT_CHUNK = 16 * 1024 * 1024
DEFAULT_RETRIES = 3
# 每段的超时（秒）—— 单段 16MB，10MB/s 也要 1.6 秒，给足余量
DEFAULT_TIMEOUT = 120.0


class FetchError(RuntimeError):
    """下载失败（某段重试用尽，或服务端不支持 Range）。"""


def _supports_range(url: str, timeout: float) -> tuple[bool, int]:
    """问服务端：支不支持 Range？文件多大？

    返回 (支持?, 总大小)。拿不到总大小就返回 0。
    """
    req = urllib.request.Request(url, headers={"Range": "bytes=0-1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            cr = resp.headers.get("Content-Range")
            total = int(cr.split("/")[-1]) if cr else 0
            if resp.status != 206 or not total:
                return False, 0
            return True, total
    except Exception as exc:                    # noqa: BLE001 探不到就当不支持
        # 416 = 范围不合法；501 = 不支持 Range；503 = 限流（探测阶段就限流，
        # 说明并发调太高了 —— 退回去单连接更稳）
        log.info("探测 Range 支持失败（按不支持处理）：%s", str(exc)[:120])
        return False, 0


def _download_range(url: str, start: int, length: int, dest: Path,
                    timeout: float, retries: int) -> int:
    """把 [start, start+length) 拉到 dest 的对应偏移。返回写入字节数。"""
    last = ""
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"Range": f"bytes={start}-{start + length - 1}"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 206:
                    raise FetchError(f"服务端不支持分段下载（HTTP {resp.status}）")
                pos = start
                while True:
                    blob = resp.read(1024 * 1024)
                    if not blob:
                        break
                    with open(dest, "r+b") as fh:
                        fh.seek(pos)
                        fh.write(blob)
                    pos += len(blob)
                return pos - start
        except Exception as exc:                # noqa: BLE001 失败重试
            last = f"{type(exc).__name__}: {str(exc)[:120]}"
            if attempt < retries:
                # 退避：503 限流时多等一会儿
                time.sleep(min(2 ** attempt, 8))
                continue
    raise FetchError(f"分段 {start} 起的 {length} 字节下载失败（重试 {retries} 次）：{last}")


def fetch(url: str, dest: Path, *, concurrency: int = DEFAULT_CONCURRENCY,
          chunk: int = DEFAULT_CHUNK, timeout: float = DEFAULT_TIMEOUT,
          retries: int = DEFAULT_RETRIES,
          progress=None) -> int:
    """把 url 拉到 dest（分块并发）。返回总字节数。

    progress(done_bytes, total_bytes) 会被周期性调用。
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    ranged, total = _supports_range(url, timeout)
    if not ranged or not total:
        # 服务端不支持 Range → 退回单连接整片拉
        log.info("服务端不支持 Range，改用单连接下载")
        return _fetch_whole(url, dest, timeout, progress)

    log.info("分块下载 %d 字节，%d 并发，每段 %d 字节",
             total, concurrency, chunk)
    # 先把文件撑到目标大小（各线程按偏移写）
    with open(dest, "wb") as fh:
        fh.truncate(total)

    ranges = []
    offset = 0
    while offset < total:
        length = min(chunk, total - offset)
        ranges.append((offset, length))
        offset += length

    done = [0]
    done_lock = threading.Lock()
    errors: list[str] = []
    err_lock = threading.Lock()
    stop = threading.Event()

    def worker(start: int, length: int) -> None:
        if stop.is_set():
            return
        try:
            got = _download_range(url, start, length, dest, timeout, retries)
            with done_lock:
                done[0] += got
                if progress:
                    progress(done[0], total)
        except Exception as exc:                # noqa: BLE001
            with err_lock:
                errors.append(str(exc)[:200])
            stop.set()

    threads = []
    for start, length in ranges:
        t = threading.Thread(target=worker, args=(start, length), daemon=True)
        threads.append(t)
    # 同时最多 concurrency 个在跑
    for i in range(0, len(threads), concurrency):
        batch = threads[i:i + concurrency]
        for t in batch:
            t.start()
        for t in batch:
            t.join()
        if errors:
            break

    if errors:
        raise FetchError("；".join(errors[:3]))
    size = dest.stat().st_size
    if size != total:
        raise FetchError(f"下载不完整：{size} / {total} 字节")
    return size


def _fetch_whole(url: str, dest: Path, timeout: float, progress=None) -> int:
    """单连接整片拉（服务端不支持 Range 时的退路）。"""
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        with open(dest, "wb") as fh:
            while True:
                blob = resp.read(4 * 1024 * 1024)
                if not blob:
                    break
                fh.write(blob)
                got += len(blob)
                if progress and total:
                    progress(got, total)
    return got
