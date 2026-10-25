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


def test_cloud_finish_uses_actual_destination_not_global_root(env, monkeypatch):
    """云添加的归档位置用**实际目的地**，不是全局 STORAGE_ROOT。

    云添加落在 CLOUD_DEST（迅雷云盘/X-Drive），而全局根可能是另一个 ——
    记账必须按实际去的地方写，否则之后的核验/补刮/改名会找错目录。
    """
    monkeypatch.setattr(storage, "root", lambda: "other-root")
    rec = record()
    rec["root"] = "cloud-dest"
    rec["rel"] = "cloud-dest/AB-1"
    rec["path"] = "clouddrive2:cloud-dest/AB-1"
    web._finish_cloud("j", "magnet:a", {"records": [rec]})
    saved = core.load_state(web.STATE)["AB-1"]
    assert saved["backend"] == "clouddrive2"
    assert saved["root"] == "cloud-dest" and saved["rel"] == "cloud-dest/AB-1"
    assert saved["path"] == "clouddrive2:cloud-dest/AB-1"


def test_cleanup_unknowns_remain_done_and_pending_is_persistent(env, monkeypatch):
    monkeypatch.setattr(web, "cloud_ready", lambda: (True, ""))
    backend = SimpleNamespace(kind="clouddrive2")
    plan = SimpleNamespace(source="incoming", target="AB-1")
    out = {"records": [record()], "_backend": backend,
           "cleanup": [{"plan": plan, "src_root": "inbox", "dst_root": "cloud-dest"}]}
    monkeypatch.setattr(web, "_cloud_run", lambda *args: out)
    # Any attempt to fetch a newly configured client after run would be a bug.
    monkeypatch.setattr(storage, "get", lambda: pytest.fail("cleanup replaced actual backend"))
    def unknown(got, *args):
        assert got is backend
        raise cloudorganize.OrganizeError("inbox/incoming contains unknown late.mp4")
    monkeypatch.setattr(cloudorganize, "cleanup_source", unknown)  # 共享模块内部调用
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


def test_terminal_cas_failure_does_not_notify_or_report_success(env, monkeypatch):
    """终态写不进去（失租）时不发通知、不报成功。"""
    notices = []
    monkeypatch.setattr(web.notify, "job_done", lambda *a: notices.append(a))
    monkeypatch.setattr(env, "update_cas", lambda *a, **kw: False)
    with pytest.raises(web.LossOfLease):
        web._finish_cloud("j", "magnet:a", {"records": [record()]})
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


def test_archive_record_lookup_finds_unrecognised_names(env, monkeypatch):
    """归档表的键是**归档当时的目录名**，不一定等于规范番号。

    实测（线上 MIMK-186）：磁力里的名字是 `mimk-186ch`，番号粘连后缀认不出来，
    目录名就用了磁力原名，键也跟着是原名。这时用大写去查就查不到 ——
    记录明明在，界面却说「没有归档记录」。

    所以查找要依次试：原样 → 大写 → 小写 → 归一后比对。
    """
    state = {"mimk-186ch": {"code": "mimk-186ch", "files": ["mimk-186ch.mp4"],
                             "rel": "dest/mimk-186ch", "backend": "clouddrive2"}}
    assert web._find_archive_record(state, "mimk-186ch")
    assert web._find_archive_record(state, "MIMK-186CH")
    assert web._find_archive_record(state, "MIMK-186") is not None, "归一后应能对上"
    assert web._find_archive_record(state, "MIMK-999") == {}


def test_verify_archive_reports_found_record_by_raw_key(env, monkeypatch):
    """核验接口要能找到那条记录（键是磁力原名时也能核）。"""
    core.save_state(web.STATE, {"mimk-186ch": {
        "code": "mimk-186ch", "files": ["mimk-186ch.mp4"], "size": 7,
        "backend": "clouddrive2", "rel": "dest/mimk-186ch",
        "manifest": {"files": [{"name": "mimk-186ch.mp4", "size": 7, "ext": ".mp4"}]}}})
    monkeypatch.setattr(web, "storage_backend", lambda: SimpleNamespace(kind="clouddrive2"))
    monkeypatch.setattr(web, "remote_ls", lambda code, **_: [{"name": "mimk-186ch.mp4", "size": 7}])

    out = web.verify_archive("mimk-186ch")

    assert out["ok"] is True, out
    assert out["state"] == "校验通过", out
