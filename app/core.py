"""基础工具：状态文件读写、URL 提取、大小格式化。

业务逻辑（番号识别、下载、上传）分别在 codes / fetcher / main 里。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from contextlib import contextmanager

try:
    import fcntl
except ImportError:      # Windows 上降级为无锁（本项目部署在 Linux）
    fcntl = None

log = logging.getLogger("x-drive.core")

URL_RE = re.compile(r"(?:magnet:\?[^\s\"'<>]+|https?://[^\s\"'<>]+)", re.I)
TRAILING = ").,;!?'\"”’》】」>"


def extract_urls(text: str) -> list[str]:
    """文本里所有可下载来源，去重保序。"""
    seen: set[str] = set()
    out: list[str] = []
    for raw in URL_RE.findall(text or ""):
        url = raw.rstrip(TRAILING)
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def extract_url(text: str) -> str | None:
    urls = extract_urls(text)
    return urls[0] if urls else None


def human_size(num: float) -> str:
    """字节 → 人类可读；整 GB 保留两位小数。"""
    if not num:
        return "—"
    for unit in ("B", "KB", "MB"):
        if num < 1024:
            return f"{num:.0f}{unit}"
        num /= 1024
    for unit in ("GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.2f}{unit}"
        num /= 1024
    return f"{num:.2f}TB"


def next_quota_retry(now: float | None = None) -> float:
    """配额恢复时间：次日 00:10。Drive 与迅雷的日额度都按本地日切。

    网页与 bot 共用这一个口径：延期必须有确定时间，否则永远等不到唤醒。
    """
    t = time.localtime(time.time() if now is None else now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 10, 0, 0, 0, -1)) + 86400


def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


# bot 与 web 是两个进程，同时归档会互相覆盖：都用「读-改-写」整份文件时，
# 后写的那个会丢掉先写的记录。这里用文件锁把临界区串起来。
@contextmanager
def _locked(path: str, timeout: float = 10.0):
    lock_path = f"{path}.lock"
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    fh = open(lock_path, "a+")
    deadline = time.monotonic() + timeout
    try:
        if fcntl is not None:
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        log.warning("等锁超时，直接写入：%s", lock_path)
                        break
                    time.sleep(0.02)
        yield
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def rekey_state(path: str, old: str, new: str, record: dict | None = None) -> dict:
    """把一条记录换个键（归档改名用）。

    update_state 只合并不删除，改名必须显式删旧键，否则会留下两条指向同一目录的记录。
    """
    with _locked(path):
        state = load_state(path)
        data = dict(record if record is not None else state.get(old) or {})
        state.pop(old, None)
        state[new] = data
        save_state(path, state)
        return state

def update_state(path: str, updates: dict) -> dict:
    """原子合并若干条记录：加锁 → 读 → 合并 → 写，避免两个进程互相覆盖。"""
    with _locked(path):
        state = load_state(path)
        state.update(updates)
        save_state(path, state)
        return state
