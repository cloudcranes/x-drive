"""可热更新参数（settings）：读取优先级、类型转换、缓存、写库、快照来源标注。

对应「设置界面」的核心机制：17 项运行时参数从模块常量迁到 state 表的
settings 键，get_setting / Settings.get 统一访问，网页保存后立即生效。
全部离线：store 用 tmp_path，环境变量用 monkeypatch，不碰真实 /data。
"""
from __future__ import annotations

import pytest

from app import settings as settings_mod
from app import store


def _store(tmp_path):
    return store.Store(str(tmp_path / "jobs.db"))


def _s(tmp_path):
    return settings_mod.Settings(_store(tmp_path))


# ─────────────────────────────────────────── 读取优先级

def test_default_when_nothing_set(tmp_path, monkeypatch):
    monkeypatch.delenv("DAILY_UPLOAD_GB", raising=False)
    s = _s(tmp_path)
    assert s.get("DAILY_UPLOAD_GB") == 0          # 默认 0
    assert s.get("SCRAPE") is True                 # 默认 True


def test_env_overrides_default(tmp_path, monkeypatch):
    monkeypatch.setenv("DAILY_UPLOAD_GB", "3")
    s = _s(tmp_path)
    assert s.get("DAILY_UPLOAD_GB") == 3


def test_state_overrides_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DAILY_UPLOAD_GB", "3")
    _store(tmp_path).state_put({"settings": {"DAILY_UPLOAD_GB": 9}})
    s = _s(tmp_path)
    assert s.get("DAILY_UPLOAD_GB") == 9           # state 表 > env


# ─────────────────────────────────────────── 类型转换

