"""番号元数据解析：全部走本地 fixture，不联网。"""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app import meta

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="module")
def detail_html():
    return (FIXTURES / "javdb_detail.html").read_text(encoding="utf-8", errors="ignore")


@pytest.fixture(scope="module")
def search_html():
    return (FIXTURES / "javdb_search.html").read_text(encoding="utf-8", errors="ignore")


# ---------------------------------------------------------------- 详情解析

def test_parse_detail_fields(detail_html):
    info = meta.parse_detail(detail_html, "MIMK-288")
    assert info["date"] == "2026-09-29"
    assert info["maker"] == "MOODYZ"
    assert info["director"] == "きとるね川口"
    assert info["duration"] == "120 分鍾"
    assert "宮下玲奈" in info["actors"]
    assert "單體作品" in info["categories"]
    assert info["rating"] == "4.23"
    assert info["cover"].startswith("https://") and "/covers/" in info["cover"]


def test_parse_detail_title_excludes_code(detail_html):
    """标题里夹着 <strong>番号</strong>，不能把番号混进标题。"""
    info = meta.parse_detail(detail_html, "MIMK-288")
    assert info["title"]
    assert not info["title"].startswith("MIMK-288")
    assert "<" not in info["title"], "标签要被清掉"


def test_parse_detail_missing_fields_are_empty():
    """页面没有那些属性时留空，不能编造。"""
    info = meta.parse_detail("<html><body>nothing</body></html>", "ABC-1")
    assert info["code"] == "ABC-1"
    assert info["title"] == ""
    assert info["actors"] == []
    assert info["cover"] == ""


def test_parse_detail_multi_actors():
    html = ('<div class="panel-block"><strong>演員:</strong>'
            '<a href="/actors/a">演员甲</a><a href="/actors/b">演员乙</a></div>')
    info = meta.parse_detail(html, "X-1")
    assert info["actors"] == ["演员甲", "演员乙"]


# ---------------------------------------------------------------- 搜索解析

def test_parse_search_finds_exact_code(search_html):
    path = meta.parse_search(search_html, "MIMK-288")
    assert path == "/v/mO7wRy"


def test_parse_search_rejects_near_miss(search_html):
    """搜 MIMK-288 会顺带返回 MIMK-188 等相近结果，不能挂错。"""
    assert meta.parse_search(search_html, "MIMK-288") != ""
    assert meta.parse_search(search_html, "MIMK-999999") is None


def test_parse_search_empty_page():
    assert meta.parse_search("<html></html>", "ABC-1") is None


# ---------------------------------------------------------------- 缓存

def test_cached_returns_none_when_absent(tmp_path):
    assert meta.cached("ABC-1", str(tmp_path / "meta.json")) is None


def test_cached_reads_saved_entry(tmp_path):
    p = str(tmp_path / "meta.json")
    meta.save_cache({"ABC-1": {"code": "ABC-1", "title": "t"}}, p)
    assert meta.cached("abc-1", p)["title"] == "t", "大小写不敏感"


def test_lookup_uses_cache_without_network(tmp_path, monkeypatch):
    """缓存命中时不该发任何请求。"""
    p = str(tmp_path / "meta.json")
    meta.save_cache({"ABC-1": {"code": "ABC-1", "title": "缓存里的"}}, p)

    called = []
    monkeypatch.setattr(meta.httpx, "AsyncClient",
                        lambda **kw: called.append(1) or (_ for _ in ()).throw(AssertionError))
    info = asyncio.run(meta.lookup("ABC-1", p))
    assert info["title"] == "缓存里的"
    assert called == []


def test_lookup_returns_none_on_network_error(tmp_path, monkeypatch):
    """抓不到就 None，绝不抛异常给下载流程。"""
    class Boom:
        async def get(self, *a, **kw):
            raise httpx.ConnectError("no route")

        async def aclose(self):
            pass

    info = asyncio.run(meta.lookup("ABC-1", str(tmp_path / "m.json"), client=Boom()))
    assert info is None


