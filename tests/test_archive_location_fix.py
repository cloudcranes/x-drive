"""Actual completion/maintenance paths preserve destination and pending cleanup."""
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import cloudorganize, core, storage, store, web


@pytest.fixture
def env(tmp_path, monkeypatch):
    st = store.Store(str(tmp_path / "jobs.db"))
    monkeypatch.setattr(web, "STORE", st)
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(web, "TMP_ROOT", str(tmp_path / "downloads"))
    monkeypatch.setattr(web, "STATE_DIR", str(tmp_path / "work"))
    monkeypatch.setattr(web, "JOBS", {})
    monkeypatch.setattr(web, "LEASES", {})
    monkeypatch.setattr(web, "_lease_live", {})
    monkeypatch.setattr(web, "CANCEL", {})
    monkeypatch.setattr(web, "_EXECUTION", ContextVar("location_test", default=None))
    monkeypatch.setattr(web.notify, "job_done", lambda *a: None)
    async def no_lookup(*a):
        return {}
    monkeypatch.setattr(web.meta, "lookup", no_lookup)
    st.start("j", "AB-1", "magnet:a")
    st.claim_owner("j", "A")
    web.lease_begin("j", "A")
    web._UPLOAD_PROGRESS.location = None
    yield st
    web.lease_end("j")
    web._UPLOAD_PROGRESS.location = None
    web._UPLOAD_PROGRESS.location_work = None


def record():
    return {"folder": "AB-1", "path": "clouddrive2:cloud-dest/AB-1",
            "backend": "clouddrive2", "root": "cloud-dest", "rel": "cloud-dest/AB-1",
            "files": ["AB-1.mp4"], "size": 7,
            "files_detail": [{"name": "AB-1.mp4", "size": 7}]}


def test_legacy_record_without_triple_uses_its_own_path(env, monkeypatch):
    """旧记录没有三元组，但 path 记着归档当时的真实位置 —— 照它定位。

    实测 MIMK-267：全局 STORAGE_ROOT 后来改成了别的根，退回全局根只会报
    「路径不存在」，而文件其实还在记录写明的位置。
    """
    monkeypatch.setattr(storage, "root", lambda: "changed-root")
    legacy = {"code": "AB-1", "path": "clouddrive2:迅雷云盘/X-Drive/AB-1", "files": []}
    assert web.archive_locator(legacy) == "迅雷云盘/X-Drive/AB-1"


def test_legacy_path_with_mismatched_leaf_falls_back(env, monkeypatch):
    """path 末段与番号对不上时不采信它 —— 宁可退回全局根也不指错目录。"""
    monkeypatch.setattr(storage, "root", lambda: "global-root")
    legacy = {"code": "AB-1", "path": "clouddrive2:迅雷云盘/X-Drive/OTHER", "files": []}
    assert web.archive_locator(legacy) == "global-root/AB-1"


def test_legacy_path_without_backend_prefix_is_ignored(env, monkeypatch):
    monkeypatch.setattr(storage, "root", lambda: "global-root")
    legacy = {"code": "AB-1", "path": "迅雷云盘/X-Drive/AB-1", "files": []}
    assert web.archive_locator(legacy) == "global-root/AB-1"


def test_legacy_path_with_traversal_is_rejected(env, monkeypatch):
    monkeypatch.setattr(storage, "root", lambda: "global-root")
    legacy = {"code": "AB-1", "path": "clouddrive2:../etc/AB-1", "files": []}
    assert web.archive_locator(legacy) == "global-root/AB-1"


def test_cloud_finish_preserves_actual_location(env, monkeypatch):
    monkeypatch.setattr(storage, "root", lambda: "other-root")
    web._finish_cloud("j", "magnet:a", {"records": [record()]})
    saved = core.load_state(web.STATE)["AB-1"]
    for key in ("backend", "root", "rel", "path"):
        assert saved[key] == record()[key]
    assert web.archive_locator(saved) == "cloud-dest/AB-1"


