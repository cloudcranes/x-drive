"""备份与恢复：一致快照、不带凭据、恢复前先留底、恢复后要先对账。"""
import os
import sqlite3

import pytest

from app import store, web


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = str(tmp_path / "jobs.db")
    monkeypatch.setattr(web, "STORE", store.Store(db))
    monkeypatch.setenv("JOBS_DB", db)
    monkeypatch.setattr(web, "JOBS", {})
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    # 内存状态是模块级的：别的测试留下的排队条目会让「有活跃任务」误判
    monkeypatch.setattr(web, "WAITING", {})
    monkeypatch.setattr(web, "_queued_at", {})
    web._threads.clear()
    web.set_paused(False)
    return db


def test_backup_contains_written_rows(env, tmp_path):
    web.STORE.start("j1", "SSIS-1", "magnet:a")
    web.STORE.finish("j1", "done")
    web.STORE.state_put({"subs": {"k": {"id": "w5a"}}})
    out = web.backup_now(str(tmp_path / "b.db"))
    assert out["ok"] and out["size"] > 0
    other = store.Store(out["path"])
    assert other.get("j1")["stage"] == "done"
    assert other.state_get("subs")["k"]["id"] == "w5a"


def test_backup_has_no_credentials(env, tmp_path, monkeypatch):
    """备份只含业务数据：token / 密码不在库里，也不该被写进去。"""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "SECRET-TG")
    monkeypatch.setenv("METATUBE_TOKEN", "SECRET-MT")
    web.STORE.start("j1", "SSIS-1", "magnet:a")
    path = web.backup_now(str(tmp_path / "b.db"))["path"]
    blob = open(path, "rb").read()
    assert b"SECRET-TG" not in blob and b"SECRET-MT" not in blob


def test_restore_requires_confirm_and_keeps_safety_copy(env, tmp_path):
    web.STORE.start("old", "A-1", "magnet:a")
    bak = web.backup_now(str(tmp_path / "b.db"))["path"]
    web.STORE.start("new", "B-2", "magnet:b")

    assert web.restore_apply(bak, confirm=False)["ok"] is False
    # 新语义：库里还有未完成任务时一律拒绝恢复（以前会直接覆盖，旧 worker 还在往新库写）
    blocked = web.restore_apply(bak, confirm=True)
    assert blocked["ok"] is False and blocked.get("refused") is True
    assert "排空" in blocked["hint"]
    assert web.STORE.get("new")["code"] == "B-2", "拒绝时不得改动当前库"
    web.STORE.finish("new", "done")           # 排空后才允许恢复
    web.STORE.finish("old", "done")
    out = web.restore_apply(bak, confirm=True)
    assert out["ok"] is True
    assert os.path.isfile(out["safety"]), "恢复前必须留一份当前快照"
    # 恢复后是备份时的状态
    assert web.STORE.get("old")["code"] == "A-1"
    assert web.STORE.get("new") == {}
    # 恢复后进入待对账：不自动开工，要人确认
    assert web.reconcile_pending() is True and web.paused() is True
    # 备份里 old 还是 queued（延期类「未完成」同样算）：不 force 就不放行
    assert web.restore_confirm(confirm=True)["ok"] is False
    assert web.restore_confirm(confirm=True, force=True)["ok"] is True
    assert web.reconcile_pending() is False and web.paused() is False
    # 安全副本里还能找回恢复前的数据
    assert store.Store(out["safety"]).get("new")["code"] == "B-2"


def test_restore_check_reports_active_jobs(env, tmp_path):
    web.STORE.start("run1", "C-3", "magnet:c")
    web.STORE.update("run1", stage="downloading")
    bak = web.backup_now(str(tmp_path / "b.db"))["path"]
    out = web.restore_check(bak)
    assert out["ok"] is True
    assert [j["code"] for j in out["active_in_backup"]] == ["C-3"]
    assert "对账" in out["hint"]


def test_restore_check_rejects_missing_file(env):
    out = web.restore_check("/nonexistent/b.db")
    assert out["ok"] is False


def test_restore_check_rejects_broken_file(env, tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database")
    out = web.restore_check(str(bad))
    assert out["ok"] is False and "打不开" in out["error"]
