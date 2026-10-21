"""javbus API 客户端的规范化与挑选逻辑：全部走本地 fixture，不联网。"""
import asyncio
import json
from pathlib import Path

import pytest

from app import javbus

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "javbus_api.json"


@pytest.fixture(scope="module")
def fx():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 详情

def test_normalize_movie_fields(fx):
    info = javbus.normalize_movie(fx["detail"])
    assert info["code"] == "MIMK-288"
    assert info["maker"] == "ムーディーズ"
    assert info["director"] == "きとるね川口"
    assert info["publisher"] == "まんきつ"
    assert "宮下玲奈" in info["actors"]
    assert "單體作品" in info["categories"]
    assert info["duration"] == "121 分钟"
    assert info["cover"].startswith("https://")
    assert info["source"] == "javbus"


def test_normalize_movie_keeps_gid_uc(fx):
    """磁力要用 gid/uc，丢了就查不到磁力。"""
    info = javbus.normalize_movie(fx["detail"])
    assert info["gid"] and info["uc"] != ""


def test_normalize_movie_missing_fields():
    info = javbus.normalize_movie({"id": "ABC-1"})
    assert info["code"] == "ABC-1"
    assert info["title"] == ""
    assert info["actors"] == []
    assert info["cover"] == ""
    assert info["duration"] == ""


def test_normalize_movie_series_null():
    """系列字段是 null 时不能写成 "None"。"""
    info = javbus.normalize_movie({"id": "X-1", "series": None})
    assert info["series"] == ""


# ---------------------------------------------------------------- 磁力

def test_normalize_magnet_fields(fx):
    items = [javbus.normalize_magnet(m) for m in fx["magnets"]]
    items = [m for m in items if m]
    assert len(items) >= 10
    first = items[0]
    assert first["magnet"].startswith("magnet:?xt=urn:btih:")
    assert first["size_bytes"] > 0
    assert isinstance(first["is_hd"], bool)
    assert isinstance(first["has_subtitle"], bool)


def test_normalize_magnet_rejects_non_magnet():
    assert javbus.normalize_magnet({"link": "http://x", "numberSize": 1}) is None
    assert javbus.normalize_magnet({}) is None


def test_magnets_have_exact_byte_size(fx):
    """numberSize 是精确字节数，用来判体积上限比 "5.43GB" 这种文本可靠。"""
    items = [javbus.normalize_magnet(m) for m in fx["magnets"]]
    sizes = [m["size_bytes"] for m in items if m]
    assert all(isinstance(s, int) and s > 0 for s in sizes)


def test_fixture_contains_known_subtitle_magnet(fx):
    """实测确认 mimk-288ch 带中文字幕，标记必须准确。"""
    items = [javbus.normalize_magnet(m) for m in fx["magnets"]]
    ch = [m for m in items if m and "mimk-288ch" in m["magnet"].lower()]
    assert ch, "fixture 里应有 mimk-288ch"
    assert ch[0]["has_subtitle"] is True


def test_pick_magnet_prefers_subtitle(fx):
    items = [javbus.normalize_magnet(m) for m in fx["magnets"]]
    items = [m for m in items if m]
    best = javbus.pick_magnet(items)
    assert best["has_subtitle"] is True, "有字幕的必须优先"


def test_pick_magnet_prefers_hd_within_subtitle():
    items = [
        {"magnet": "a", "size_bytes": 900, "is_hd": False, "has_subtitle": True},
        {"magnet": "b", "size_bytes": 800, "is_hd": True, "has_subtitle": True},
    ]
    assert javbus.pick_magnet(items)["magnet"] == "b"


def test_pick_magnet_falls_back_to_largest():
    items = [
        {"magnet": "small", "size_bytes": 100, "is_hd": True, "has_subtitle": False},
        {"magnet": "big", "size_bytes": 5000, "is_hd": True, "has_subtitle": False},
    ]
    assert javbus.pick_magnet(items)["magnet"] == "big"


def test_pick_magnet_can_ignore_subtitle():
    items = [
        {"magnet": "sub", "size_bytes": 100, "is_hd": False, "has_subtitle": True},
        {"magnet": "hd", "size_bytes": 900, "is_hd": True, "has_subtitle": False},
    ]
    assert javbus.pick_magnet(items, prefer_subtitle=False)["magnet"] == "hd"


def test_pick_magnet_empty():
    assert javbus.pick_magnet([]) is None


# ---------------------------------------------------------------- 列表

def test_parse_movies(fx):
    items = javbus.parse_movies(fx["star"])
    assert len(items) >= 10
    assert items[0]["code"].startswith(("MIDA", "MIZD", "MIAA"))
    assert len(items[0]["date"]) == 10
    assert isinstance(items[0]["tags"], list)


def test_parse_movies_skips_junk():
    assert javbus.parse_movies(None) == []
    assert javbus.parse_movies({"movies": []}) == []
    assert javbus.parse_movies({"movies": [{}, {"id": ""}]}) == []


def test_parse_movies_series(fx):
    items = javbus.parse_movies(fx["series"])
    assert items and all(i["code"] for i in items)


# ---------------------------------------------------------------- 网络失败

def test_movie_returns_none_on_error():
    class Boom:
        async def get(self, *a, **kw):
            raise OSError("no net")

        async def aclose(self):
            pass

    assert asyncio.run(javbus.movie("MIMK-288", client=Boom())) is None


def test_magnets_without_gid_returns_empty():
    """缺 gid/uc 直接返回空，不发请求（实测接口会 400）。"""
    assert asyncio.run(javbus.magnets("MIMK-288", "", "")) == []
    assert asyncio.run(javbus.magnets("MIMK-288", "123", "")) == []


def test_list_movies_requires_filter():
    assert asyncio.run(javbus.list_movies("", "")) == []


def test_magnets_for_uses_detail_gid(fx):
    """两段式：先详情取 gid/uc，再查磁力。URL 拼错就一条都拿不到。"""
    seen = []

    class Fake:
        async def get(self, url, **kw):
            seen.append(url)

            class R:
                status_code = 200
                def __init__(self, payload):
                    self._p = payload
                def json(self):
                    return self._p

            if "/api/magnets/" in url:
                assert "gid=" in url and "uc=" in url, url
                return R(fx["magnets"])
            return R(fx["detail"])

        async def aclose(self):
            pass

    items = asyncio.run(javbus.magnets_for("MIMK-288", client=Fake()))
    assert len(items) >= 10
    assert len(seen) == 2, "应该正好两次请求：详情 + 磁力"
