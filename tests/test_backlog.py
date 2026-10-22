"""订阅待办表：发现与提交分开后，漏项与队头阻塞都不该再出现。"""
import time

from app import backlog

def item(code, date):
    return {"code": code, "date": date, "title": f"T {code}"}

def test_discovered_items_are_all_remembered():
    data = {}
    added = backlog.remember(data, "javbus:actor:w5a",
                             [item("A-1", "2020-01-01"), item("A-2", "2020-01-02")], "2026-01-01")
    assert added == 2
    assert set(data["javbus:actor:w5a"]) == {"A-1", "A-2"}

def test_due_returns_oldest_first_with_spare_candidates():
    """按日期从旧到新返回，并多备几个候选（队头没磁力时不白占名额）。"""
    data = {}
    backlog.remember(data, "s", [item("B-2", "2020-02-02"), item("B-1", "2020-01-01")], "2026-01-01")
    assert backlog.due(data, "s", limit=1) == ["B-1", "B-2"]
    assert backlog.due(data, "s", limit=5) == ["B-1", "B-2"]

def test_due_spare_candidates_are_bounded():
    data = {}
    backlog.remember(data, "s", [item(f"B-{i:02d}", f"2020-01-{i:02d}") for i in range(1, 21)],
                     "2026-01-01")
    got = backlog.due(data, "s", limit=2)
    assert len(got) == 2 + backlog.EXTRA_CANDIDATES

def test_missing_magnet_backs_off_without_blocking_others():
    """队头没有磁力：它退避，后面的作品照常轮到 —— 这就是队头阻塞的修法。"""
    data = {}
    backlog.remember(data, "s", [item("C-1", "2020-01-01"), item("C-2", "2020-01-02")], "2026-01-01")
    assert backlog.due(data, "s", limit=2) == ["C-1", "C-2"]
    backlog.mark(data, "s", "C-1", backlog.PENDING, "还没有磁力")
    # 刚退避过的不再重复查，但 C-2 仍能被取到
    assert backlog.due(data, "s", limit=2) == ["C-2"]
    # 退避到点后 C-1 会回来
    data["s"]["C-1"]["next_at"] = time.time() - 1
    assert "C-1" in backlog.due(data, "s", limit=2)

def test_pending_items_survive_prune():
    """待办里的 pending 永不清理：删掉就等于漏订。"""
    data = {}
    backlog.remember(data, "s", [item("D-1", "2020-01-01")], "2026-01-01")
    data["s"]["D-1"]["next_at"] = 0
    backlog.mark(data, "s", "D-2", backlog.SKIPPED)
    backlog.remember(data, "s", [item("D-2", "2019-01-01")], "2026-01-01")
    backlog.mark(data, "s", "D-2", backlog.SKIPPED)
    data["s"]["D-2"]["next_at"] = 0
    removed = backlog.prune(data, days=1, keep=0)
    assert removed >= 1
    assert "D-1" in data["s"], "pending 不能删"
    assert "D-2" not in data["s"], "已结束的老记录可以清"

def test_counts_by_state():
    data = {}
    backlog.remember(data, "s", [item("E-1", "2020-01-01"), item("E-2", "2020-01-02")], "2026-01-01")
    backlog.mark(data, "s", "E-2", backlog.QUEUED)
    tally = backlog.counts(data, "s")
    assert tally[backlog.PENDING] == 1 and tally[backlog.QUEUED] == 1
