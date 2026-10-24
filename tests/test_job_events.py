"""D2-a 任务事件表：迁移矩阵、写入/读取顺序、失败隔离、接口附加 events。

时间线以前只能画「入队 / 最近更新」——库里只有最新值，没有阶段历史。
这批加了 job_events 表并在真实写入点埋事件，这里逐条盯住：
  · v0/v1/v2/v3/v4 的库都能升到 v5，包括「版本号写着 v4 但缺 state 表」的被误标库；
  · 事件按时间升序读出，limit 取的是「最近 N 条」而不是最老的 N 条；
  · event() 失败绝不影响主流程（任务照样登记、照样推进）；
  · /api/jobs 每条任务都带 events，老任务为空表也不报错。
"""
from __future__ import annotations

import shutil
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from app import store, web

LEGACY_V1 = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, code TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
    stage TEXT NOT NULL DEFAULT 'queued', percent REAL NOT NULL DEFAULT 0,
    speed REAL NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
    downloaded INTEGER NOT NULL DEFAULT 0, folder TEXT NOT NULL DEFAULT '',
    link TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
    retries INTEGER NOT NULL DEFAULT 0, owner TEXT NOT NULL DEFAULT '',
    xunlei_task TEXT NOT NULL DEFAULT '', xunlei_ours INTEGER NOT NULL DEFAULT 1,
    keep_dir TEXT NOT NULL DEFAULT '', extra TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_stage ON jobs(stage);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_active_code
    ON jobs(code) WHERE code <> '' AND stage IN
        ('queued','downloading','organizing','uploading');