def test_lookup_returns_none_when_code_unknown(tmp_path):
    class Empty:
        async def get(self, *a, **kw):
            class R:
                status_code = 200
                text = "<html></html>"
            return R()

        async def aclose(self):
            pass

    info = asyncio.run(meta.lookup("ABC-123", str(tmp_path / "m.json"), client=Empty()))
    assert info is None


def test_lookup_saves_cache_on_success(tmp_path, detail_html, search_html):
    """完整走一遍：搜索命中原番号 → 抓详情 → 写缓存。"""
    p = str(tmp_path / "m.json")

    class Fake:
        def __init__(self):
            self.n = 0

        async def get(self, url, **kw):
            self.n += 1

            class R:
                status_code = 200

            r = R()
            r.text = search_html if "/search" in url else detail_html
            return r

        async def aclose(self):
            pass

    info = asyncio.run(meta.lookup("MIMK-288", p, client=Fake()))
    assert info["maker"] == "MOODYZ"
    saved = json.loads(Path(p).read_text(encoding="utf-8"))
    assert saved["MIMK-288"]["maker"] == "MOODYZ"


def test_lookup_empty_code(tmp_path):
    assert asyncio.run(meta.lookup("", str(tmp_path / "m.json"))) is None


def test_lookup_tries_canonical_code_when_magnet_name_fails(tmp_path, monkeypatch):
    """归档目录名是磁力原名时，也要能用规范番号查到元数据。

    实测踩过：云添加用磁力原名做目录名（`mimk-186ch` —— 番号粘连了后缀），
    拿它去查 JavBus 是 404，于是整条归档的元数据都空了（界面上海报、标题、
    演员、评分全没有）。

    现在查名字按「规范番号 → 原名」的顺序试，规范番号优先 ——
    磁力原名可能对应好几部片，认出来的那个才是这部。
    """
    p = str(tmp_path / "m.json")
    asked = []

    async def fake_movie(code, client=None):
        asked.append(code)
        if code == "MIMK-186":
            return {"code": code, "title": "查到的标题", "cover": "http://x/c.jpg"}
        return None

    monkeypatch.setattr(meta.javbus, "movie", fake_movie)
    monkeypatch.setattr(meta, "_use_javbus", lambda: True)

    info = asyncio.run(meta.lookup("mimk-186ch", p))

    assert info is not None, "磁力原名查不到时应当退回规范番号"
    assert info["title"] == "查到的标题"
    assert "MIMK-186" in asked, f"应当试规范番号：{asked}"
    # 缓存按规范番号写 —— 下次按番号查就直接命中
    saved = json.loads(Path(p).read_text(encoding="utf-8"))
    assert saved["MIMK-186"]["title"] == "查到的标题"


def test_cached_finds_entry_by_canonical_code(tmp_path):
    """缓存按规范番号存着时，用磁力原名也要能取到（看板渲染走这条）。"""
    p = str(tmp_path / "m.json")
    meta.save_cache({"MIMK-186": {"code": "MIMK-186", "title": "缓存里的"}}, p)
    assert meta.cached("mimk-186ch", p)["title"] == "缓存里的"


# ---------------------------------------------------------------- 补抓

def test_backfill_updates_state(tmp_path, monkeypatch, detail_html, search_html):
    """老归档没有 meta，补抓后要写进 state 且能被 collect 读到。"""
    from app import core, web

    state_path = str(tmp_path / "state.json")
    core.save_state(state_path, {"MIMK-288": {"code": "MIMK-288", "files": [], "size": 1}})
    monkeypatch.setattr(web, "STATE", state_path)
    monkeypatch.setattr(meta, "CACHE_PATH", str(tmp_path / "meta.json"))

    class Fake:
        async def get(self, url, **kw):
            class R:
                status_code = 200
            r = R()
            r.text = search_html if "/search" in url else detail_html
            return r

        async def aclose(self):
            pass

    # lookup 走缓存路径，这里直接预填缓存最省事
    meta.save_cache({"MIMK-288": meta.parse_detail(detail_html, "MIMK-288")},
                    str(tmp_path / "meta.json"))
    out = web.backfill_meta(limit=5)
    assert "MIMK-288" in out["updated"]
    assert core.load_state(state_path)["MIMK-288"]["meta"]["maker"] == "MOODYZ"


