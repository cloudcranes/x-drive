"""多源磁力搜索：解析、路由、去重、排序，全部离线。"""
import asyncio
import json

import httpx
import pytest

from app import sources

SUKEBEI = """<?xml version="1.0"?><rss><channel>
<item><title>[H265 1080p] FC2-PPV-4519894 作品A</title>
 <nyaa:infoHash>a066ceeb97cd20e847a8155ed5e2b91ff61ee557</nyaa:infoHash>
 <nyaa:size>468.3 MiB</nyaa:size><nyaa:seeders>1</nyaa:seeders></item>
<item><title>FC2-PPV-4519894 中文字幕 1080p</title>
 <nyaa:infoHash>5806ff5dc7f98f044e1d85d45dd405c9fd1bca30</nyaa:infoHash>
 <nyaa:size>3.3 GiB</nyaa:size><nyaa:seeders>21</nyaa:seeders></item>
</channel></rss>"""

BT4G = """<rss><channel>
<item><title>FC2-PPV-4519894</title><link>magnet:?xt=urn:btih:a066ceeb97cd20e847a8155ed5e2b91ff61ee557&amp;dn=x</link>
<description><![CDATA[FC2-PPV-4519894<br>468.27MB<br>Movie<br>a066ceeb97cd20e847a8155ed5e2b91ff61ee557]]></description></item>
</channel></rss>"""

# ---------------------------------------------------------------- 基础解析

def test_size_bytes_understands_rss_units():
    assert sources.size_bytes("468.27MB") == 468270000
    assert sources.size_bytes("4.0 GiB") == 4 * 1024 ** 3
    assert sources.size_bytes("3.3 GiB") > 3 * 1024 ** 3
    assert sources.size_bytes("") == 0

def test_flags_guess_subtitle_and_hd():
    assert sources.flags("SSIS-123 中文字幕 1080p") == (True, True)
    assert sources.flags("SSIS-123 4K") == (False, True)
    assert sources.flags("SSIS-123") == (False, False)

def test_magnet_of_adds_trackers():
    m = sources.magnet_of("a" * 40, "SSIS-123")
    assert m.startswith("magnet:?xt=urn:btih:") and "&dn=SSIS-123" in m
    assert m.count("&tr=") == len(sources.TRACKERS)

# ---------------------------------------------------------------- 各源解析

def test_parse_sukebei_reads_hash_size_seeders():
    items = sources.parse_sukebei(SUKEBEI)
    assert len(items) == 2
    first = {i["magnet"] for i in items}
    assert any("a066ceeb" in m for m in first)
    sub = [i for i in items if i["has_subtitle"]][0]
    assert sub["seeders"] == 21 and sub["size_bytes"] == 3 * 1024 ** 3 + int(0.3 * 1024 ** 3)
    assert sub["source"] == "sukebei"

def test_parse_bt4g_unescapes_magnet_and_reads_size():
    items = sources.parse_bt4g(BT4G)
    assert len(items) == 1
    assert "&amp;" not in items[0]["magnet"], "RSS 里的 &amp; 要还原，否则迅雷不认"
    assert items[0]["size_bytes"] == 468270000

def test_parse_keeps_going_without_infohash():
    xml = SUKEBEI.replace("<nyaa:infoHash>a066ceeb97cd20e847a8155ed5e2b91ff61ee557</nyaa:infoHash>", "")
    assert len(sources.parse_sukebei(xml)) == 1

# ---------------------------------------------------------------- 路由

def test_fc2_skips_javbus(monkeypatch):
    """javbus 没有 FC2 条目（实测 404），路由不该再去打它。"""
    monkeypatch.setattr(sources, "configured", lambda: ["javbus", "sukebei", "bt4g"])
    assert sources.route("FC2-PPV-4519894") == ["sukebei", "bt4g"]
    assert sources.route("SSIS-123") == ["javbus", "sukebei", "bt4g"]

def test_configured_ignores_unknown_names(monkeypatch):
    monkeypatch.setenv("SOURCES", "sukebei, bogus ,bt4g")
    assert sources.configured() == ["sukebei", "bt4g"]
    monkeypatch.setenv("SOURCES", "bogus")
    assert sources.configured() == ["javbus", "sukebei", "bt4g"], "全不认识就回默认"

# ---------------------------------------------------------------- 汇总

def test_search_returns_first_source_with_hits(monkeypatch):
    calls = []

    async def first(code, client):
        calls.append("javbus")
        return []

    async def second(code, client):
        calls.append("sukebei")
        return sources.parse_sukebei(SUKEBEI)

    async def third(code, client):
        calls.append("bt4g")
        return []

    monkeypatch.setitem(sources.REGISTRY, "javbus", first)
    monkeypatch.setitem(sources.REGISTRY, "sukebei", second)
    monkeypatch.setitem(sources.REGISTRY, "bt4g", third)
    monkeypatch.setattr(sources, "configured", lambda: ["javbus", "sukebei", "bt4g"])
    got = asyncio.run(sources.search("SSIS-123"))
    assert calls == ["javbus", "sukebei"], "拿到结果就停，别再打后面的源"
    assert got[0]["seeders"] == 21, "字幕 + 做种多者排前"

def test_search_survives_broken_source(monkeypatch):
    async def boom(code, client):
        raise httpx.ConnectError("tls eof")

    async def ok(code, client):
        return sources.parse_bt4g(BT4G)

    monkeypatch.setitem(sources.REGISTRY, "javbus", boom)
    monkeypatch.setitem(sources.REGISTRY, "bt4g", ok)
    monkeypatch.setattr(sources, "configured", lambda: ["javbus", "bt4g"])
    assert len(asyncio.run(sources.search("SSIS-123"))) == 1

def test_search_empty_when_all_sources_dry(monkeypatch):
    async def dry(code, client):
        return []
    for name in list(sources.REGISTRY):
        monkeypatch.setitem(sources.REGISTRY, name, dry)
    monkeypatch.setattr(sources, "configured", lambda: ["javbus", "sukebei", "bt4g"])
    assert asyncio.run(sources.search("XXX-000")) == []
    assert asyncio.run(sources.search("")) == []

def test_dedupe_by_infohash(monkeypatch):
    items = [
        {"magnet": "magnet:?xt=urn:btih:aa", "title": "a", "size_bytes": 1, "size_text": "",
         "has_subtitle": False, "is_hd": False, "seeders": 0, "source": "x"},
        {"magnet": "magnet:?xt=urn:btih:AA&dn=dup", "title": "b", "size_bytes": 2, "size_text": "",
         "has_subtitle": False, "is_hd": False, "seeders": 0, "source": "y"},
    ]
    assert len(sources.dedupe(items)) == 1, "同一 infoHash 不分大小写"
