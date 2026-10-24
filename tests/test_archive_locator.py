"""1.5 归档记录自带定位：核验、删除、改名、补刮都按记录找目录。

云添加的归档在 CLOUD_DEST，全局 STORAGE_ROOT 可能是另一个根（或后来被改过）。
曾经就是「记录写着 cloud:X-Drive/DEMO，核验却去 legacy-root/DEMO 找」——
账上写 a 处，操作落在 b 处，比单纯报错危险得多。
"""
from __future__ import annotations

import pytest

from app import storage, web


def test_locator_prefers_record_rel_over_global_root(monkeypatch):
    """记录里有 rel 就直接用，无视当前全局根。"""
    monkeypatch.setattr(storage, "root", lambda: "legacy-root")
    record = {"code": "DEMO-1", "backend": "clouddrive2",
              "root": "迅雷云盘/X-Drive", "rel": "迅雷云盘/X-Drive/DEMO-1"}
    assert web.archive_locator(record) == "迅雷云盘/X-Drive/DEMO-1"


def test_locator_falls_back_for_legacy_records(monkeypatch):
    """旧记录没有 rel：退回 storage.rel(code)，行为与升级前一致。"""
    monkeypatch.setattr(storage, "root", lambda: "X-Drive")
    assert web.archive_locator({"code": "DEMO-1"}) == "X-Drive/DEMO-1"
    assert web.archive_locator({}) == "X-Drive"


def test_locator_rejects_unsafe_code(monkeypatch):
    monkeypatch.setattr(storage, "root", lambda: "X-Drive")
    with pytest.raises(storage.StorageError):
        web.archive_locator({"code": "../evil"})


def test_verify_uses_record_locator(monkeypatch, tmp_path):
    """CLOUD_DEST != STORAGE_ROOT 时，核验必须去记录指向的目录。"""
    from app import core, store as store_mod
    seen: list[str] = []

    class Backend:
        kind = "clouddrive2"

        def list_files(self, rel):
            seen.append(rel)
            return []

    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(web, "STORE", store_mod.Store(str(tmp_path / "jobs.db")))
    monkeypatch.setattr(web, "storage_backend", lambda: Backend())
    monkeypatch.setattr(storage, "root", lambda: "legacy-root")
    core.save_state(web.STATE, {
        "DEMO-1": {"code": "DEMO-1", "path": "clouddrive2:cloud/X-Drive/DEMO-1",
                   "backend": "clouddrive2", "root": "cloud/X-Drive",
                   "rel": "cloud/X-Drive/DEMO-1",
                   "files": ["DEMO-1.mp4"]},
    })
    out = web.verify_archive("DEMO-1")
    assert out["ok"] is True
    assert seen == ["cloud/X-Drive/DEMO-1"], \
        f"核验去了错误的位置：{seen}（全局根是 legacy-root）"