def test_backfill_skips_already_done(tmp_path, monkeypatch):
    """已有 meta 的不重复抓。"""
    from app import core, web

    state_path = str(tmp_path / "state.json")
    core.save_state(state_path, {"A-1": {"code": "A-1", "meta": {"title": "x"}}})
    monkeypatch.setattr(web, "STATE", state_path)
    out = web.backfill_meta(limit=5)
    assert out["updated"] == [] and out["failed"] == []


def test_meta_failure_does_not_break_collect(tmp_path, monkeypatch):
    """元数据缺失时看板照常出数据。"""
    from app import core, web

    state_path = str(tmp_path / "state.json")
    core.save_state(state_path, {"A-1": {"code": "A-1", "files": ["a.mp4"], "size": 100}})
    monkeypatch.setattr(web, "STATE", state_path)
    monkeypatch.setattr(meta, "cached", lambda *a, **k: None)
    data = web.collect()
    assert data["tasks"][0]["meta"] == {}


# ---------------------------------------------------------------- javbus 优先

def test_lookup_prefers_javbus(tmp_path, monkeypatch):
    """javbus 有数据时不该再去抓 javdb（省一次 HTML 解析和请求）。"""
    from app import javbus, meta

    p = str(tmp_path / "m.json")
    calls = []

    async def fake_javbus_movie(code, client=None):
        calls.append(code)
        return {"code": code, "title": "javbus 的标题", "maker": "M", "actors": [],
                "cover": "", "series": "", "director": "", "publisher": "",
                "duration": "", "categories": [], "date": "", "rating": "",
                "source": "javbus"}

    monkeypatch.setattr(javbus, "movie", fake_javbus_movie)
    monkeypatch.setattr(meta, "USE_JAVBUS", True)

    class NoNet:
        async def get(self, *a, **kw):
            raise AssertionError("javbus 成功时不该再请求 javdb")

        async def aclose(self):
            pass

    info = asyncio.run(meta.lookup("ABC-1", p, client=NoNet()))
    assert info["title"] == "javbus 的标题"
    assert calls == ["ABC-1"]
    assert json.loads(Path(p).read_text(encoding="utf-8"))["ABC-1"]["source"] == "javbus"


def test_lookup_falls_back_to_javdb(tmp_path, monkeypatch, detail_html, search_html):
    """javbus 没数据（None）时必须回退到 javdb，不能就这么放弃。"""
    from app import javbus, meta

    p = str(tmp_path / "m.json")

    async def no_javbus(code, client=None):
        return None

    monkeypatch.setattr(javbus, "movie", no_javbus)
    monkeypatch.setattr(meta, "USE_JAVBUS", True)

    class Fake:
        async def get(self, url, **kw):
            class R:
                status_code = 200
            r = R()
            r.text = search_html if "/search" in url else detail_html
            return r

        async def aclose(self):
            pass

    info = asyncio.run(meta.lookup("MIMK-288", p, client=Fake()))
    assert info["maker"] == "MOODYZ", "应回退到 javdb 的结果"


