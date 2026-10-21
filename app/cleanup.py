"""归档回收：决定「哪些旧归档该删」，与真正删除动作分开。

只做决策、不碰文件/网络，方便纯逻辑测试；删除动作由调用方（web.py）执行。
设计原则：
  · 永远保留最近 N 个（默认 10），避免误配一条清空
  · 可按容量上限淘汰最旧的
  · 正在下载/上传中的番号绝不入选
  · dry_run 时只返回清单，调用方负责不执行
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Plan:
    """一次回收计划：删哪些、为什么、能省多少。"""

    delete: list[dict] = field(default_factory=list)
    keep: list[dict] = field(default_factory=list)
    reason: str = ""
    dry_run: bool = True

    @property
    def freed_bytes(self) -> int:
        return sum(int(item.get("size") or 0) for item in self.delete)

    def as_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "reason": self.reason,
            "delete": self.delete,
            "keep_count": len(self.keep),
            "delete_count": len(self.delete),
            "freed_bytes": self.freed_bytes,
        }


def plan_cleanup(
    archives: list[dict],
    *,
    keep_recent: int = 10,
    max_bytes: int = 0,
    active_codes: set[str] | None = None,
    older_than_days: float = 0,
    now: float | None = None,
    dry_run: bool = True,
) -> Plan:
    """算出该删哪些归档。

    archives: 每项含 code / size / at(ISO 字符串) / at_ts(秒，可选)
    keep_recent: 最近几个永不删
    max_bytes: 总容量上限，超出就淘汰最旧的；0 表示不限
    active_codes: 正在下载/上传的番号，绝不删
    older_than_days: 只考虑这么久的归档；0 表示不限
    """
    import time

    active = {c.upper() for c in (active_codes or set()) if c}
    current = time.time() if now is None else now

    def ts_of(item: dict) -> float:
        if item.get("at_ts"):
            return float(item["at_ts"])
        raw = (item.get("at") or "").replace("T", " ")
        try:
            return time.mktime(time.strptime(raw[:19], "%Y-%m-%d %H:%M:%S"))
        except (ValueError, TypeError):
            return 0.0

    items = []
    for a in archives:
        if not a.get("code") and not a.get("id"):
            continue
        entry = dict(a)
        entry["_ts"] = ts_of(entry)
        items.append(entry)
    # 新的在前
    items.sort(key=lambda x: x["_ts"], reverse=True)

    protected: list[dict] = []
    candidates: list[dict] = []
    for idx, item in enumerate(items):
        code = (item.get("code") or item.get("id") or "").upper()
        if idx < max(0, keep_recent):
            protected.append(item)
        elif code in active:
            protected.append(item)          # 正在跑的不删
        elif older_than_days and (current - item["_ts"]) < older_than_days * 86400:
            protected.append(item)          # 还没到门槛
        else:
            candidates.append(item)

    def brief(item: dict) -> dict:
        return {
            "code": item.get("code") or item.get("id") or "",
            "size": int(item.get("size") or 0),
            "at": item.get("at") or "",
        }

    delete: list[dict] = []
    if max_bytes and max_bytes > 0:
        # 保留部分 + 候选，算总量；超了就按「最旧的先删」
        total = sum(int(i.get("size") or 0) for i in items)
        if total > max_bytes:
            for item in sorted(candidates, key=lambda x: x["_ts"]):
                if total <= max_bytes:
                    break
                delete.append(item)
                total -= int(item.get("size") or 0)
            # 极端情况：删光候选仍超标，也只能删到这儿（不动 keep_recent）
    elif older_than_days:
        delete = list(candidates)          # 只按时间门槛清

    if not delete:
        reason = "没有需要清理的归档"
        if not max_bytes and not older_than_days:
            reason = "未设置容量上限或时间门槛，不做清理"
    elif max_bytes:
        reason = f"总容量超过 {max_bytes // 1024**3}GB，按最旧优先淘汰"
    else:
        reason = f"超过 {older_than_days} 天的归档"

    return Plan(
        delete=[brief(i) for i in sorted(delete, key=lambda x: x["_ts"])],
        keep=[brief(i) for i in protected],
        reason=reason,
        dry_run=dry_run,
    )