def test_local_finish_uses_actual_backend_root_not_rclone_constants(env, monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "kind", lambda: "clouddrive2")
    monkeypatch.setattr(storage, "root", lambda: "actual-root")
    monkeypatch.setattr(storage, "real_folder", lambda name: name)
    web._finish("j", tmp_path, "AB-1", [], 0, "", "magnet:a",
                {"download": 0, "organize": 0, "upload": 0}, SimpleNamespace(attempts=0))
    saved = core.load_state(web.STATE)["AB-1"]
    assert saved["backend"] == "clouddrive2"
    assert saved["root"] == "actual-root" and saved["rel"] == "actual-root/AB-1"
    assert saved["path"] == "clouddrive2:actual-root/AB-1"


def test_cleanup_unknowns_remain_done_and_pending_is_persistent(env, monkeypatch):
    monkeypatch.setattr(web, "cloud_ready", lambda: (True, ""))
    backend = SimpleNamespace(kind="clouddrive2")
    plan = SimpleNamespace(source="incoming", target="AB-1")
    out = {"records": [record()], "_backend": backend,
           "cleanup": [(plan, "inbox", "cloud-dest")]}
    monkeypatch.setattr(web, "_cloud_run", lambda *args: out)
    # Any attempt to fetch a newly configured client after run would be a bug.
    monkeypatch.setattr(storage, "get", lambda: pytest.fail("cleanup replaced actual backend"))
    def unknown(got, *args):
        assert got is backend
        raise cloudorganize.OrganizeError("inbox/incoming contains unknown late.mp4")
    monkeypatch.setattr(cloudorganize, "cleanup_source", unknown)
    web.run_web_job("j", "magnet:a", "AB-1")
    row = env.get("j")
    assert row["stage"] == "done"
    pending = row["extra"]["cleanup_pending"]
    assert "late.mp4" in pending[0]["error"]
    assert core.load_state(web.STATE)["AB-1"]["cleanup_pending"] == pending
    assert web.jobs_snapshot()[0]["cleanup_pending"] == pending
    assert web.collect()["tasks"][0]["cleanup_pending"] == pending


def test_rclone_ls_and_delete_use_record_rel(env, monkeypatch):
    calls = []
    backend = SimpleNamespace(kind="rclone")
    monkeypatch.setattr(web, "storage_backend", lambda: backend)
    monkeypatch.setattr(storage, "kind", lambda: "rclone")
    monkeypatch.setattr(web, "rclone", lambda *args, **kw: (calls.append(args) or (0, "[]", "")))
    saved = {"code": "AB-1", "backend": "rclone", "root": "old-root",
             "remote": web.RCLONE_REMOTE, "rel": "old-root/AB-1", "files": []}
    core.save_state(web.STATE, {"AB-1": saved})
    assert web.verify_archive("AB-1")["ok"]
    assert web.purge_archives(["AB-1"])
    assert calls[0][1] == f"{web.RCLONE_REMOTE}:old-root/AB-1"
    assert calls[1][1] == f"{web.RCLONE_REMOTE}:old-root/AB-1"


def test_changed_backend_blocks_read_delete_rename_and_attachment_sync(env, monkeypatch):
    calls = []
    backend = SimpleNamespace(kind="rclone")
    monkeypatch.setattr(web, "storage_backend", lambda: backend)
    monkeypatch.setattr(web, "rclone", lambda *args, **kw: calls.append(args))
    saved = {"code": "AB-1", **record()}
    core.save_state(web.STATE, {"AB-1": saved})
    assert not web.verify_archive("AB-1")["ok"]
    assert not web.purge_archives(["AB-1"])
    assert not web.rename_archive("AB-1", "AB-2")["ok"]
    with pytest.raises(storage.StorageError):
        web.sync_attachments("AB-1", saved)
    assert calls == []
    assert core.load_state(web.STATE)["AB-1"] == saved


