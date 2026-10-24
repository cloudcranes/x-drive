"""javdb 首页/搜索页的 HTML 解析：条目结构、缺字段、翻页标记。

javdb 没有公开 JSON 接口，媒体墙要用它就只能解析 HTML —— 所以这里把真实
页面结构钉成 fixture，版面一变测试就会红，不会静默变成「媒体墙空了」。
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app import javdbwall

FIXTURE = Path(__file__).parent / "fixtures" / "javdb_home.html"


def html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


# ─────────────────────────────────────────── 解析

def test_parse_items():
    items = javdbwall.parse_list(html())
    assert [i["code"] for i in items] == ["SIMM-912", "CAWD-706"], "缺封面的条目要跳过"
    first = items[0]
    assert first["title"] == "ひより"
    assert first["cover"] == "https://c0.jdbstatic.com/covers/aq/Aq2VRO.jpg"
    assert first["date"] == "2026-10-03"
    assert first["url"].endswith("/v/Aq2VRO")


def test_parse_cover_after_cnsub_badge():
    """`<div class="cover">` 里可能先有「中字可播放」的 span，封面不能因此丢掉。"""
    items = javdbwall.parse_list(html())
    second = items[1]
    assert second["code"] == "CAWD-706"
    assert second["cover"].endswith("GZZw9J.jpg")


def test_parse_rating_becomes_first_tag():
    first = javdbwall.parse_list(html())[0]
    assert first["tags"][0] == "★4.29"
    assert "含磁鏈" in first["tags"]


def test_parse_skips_rows_without_cover():
    codes = [i["code"] for i in javdbwall.parse_list(html())]
    assert "XXX-001" not in codes, "缺封面的条目摆出来就是个空格子"


def test_parse_empty_html():
    assert javdbwall.parse_list("") == []
    assert javdbwall.parse_list("<html>确认页</html>") == []


def test_parse_limit():
    assert len(javdbwall.parse_list(html(), limit=1)) == 1


# ─────────────────────────────────────────── 请求

class FakeResp:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status


def test_search_parses_results():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["cookies"] = request.headers.get("cookie", "")
        return httpx.Response(200, text=html())

    # 注入的 client 由调用方建：Cookie 得跟着它一起设（生产路径也是这样传的）
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                               cookies=javdbwall.COOKIES)
    items = __import__("asyncio").run(javdbwall.search("MIMK", client=client))
    assert [i["code"] for i in items] == ["SIMM-912", "CAWD-706"]
    assert "q=MIMK" in seen["url"]
    assert "over18" in seen["cookies"], "没有 18 岁确认 Cookie 会拿到确认页"


def test_search_handles_http_error():
    def handler(_request):
        return httpx.Response(503, text="")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert __import__("asyncio").run(javdbwall.search("MIMK", client=client)) == []


def test_search_empty_keyword():
    assert __import__("asyncio").run(javdbwall.search("  ")) == []
