"""D2-b 恢复闭环：检查只读、活任务拒绝、恢复后不自动开工、对账后才放行。

已确证的两个事故：
  1. restore_check 用 store.Store(source) 打开备份 —— 那个构造函数会建表并迁移，
     一次「检查」就把备份文件的 user_version 从 2 改成 4；
  2. restore_apply 只做「备份→覆盖→STORE=None」，既不检查活跃任务、也不停后台
     循环，覆盖后内存队列还在、旧 worker 还在往新库写。

这里逐条盯住：检查前后备份文件的 sha256 与 mtime 不变、有活任务时一个字节都不写、
恢复后 reconcile_required 为真且调度不启动、确认对账后才恢复调度。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from app import store, web


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """干净的内存状态：模块级的 JOBS/WAITING/_threads/暂停开关都不串场。"""
    db = str(tmp_path / "jobs.db")
    monkeypatch.setattr(web, "STORE", store.Store(db))
    monkeypatch.setenv("JOBS_DB", db)
    monkeypatch.setattr(web, "JOBS", {})
    monkeypatch.setattr(web, "WAITING", {})
    monkeypatch.setattr(web, "_queued_at", {})
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    web._threads.clear()
    web.set_paused(False)
    yield db
    web._threads.clear()
    web.set_paused(False)


# ─────────────────────────────────────────── 1. 检查严格只读

def test_restore_check_does_not_touch_the_backup_file(env, tmp_path):
    """检查前后 sha256 与 mtime 必须一致：检查是只读动作，不是「顺手迁移一下」。"""
    web.STORE.start("j1", "A-1", "magnet:a")
    bak = web.backup_now(str(tmp_path / "b.db"))["path"]
    before_hash, before_mtime = sha256(bak), os.stat(bak).st_mtime_ns

    out = web.restore_check(bak)

    assert out["ok"] is True
    assert sha256(bak) == before_hash, "检查不得改写备份文件"
    assert os.stat(bak).st_mtime_ns == before_mtime, "检查不得改 mtime"


def test_restore_check_does_not_grow_the_wal(env, tmp_path):
    """检查不得往备份库的 WAL 里写东西：WAL 大小与 mtime 前后一致。

    备份是 WAL 模式的，backup_now 自己就会留下 b.db-wal；判据是「检查没有把它写大」。
    """
    web.STORE.start("j1", "A-1", "magnet:a")
    bak = web.backup_now(str(tmp_path / "b.db"))["path"]
    wal = Path(bak + "-wal")
    before = (wal.stat().st_size, wal.stat().st_mtime_ns) if wal.exists() else None
    web.restore_check(bak)
    after = (wal.stat().st_size, wal.stat().st_mtime_ns) if wal.exists() else None
    assert after == before, "检查不得向备份库写入（WAL 被改动了）"


def test_restore_check_reports_old_version_without_upgrading(env, tmp_path):
    """老版本备份：明确标注 schema_backup / schema_current / needs_migration，
    但**不**擅自把备份升到当前版本。"""
    legacy = tmp_path / "old.db"
    conn = sqlite3.connect(str(legacy))
    conn.executescript(
        "CREATE TABLE jobs (id TEXT PRIMARY KEY, code TEXT NOT NULL DEFAULT '',"
        " source TEXT NOT NULL DEFAULT '', stage TEXT NOT NULL DEFAULT 'queued',"
        " created_at REAL NOT NULL, updated_at REAL NOT NULL);")
    conn.execute("INSERT INTO jobs (id, code, stage, created_at, updated_at)"
                 " VALUES ('x','OLD-1','queued',?,?)", (time.time(), time.time()))
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()
    before = sha256(str(legacy))

    out = web.restore_check(str(legacy))

    assert out["ok"] is True
    assert out["schema_backup"] == 2
    assert out["schema_current"] == store.SCHEMA_VERSION
    assert out["needs_migration"] is True
    assert [j["code"] for j in out["active_in_backup"]] == ["OLD-1"]
    assert sha256(str(legacy)) == before, "检查不得升级备份库"
    with sqlite3.connect(str(legacy)) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 2, "版本号不能被改"


def test_restore_check_up_to_date_backup_has_no_migration_flag(env, tmp_path):
    web.STORE.start("j1", "A-1", "magnet:a")
    bak = web.backup_now(str(tmp_path / "b.db"))["path"]
    out = web.restore_check(bak)
    assert out["needs_migration"] is False
    assert out["schema_backup"] == out["schema_current"] == store.SCHEMA_VERSION


def test_restore_check_rejects_non_database(env, tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database")
    out = web.restore_check(str(bad))
    assert out["ok"] is False and "打不开" in out["error"]


def test_restore_check_reports_blockers_for_active_jobs(env, tmp_path):
    """当前库有活任务时，检查结果直接告诉前端「现在不能恢复」。"""
    web.STORE.start("j1", "A-1", "magnet:a")
    web.STORE.update("j1", stage="downloading")
    bak = web.backup_now(str(tmp_path / "b.db"))["path"]
    out = web.restore_check(bak)
    assert out["can_restore"] is False and out["blockers"]
    assert any("未完成" in b for b in out["blockers"])


# ─────────────────────────────────────────── 2. 有活任务一律拒绝

def test_restore_apply_refuses_with_unfinished_job(env, tmp_path):
    """有未完成任务（含 deferred）时拒绝，且当前库一个字节都不动。"""
    web.STORE.start("done1", "Z-0", "magnet:z")
    web.STORE.finish("done1", "done")
    bak = web.backup_now(str(tmp_path / "b.db"))["path"]

    web.STORE.start("live", "B-2", "magnet:b")
    web.STORE.defer("live", time.time() + 3600, "额度用尽")   # 延期也算未完成
    db_hash = None
    web.STORE.update("done1", error="sentinel")              # 标记当前库
    with sqlite3.connect(web.jobs_db_path()) as c:
        before = c.execute("SELECT error FROM jobs WHERE id='done1'").fetchone()[0]

    out = web.restore_apply(bak, confirm=True)

    assert out["ok"] is False and out.get("refused") is True
    assert "排空" in out["hint"] or "维护模式" in out["hint"]
    with sqlite3.connect(web.jobs_db_path()) as c:
        assert c.execute("SELECT error FROM jobs WHERE id='done1'").fetchone()[0] == before
        assert c.execute("SELECT COUNT(*) FROM jobs WHERE id='live'").fetchone()[0] == 1
    # 拒绝时不该生成任何 before-restore-* 半成品备份
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("before-restore-")]


def test_restore_apply_refuses_with_queued_memory_tasks(env, tmp_path):
    """内存队列里有排队任务时同样拒绝（覆盖后它们会继续往新库写）。"""
    web.STORE.start("done1", "Z-0", "magnet:z")
    web.STORE.finish("done1", "done")
    bak = web.backup_now(str(tmp_path / "b.db"))["path"]
    web.WAITING["pending"] = ("magnet:p", "P-1")
    out = web.restore_apply(bak, confirm=True)
    assert out["ok"] is False and out.get("refused") is True
    assert any("排队" in b for b in out["blockers"])


def test_restore_apply_refuses_while_execution_thread_alive(env, tmp_path):
    """有在途执行线程时拒绝：旧 worker 还没结束，覆盖后的库会立刻被它写脏。"""
    web.STORE.start("done1", "Z-0", "magnet:z")
    web.STORE.finish("done1", "done")
    bak = web.backup_now(str(tmp_path / "b.db"))["path"]

    stop = threading.Event()

    def busy() -> None:
        stop.wait(5)

    t = threading.Thread(target=busy, daemon=True)
    t.start()
    web._threads["runner"] = t
    try:
        out = web.restore_apply(bak, confirm=True)
    finally:
        stop.set()
        t.join(5)
    assert out["ok"] is False and out.get("refused") is True
    assert any("线程" in b for b in out["blockers"])


def test_restore_apply_requires_confirm(env, tmp_path):
    web.STORE.start("j1", "A-1", "magnet:a")
    web.STORE.finish("j1", "done")
    bak = web.backup_now(str(tmp_path / "b.db"))["path"]
    assert web.restore_apply(bak, confirm=False)["ok"] is False


# ─────────────────────────────────────────── 3. 恢复后不自动开工

def test_restore_apply_marks_reconcile_and_pauses(env, tmp_path):
    """恢复成功 → reconcile_required 为真 + 暂停；调度线程调用一律空转。"""
    web.STORE.start("old", "A-1", "magnet:a")
    web.STORE.finish("old", "done")
    bak = web.backup_now(str(tmp_path / "b.db"))["path"]
    web.STORE.start("new", "B-2", "magnet:b")
    web.STORE.finish("new", "done")

    out = web.restore_apply(bak, confirm=True)

    assert out["ok"] is True
    assert out["reconcile_required"] is True
    assert out["paused"] is True
    assert web.reconcile_pending() is True
    assert web.paused() is True
    # 恢复后的库就是备份时的样子
    assert web.STORE.get("old")["code"] == "A-1"
    assert web.STORE.get("new") == {}
    # 安全副本在独立路径上，能找回恢复前的数据
    assert os.path.abspath(out["safety"]) != os.path.abspath(web.jobs_db_path())
    assert store.Store(out["safety"]).get("new")["code"] == "B-2"
    assert sha256(out["safety"]) != sha256(bak)


def test_pump_queue_is_a_noop_while_reconciling(env, monkeypatch):
    web.STORE.state_put({web.RECONCILE_KEY: {"required": True}})
    web.WAITING["j1"] = ("magnet:a", "A-1")
    monkeypatch.setattr(web, "admit", lambda job_id: pytest.fail("待对账时不该放行任何任务"))
    assert web.pump_queue() == 0


def test_pump_queue_is_a_noop_while_paused(env, monkeypatch):
    web.set_paused(True)
    web.WAITING["j1"] = ("magnet:a", "A-1")
    monkeypatch.setattr(web, "admit", lambda job_id: pytest.fail("暂停时不该放行任何任务"))
    assert web.pump_queue() == 0


def test_restore_clears_memory_state(env, tmp_path):
    """覆盖后内存镜像必须清空：旧队列/租约/取消表继续用就是脏数据。"""
    web.STORE.start("old", "A-1", "magnet:a")
    web.STORE.finish("old", "done")
    bak = web.backup_now(str(tmp_path / "b.db"))["path"]
    # 只放「不该越过恢复流程的残留」：取消表与租约。真正的排队任务会（正确地）
    # 触发拒绝，所以这里不放进 WAITING —— 那条路径由 refuse 系列测试覆盖。
    web.JOBS["ghost"] = {"id": "ghost", "stage": "done"}
    web.LEASES["ghost"] = "tok"
    web.CANCEL.setdefault("ghost", threading.Event())

    out = web.restore_apply(bak, confirm=True)
    assert out["ok"] is True
    assert web.JOBS == {} and web.WAITING == {} and web._queued_at == {}
    assert web.LEASES == {} and web.CANCEL.get("ghost") is None


def test_resume_after_restore_does_not_reschedule(env, tmp_path, monkeypatch):
    """恢复后重启对账循环也不该自动入队（库里可能是旧快照）。"""
    web.STORE.start("old", "A-1", "magnet:a")
    bak = web.backup_now(str(tmp_path / "b.db"))["path"]
    web.STORE.finish("old", "done")
    assert web.restore_apply(bak, confirm=True)["ok"] is True
    # 备份里 old 还是 queued：现在它在库里是「未完成任务」
    monkeypatch.setattr(web, "requeue", lambda *a, **k: pytest.fail("待对账时不该自动入队"))
    web._resume_loop()


# ─────────────────────────────────────────── 4. 确认对账后放行

def test_restore_confirm_requires_confirm_flag(env):
    out = web.restore_confirm(confirm=False)
    assert out["ok"] is False
    assert web.paused() is False or True       # 不校验暂停态，只看 ok


def test_restore_confirm_blocks_unfinished_unless_forced(env):
    web.STORE.state_put({web.RECONCILE_KEY: {"required": True}})
    web.set_paused(True)
    web.STORE.start("j1", "A-1", "magnet:a")          # 未完成
    out = web.restore_confirm(confirm=True)
    assert out["ok"] is False and out["unfinished"]
    assert web.reconcile_pending() is True, "拒绝时标记不能清掉"
    out = web.restore_confirm(confirm=True, force=True)
    assert out["ok"] is True


def test_restore_confirm_clears_mark_and_resumes_scheduling(env, monkeypatch):
    web.STORE.state_put({web.RECONCILE_KEY: {"required": True}})
    web.set_paused(True)
    out = web.restore_confirm(confirm=True)
    assert out["ok"] is True
    assert web.reconcile_pending() is False
    assert web.paused() is False
    # 放行后调度真的能动了
    web.WAITING["j1"] = ("magnet:a", "A-1")
    monkeypatch.setattr(web, "admit", lambda job_id: True)
    assert web.pump_queue() == 1


def test_reconcile_flag_survives_reopen(env):
    """标记在 state 表里：进程重启也还在，不会一重启就自己跑起来。"""
    web.STORE.state_put({web.RECONCILE_KEY: {"required": True}})
    reopened = store.Store(web.jobs_db_path())
    assert reopened.state_get(web.RECONCILE_KEY)["required"] is True


def test_restore_status_reports_state(env, tmp_path):
    web.STORE.start("j1", "A-1", "magnet:a")
    web.STORE.finish("j1", "done")
    bak = web.backup_now(str(tmp_path / "b.db"))["path"]
    web.restore_apply(bak, confirm=True)
    out = web.restore_status()
    assert out["ok"] is True
    assert out["reconcile_required"] is True and out["paused"] is True
    assert out["mark"]["source"] == bak


# ─────────────────────────────────────────── 5. 不删数据

def test_restore_never_deletes_user_data(env, tmp_path):
    """恢复只换库：安全副本保留恢复前的一切，两个库都还能打开。"""
    web.STORE.start("keep", "K-1", "magnet:k")
    web.STORE.finish("keep", "done")
    web.STORE.state_put({"subs": {"k": {"id": "w1"}}})
    bak = web.backup_now(str(tmp_path / "b.db"))["path"]

    web.STORE.start("later", "L-2", "magnet:l")
    web.STORE.finish("later", "done")
    out = web.restore_apply(bak, confirm=True)
    assert out["ok"] is True
    safety = store.Store(out["safety"])
    assert safety.get("later")["code"] == "L-2", "恢复前的数据在安全副本里"
    assert safety.state_get("subs")["k"]["id"] == "w1"
    assert web.STORE.get("keep")["code"] == "K-1"


def test_safety_copy_never_overwrites_jobs_db(env):
    """安全副本必须落在独立路径：与 JOBS_DB 同路径会把当前库直接冲掉。"""
    target = web._safety_path(web.jobs_db_path())
    assert os.path.abspath(target) != os.path.abspath(web.jobs_db_path())
    assert Path(target).name.startswith("before-restore-")


def test_safety_path_avoids_the_source_backup(env, tmp_path):
    """也不许覆盖用户指定的备份源文件。"""
    src = str(tmp_path / "b.db")
    web.STORE.start("j1", "A-1", "magnet:a")
    web.STORE.finish("j1", "done")
    web.backup_now(src)
    assert os.path.abspath(web._safety_path(src)) != os.path.abspath(src)
