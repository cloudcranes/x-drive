"""刮削源配置的热更新：网页改完，下一次请求就按新值走。

METATUBE_URL / TOKEN / PROXY / TIMEOUT、JAVBUS_BASE / TIMEOUT、META_USE_JAVBUS
以前都是模块导入时读一次的环境变量 —— 想改得改 .env 再重启容器，网页里也看不见。
现在收进 SETTINGS_SPEC，模块通过 settings.hot() 取值：绑定了 Settings 就取
state/env 的显式值，没绑定（单测、离线脚本）仍回落到模块常量。

这里验证「桥」的两端：绑定时热值优先，解绑后回落。
"""
from __future__ import annotations

import pytest

from app import javbus, meta, metatube
from app import settings as settings_mod
from app import store as store_mod


@pytest.fixture
def bound(tmp_path):
    """绑定一个临时库的 Settings；结束时解绑，避免影响别的测试。"""
    inst = settings_mod.Settings(store_mod.Store(str(tmp_path / "jobs.db")))
    settings_mod.bind(inst)
    try:
        yield inst
    finally:
        settings_mod.bind(None)


def test_unbound_falls_back_to_module_constants(monkeypatch):
    settings_mod.bind(None)
    monkeypatch.setattr(metatube, "BASE", "http://from-module")
    assert metatube.base() == "http://from-module"
    monkeypatch.setattr(javbus, "BASE", "http://jb-module")
    assert javbus.base() == "http://jb-module"
    monkeypatch.setattr(meta, "USE_JAVBUS", True)
    assert meta._use_javbus() is True


def test_metatube_url_and_token_are_hot(bound, monkeypatch):
    monkeypatch.setattr(metatube, "BASE", "")
    monkeypatch.setattr(metatube, "TOKEN", "")
    assert metatube.configured() is False          # 模块常量还是空的

    assert bound.set("METATUBE_URL", "http://mt.local:8090/") is True
    assert bound.set("METATUBE_TOKEN", "SECRET-T") is True
    assert metatube.base() == "http://mt.local:8090"   # 末尾斜杠被吃掉
    assert metatube.configured() is True
    assert metatube._headers() == {"Authorization": "Bearer SECRET-T"}


def test_metatube_auth_header_and_scheme_are_hot(bound, monkeypatch):
    monkeypatch.setattr(metatube, "TOKEN", "")
    bound.set("METATUBE_TOKEN", "T")
    bound.set("METATUBE_AUTH_HEADER", "X-Api-Key")
    bound.set("METATUBE_AUTH_SCHEME", "")           # 空 = 只发裸 token
    assert metatube._headers() == {"X-Api-Key": "T"}


def test_metatube_proxy_and_timeout_are_hot(bound):
    bound.set("METATUBE_PROXY", "http://192.168.1.2:7893")
    bound.set("METATUBE_TIMEOUT", 7.5)
    assert metatube._proxy() == "http://192.168.1.2:7893"
    assert metatube._timeout() == 7.5


def test_javbus_base_and_timeout_are_hot(bound, monkeypatch):
    monkeypatch.setattr(javbus, "BASE", "http://old")
    bound.set("JAVBUS_BASE", "http://jb.local/")
    bound.set("JAVBUS_TIMEOUT", 9.0)
    assert javbus.base() == "http://jb.local"
    assert javbus.timeout() == 9.0


def test_meta_use_javbus_toggles(bound, monkeypatch):
    monkeypatch.setattr(meta, "USE_JAVBUS", True)
    bound.set("META_USE_JAVBUS", False)
    assert meta._use_javbus() is False


def test_env_value_also_counts_when_bound(bound, monkeypatch):
    """绑定之后仍要尊重 .env：显式配置=state 或环境变量，两者都算。"""
    monkeypatch.setenv("METATUBE_URL", "http://from-env:8090")
    assert metatube.base() == "http://from-env:8090"


def test_settings_group_is_exposed_for_data_sources():
    """设置面板要能看到这些项（分组「数据源」），否则用户还是找不到。"""
    keys = ("METATUBE_URL", "METATUBE_TOKEN", "METATUBE_PROXY", "METATUBE_TIMEOUT",
            "JAVBUS_BASE", "JAVBUS_TIMEOUT", "META_USE_JAVBUS")
    for key in keys:
        spec = settings_mod.SETTINGS_SPEC[key]
        assert spec["group"] == "数据源", key
        assert spec["label"], key
