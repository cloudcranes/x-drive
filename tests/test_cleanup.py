"""归档回收决策的纯逻辑测试。"""
import time

from app import cleanup


def mk(code: str, size_gb: float, days_ago: float, now: float):
    return {"code": code, "size": int(size_gb * 1024**3),
            "at_ts": now - days_ago * 86400,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - days_ago * 86400))}


NOW = time.mktime(time.strptime("2026-10-21 12:00:00", "%Y-%m-%d %H:%M:%S"))
ARCHIVES = [mk(f"CODE-{i}", 1, i, NOW) for i in range(20)]   # 20 条，每条 1GB


def test_keeps_most_recent_regardless_of_capacity():
    plan = cleanup.plan_cleanup(ARCHIVES, keep_recent=10, max_bytes=5 * 1024**3, now=NOW)
    kept = {i["code"] for i in plan.keep}
    assert {"CODE-0", "CODE-9"} <= kept, "最近 10 条必须保留"
    assert all(i["code"] != "CODE-0" for i in plan.delete)


def test_capacity_limit_evicts_oldest_first():
    plan = cleanup.plan_cleanup(ARCHIVES, keep_recent=5, max_bytes=10 * 1024**3, now=NOW)
    assert plan.freed_bytes >= 10 * 1024**3 - 0, "要删到总量不超过上限"
    deleted = [i["code"] for i in plan.delete]
    assert "CODE-19" in deleted, "最旧的应最先被删"
    assert "CODE-5" not in deleted, "较新的不该被删"


def test_active_downloads_never_deleted():
    """正在下载的番号即使很旧也不能删，否则会把任务的数据清掉。"""
    plan = cleanup.plan_cleanup(
        ARCHIVES, keep_recent=2, max_bytes=1, active_codes={"CODE-19"}, now=NOW)
    assert "CODE-19" not in [i["code"] for i in plan.delete]
    assert "CODE-19" in [i["code"] for i in plan.keep]


def test_no_limit_means_nothing_deleted():
    plan = cleanup.plan_cleanup(ARCHIVES, keep_recent=5, max_bytes=0, now=NOW)
    assert plan.delete == []
    assert "未设置" in plan.reason


def test_older_than_days_threshold():
    plan = cleanup.plan_cleanup(ARCHIVES, keep_recent=1, older_than_days=15, now=NOW)
    deleted = {i["code"] for i in plan.delete}
    assert "CODE-19" in deleted and "CODE-16" in deleted
    assert "CODE-14" not in deleted, "未满 15 天的不该删"


def test_dry_run_flag_propagates():
    plan = cleanup.plan_cleanup(ARCHIVES, keep_recent=1, max_bytes=1024**3, dry_run=True, now=NOW)
    assert plan.as_dict()["dry_run"] is True
    plan2 = cleanup.plan_cleanup(ARCHIVES, keep_recent=1, max_bytes=1024**3, dry_run=False, now=NOW)
    assert plan2.as_dict()["dry_run"] is False


def test_iso_string_time_is_parsed():
    """state.json 里存的是 ISO 字符串，没有 at_ts 也要能排序。"""
    old = {"code": "OLD-1", "size": 100, "at": "2026-01-01T00:00:00"}
    new = {"code": "NEW-1", "size": 100, "at": "2026-10-20T00:00:00"}
    plan = cleanup.plan_cleanup([old, new], keep_recent=1, max_bytes=1, now=NOW)
    assert [i["code"] for i in plan.delete] == ["OLD-1"]


def test_empty_archive_list():
    plan = cleanup.plan_cleanup([], max_bytes=1024, now=NOW)
    assert plan.delete == [] and plan.freed_bytes == 0


def test_keep_recent_larger_than_list_protects_all():
    plan = cleanup.plan_cleanup(ARCHIVES[:3], keep_recent=10, max_bytes=1, now=NOW)
    assert plan.delete == [], "全部都在 keep_recent 范围内时不能删任何东西"
