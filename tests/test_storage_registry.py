"""存储注册表：按设置选后端、根目录解析、实例缓存与失效、快照与自检兜底。"""
from __future__ import annotations

import pytest

from app import settings as settings_mod
from app import storage
from app import store


@pytest.fixture(autouse=True)
def clean():
    storage.reset()
    settings_mod.bind(None)
    yield
    storage.reset()
    settings_mod.bind(None)


def _bind(tmp_path):
    inst = settings_mod.Settings(store.Store(str(tmp_path / "jobs.db")))
    settings_mod.bind(inst)
    return inst


# ─────────────────────────────────────────── 选择与路径

def test_default_is_rclone(monkeypatch, tmp_path):
    monkeypatch.delenv("DRIVE_ROOT", raising=False)
    _bind(tmp_path)
    assert storage.kind() == "rclone"
    assert storage.root() == "X-Drive"
    assert storage.rel("SSIS-555") == "X-Drive/SSIS-555"


def test_root_falls_back_to_drive_root_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DRIVE_ROOT", "/Old-Root/")
    _bind(tmp_path)
    assert storage.root() == "Old-Root"


def test_storage_root_setting_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("DRIVE_ROOT", "Old-Root")
    inst = _bind(tmp_path)
    assert inst.set("STORAGE_ROOT", "New-Root")
    assert storage.root() == "New-Root"


def test_backend_from_settings(tmp_path):
    inst = _bind(tmp_path)
    inst.set("STORAGE_BACKEND", "openlist")
    inst.set("OPENLIST_URL", "http://ol:5244")
    assert storage.kind() == "openlist"
    assert storage.get().kind == "openlist"


def test_unknown_backend_falls_back_to_rclone(tmp_path):
    inst = _bind(tmp_path)
    inst.set("STORAGE_BACKEND", "ftp")
    assert storage.kind() == "rclone"


def test_rel_ignores_empty_parts(tmp_path):
    _bind(tmp_path)
    assert storage.rel("", "A", "/") == "X-Drive/A"


# ─────────────────────────────────────────── 实例缓存

def test_instance_is_cached(tmp_path):
    inst = _bind(tmp_path)
    inst.set("OPENLIST_URL", "http://ol:5244")
    inst.set("STORAGE_BACKEND", "openlist")
    assert storage.get() is storage.get()


def test_cache_invalidated_on_config_change(tmp_path):
    inst = _bind(tmp_path)
    inst.set("STORAGE_BACKEND", "openlist")
    inst.set("OPENLIST_URL", "http://a:5244")
    first = storage.get()
    inst.set("OPENLIST_URL", "http://b:5244")
    assert storage.get() is not first


def test_reset_clears_cache(tmp_path):
    inst = _bind(tmp_path)
    inst.set("STORAGE_BACKEND", "openlist")
    inst.set("OPENLIST_URL", "http://a:5244")
    first = storage.get()
    storage.reset()
    assert storage.get() is not first


# ─────────────────────────────────────────── 辅助函数

def test_openlist_link_id():
    assert storage.openlist_link_id("http://ol:5244/s/abc") == "abc"
    assert storage.openlist_link_id("http://ol:5244/s/abc/") == "abc"
    assert storage.openlist_link_id("https://drive.google.com/x") == ""
    assert storage.openlist_link_id("") == ""


def test_real_folder_passthrough_for_rclone(tmp_path):
    _bind(tmp_path)
    assert storage.real_folder("SSIS-555") == "SSIS-555"


# ─────────────────────────────────────────── 快照 / 自检

def test_snapshot_shape(tmp_path):
    inst = _bind(tmp_path)
    inst.set("STORAGE_BACKEND", "clouddrive2")
    inst.set("CD2_URL", "http://cd2:19798")
    snap = storage.snapshot()
    assert snap["kind"] == "clouddrive2"
    assert snap["configured"] is True
    assert "PASSWORD" not in str(snap), "快照里绝不能出现密钥字段"


def test_snapshot_marks_missing_config(tmp_path):
    inst = _bind(tmp_path)
    inst.set("STORAGE_BACKEND", "openlist")
    assert storage.snapshot()["configured"] is False


def test_check_reports_config_error_without_raising(tmp_path):
    inst = _bind(tmp_path)
    inst.set("STORAGE_BACKEND", "openlist")          # 故意不填 URL
    result = storage.check()
    assert result["ok"] is False
    assert "OPENLIST_URL" in result["error"]
    assert isinstance(result["latency_ms"], int)


def test_check_handles_unknown_exception(tmp_path, monkeypatch):
    _bind(tmp_path)

    class Boom:
        kind = "rclone"

        def check(self, probe_path=""):
            raise RuntimeError("炸了")

    monkeypatch.setattr(storage, "get", lambda which=None: Boom())
    result = storage.check()
    assert result["ok"] is False and "炸了" in result["error"]


def test_unbound_process_reads_web_settings(tmp_path, monkeypatch):
    """没绑定时按同一个 JOBS_DB 自己读设置 —— 不能只看环境变量。

    bot 的 `--check` 与离线脚本都是这种情形：网页里选了 clouddrive2，
    只看 env 会对着 rclone 自检，结论完全是错的。
    """
    import sqlite3, json
    db = tmp_path / "jobs.db"
    store.Store(str(db))                      # 建表
    with sqlite3.connect(db) as conn:
        conn.execute("insert or replace into state(key, value) values(?, ?)",
                     ("settings", json.dumps({"STORAGE_BACKEND": "clouddrive2",
                                              "CD2_URL": "http://cd2:19798",
                                              "STORAGE_ROOT": "Cloud"})))
        conn.commit()
    monkeypatch.setenv("JOBS_DB", str(db))
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    settings_mod.bind(None)                   # 模拟「没有绑定」的进程
    monkeypatch.setattr(storage, "_fallback", None)

    assert storage.kind() == "clouddrive2"
    assert storage.root() == "Cloud"
