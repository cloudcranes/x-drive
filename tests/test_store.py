"""任务表：并发去重、状态推进、重启可见。"""
import json
import time

from app import store

def test_start_and_get(tmp_path):
    s = store.Store(str(tmp_path / "jobs.db"))
    assert s.start("j1", "SSIS-123", "magnet:?xt=urn:btih:aa") is True
    job = s.get("j1")
    assert job["stage"] == "queued" and job["code"] == "SSIS-123"

def test_same_code_cannot_run_twice(tmp_path):
    """并发提交同一番号只能有一个未结束任务 —— 靠部分唯一索引兜住。"""
    s = store.Store(str(tmp_path / "jobs.db"))
    assert s.start("j1", "SSIS-123", "magnet:a") is True
    assert s.start("j2", "SSIS-123", "magnet:b") is False
    s.finish("j1", "done")
    assert s.start("j3", "SSIS-123", "magnet:c") is True, "前一个结束后可以再来"

def test_empty_code_not_deduped(tmp_path):
    s = store.Store(str(tmp_path / "jobs.db"))
    assert s.start("j1", "", "magnet:a") is True
    assert s.start("j2", "", "magnet:b") is True

def test_update_and_finish(tmp_path):
    s = store.Store(str(tmp_path / "jobs.db"))
    s.start("j1", "ABP-1", "magnet:a")
    s.update("j1", stage="downloading", percent=42.5, xunlei_task="T9", xunlei_ours=False,
             extra={"note": "x"})
    job = s.get("j1")
    assert job["percent"] == 42.5 and job["xunlei_task"] == "T9"
    assert job["xunlei_ours"] is False and job["extra"] == {"note": "x"}
    s.finish("j1", "failed", "死种")
    assert s.get("j1")["stage"] == "failed" and s.get("j1")["error"] == "死种"

def test_unfinished_survives_reopen(tmp_path):
    path = str(tmp_path / "jobs.db")
    a = store.Store(path)
    a.start("j1", "SSIS-1", "magnet:a")
    a.update("j1", stage="downloading", xunlei_task="T1")
    b = store.Store(path)          # 模拟进程重启
    rows = b.unfinished()
    assert [r["id"] for r in rows] == ["j1"] and rows[0]["xunlei_task"] == "T1"

def test_active_codes_and_prune(tmp_path):
    s = store.Store(str(tmp_path / "jobs.db"))
    s.start("j1", "SSIS-1", "magnet:a")
    assert s.active_codes() == {"SSIS-1"}
    s.finish("j1", "done")
    assert s.active_codes() == set()
    s.update("j1", )  # no-op
    with s._conn() as conn:
        conn.execute("UPDATE jobs SET updated_at=? WHERE id='j1'", (time.time() - 40 * 86400,))
    assert s.prune(30) == 1 and s.get("j1") == {}

def test_quota_job_is_deferred_not_dropped(tmp_path):
    s = store.Store(str(tmp_path / "jobs.db"))
    s.start("j1", "SSIS-1", "magnet:a")
    until = time.time() + 3600
    s.defer("j1", until, "额度用尽")
    job = s.get("j1")
    assert job["stage"] == "deferred" and job["extra"]["defer_until"] == until
    assert s.due() == [], "没到点不该重跑"
    assert [j["id"] for j in s.due(now=until + 1)] == ["j1"]

def test_deferred_job_still_blocks_same_code(tmp_path):
    """延期不是失败：它还要跑，同番号不能再进来一份。"""
    s = store.Store(str(tmp_path / "jobs.db"))
    assert s.start("j1", "SSIS-1", "magnet:a") is True
    s.defer("j1", time.time() + 3600, "额度用尽")
    assert s.get("j1")["stage"] == "deferred"
    assert s.start("j2", "SSIS-1", "magnet:b") is False, "延期中也要挡住重复提交"
    assert "SSIS-1" in s.active_codes()

def test_claim_due_is_atomic_and_clears_timer(tmp_path):
    s = store.Store(str(tmp_path / "jobs.db"))
    s.start("j1", "SSIS-1", "magnet:a")
    s.defer("j1", time.time() - 1, "额度用尽")
    first = s.get("j1")
    assert s.claim_due("j1") is True
    assert s.claim_due("j1") is False, "第二次领取必须失败"
    assert s.get("j1")["stage"] == "queued"
    assert "defer_until" not in s.get("j1")["extra"], "领走后不能再被复活"
    assert s.due() == []

def test_claim_due_before_time_returns_false(tmp_path):
    s = store.Store(str(tmp_path / "jobs.db"))
    s.start("j1", "SSIS-1", "magnet:a")
    s.defer("j1", time.time() + 3600)
    assert s.claim_due("j1") is False
    assert s.get("j1")["stage"] == "deferred"

def test_cancel_defer_stops_resurrection(tmp_path):
    """取消延期后，即使到了时间也不该自己爬起来。"""
    s = store.Store(str(tmp_path / "jobs.db"))
    s.start("j1", "SSIS-1", "magnet:a")
    s.defer("j1", time.time() - 1)
    assert s.cancel_defer("j1") is True
    assert s.get("j1")["stage"] == "cancelled", "用户主动取消，不该混进失败"
    assert s.due() == [] and s.claim_due("j1") is False