@pytest.mark.parametrize("cloud", [True, False])
def test_terminal_cas_failure_does_not_notify_or_report_success(env, monkeypatch, tmp_path, cloud):
    notices = []
    monkeypatch.setattr(web.notify, "job_done", lambda *a: notices.append(a))
    monkeypatch.setattr(env, "update_cas", lambda *a, **kw: False)
    with pytest.raises(web.LossOfLease):
        if cloud:
            web._finish_cloud("j", "magnet:a", {"records": [record()]})
        else:
            web._finish("j", tmp_path, "AB-1", [], 0, "", "magnet:a",
                        {"download": 0, "organize": 0, "upload": 0}, SimpleNamespace(attempts=0))
    assert notices == []


def test_actual_cloud_run_carries_client_and_destination_into_archive(env, monkeypatch):
    backend = SimpleNamespace(kind="clouddrive2")
    monkeypatch.setattr(storage, "get", lambda: backend)
    monkeypatch.setattr(storage, "root", lambda: "global-root")
    monkeypatch.setattr(web, "cloud_ready", lambda: (True, ""))
    settings = {"CLOUD_INBOX": "inbox", "CLOUD_DEST": "chosen-dest"}
    monkeypatch.setattr(web, "get_setting", lambda key: settings.get(key))
    monkeypatch.setattr(web, "_cloud_submit", lambda *a: {"before": {}})
    async def landed(*a, **kw):
        return [SimpleNamespace(folder="incoming", files=[{"name": "v.mp4", "size": 7}], total=7)]
    monkeypatch.setattr(web.cloudingest, "wait_landing", landed)
    monkeypatch.setattr(cloudorganize, "plan", lambda *a, **kw: SimpleNamespace(move=["v.mp4"], target="AB-1"))
    monkeypatch.setattr(cloudorganize, "apply", lambda *a, **kw: {"target": "AB-1", "files": ["AB-1.mp4"], "size": 7})
    out = web._cloud_run("j", "magnet:a", "AB-1", "AB1")
    assert out["_backend"] is backend
    web._finish_cloud("j", "magnet:a", out)
    saved = core.load_state(web.STATE)["AB-1"]
    assert saved["backend"] == "clouddrive2"
    assert saved["rel"] == "chosen-dest/AB-1" and saved["root"] == "chosen-dest"


def test_upload_takes_one_consistent_root_snapshot(env, monkeypatch, tmp_path):
    roots = []
    def changing_root():
        roots.append(True)
        return "original-root" if len(roots) == 1 else "changed-root"
    sent = []
    backend = SimpleNamespace(kind="clouddrive2", ensure_dir=lambda rel: None,
                              upload_dir=lambda work, rel, **kw: sent.append(rel) or 0,
                              link=lambda rel: "")
    monkeypatch.setattr(web, "storage_backend", lambda: backend)
    monkeypatch.setattr(storage, "root", changing_root)
    web.upload_folder(tmp_path, "AB-1")
    assert len(roots) == 1
    assert sent == ["original-root/AB-1"]
    assert web._UPLOAD_PROGRESS.location["rel"] == sent[0]
    assert web._UPLOAD_PROGRESS.location["root"] == "original-root"


def test_upload_location_survives_settings_change_before_finish(env, monkeypatch, tmp_path):
    sent = []
    backend = SimpleNamespace(kind="clouddrive2", ensure_dir=lambda rel: None,
                             upload_dir=lambda work, rel, **kw: sent.append(rel) or 0,
                             link=lambda rel: "", real_folder=lambda rel: "AB-1")
    monkeypatch.setattr(web, "storage_backend", lambda: backend)
    monkeypatch.setattr(storage, "root", lambda: "original-root")
    web.upload_folder(tmp_path, "AB-1")
    monkeypatch.setattr(storage, "root", lambda: "changed-root")
    monkeypatch.setattr(storage, "kind", lambda: "rclone")
    web._finish("j", tmp_path, "AB-1", [], 0, "", "magnet:a",
                {"download": 0, "organize": 0, "upload": 0}, SimpleNamespace(attempts=0))
    saved = core.load_state(web.STATE)["AB-1"]
    assert sent == ["original-root/AB-1"]
    assert saved["backend"] == "clouddrive2" and saved["root"] == "original-root"
    assert saved["path"] == "clouddrive2:original-root/AB-1"
