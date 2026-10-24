"""关键词搜索：字段别名、去重、缓存，以及「上游挂了不影响页面」。

首页媒体墙已于 2026-10-25 移除（只展示已归档资源）。本模块现在只服务
搜索框的「关键词 → 看看有哪些片」（/api/find）。
"""
from __future__ import annotations

import pytest

from app import settings as settings_mod
from app import find


@pytest.fixture(autouse=True)
def fresh():
    find.clear_cache()
    settings_mod.bind(None)
    yield
    find.clear_cache()
    settings_mod.bind(None)


# ─────────────────────────────────────────── 字段别名

def test_normalize_accepts_common_aliases():
    """javbus 用 id/img/title，别的实现常见 code/cover/name，都要认。"""
    a = find.normalize_item({"id": "SSIS-123", "img": "http://x/1.jpg",
                             "title": "标题", "date": "2026-01-01"})
    b = find.normalize_item({"code": "SSIS-124", "cover": "http://x/2.jpg",
                             "name": "标题2", "release_date": "2026-01-02"})
    assert a and b
    assert a["code"] == "SSIS-123" and a["cover"] == "http://x/1.jpg"
    assert b["code"] == "SSIS-124" and b["cover"] == "http://x/2.jpg"
    assert b["date"] == "2026-01-02"


def test_normalize_rejects_incomplete_rows():
    """没番号或没封面的条目直接丢：摆一个点不动的空格子更糟。"""
    assert find.normalize_item({"title": "没有番号"}) is None
    assert find.normalize_item({"id": "SSIS-1"}) is None
    assert find.normalize_item("不是字典") is None


def test_normalize_tags_can_be_dicts():
    item = find.normalize_item({"id": "A-1", "img": "u", "tags": [{"name": "高清"}, "字幕", ""]})
    assert item["tags"] == ["高清", "字幕"]


# ─────────────────────────────────────────── 关键词搜索

def test_find_returns_cards(monkeypatch):
    async def fake_search(keyword):
        assert keyword == "MIMK"
        return [{"code": "MIMK-289", "img": "http://x/1.jpg", "title": "标题",
                 "date": "2026-10-02"}]

    monkeypatch.setattr(find.javbus, "search", fake_search)
    result = find.find("MIMK")
    assert result["ok"] and result["query"] == "MIMK"
    assert result["items"][0]["code"] == "MIMK-289"


def test_find_dedupes_and_limits(monkeypatch):
    rows = [{"id": "MIMK-1", "img": "u1"}, {"id": "MIMK-1", "img": "u2"},
            {"id": "MIMK-2", "img": "u3"}]

    async def fake_search(_keyword):
        return rows

    monkeypatch.setattr(find.javbus, "search", fake_search)
    assert [i["code"] for i in find.find("MIMK")["items"]] == ["MIMK-1", "MIMK-2"]
    assert len(find.find("MIMK", limit=1)["items"]) == 1


def test_find_empty_keyword():
    assert find.find("   ")["ok"] is False


def test_find_upstream_failure_is_not_raised(monkeypatch):
    async def boom(_keyword):
        raise RuntimeError("上游 503")

    monkeypatch.setattr(find.javbus, "search", boom)
    result = find.find("MIMK")
    assert result["ok"] is False and "503" in result["error"]


def test_find_is_cached(monkeypatch):
    calls = []

    async def fake_search(keyword):
        calls.append(keyword)
        return [{"id": "A-1", "img": "u"}]

    monkeypatch.setattr(find.javbus, "search", fake_search)
    find.find("MIMK")
    second = find.find("MIMK")
    assert second["cached"] is True and calls == ["MIMK"]


def test_keyword_search_falls_back_to_javdb(monkeypatch):
    """javbus 没结果时才问 javdb：两个源覆盖面不一样，多试一次不算浪费。"""
    async def empty_bus(_keyword):
        return []

    async def db_hit(_keyword):
        return [{"code": "MIMK-295", "img": "u"}]

    monkeypatch.setattr(find.javbus, "search", empty_bus)
    monkeypatch.setattr(find.javdbwall, "search", db_hit)
    find.clear_cache()
    result = find.find("MIMK")
    assert result["ok"] and result["items"][0]["code"] == "MIMK-295"


# ─────────────────────────────────────────── 媒体墙已移除

def test_media_wall_is_gone():
    """媒体墙于 2026-10-25 移除：feed / 来源表 / 相关设置都不该再有。"""
    assert not hasattr(find, "feed")
    assert not hasattr(find, "feed_async")
    assert not hasattr(find, "source")
    assert not hasattr(find, "enabled")
    assert not hasattr(find, "SOURCES")
    assert not hasattr(find, "_FETCHERS")


def test_wall_settings_are_gone():
    """WALL_* 设置项随媒体墙一起删了。"""
    from app import settings as sm
    keys = {k for k in sm.SETTINGS_SPEC if k.startswith("WALL_")}
    assert keys == set(), f"还剩这些媒体墙设置：{keys}"