def test_deferred_does_not_count_as_running(tmp_path):
    s = store.Store(str(tmp_path / "jobs.db"))
    s.start("j1", "SSIS-1", "magnet:a")
    s.defer("j1", time.time() + 60)
    assert all(j["id"] != "j1" for j in s.unfinished()), "延期不占下载并发"

# ---------------------------------------------------------------- 旧库迁移（v1 → v2）

LEGACY_SCHEMA = """
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
-- v1 的旧索引：不含 deferred。IF NOT EXISTS 不会更新它，这正是必须迁移的原因
CREATE UNIQUE INDEX IF NOT EXISTS jobs_active_code
    ON jobs(code) WHERE code <> '' AND stage IN
        ('queued','downloading','organizing','uploading');
"""

def legacy_db(tmp_path, rows=()):
    import sqlite3
    path = str(tmp_path / "jobs.db")
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    for job in rows:
        conn.execute(
            "INSERT INTO jobs (id, code, source, stage, extra, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (job["id"], job.get("code", ""), job.get("source", ""), job.get("stage", "queued"),
             job.get("extra", "{}"), time.time(), time.time()))
    conn.commit()
    conn.close()
    return path

def test_legacy_db_migration_blocks_duplicate_during_deferral(tmp_path):
    """旧库升级后，延期期间同番号必须挡得住 —— 否则线上照旧重复下载。"""
    path = legacy_db(tmp_path, [{"id": "j1", "code": "SSIS-1"}])
    s = store.Store(path)
    s.defer("j1", time.time() + 3600)
    assert s.start("j2", "SSIS-1", "magnet:b") is False, "迁移后唯一索引要覆盖 deferred"

def test_legacy_db_migration_recovers_old_deferrals(tmp_path):
    """旧语义的 failed + defer_until 要改判为 deferred，否则永远不再被唤醒。"""
    until = time.time() - 10
    path = legacy_db(tmp_path, [
        {"id": "old", "code": "SSIS-2", "stage": "failed",
         "extra": json.dumps({"defer_until": until})}])
    s = store.Store(path)
    assert s.get("old")["stage"] == "deferred"
    assert [j["id"] for j in s.due()] == ["old"]

def test_migration_is_idempotent_and_versioned(tmp_path):
    path = legacy_db(tmp_path)
    s = store.Store(path)
    assert s.migrate() == store.SCHEMA_VERSION, "再跑一次不该重复迁移"
    import sqlite3
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
    conn.close()

def test_migration_keeps_one_active_and_marks_others(tmp_path):
    """同番号多条活跃记录（旧索引漏洞造成的）：保留最新，其余转失败留痕，不删数据。"""
    import sqlite3
    path = legacy_db(tmp_path)
    conn = sqlite3.connect(path)
    # 旧库可能就带着这种脏数据（索引曾被删/重建过），先删索引才插得进去
    conn.execute("DROP INDEX jobs_active_code")
    for jid in ("older", "newer"):
        conn.execute(
            "INSERT INTO jobs (id, code, stage, created_at, updated_at)"
            " VALUES (?,?,?,?,?)", (jid, "SSIS-3", "queued", time.time(), time.time()))
    conn.commit(); conn.close()
    s = store.Store(path)
    assert s.get("newer")["stage"] == "queued"
    assert s.get("older")["stage"] == "failed"
    assert "保留 newer" in s.get("older")["error"]

def test_concurrent_first_init_does_not_crash(tmp_path):
    """两个进程同时第一次打开同一个库：谁先建索引都不该让另一个起不来。"""
    path = str(tmp_path / "jobs.db")
    first = store.Store(path)
    second = store.Store(path)          # 模拟第二个进程（user_version 已是最新）
    assert second.migrate() == store.SCHEMA_VERSION
    assert first.start("a1", "SSIS-1", "magnet:a") is True
    assert second.start("b1", "SSIS-1", "magnet:b") is False, "索引仍然是唯一的"


def test_legacy_deferral_conflicting_with_running_job_migrates(tmp_path):
    """旧延期记录与同番号的排队/下载任务相撞时，迁移不能整体崩掉。

    以前只看「是否已有 deferred」，同番号卡在 queued 时照样改判 deferred，
    撞唯一索引抛 IntegrityError，整个 Store 构造失败 —— 程序直接起不来。
    """
    import sqlite3
    until = time.time() - 10
    path = legacy_db(tmp_path, [
        {"id": "old", "code": "SSIS-9", "stage": "failed",
         "extra": json.dumps({"defer_until": until})},
        {"id": "live", "code": "SSIS-9", "stage": "queued"}])
    s = store.Store(path)                       # 不抛异常即达标
    assert s.get("live")["stage"] == "queued"
    assert s.get("old")["stage"] == "failed", "冲突时旧记录保持失败留痕"
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
    conn.close()


def test_defer_requires_a_wake_up_time(tmp_path):
    """没有唤醒时间的 deferred 永远醒不过来，宁可当场报错。"""
    import pytest
    s = store.Store(str(tmp_path / "jobs.db"))
    s.start("d1", "SSIS-10", "magnet:a")
    for bad in (0, 0.0, -1):
        with pytest.raises(ValueError):
            s.defer("d1", bad)
    assert s.get("d1")["stage"] == "queued", "报错不该改动任务状态"
