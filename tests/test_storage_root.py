"""归档根目录的解析顺序。

**为什么要有这条**：全局根（STORAGE_ROOT）与云添加的落地目标（CLOUD_DEST）
本来指的是同一个地方 —— 云添加把片子整理到 CLOUD_DEST 下，全局根是「兜底
路径」。这两个不一致时，兜底会指向一个不存在的位置：实测 MIMK-267 的
`rel` 是 `迅雷云盘/X-Drive/MIMK-267`，而全局根当时是
`Downloads/downloads/test` —— 拿全局根拼出来的路径根本不存在。
所以 STORAGE_ROOT 留空时要**跟随 CLOUD_DEST**。
"""
from __future__ import annotations

import pytest

from app import storage


@pytest.fixture
def env(tmp_path, monkeypatch):
    """把设置层换成一个内存实现，方便切换各组合。"""
    values: dict[str, str] = {}

    class _Settings:
        def explicit(self, key):
            if key in values:
                return True, values[key]
            spec = storage.settings_mod.SETTINGS_SPEC.get(key) or {}
            return False, None

    monkeypatch.setattr(storage, "settings", lambda: _Settings())
    monkeypatch.delenv("DRIVE_ROOT", raising=False)
    return values


def test_explicit_storage_root_wins(env):
    env["STORAGE_ROOT"] = "我的根"
    env["CLOUD_DEST"] = "迅雷云盘/X-Drive"
    assert storage.root() == "我的根"


def test_falls_back_to_cloud_dest_when_storage_root_empty(env):
    """STORAGE_ROOT 留空 → 跟随 CLOUD_DEST（两者本来是同一个地方）。"""
    env["STORAGE_ROOT"] = ""
    env["CLOUD_DEST"] = "迅雷云盘/X-Drive"
    assert storage.root() == "迅雷云盘/X-Drive"
    assert storage.rel("MIMK-186") == "迅雷云盘/X-Drive/MIMK-186"


def test_falls_back_to_drive_root_when_both_empty(env, monkeypatch):
    env["STORAGE_ROOT"] = ""
    env["CLOUD_DEST"] = ""
    monkeypatch.setenv("DRIVE_ROOT", "老根")
    assert storage.root() == "老根"


def test_root_is_stripped_of_slashes(env):
    env["STORAGE_ROOT"] = "/迅雷云盘/X-Drive/"
    assert storage.root() == "迅雷云盘/X-Drive"