def test_bool_coercion(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRAPE", "0")
    assert _s(tmp_path).get("SCRAPE") is False
    monkeypatch.setenv("SCRAPE", "false")
    assert _s(tmp_path).get("SCRAPE") is False
    monkeypatch.setenv("SCRAPE", "1")
    assert _s(tmp_path).get("SCRAPE") is True


def test_int_float_coercion(tmp_path, monkeypatch):
    monkeypatch.setenv("RETRIES", "7")
    monkeypatch.setenv("RETRY_BASE_DELAY", "2.5")
    s = _s(tmp_path)
    assert s.get("RETRIES") == 7 and isinstance(s.get("RETRIES"), int)
    assert s.get("RETRY_BASE_DELAY") == 2.5 and isinstance(s.get("RETRY_BASE_DELAY"), float)


# ─────────────────────────────────────────── 写

def test_set_writes_and_invalidates_cache(tmp_path):
    s = _s(tmp_path)
    assert s.get("RETRIES") == 2                   # 默认，进缓存
    assert s.set("RETRIES", 5) is True
    assert s.get("RETRIES") == 5                   # 缓存失效，读到新值
    # 落库：另一个 Settings 实例（模拟 bot 进程）也读到
    assert _s(tmp_path).get("RETRIES") == 5


def test_set_rejects_bad_type(tmp_path):
    s = _s(tmp_path)
    assert s.set("RETRIES", "not-a-number") is False   # 转换失败
    assert s.get("RETRIES") == 2


def test_set_rejects_unknown_key(tmp_path):
    s = _s(tmp_path)
    assert s.set("NO_SUCH_KEY", 1) is False


def test_set_many_reports_failures(tmp_path):
    s = _s(tmp_path)
    out = s.set_many({"RETRIES": 5, "SCRAPE": False, "BAD_KEY": 1, "DAILY_UPLOAD_GB": "x"})
    assert out["ok"] is False                       # 有失败项
    assert set(out["saved"]) == {"RETRIES", "SCRAPE"}
    assert set(out["failed"]) == {"BAD_KEY", "DAILY_UPLOAD_GB"}


# ─────────────────────────────────────────── 快照与来源标注

def test_snapshot_marks_source(tmp_path, monkeypatch):
    monkeypatch.delenv("DAILY_UPLOAD_GB", raising=False)
    monkeypatch.setenv("RETRIES", "7")
    _store(tmp_path).state_put({"settings": {"SCRAPE": False}})
    s = _s(tmp_path)
    snap = s.snapshot()
    assert snap["DAILY_UPLOAD_GB"]["source"] == "default"
    assert snap["RETRIES"]["source"] == "env"
    assert snap["SCRAPE"]["source"] == "settings"
    assert snap["SCRAPE"]["value"] is False


def test_snapshot_covers_every_spec(tmp_path):
    """设置面板列表 = SETTINGS_SPEC 全量（别写死条数，加配置项不用再改测试）。"""
    s = _s(tmp_path)
    snap = s.snapshot()
    assert len(snap) == len(settings_mod.SETTINGS_SPEC)
    for key, spec in settings_mod.SETTINGS_SPEC.items():
        assert key in snap, f"缺 {key}"
        assert snap[key]["group"] == spec["group"]
        assert snap[key]["label"]


def test_secret_settings_are_not_echoed(tmp_path, monkeypatch):
    """METATUBE_TOKEN 这类密钥只回「已设置」，快照里绝不能带明文。"""
    monkeypatch.setenv("METATUBE_TOKEN", "SECRET-MT")
    s = _s(tmp_path)
    snap = s.snapshot()
    assert snap["METATUBE_TOKEN"]["secret"] is True
    assert snap["METATUBE_TOKEN"]["value"] == ""
    assert snap["METATUBE_TOKEN"]["configured"] is True
    assert "SECRET-MT" not in repr(snap)


def test_secret_set_from_web_wins_over_env(tmp_path, monkeypatch):
    """网页写入的 token 优先级高于环境变量（state > env）。"""
    monkeypatch.setenv("METATUBE_TOKEN", "FROM-ENV")
    s = _s(tmp_path)
    assert s.set("METATUBE_TOKEN", "FROM-WEB") is True
    assert s.explicit("METATUBE_TOKEN") == (True, "FROM-WEB")
    assert s.snapshot()["METATUBE_TOKEN"]["configured"] is True


def test_explicit_reports_unset_keys(tmp_path, monkeypatch):
    """没配过的键要能分辨出来，模块常量才有机会兜底。"""
    monkeypatch.delenv("METATUBE_URL", raising=False)
    s = _s(tmp_path)
    assert s.explicit("METATUBE_URL") == (False, None)
    monkeypatch.setenv("METATUBE_URL", "http://mt:8090")
    assert s.explicit("METATUBE_URL") == (True, "http://mt:8090")


# ─────────────────────────────────────────── TTL 缓存

def test_cache_serves_within_ttl(tmp_path, monkeypatch):
    """TTL 内不重复打库：改环境变量后（未过期缓存）旧值仍短暂可见。

    这是刻意的：bot 进程靠 TTL 到期读到新值，所以这里验证「不主动失效」，
    而不是验证「立刻生效」。web 进程 set 时会主动失效（test_set 已覆盖）。
    """
    monkeypatch.setenv("RETRIES", "2")
    s = _s(tmp_path)
    assert s.get("RETRIES") == 2                   # 首次读，进缓存
    monkeypatch.setenv("RETRIES", "99")            # 改 env（模拟别的进程改了）
    # TTL 内仍读到旧值（缓存未过期）
    assert s.get("RETRIES") == 2


def test_ttl_expiry_rereads(tmp_path, monkeypatch):
    monkeypatch.setenv("RETRIES", "2")
    s = _s(tmp_path)
    assert s.get("RETRIES") == 2
    monkeypatch.setenv("RETRIES", "99")
    # 直接把缓存项标成过期，验证过期后会重新读
    with s._lock:
        s._cache["RETRIES"] = (s._cache["RETRIES"][0], 0)   # fetched_at 归零 = 过期
    assert s.get("RETRIES") == 99


# ─────────────────────────────────────────── web.py 接线

def test_web_get_setting_wiring(tmp_path, monkeypatch):
    from app import web
    monkeypatch.setattr(web, "STORE", _store(tmp_path))
    web._settings = None
    monkeypatch.delenv("RETRIES", raising=False)
    assert web.get_setting("RETRIES") == 2


def test_web_set_settings_wiring(tmp_path, monkeypatch):
    from app import web
    monkeypatch.setattr(web, "STORE", _store(tmp_path))
    web._settings = None
    out = web.set_settings({"RETRIES": 5})
    assert out["ok"] is True
    assert web.get_setting("RETRIES") == 5


# ─────────────────────────────────────────── 存储后端（新增分组）

def test_storage_group_present(tmp_path, monkeypatch):
    for key in ("STORAGE_BACKEND", "OPENLIST_URL", "OPENLIST_PASSWORD",
                "CD2_URL", "CD2_PASSWORD", "CD2_LINK_MODE"):
        assert key in settings_mod.SETTINGS_SPEC, f"{key} 没进设置表"
        assert settings_mod.SETTINGS_SPEC[key]["group"] == "存储"
    snap = _s(tmp_path).snapshot()
    assert snap["STORAGE_BACKEND"]["value"] == "rclone"
    assert snap["STORAGE_BACKEND"]["choices"] == ["rclone", "openlist", "clouddrive2"]
    assert snap["CD2_LINK_MODE"]["choices"] == ["none", "static", "template"]


def test_storage_secrets_are_never_echoed(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENLIST_PASSWORD", "s3cret")
    monkeypatch.setenv("CD2_PASSWORD", "hunter2")
    snap = _s(tmp_path).snapshot()
    for key in ("OPENLIST_PASSWORD", "CD2_PASSWORD"):
        assert snap[key]["value"] == "", "密码不能出现在快照里"
        assert snap[key]["configured"] is True
    assert "s3cret" not in str(snap) and "hunter2" not in str(snap)


def test_storage_backend_roundtrip(tmp_path, monkeypatch):
    inst = _s(tmp_path)
    assert inst.set("STORAGE_BACKEND", "clouddrive2") is True
    assert inst.get("STORAGE_BACKEND") == "clouddrive2"
    assert inst.explicit("STORAGE_BACKEND") == (True, "clouddrive2")
    # 非法类型不该被写进去
    assert inst.set("CD2_CHUNK_MB", "太宽") is False


def test_storage_specs_declare_owning_backend():
    """每项存储参数标了它属于哪个后端：界面据此裁剪，别让两套参数混在一起。"""
    spec = settings_mod.SETTINGS_SPEC
    assert "only" not in spec["STORAGE_BACKEND"] and "only" not in spec["STORAGE_ROOT"]
    for key in ("OPENLIST_URL", "OPENLIST_PASSWORD", "OPENLIST_SHARE_PWD"):
        assert spec[key]["only"] == "openlist"
    for key in ("CD2_URL", "CD2_PASSWORD", "CD2_LINK_MODE", "SHARE_LINK_TEMPLATE"):
        assert spec[key]["only"] == "clouddrive2"
    # 快照要透传，前端才拿得到
    snap = _s(__import__("pathlib").Path("/tmp/xd-scope-test")).snapshot()
    assert snap["CD2_URL"]["only"] == "clouddrive2"