"""

# v2 结构：v1 + resume_from
V2 = LEGACY_V1.replace(
    "    keep_dir TEXT NOT NULL DEFAULT '',",
    "    keep_dir TEXT NOT NULL DEFAULT '', resume_from TEXT NOT NULL DEFAULT '',")
# v3 结构：v2 + exec_owner / exec_beat
V3 = V2.replace(
    "    keep_dir TEXT NOT NULL DEFAULT '', resume_from TEXT NOT NULL DEFAULT '',",
    "    keep_dir TEXT NOT NULL DEFAULT '', resume_from TEXT NOT NULL DEFAULT '',"
    " exec_owner TEXT NOT NULL DEFAULT '', exec_beat REAL NOT NULL DEFAULT 0,")
V4 = V3 + """
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL DEFAULT 0);
"""


def make_db(path: Path, schema: str, version: int, rows=()) -> str:
    conn = sqlite3.connect(str(path))
    if schema:
        conn.executescript(schema)
    for job in (rows if schema else ()):   # 空库（v0）还没有 jobs 表
        conn.execute(
            "INSERT INTO jobs (id, code, source, stage, extra, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (job["id"], job.get("code", ""), job.get("source", "magnet:a"),
             job.get("stage", "queued"), job.get("extra", "{}"), time.time(), time.time()))
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
    conn.close()
    return str(path)


def columns(path: str, table: str) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    finally:
        conn.close()


def tables(path: str) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    finally:
        conn.close()


def user_version(path: str) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


# ─────────────────────────────────────────── 迁移矩阵

@pytest.mark.parametrize("label,schema,version", [
    ("v0-空库", "", 0),
    ("v1", LEGACY_V1, 1),
    ("v2", V2, 2),
    ("v3", V3, 3),
    ("v4", V4, 4),
])
def test_every_old_version_upgrades_to_v5(tmp_path, label, schema, version):
    path = make_db(tmp_path / "jobs.db", schema, version,
                   rows=[{"id": "old1", "code": "SSIS-1"}])
    s = store.Store(path)
    assert user_version(path) == store.SCHEMA_VERSION == 6, label
    assert {"job_events", "jobs", "state"} <= tables(path), label
    assert {"job_id", "at", "kind", "detail"} <= columns(path, "job_events"), label
    # 老数据还在（v0 是空库，本来就没有行），且升级后能正常记事件
    if schema:
        assert s.get("old1")["code"] == "SSIS-1"
    assert s.event("old1", "resumed", "升级后补记") is True
    assert [e["kind"] for e in s.events("old1")] == ["resumed"]


def test_v2_upgrade_adds_resume_from_and_v3_columns(tmp_path):
    """版本号逐级推进：v2 库升到 v3 时缺的两列要补上，不能只跳到 v5。"""
    path = make_db(tmp_path / "jobs.db", V2, 2, rows=[{"id": "a", "code": "ABP-1"}])
    assert "exec_owner" not in columns(path, "jobs")
    store.Store(path)
    assert {"resume_from", "exec_owner", "exec_beat"} <= columns(path, "jobs")
    assert user_version(path) == 6


def test_mislabelled_v4_without_state_table_is_repaired(tmp_path):
    """被误标成 v4、实际缺 state 表（迁移中途失败）的库：按表结构补，不只信版本号。"""
    path = make_db(tmp_path / "jobs.db", V3, 4, rows=[{"id": "a", "code": "ABP-2"}])
    assert "state" not in tables(path)
    s = store.Store(path)
    assert {"state", "job_events"} <= tables(path)
    assert s.state_get("subs") == {}


def test_mislabelled_v4_missing_exec_columns_is_repaired(tmp_path):
    """更狠的一种：版本号 v4、实际缺 exec_owner/exec_beat。按列检查补齐。"""
    bare = V2 + """
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL DEFAULT 0);
"""
    path = make_db(tmp_path / "jobs.db", bare, 4, rows=[{"id": "a", "code": "ABP-3"}])
    assert "exec_owner" not in columns(path, "jobs")
    store.Store(path)
    assert {"resume_from", "exec_owner", "exec_beat"} <= columns(path, "jobs")
    assert user_version(path) == 6


def test_migration_is_idempotent(tmp_path):
    path = make_db(tmp_path / "jobs.db", V4, 4)
    first = store.Store(path)
    second = store.Store(path)
    assert second.migrate() == store.SCHEMA_VERSION
    first.event("x", "queued")
    assert len(second.events("x")) == 1, "再开一次库不该清空事件"


def test_migration_failure_rolls_back(tmp_path, monkeypatch):
    """迁移中途失败要整体回滚：版本号不前移，库还能再试。"""
    path = make_db(tmp_path / "jobs.db", V4, 4, rows=[{"id": "a", "code": "ABP-4"}])

    def boom(conn):
        raise sqlite3.OperationalError("模拟迁移失败")

    monkeypatch.setattr(store.Store, "_migrate_v5", staticmethod(boom))
    with pytest.raises(sqlite3.OperationalError):
        store.Store(path)
    # 版本号必须留在原地，且迁移事务里写的东西（这张表）整体回滚掉。
    # job_events 可能已被 __init__ 的 executescript(SCHEMA) 建出来，这里不用它判回滚；
    # 用户数据与版本号才是回滚是否生效的判据。
    assert user_version(path) == 4, "失败后版本号必须留在原地"
    monkeypatch.undo()                   # 撤掉注入的失败，验证「还能再试」
    assert store.Store(path).get("a")["code"] == "ABP-4", "旧数据完好"
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION, \
            "回滚后还能再试，重试要成功升到 v5"


# ─────────────────────────────────────────── 事件写入与读取

def test_events_are_returned_in_ascending_order(tmp_path):
    s = store.Store(str(tmp_path / "jobs.db"))
    s.start("j1", "SSIS-1", "magnet:a", owner="web")
    for kind, detail in (("claimed", "web:tok"), ("stage", "downloading"),
                         ("retry", "下载第 1 次"), ("stage", "organizing"),
                         ("stage", "uploading"), ("done", "SSIS-1 · 1.2 GB")):
        assert s.event("j1", kind, detail) is True
        time.sleep(0.002)                 # 时间戳有先后，排序才有意义
    evs = s.events("j1")
    assert [e["kind"] for e in evs] == ["queued", "claimed", "stage", "retry",
                                        "stage", "stage", "done"]
    assert evs[0]["detail"].startswith("发起方 web")
    assert [e["at"] for e in evs] == sorted(e["at"] for e in evs)
    assert set(evs[0]) == {"at", "kind", "detail"}, "契约只有这三个字段"


def test_events_limit_keeps_the_newest(tmp_path):
    s = store.Store(str(tmp_path / "jobs.db"))
    s.start("j1", "SSIS-1", "magnet:a")
    for i in range(30):
        s.event("j1", "stage", f"n{i}")
    evs = s.events("j1", limit=5)
    assert len(evs) == 5
    assert [e["detail"] for e in evs] == ["n25", "n26", "n27", "n28", "n29"], "取最近 N 条"
    assert [e["at"] for e in evs] == sorted(e["at"] for e in evs), "读出来仍是升序"


def test_events_of_unknown_job_are_empty_not_error(tmp_path):
    """老任务没有事件：读出来是空表，不能抛异常。"""
    s = store.Store(str(tmp_path / "jobs.db"))
    assert s.events("不存在的任务") == []
    assert s.events("") == []


def test_event_without_job_id_is_rejected_silently(tmp_path):
    s = store.Store(str(tmp_path / "jobs.db"))
    assert s.event("", "queued") is False
    assert s.event("j1", "") is False


def test_prune_removes_events_of_pruned_jobs(tmp_path):
    s = store.Store(str(tmp_path / "jobs.db"))
    s.start("old", "A-1", "magnet:a")
    s.finish("old", "done")
    s.event("old", "done", "归档完成")
    with s._conn() as conn:
        conn.execute("UPDATE jobs SET updated_at=? WHERE id='old'", (time.time() - 40 * 86400,))
    assert s.prune(30) == 1
    assert s.events("old") == [], "任务记录删了，它的事件也不该无限留着"


# ─────────────────────────────────────────── 失败隔离

def test_event_failure_never_breaks_the_caller(tmp_path, monkeypatch):
    """event() 写失败只记日志：调用方拿到 False，主流程照走。"""
    s = store.Store(str(tmp_path / "jobs.db"))
    s.start("j1", "SSIS-1", "magnet:a")

    def boom(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(s, "_conn", boom)
    assert s.event("j1", "claimed", "x") is False
    assert s.events("j1") == [], "读失败也退化成空表"

    # 网页侧包一层：库整个不可用也不该把任务流程打断
    class Broken:
        def event(self, *_a, **_k):
            raise RuntimeError("库炸了")

        def events(self, *_a, **_k):
            raise RuntimeError("库炸了")

        def get(self, *_a, **_k):
            return {}

    monkeypatch.setattr(web, "_store", lambda: Broken())
    web.job_event("j1", "stage", "downloading")        # 不抛即达标


def test_store_start_survives_event_failure(tmp_path, monkeypatch):
    """入队事件写失败不能回滚任务登记：任务必须存在，只少一条时间线节点。"""
    path = str(tmp_path / "jobs.db")
    s = store.Store(path)
    real_conn = store.Store._conn

    class Conn:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def execute(self, sql, *args):
            if "INSERT INTO job_events" in sql:
                raise sqlite3.OperationalError("no such table: job_events")
            return self._inner.execute(sql, *args)

    import contextlib

    @contextlib.contextmanager
    def wrapped(self_):
        with real_conn(self_) as conn:
            yield Conn(conn)

    monkeypatch.setattr(store.Store, "_conn", wrapped)
    assert s.start("j2", "SSIS-2", "magnet:b") is True, "任务登记不受事件失败影响"
    monkeypatch.undo()
    assert s.get("j2")["stage"] == "queued"


# ─────────────────────────────────────────── 接口与写入点

@pytest.fixture
def env(tmp_path, monkeypatch):
    db = str(tmp_path / "jobs.db")
    monkeypatch.setattr(web, "STORE", store.Store(db))
    monkeypatch.setenv("JOBS_DB", db)
    monkeypatch.setattr(web, "JOBS", {})
    monkeypatch.setattr(web, "WAITING", {})
    monkeypatch.setattr(web, "_queued_at", {})
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    web._threads.clear()
    web.set_paused(False)
    return db


def test_api_snapshot_attaches_events(env):
    web.STORE.start("j1", "SSIS-1", "magnet:a", owner="web")
    web.STORE.event("j1", "claimed", "web:tok")
    rows = web.jobs_snapshot()
    assert len(rows) == 1
    assert [e["kind"] for e in rows[0]["events"]] == ["queued", "claimed"]
    assert all({"at", "kind", "detail"} <= set(e) for e in rows[0]["events"])


def test_api_snapshot_old_job_without_events_is_empty_list(env):
    """迁移前入库的老任务：events 是空表，接口不报错。"""
    web.STORE.start("old", "A-1", "magnet:a")
    with web.STORE._conn() as conn:
        conn.execute("DELETE FROM job_events WHERE job_id='old'")
    rows = web.jobs_snapshot()
    assert rows[0]["events"] == []


def test_start_job_writes_queued_event(env, monkeypatch):
    monkeypatch.setattr(web, "admit", lambda job_id: False)     # 不真起线程
    job_id = web.start_job("magnet:?xt=urn:btih:" + "a" * 40, "SSIS-1")
    assert job_id
    evs = web.STORE.events(job_id)
    assert evs and evs[0]["kind"] == "queued" and "发起方 web" in evs[0]["detail"]


def test_requeue_and_stop_write_events(env, monkeypatch):
    monkeypatch.setattr(web, "admit", lambda job_id: False)
    web.STORE.start("j1", "SSIS-1", "magnet:a")
    web.requeue("j1", "magnet:a", "SSIS-1")
    assert web.STORE.events("j1")[-1]["kind"] == "queued"
    assert web.stop_job("j1") is True
    assert web.STORE.events("j1")[-1]["kind"] == "cancelled"


def test_defer_path_writes_deferred_event_with_wake_time(env):
    web.STORE.start("j1", "SSIS-1", "magnet:a")
    until = time.time() + 3600
    web.STORE.defer("j1", until, "额度用尽")
    web._event_deferred("j1", until, "额度用尽", "quota exceeded\n第二行不要")
    ev = web.STORE.events("j1")[-1]
    assert ev["kind"] == "deferred"
    assert time.strftime("%m-%d %H:%M", time.localtime(until)) in ev["detail"]
    assert "quota exceeded" in ev["detail"] and "第二行" not in ev["detail"]


def test_cancel_defer_writes_cancelled_event(tmp_path):
    s = store.Store(str(tmp_path / "jobs.db"))
    s.start("j1", "SSIS-1", "magnet:a")
    s.defer("j1", time.time() + 60)
    assert s.cancel_defer("j1") is True
    assert [e["kind"] for e in s.events("j1")][-1] == "cancelled"


def test_resume_loop_writes_resumed_event(env, monkeypatch):
    web.STORE.start("j1", "SSIS-1", "magnet:a")
    web.STORE.update("j1", stage="downloading")
    queued: list[str] = []
    monkeypatch.setattr(web, "requeue",
                        lambda job_id, source, code, stage="queued": queued.append(job_id) or True)
    monkeypatch.setattr(web, "recover_jobs", lambda: [
        {"id": "j1", "code": "SSIS-1", "source": "magnet:a", "stage": "downloading"}])
    web._resume_loop()
    assert queued == ["j1"]
    ev = web.STORE.events("j1")[-1]
    assert ev["kind"] == "resumed" and "downloading" in ev["detail"]


def test_resume_loop_skips_while_reconcile_pending(env, monkeypatch):
    web.STORE.state_put({web.RECONCILE_KEY: {"required": True}})
    called: list[int] = []
    monkeypatch.setattr(web, "recover_jobs", lambda: called.append(1) or [])
    web._resume_loop()
    assert called == [], "待对账时不该自动把未完成任务重新入队"


def test_upload_retry_records_first_line_only(env):
    assert web._first_line("死种\nTraceback...") == "死种"
    assert web._first_line("") == ""
    assert len(web._first_line("x" * 500)) == 120
