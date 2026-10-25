"""Behavior regressions: worker A must never borrow replacement B's token."""
import asyncio
from contextvars import ContextVar, copy_context
from types import SimpleNamespace
import time

import pytest

from app import core, main as bot, store, web


@pytest.fixture
def env(tmp_path, monkeypatch):
    st = store.Store(str(tmp_path / "jobs.db"))
    monkeypatch.setattr(web, "STORE", st)
    monkeypatch.setattr(web, "JOBS", {})
    monkeypatch.setattr(web, "CANCEL", {})
    monkeypatch.setattr(web, "LEASES", {})
    monkeypatch.setattr(web, "_lease_live", {})
    monkeypatch.setattr(web, "_EXECUTION", ContextVar("web_test", default=None))
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(web, "TMP_ROOT", str(tmp_path / "dl"))
    monkeypatch.setattr(web, "STATE_DIR", str(tmp_path / "work"))
    st.start("j", "AB-1", "magnet:a")
    st.claim_owner("j", "A")
    web.lease_begin("j", "A")
    a = copy_context()
    yield st, a
    for context in (copy_context(), a):
        context.run(web.lease_end, "j")


def replace(st):
    assert st.claim_owner("j", "B", now=time.time() + store.STALE_BEAT + 1)
    web.lease_begin("j", "B")
    st.update_cas("j", "B", stage="uploading", percent=42, error="B active")
    st.request_cancel("j")
    return copy_context()


def test_old_progress_failure_finish_and_release_leave_b_intact(env, monkeypatch, tmp_path):
    st, a = env
    b = replace(st)
    before = st.get("j")
    assert not a.run(web.job_update, "j", stage="failed", error="A late")
    assert not a.run(web.job_update, "j", stage="done", percent=100)
    with pytest.raises(web.LossOfLease):
        a.run(web._finish_cloud, "j", "magnet:a", {"records": [
            {"folder": "AB-1", "path": "clouddrive2:dest/AB-1", "files": [], "size": 0}]})
    a.run(web.lease_end, "j")
    after = st.get("j")
    for key in ("stage", "percent", "error", "exec_owner", "extra"):
        assert after[key] == before[key]
    assert web.LEASES["j"] == "B"
    assert not core.load_state(web.STATE)


def test_late_cloud_exception_does_not_fail_or_clear_b(env, monkeypatch):
    st, a = env
    monkeypatch.setattr(web, "cloud_ready", lambda: (True, ""))
    b = []

    def late_failure(*args):
        # Replacement runs in a different context, as a real second worker does.
        b.append(copy_context().run(replace, st))
        raise OSError("A upload returned late failure")

    monkeypatch.setattr(web, "_cloud_run", late_failure)
    a.run(web.run_web_job, "j", "magnet:a", "AB-1")
    row = st.get("j")
    assert row["stage"] == "uploading" and row["error"] == "B active"
    assert row["exec_owner"] == "B" and st.cancel_marked("j")
    assert web.LEASES["j"] == "B"
    b[0].run(web.lease_end, "j")


def test_re_admission_mints_new_token_without_reusing_registry(env, monkeypatch):
    st, a = env
    old = web.LEASES["j"]
    st.update("j", exec_beat=0)
    monkeypatch.setattr(web, "WAITING", {"j": ("magnet:a", "AB-1")})
    monkeypatch.setattr(web, "_queued_at", {})
    monkeypatch.setattr(web, "scheduling_open", lambda: True)

    monkeypatch.setattr(web, "get_setting", lambda key: 0)
    class DormantThread:
        def __init__(self, *args, **kw):
            pass
        def start(self):
            pass
    monkeypatch.setattr(web.threading, "Thread", DormantThread)
    assert web.admit("j")
    assert web.LEASES["j"] != old
    assert st.get("j")["exec_owner"] == web.LEASES["j"]
    assert not a.run(web.job_update, "j", stage="failed")
    before = st.get("j")["stage"]
    web.WAITING["j"] = ("magnet:a", "AB-1")
    assert not web.admit("j")
    assert st.get("j")["stage"] == before


def test_contextless_callbacks_never_borrow_latest_lease(env):
    st, _ = env
    replace(st)
    empty = ContextVar("empty", default=None)
    reset = web._EXECUTION.set(None)
    try:
        assert not web.job_update("j", stage="done")
    finally:
        web._EXECUTION.reset(reset)
    assert st.get("j")["stage"] == "uploading"


def test_bot_old_finish_cleanup_and_to_thread_remain_fenced(tmp_path, monkeypatch):
    st = store.Store(str(tmp_path / "bot.db"))
    monkeypatch.setattr(bot, "_STORE", st)
    monkeypatch.setattr(bot, "LEASES", {})
    monkeypatch.setattr(bot, "_lease_live", {})
    monkeypatch.setattr(bot, "_EXECUTION", ContextVar("bot_test", default=None))
    cfg = SimpleNamespace(jobs_db=st.path)
    st.start("j", "AB-1", "magnet:a")
    assert bot.lease_begin(cfg, "j")
    a = copy_context()
    st.update("j", exec_beat=0)
    b = copy_context()
    assert b.run(bot.lease_begin, cfg, "j")
    owner_b = bot.LEASES["j"]
    st.request_cancel("j")
    assert not a.run(bot.finish_task, cfg, "j", "failed", "late A")
    assert not a.run(bot.task_update, cfg, "j", stage="done")

    async def late_thread():
        return await asyncio.to_thread(bot.finish_task, cfg, "j", "done")

    assert a.run(asyncio.run, late_thread()) is False
    a.run(bot.lease_end, cfg, "j")
    assert st.get("j")["exec_owner"] == owner_b and st.cancel_marked("j")
    assert bot.LEASES["j"] == owner_b
    b.run(bot.lease_end, cfg, "j")


@pytest.mark.parametrize("current_kind", ["openlist", "rclone"])
def test_bot_delete_blocks_cross_backend_archive(tmp_path, monkeypatch, current_kind):
    calls, replies = [], []
    cfg = SimpleNamespace(allowed={7}, remote="gdrive", drive_root="new-root",
                          state_path=str(tmp_path / "state.json"))
    saved = {"code": "AB-1", "backend": "clouddrive2", "rel": "old-root/AB-1",
             "path": "clouddrive2:old-root/AB-1"}
    state = {"AB-1": saved}
    backend = SimpleNamespace(kind=current_kind, purge=lambda rel: calls.append(rel))
    monkeypatch.setattr(bot.storage, "get", lambda: backend)
    async def no_rclone(*args, **kw):
        calls.append(args)
        return 0, "", ""
    monkeypatch.setattr(bot, "rclone", no_rclone)
    async def reply(text):
        replies.append(text)
    update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=reply),
                             effective_user=SimpleNamespace(id=7))
    ctx = SimpleNamespace(args=["AB-1", "--yes"], application=SimpleNamespace(
        bot_data={"cfg": cfg, "state": state}))
    asyncio.run(bot.on_delete(update, ctx))
    assert calls == []
    assert state == {"AB-1": saved}
    assert "后端" in replies[-1]