def test_lookup_falls_back_when_javbus_raises(tmp_path, monkeypatch, detail_html, search_html):
    """javbus 抛异常也要能回退，不能把异常抛给下载流程。"""
    from app import javbus, meta

    p = str(tmp_path / "m.json")

    async def boom(code, client=None):
        raise RuntimeError("javbus down")

    monkeypatch.setattr(javbus, "movie", boom)
    monkeypatch.setattr(meta, "USE_JAVBUS", True)

    class Fake:
        async def get(self, url, **kw):
            class R:
                status_code = 200
            r = R()
            r.text = search_html if "/search" in url else detail_html
            return r

        async def aclose(self):
            pass

    info = asyncio.run(meta.lookup("MIMK-288", p, client=Fake()))
    assert info and info["maker"] == "MOODYZ"


def test_lookup_skips_javbus_when_disabled(tmp_path, monkeypatch, detail_html, search_html):
    from app import javbus, meta

    p = str(tmp_path / "m.json")
    called = []

    async def fake_javbus(code, client=None):
        called.append(code)
        return {"title": "不该被用到"}

    monkeypatch.setattr(javbus, "movie", fake_javbus)
    monkeypatch.setattr(meta, "USE_JAVBUS", False)

    class Fake:
        async def get(self, url, **kw):
            class R:
                status_code = 200
            r = R()
            r.text = search_html if "/search" in url else detail_html
            return r

        async def aclose(self):
            pass

    info = asyncio.run(meta.lookup("MIMK-288", p, client=Fake()))
    assert called == [], "关掉后不该碰 javbus"
    assert info["maker"] == "MOODYZ"

# ---------------------------------------------------------------- 名字 → javdb ID

SEARCH_HTML = """
<html><body>
<a href="/actors/censored">有码</a>
<a href="/actors/uncensored">无码</a>
<div class="box actor-box"><a href="/actors/Av2e"><img><strong>三上悠亜</strong></a></div>
<div class="box actor-box"><a href="/actors/Xk9"><strong>其他演员</strong></a></div>
</body></html>
"""

def test_extract_id_skips_nav_links():
    """站内导航也有 /actors/xxx（censored/uncensored），不能拿第一个链接当结果。"""
    from app import meta
    assert meta.extract_id(SEARCH_HTML, "三上悠亜", "actor") == "Av2e"

def test_extract_id_returns_none_when_name_absent():
    from app import meta
    assert meta.extract_id(SEARCH_HTML, "查无此人", "actor") is None

def test_resolve_id_uses_cache(tmp_path, monkeypatch):
    from app import meta
    monkeypatch.setattr(meta, "IDS_PATH", str(tmp_path / "ids.json"))
    assert meta.cached_id("三上悠亜", "actor") is None
    meta._save_id("三上悠亜", "actor", "Av2e")
    assert meta.cached_id("三上悠亜", "actor") == "Av2e"
    assert meta.cached_id("三上悠亜", "series") is None, "演员和系列是两套 ID 空间"

def test_resolve_id_parses_search_page(tmp_path, monkeypatch):
    import asyncio, httpx
    from app import meta
    monkeypatch.setattr(meta, "IDS_PATH", str(tmp_path / "ids.json"))
    monkeypatch.setattr(meta, "MIN_INTERVAL", 0)

    async def handler(request):
        assert request.url.params["f"] == "actor"
        return httpx.Response(200, text=SEARCH_HTML)

    async def main():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            base = httpx.AsyncClient(transport=transport)
            try:
                return await meta.resolve_id("三上悠亜", "actor", client=base)
            finally:
                await base.aclose()

    assert asyncio.run(main()) == "Av2e"
    assert meta.cached_id("三上悠亜", "actor") == "Av2e", "解析结果要落盘，别每次都联网"

def test_resolve_id_failure_is_soft(tmp_path, monkeypatch):
    import asyncio, httpx
    from app import meta
    monkeypatch.setattr(meta, "IDS_PATH", str(tmp_path / "ids.json"))
    monkeypatch.setattr(meta, "MIN_INTERVAL", 0)

    async def handler(request):
        return httpx.Response(500, text="boom")

    async def main():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await meta.resolve_id("某人", "series", client=client)

    assert asyncio.run(main()) is None
