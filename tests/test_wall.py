"""首页媒体墙：字段别名、分页判定、缓存、以及「上游挂了不影响首页」。

上游接口不是一家：javbus 的 JSON 列表、自建聚合、alist 索引，字段名各不相同。
这里把「怎么认字段」和「怎么判下一页」钉住，换源时不用改前端。
"""
from __future__ import annotations

import pytest

from app import settings as settings_mod
from app import wall


@pytest.fixture(autouse=True)
def fresh():
    wall.clear_cache()
    settings_mod.bind(None)
    yield
    wall.clear_cache()
    settings_mod.bind(None)


# ─────────────────────────────────────────── 字段别名

def test_normalize_accepts_common_aliases():
    """javbus 用 id/img/title，别的实现常见 code/cover/name，都要认。"""
    a = wall.normalize_item({"id": "SSIS-123", "img": "http://x/1.jpg",
                             "title": "标题", "date": "2026-01-01"})
    b = wall.normalize_item({"code": "SSIS-124", "cover": "http://x/2.jpg",
                             "name": "标题2", "release_date": "2026-01-02"})
    assert a and b
    assert a["code"] == "SSIS-123" and a["cover"] == "http://x/1.jpg"
    assert b["code"] == "SSIS-124" and b["cover"] == "http://x/2.jpg"
    assert b["date"] == "2026-01-02"


def test_normalize_rejects_incomplete_rows():
    """没番号或没封面的条目直接丢：摆一个点不动的空格子更糟。"""
    assert wall.normalize_item({"title": "没有番号"}) is None
    assert wall.normalize_item({"id": "SSIS-1"}) is None
    assert wall.normalize_item("不是字典") is None


def test_normalize_tags_can_be_dicts():
    item = wall.normalize_item({"id": "A-1", "img": "u", "tags": [{"name": "高清"}, "字幕", ""]})
    assert item["tags"] == ["高清", "字幕"]


# ─────────────────────────────────────────── 分页判定

def test_normalize_payload_reads_pagination():
    payload = {"movies": [{"id": "A-1", "img": "u1"}],
               "pagination": {"currentPage": 1, "hasNextPage": True}}
    items, more = wall.normalize_payload(payload, 1)
    assert len(items) == 1 and more is True


def test_normalize_payload_without_pagination_uses_full_page_rule():
    """上游不给分页信息时：满页当作「可能还有」，不满就是最后一页。"""
    rows = [{"id": f"A-{i}", "img": f"u{i}"} for i in range(30)]
    _items, more = wall.normalize_payload({"data": rows}, 1)
    assert more is True
    _items, more = wall.normalize_payload({"data": rows[:5]}, 1)
    assert more is False


def test_normalize_payload_accepts_bare_list():
    items, _more = wall.normalize_payload([{"id": "B-1", "img": "u"}], 1)
    assert [i["code"] for i in items] == ["B-1"]


def test_normalize_payload_unknown_shape_is_empty():
    assert wall.normalize_payload({"unexpected": 1}, 1) == ([], False)
    assert wall.normalize_payload("html", 1) == ([], False)


# ─────────────────────────────────────────── 取数与缓存

def test_unknown_source_is_explained():
    result = wall.feed(1, "nope")
    assert result["ok"] is False
    assert "未知来源" in result["error"] and "javbus" in result["error"]


def test_custom_source_without_url_says_so():
    result = wall.feed(1, "custom")
    assert result["ok"] is False and "WALL_URL" in result["error"]


def test_fetch_is_cached(monkeypatch):
    calls = []

    async def fake(page):
        calls.append(page)
        return [{"code": "A-1", "cover": "u", "title": "", "date": "", "tags": []}], True

    monkeypatch.setitem(wall._FETCHERS, "javbus", fake)
    first = wall.feed(1)
    second = wall.feed(1)
    assert first["ok"] and first["cached"] is False
    assert second["cached"] is True
    assert calls == [1], "同一页在 TTL 内只该打一次上游"


def test_upstream_failure_does_not_raise(monkeypatch):
    async def boom(page):
        raise RuntimeError("上游 500")

    monkeypatch.setitem(wall._FETCHERS, "javbus", boom)
    result = wall.feed(1)
    assert result["ok"] is False and "上游 500" in result["error"]
    assert result["items"] == []


