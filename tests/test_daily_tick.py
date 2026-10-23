"""每日 tick：让已经写好却没人调用的每日汇总/长期未达提醒/数据表修剪跑起来。

三件事：
  1. 每日 tick 跨天触发一次、同一天不重复、进程重启后当天也不重跑；
  2. 两个 prune 被真正调用（参数合理）；
  3. reconcile_backlog 的正确性缺陷：以前用 recent(200) 取「最新 200 条」兜底，
     待办里的任务一多就查不到 → 误判「记录已不存在」→ 放回 pending → 重复下载。

断言全看行为（调用了几次、任务落在什么状态、待办里剩什么），不看源码字符串。
notify 全部打桩：它会发 Telegram 真请求。
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from app import backlog, store, web


@pytest.fixture
def env(tmp_path, monkeypatch):
    """干净隔离：库与状态文件都在 tmp_path，通知全部打桩。"""
    db = str(tmp_path / "jobs.db")
    monkeypatch.setattr(web, "STORE", store.Store(db))
    monkeypatch.setenv("JOBS_DB", db)
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    # 通知会发 Telegram，一律打桩；只记「发了几条」
    sent: list[str] = []
    monkeypatch.setattr(web.notify, "daily_summary",
                        lambda text, day: sent.append(f"digest:{day}") or True)
    monkeypatch.setattr(web.notify, "long_pending",
                        lambda code, days, note="": sent.append(f"pending:{code}") or True)
    monkeypatch.setattr(web.notify, "job_done", lambda *a, **k: True)
    monkeypatch.setattr(web.notify, "job_failed", lambda *a, **k: True)
    monkeypatch.setattr(web.notify, "job_deferred", lambda *a, **k: True)
    yield {"db": db, "sent": sent}
    web.STORE = None


# ─────────────────────────────────────────── 每日 tick

def test_daily_tick_runs_once_per_day(env, monkeypatch):
    """同一天里反复调用只跑一次。"""
    calls: list[str] = []
    monkeypatch.setattr(web, "daily_digest",
                        lambda now=None: calls.append("digest") or {"ok": True})
    monkeypatch.setattr(web, "notify_long_pending",
                        lambda days=14.0, now=None: calls.append("pending") or [])

    first = web._daily_tick(now=time.time())
    second = web._daily_tick(now=time.time())

    assert first["skipped"] is False, "当天第一次必须跑"
    assert second["skipped"] is True, "同一天不该再跑"
    assert calls == ["digest", "pending"], f"各该跑一次：{calls}"


def test_daily_tick_runs_again_next_day(env, monkeypatch):
    """跨天后再跑一次。"""
    calls: list[str] = []
    monkeypatch.setattr(web, "daily_digest",
                        lambda now=None: calls.append("digest") or {"ok": True})
    monkeypatch.setattr(web, "notify_long_pending",
                        lambda days=14.0, now=None: calls.append("pending") or [])

    day1 = time.mktime(time.strptime("2026-10-23 03:00:00", "%Y-%m-%d %H:%M:%S"))
    day2 = time.mktime(time.strptime("2026-10-24 03:00:00", "%Y-%m-%d %H:%M:%S"))

    web._daily_tick(now=day1)
    out = web._daily_tick(now=day2)

    assert out["skipped"] is False
    assert calls == ["digest", "pending", "digest", "pending"], calls


def test_daily_tick_survives_restart_without_resending(env, monkeypatch):
    """进程重启后当天已经跑过的不再重跑（日期记在库里，不在内存）。"""
    calls: list[str] = []
    monkeypatch.setattr(web, "daily_digest",
                        lambda now=None: calls.append("digest") or {"ok": True})
    monkeypatch.setattr(web, "notify_long_pending",
                        lambda days=14.0, now=None: calls.append("pending") or [])

    now = time.time()
    web._daily_tick(now=now)

    # 模拟重启：内存里的模块级状态清空，重新按库路径打开
    web.STORE = None
    out = web._daily_tick(now=now)

    assert out["skipped"] is True, "重启后不该把当天的汇总重发一遍"
    assert calls == ["digest", "pending"], calls


def test_daily_tick_records_day_in_state(env):
    """跑过的日期要落在任务库的 state 表里。"""
    web._daily_tick(now=time.time())
    saved = web._store().state_get(web.DAILY_TICK_KEY)
    assert saved.get("day") == time.strftime("%Y-%m-%d", time.localtime())


def test_daily_tick_calls_prunes(env, monkeypatch):
    """两个 prune 都要被调用，且参数合理。"""
    seen: dict = {}

    real_prune = web._store().prune

    def spy_prune(days: float = 30.0):
        seen["store_prune_days"] = days
        return real_prune(days)

    monkeypatch.setattr(web._store(), "prune", spy_prune)

    real_backlog_prune = backlog.prune
    seen["backlog_prune"] = []

    def spy_backlog_prune(data, days=90.0, keep=500):
        seen["backlog_prune"].append((days, keep))
        return real_backlog_prune(data, days=days, keep=keep)

    monkeypatch.setattr(web.backlog, "prune", spy_backlog_prune)
    monkeypatch.setattr(web, "daily_digest", lambda now=None: {"ok": True})
    monkeypatch.setattr(web, "notify_long_pending", lambda days=14.0, now=None: [])

    out = web._daily_tick(now=time.time())

    assert seen.get("store_prune_days") == 30, "任务表保留天数不对"
    assert seen.get("backlog_prune"), "待办 prune 没被调用"
    assert seen["backlog_prune"][0] == (90, 500), f"待办 prune 参数不对：{seen['backlog_prune']}"
    assert out["jobs_pruned"] == 0 and out["backlog_pruned"] == 0


def test_daily_tick_prunes_old_finished_jobs(env, monkeypatch):
    """真的 prune：30 天前结束的旧任务被清掉，正在跑的不动。"""
    monkeypatch.setattr(web, "daily_digest", lambda now=None: {"ok": True})
    monkeypatch.setattr(web, "notify_long_pending", lambda days=14.0, now=None: [])
    st = web._store()
    st.start("old-done", "OLD-1", "magnet:a")
    st.finish("old-done", "done")
    st.start("old-failed", "OLD-2", "magnet:b")
    st.finish("old-failed", "failed")
    st.start("running", "LIVE-1", "magnet:c")
    st.update("running", stage="downloading")
    # update() 会把 updated_at 覆盖成当前时间，要造「很久以前」只能直接改库
    old = time.time() - 60 * 86400
    with sqlite3.connect(env["db"]) as conn:
        conn.execute("UPDATE jobs SET updated_at=? WHERE id IN ('old-done','old-failed')",
                     (old,))
        conn.commit()

    out = web._daily_tick(now=time.time())

    assert out["jobs_pruned"] == 2, f"应清掉两条旧记录：{out}"
    assert not st.get("old-done")
    assert not st.get("old-failed")
    assert st.get("running"), "正在跑的任务不许被清"


def test_daily_tick_never_prunes_pending_backlog(env, monkeypatch):
    """待办里的 pending 永不删（删了就等于漏订）。"""
    monkeypatch.setattr(web, "daily_digest", lambda now=None: {"ok": True})
    monkeypatch.setattr(web, "notify_long_pending", lambda days=14.0, now=None: [])
    long_ago = time.time() - 400 * 86400
    todo = {"sub:a": {
        "PEND-1": {"code": "PEND-1", "state": backlog.PENDING,
                   "next_at": 0, "since": long_ago},
        "DONE-1": {"code": "DONE-1", "state": backlog.DONE,
                   "next_at": 0, "since": long_ago},
    }}
    web._store().state_put({"backlog": todo})

    web._daily_tick(now=time.time())

    saved = web._store().state_get("backlog")
    assert "PEND-1" in saved["sub:a"], "pending 被删了"
    assert "DONE-1" not in saved["sub:a"], "早就结束的记录该清掉"


def test_daily_tick_sends_digest_and_long_pending(env, monkeypatch):
    """汇总与长期未达提醒都要真的发出去（通知层打桩）。"""
    monkeypatch.setattr(web, "daily_digest", lambda now=None: {"ok": True, "day": "x"})
    monkeypatch.setattr(web, "notify_long_pending",
                        lambda days=14.0, now=None: [{"code": "AAA-1", "sent": True}])

    out = web._daily_tick(now=time.time())

    assert out["digest"]["day"] == "x"
    assert out["long_pending"] == 1


def test_daily_tick_survives_broken_digest(env, monkeypatch):
    """汇总炸了不能把整个 tick 带崩（后面的 prune 仍要跑）。"""
    def boom(now=None):
        raise RuntimeError("汇总炸了")

    monkeypatch.setattr(web, "daily_digest", boom)
    monkeypatch.setattr(web, "notify_long_pending", lambda days=14.0, now=None: [])

    out = web._daily_tick(now=time.time())

    assert out.get("digest_error"), "应当如实记下错误"
    assert "backlog_pruned" in out, "后面的步骤仍要跑"


# ─────────────────────────────────────────── reconcile_backlog 的正确性缺陷

def test_reconcile_backlog_finds_job_outside_latest_200(env, monkeypatch):
    """待办里的任务不在最新 200 条里时，仍要查得到它，不该当成「记录已不存在」。

    以前 reconcile_backlog 用 recent(200) 兜底取全部（recent 取的是最新 N 条），
    任务一多就漏 → 误判记录不存在 → 放回 pending → 重复下载。
    """
    st = web._store()
    # 造 250 条更新的任务，把目标任务挤到 latest 200 之外
    target_id = "old-target"
    st.start(target_id, "TGT-1", "magnet:target")
    st.update(target_id, stage="downloading")
    for i in range(250):
        st.start(f"new-{i:03d}", f"NEW-{i:03d}", f"magnet:{i}")
        st.update(f"new-{i:03d}", stage="downloading")

    # 确认它确实不在 latest 200 里 —— 这正是触发条件
    latest = {j["id"] for j in st.recent(200)}
    assert target_id not in latest, "前置条件不成立：用例没测到东西"

    todo = {"sub:a": {"TGT-1": {"code": "TGT-1", "state": backlog.QUEUED,
                                 "job_id": target_id, "note": "", "next_at": 0}}}
    monkeypatch.setattr(web, "archived_codes", lambda: set())

    changed = web.reconcile_backlog(todo)

    assert changed == [], f"还在下载的任务不该被放回待办：{changed}"
    assert todo["sub:a"]["TGT-1"]["state"] == backlog.QUEUED, "状态被误改了"


def test_reconcile_backlog_requeues_genuinely_missing_job(env, monkeypatch):
    """任务确实不存在时，仍然放回 pending（这个行为要保留）。"""
    todo = {"sub:a": {"GONE-1": {"code": "GONE-1", "state": backlog.QUEUED,
                                 "job_id": "no-such-job", "note": "", "next_at": 0}}}
    monkeypatch.setattr(web, "archived_codes", lambda: set())

    changed = web.reconcile_backlog(todo)

    assert changed and changed[0]["result"] == "requeued"
    assert todo["sub:a"]["GONE-1"]["state"] == backlog.PENDING


def test_reconcile_backlog_keeps_deferred_job_queued(env, monkeypatch):
    """等额度（deferred）的任务不算失败，不该被放回待办。"""
    st = web._store()
    st.start("def-1", "DEF-1", "magnet:a")
    st.update("def-1", stage="deferred")
    todo = {"sub:a": {"DEF-1": {"code": "DEF-1", "state": backlog.QUEUED,
                                 "job_id": "def-1", "note": "", "next_at": 0}}}
    monkeypatch.setattr(web, "archived_codes", lambda: set())

    changed = web.reconcile_backlog(todo)

    assert changed == [], changed
    assert todo["sub:a"]["DEF-1"]["state"] == backlog.QUEUED


def test_reconcile_backlog_marks_archived_as_done(env, monkeypatch):
    """已归档的番号标成 done。"""
    todo = {"sub:a": {"ARC-1": {"code": "ARC-1", "state": backlog.QUEUED,
                                 "job_id": "j1", "note": "", "next_at": 0}}}
    monkeypatch.setattr(web, "archived_codes", lambda: {"ARC-1"})

    changed = web.reconcile_backlog(todo)

    assert changed[0]["result"] == "archived"
    assert todo["sub:a"]["ARC-1"]["state"] == backlog.DONE
