"""MetaTube 客户端：只搬真实字段、缓存只合并不覆盖、番号必须对得上。"""
import json
import time

import pytest

from app import metatube


def test_normalize_keeps_only_real_fields():
    rec = metatube.normalize({"number": "ssis-123", "title": "标题", "runtime": "120",
                              "actors": ["A", ""], "genres": [], "cover_url": "http://x/1.jpg"},
                             provider="JavBus")
    assert rec["code"] == "SSIS-123"
    assert rec["runtime"] == 120
    assert rec["actors"] == ["A"]
    assert rec["genres"] == []
    assert rec["summary"] == "", "缺失字段留空，不编"
    assert rec["provider"] == "JavBus"


def test_matches_requires_same_code():
    """标题像不算数：只认规范化后的番号。"""
    assert metatube.matches("SSIS-123", {"code": "ssis123"})
    assert not metatube.matches("SSIS-123", {"code": "SSIS-124"})
    assert not metatube.matches("", {"code": "SSIS-123"})


def test_cache_merges_and_never_drops_fields(tmp_path, monkeypatch):
    """第二次只返回了标题：演员、简介不能被冲掉。"""
    monkeypatch.setattr(metatube, "CACHE_DIR", str(tmp_path))
    metatube.cache_put("SSIS-1", {"code": "SSIS-1", "title": "旧标题",
                                  "actors": ["A"], "summary": "简介"})
    assert metatube.cache_get("SSIS-1")["actors"] == ["A"]


def test_cache_survives_corrupt_file(tmp_path, monkeypatch):
    monkeypatch.setattr(metatube, "CACHE_DIR", str(tmp_path))
    metatube.cache_put("SSIS-2", {"code": "SSIS-2"})
    path = tmp_path / (metatube._cache_key("movie", "SSIS-2") + ".json")
    path.write_text("{坏掉的 json", encoding="utf-8")
    assert metatube.cache_get("SSIS-2") == {}, "坏缓存当没有，不能让刮削崩"


def test_scrape_returns_cache_when_service_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(metatube, "CACHE_DIR", str(tmp_path))
    metatube.cache_put("SSIS-3", {"code": "SSIS-3", "title": "缓存标题", "actors": ["A"]})

    async def fake_movie(code, provider="", client=None):
        return {}

    monkeypatch.setattr(metatube, "movie", fake_movie)
    import asyncio
    out = asyncio.run(metatube.scrape("SSIS-3"))
    assert out["title"] == "缓存标题" and out["actors"] == ["A"]


def test_scrape_merges_fresh_over_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(metatube, "CACHE_DIR", str(tmp_path))
    metatube.cache_put("SSIS-4", {"code": "SSIS-4", "title": "旧", "summary": "简介",
                                  "actors": ["A"]})

    async def fake_movie(code, provider="", client=None):
        return {"code": code, "title": "新", "summary": "", "actors": [],
                "runtime": 130}

    monkeypatch.setattr(metatube, "movie", fake_movie)
    import asyncio
    out = asyncio.run(metatube.scrape("SSIS-4"))
    assert out["title"] == "新"
    assert out["summary"] == "简介" and out["actors"] == ["A"], "空值不能覆盖已有"
    assert out["runtime"] == 130


def test_token_never_leaks_into_record(monkeypatch):
    """鉴权信息绝不能进元数据文件。"""
    monkeypatch.setattr(metatube, "TOKEN", "SECRET-TOKEN")
    monkeypatch.setattr(metatube, "AUTH_HEADER", "Authorization")
    rec = metatube.normalize({"number": "X-1", "title": "t"}, provider="JavBus")
    blob = json.dumps(rec, ensure_ascii=False)
    assert "SECRET-TOKEN" not in blob
    assert metatube._headers()["Authorization"] == "Bearer SECRET-TOKEN"


def test_auth_header_is_configurable(monkeypatch):
    monkeypatch.setattr(metatube, "TOKEN", "T")
    monkeypatch.setattr(metatube, "AUTH_HEADER", "X-Api-Key")
    monkeypatch.setattr(metatube, "AUTH_SCHEME", "")
    assert metatube._headers() == {"X-Api-Key": "T"}


def test_release_date_trimmed_to_plain_date():
    """服务端给 RFC3339，NFO 只认 YYYY-MM-DD。"""
    rec = metatube.normalize({"number": "X-1", "release_date": "2021-07-14T00:00:00Z"})
    assert rec["release_date"] == "2021-07-14"


def test_movie_uses_search_payload_without_detail_call(monkeypatch):
    """实测：搜索接口已返回完整字段，不该再打详情（那条路由 404 provider not found）。"""
    calls = []

    async def fake_search(query, provider="", client=None):
        calls.append(("search", query, provider))
        return [{"id": "SSIS-1", "number": "SSIS-1", "title": "来自搜索",
                 "provider": "JavBus", "release_date": "2021-01-01T00:00:00Z"}]

    monkeypatch.setattr(metatube, "search", fake_search)
    import asyncio
    rec = asyncio.run(metatube.movie("SSIS-1"))
    assert rec["title"] == "来自搜索"
    assert rec["provider"] == "JavBus"
    assert calls == [("search", "SSIS-1", "")], "只查一次搜索，不打详情"


def test_movie_prefers_matching_number(monkeypatch):
    """搜索会顺带返回相似番号：必须挑对的那个。"""
    async def fake_search(query, provider="", client=None):
        return [{"number": "SSIS-012", "title": "错"}, {"number": "SSIS-12", "title": "对"}]

    monkeypatch.setattr(metatube, "search", fake_search)
    import asyncio
    assert asyncio.run(metatube.movie("SSIS-12"))["title"] == "对"