def test_failure_is_not_cached(monkeypatch):
    state = {"n": 0}

    async def flaky(page):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("偶发")
        return [{"code": "A-1", "cover": "u", "title": "", "date": "", "tags": []}], False

    monkeypatch.setitem(wall._FETCHERS, "javbus", flaky)
    assert wall.feed(1)["ok"] is False
    assert wall.feed(1)["ok"] is True, "失败不该被缓存，否则要等 TTL 才恢复"


def test_source_choice_reads_settings(tmp_path, monkeypatch):
    from app import store
    inst = settings_mod.Settings(store.Store(str(tmp_path / "jobs.db")))
    inst.set("WALL_SOURCE", "custom")
    inst.set("WALL_URL", "http://up.example/api?page={page}")
    settings_mod.bind(inst)
    assert wall.source() == "custom"
    assert wall._url_template() == "http://up.example/api?page={page}"


def test_disabled_setting():
    assert wall.enabled() is True


# ─────────────────────────────────────────── 关键词搜索

def test_find_returns_cards(monkeypatch):
    async def fake_search(keyword):
        assert keyword == "MIMK"
        return [{"code": "MIMK-289", "img": "http://x/1.jpg", "title": "标题",
                 "date": "2026-10-02"}]

    monkeypatch.setattr(wall.javbus, "search", fake_search)
    result = wall.find("MIMK")
    assert result["ok"] and result["query"] == "MIMK"
    assert result["items"][0]["code"] == "MIMK-289"


def test_find_dedupes_and_limits(monkeypatch):
    rows = [{"id": "MIMK-1", "img": "u1"}, {"id": "MIMK-1", "img": "u2"},
            {"id": "MIMK-2", "img": "u3"}]

    async def fake_search(_keyword):
        return rows

    monkeypatch.setattr(wall.javbus, "search", fake_search)
    assert [i["code"] for i in wall.find("MIMK")["items"]] == ["MIMK-1", "MIMK-2"]
    assert len(wall.find("MIMK", limit=1)["items"]) == 1


def test_find_empty_keyword():
    assert wall.find("   ")["ok"] is False


def test_find_upstream_failure_is_not_raised(monkeypatch):
    async def boom(_keyword):
        raise RuntimeError("上游 503")

    monkeypatch.setattr(wall.javbus, "search", boom)
    result = wall.find("MIMK")
    assert result["ok"] is False and "503" in result["error"]


def test_find_is_cached(monkeypatch):
    calls = []

    async def fake_search(keyword):
        calls.append(keyword)
        return [{"id": "A-1", "img": "u"}]

    monkeypatch.setattr(wall.javbus, "search", fake_search)
    wall.find("MIMK")
    second = wall.find("MIMK")
    assert second["cached"] is True and calls == ["MIMK"]


# ─────────────────────────────────────────── javdb 源

def test_javdb_source_is_registered():
    assert "javdb" in wall.SOURCES
    assert "javdb" in wall._FETCHERS


def test_feed_from_javdb(monkeypatch):
    async def fake_latest(page):
        assert page == 1
        return [{"code": "SIMM-912", "cover": "https://c0.jdbstatic.com/covers/aq/Aq2VRO.jpg",
                 "title": "ひより", "date": "2026-10-03", "tags": ["★4.29", "含磁鏈"]}], True

    monkeypatch.setattr(wall.javdbwall, "latest", fake_latest)
    result = wall.feed(1, "javdb")
    assert result["ok"] and result["source"] == "javdb"
    assert result["items"][0]["code"] == "SIMM-912"
    assert "★4.29" in result["items"][0]["tags"]


def test_keyword_search_falls_back_to_javdb(monkeypatch):
    """javbus 没结果时才问 javdb：两个源覆盖面不一样，多试一次不算浪费。"""
    async def empty_bus(_keyword):
        return []

    async def db_hit(_keyword):
        return [{"code": "MIMK-295", "img": "u"}]

    monkeypatch.setattr(wall.javbus, "search", empty_bus)
    monkeypatch.setattr(wall.javdbwall, "search", db_hit)
    wall.clear_cache()
    result = wall.find("MIMK")
    assert result["ok"] and result["items"][0]["code"] == "MIMK-295"
