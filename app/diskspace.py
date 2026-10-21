"""磁盘空间闸门：下载前确认放得下，放不下就排队等，而不是硬塞把盘写满。

写满的后果比排队严重得多：aria2 报错退出，而 p3terx 的 delete-on-error 钩子
还会顺手删掉残片，用户看到的是「下载失败」且没有任何可续传的数据。
"""
from __future__ import annotations

import shutil
from pathlib import Path

GB = 1024 ** 3


def free_bytes(path: str | Path) -> int | None:
    """剩余可用字节数；探测失败返回 None（调用方据此放行，不阻塞下载）。

    某些文件系统或容器里 statvfs 会抛 OSError，这时候让任务照常跑，
    比因为探测不了就全部卡死要好。
    """
    try:
        return shutil.disk_usage(str(path)).free
    except OSError:
        return None


def fits(need: int, free: int | None, reserve: int = 0) -> bool:
    """放得下吗。

    need     这次任务预估要占的字节数（0 表示大小未知）
    free     当前剩余字节数（None 表示探测失败 → 放行）
    reserve  永远留出来的余量，不参与分配
    """
    if free is None:
        return True
    if need < 0:
        need = 0
    return free - need >= max(0, reserve)


def reserved_by(jobs) -> int:
    """已经把空间许出去的总量。

    只算真正开始下载的任务。还在排队的不该占额度 —— 否则队列一长，
    剩余额就会被一串还没开始的任务全部吃掉，队头反而永远轮不到。
    只看瞬时剩余空间又会让几个任务同时判定「放得下」一起开下把盘吃穿，
    所以这里的口径是「已放行且还没落盘」。
    """
    total = 0
    for job in jobs:
        if job.get("stage") != "downloading":
            continue
        try:
            total += max(0, int(job.get("total") or 0))
        except (TypeError, ValueError):
            continue
    return total
