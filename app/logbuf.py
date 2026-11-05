"""运行日志：bot 与 web 共写一份文件，网页「运行日志」按行读取。

两个进程都挂同一个 ./data，所以日志落在同一处；FileHandler 用追加模式，
每条记录一次 write（远小于 PIPE_BUF），不会互相覆盖。文件不轮转：
运维需要的是「刚才发生了什么」，长期归档交给宿主机的日志收集。
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

LOG_PATH = os.getenv("LOG_PATH", "/data/x-drive.log")
TAIL_BYTES = 512 * 1024

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) "
    r"(DEBUG|INFO|WARNING|ERROR|CRITICAL) (\S+) (.*)$"
)

_installed = False
_path = ""


class _Noise(logging.Filter):
    """httpx 每个请求一行 INFO，会把业务日志埋掉；警告以上照留。"""
    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        return not record.name.startswith(("httpx", "httpcore"))


def install(path: str = "") -> str:
    """给根 logger 挂一个追加写文件。重复调用只装一次。"""
    global _installed, _path
    target = path or LOG_PATH
    if _installed:
        return _path
    try:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(target, mode="a", encoding="utf-8")
    except OSError as exc:
        # 路径不可写不该拖垮服务：网页仍能开，只是没有文件日志
        logging.getLogger(__name__).warning("运行日志不可写 %s：%s", target, exc)
        return target
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler.addFilter(_Noise())
    logging.getLogger().addHandler(handler)
    _installed = True
    _path = target
    return target


def tail(limit: int = 300, level: str = "", q: str = "") -> dict:
    """读文件尾部。多行异常栈会挂到上一条记录上，不拆成孤立行。"""
    limit = max(1, min(int(limit or 300), 2000))
    path = Path(LOG_PATH)
    try:
        size = path.stat().st_size
    except OSError:
        return {"ok": True, "items": [], "size": 0, "path": str(path)}
    start = max(0, size - TAIL_BYTES)
    try:
        with path.open("rb") as fh:
            fh.seek(start)
            raw = fh.read()
    except OSError as exc:
        return {"ok": False, "items": [], "size": size, "error": str(exc)[:200]}
    lines = raw.decode("utf-8", "replace").splitlines()
    if start and lines:
        lines = lines[1:]                 # 从半行开始读，丢掉残句

    items: list[dict] = []
    current: dict | None = None
    for line in lines:
        match = _LINE_RE.match(line)
        if match:
            current = {"at": match.group(1), "level": match.group(2),
                       "name": match.group(3), "text": match.group(4)}
            items.append(current)
        elif current is not None:
            current["text"] += "\n" + line

    if level:
        want = level.upper()
        items = [i for i in items if i["level"] == want]
    if q:
        needle = q.lower()
        items = [i for i in items
                 if needle in i["text"].lower() or needle in i["name"].lower()]
    return {"ok": True, "items": items[-limit:], "size": size, "path": str(path)}
