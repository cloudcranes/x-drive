"""订阅发现待办：把「看到了哪些作品」与「下不下」分开。

为什么需要它：订阅每轮只该提交少量任务（不然一次能吃掉磁盘和 Drive 配额），
但**发现**是另一回事 —— 一页上看到 12 部就该记住 12 部。
以前两者混在游标里：只处理 1 部却把游标推到整批最新，中间的作品永久消失；
某部一直没有磁力，还会把队头堵死，后面的有磁力作品永远排不上。

这里按订阅存一张小表（JSON，复用 core 的加锁读写）：
    {作品番号: {"date", "title", "state", "tries", "next_at", "note"}}

state 取值：
    pending   已发现，还没拿到磁力（或还没提交），轮到它会再试
    queued    已提交下载（记着 job_id，失败时要靠它放回来）
    skipped   按策略跳过（超体积上限等），不再重试
    done      已归档，闭环结束
"""
from __future__ import annotations

import logging
import time

from . import core

log = logging.getLogger("x-drive.backlog")

PENDING, QUEUED, SKIPPED, DONE = "pending", "queued", "skipped", "done"
# 没有磁力的作品不必每轮都查：5 分钟后、20 分钟后、1 小时、6 小时… 逐步退避
BACKOFF = (300, 1200, 3600, 21600, 86400)
# 每次多备几个候选：队头没磁力时不必等下一轮，也别一次抓太多
EXTRA_CANDIDATES = 3


def load(path: str) -> dict:
    data = core.load_state(path)
    return data if isinstance(data, dict) else {}


def save(path: str, data: dict) -> None:
    core.save_state(path, data)


def remember(data: dict, sub_key: str, items: list[dict], today: str) -> int:
    """把本轮发现的作品记进待办。返回新增数量。

    今天出的片照样记下来，只是先标 pending 不急着下 —— 它们当天还可能补磁力。
    """
    bucket = data.setdefault(sub_key, {})
    added = 0
    for it in items:
        code = (it.get("code") or "").upper()
        if not code or code in bucket:
            continue
        bucket[code] = {
            "date": it.get("date") or "",
            "title": (it.get("title") or "")[:200],
            "state": PENDING,
            "tries": 0,
            "next_at": 0 if (it.get("date") or "") < today else time.time() + BACKOFF[0],
            "note": "",
            "since": time.time(),          # 进待办的时间：算「等了多久没资源」
        }
        added += 1
    return added


def due(data: dict, sub_key: str, limit: int, now: float | None = None) -> list[str]:
    """轮到处理的作品：按日期从旧到新，跳过退避未到点的。

    返回的是**候选**（比 limit 多给几个）：队头某部查出来没磁力时不会白占一个名额，
    调用方顺着往后取即可 —— 这就是队头阻塞的修法。
    """
    now = time.time() if now is None else now
    bucket = data.get(sub_key) or {}
    ready = [(v.get("date") or "", code) for code, v in bucket.items()
             if v.get("state") == PENDING and float(v.get("next_at") or 0) <= now]
    ready.sort()
    return [code for _, code in ready[:max(1, int(limit)) + EXTRA_CANDIDATES]]


def mark(data: dict, sub_key: str, code: str, state: str, note: str = "",
         job_id: str | None = None) -> None:
    item = (data.get(sub_key) or {}).get(code.upper())
    if not item:
        return
    item["state"] = state
    item["note"] = note[:200]
    if job_id is not None:
        item["job_id"] = job_id
    if state == PENDING:
        tries = int(item.get("tries") or 0) + 1
        item["tries"] = tries
        item["next_at"] = time.time() + BACKOFF[min(tries, len(BACKOFF) - 1)]


# 同一个番号最多换几条候选：换太多次是在浪费带宽和时间，且往往说明资源本身有问题
MAX_TRIES = 3


def note_tried(data: dict, sub_key: str, code: str, magnet: str) -> bool:
    """记下「这条磁力试过且失败了」。返回 False 表示已经试到头，别再换源了。

    只记 infohash 而不是整条链接：同一资源在不同站点的链接串不同，
    同 infohash 却是同一份数据，按 hash 去重才挡得住反复撞同一条死种。
    """
    item = (data.get(sub_key) or {}).get(code.upper())
    if not item:
        return False
    from . import xunlei

    btih = xunlei.info_hash_of(magnet or "") or (magnet or "")[:60]
    tried = list(item.get("tried") or [])
    if btih and btih not in tried:
        tried.append(btih)
    item["tried"] = tried
    return len(tried) < MAX_TRIES


def tried_of(data: dict, sub_key: str, code: str) -> list[str]:
    return list(((data.get(sub_key) or {}).get(code.upper()) or {}).get("tried") or [])


def counts(data: dict, sub_key: str) -> dict:
    bucket = data.get(sub_key) or {}
    out = {PENDING: 0, QUEUED: 0, SKIPPED: 0, DONE: 0}
    for item in bucket.values():
        state = item.get("state") or PENDING
        out[state] = out.get(state, 0) + 1
    return out


def prune(data: dict, days: float = 90.0, keep: int = 500) -> int:
    """老记录清理：只清已结束的，pending 永不删（删了就等于漏订）。"""
    cutoff = time.time() - max(1.0, days) * 86400
    removed = 0
    for sub_key, bucket in list(data.items()):
        closed = [(v.get("date") or "", code) for code, v in bucket.items()
                  if v.get("state") in (QUEUED, SKIPPED)]
        closed.sort()
        if len(closed) > keep:
            for _, code in closed[:len(closed) - keep]:
                bucket.pop(code, None)
                removed += 1
        for code, item in list(bucket.items()):
            if item.get("state") in (QUEUED, SKIPPED, DONE) and float(item.get("next_at") or 0) < cutoff:
                bucket.pop(code, None)
                removed += 1
    return removed
