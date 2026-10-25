"""从真实源适配器到合并/排序的三态回归；全部离线。"""
from __future__ import annotations

import asyncio
import itertools

import httpx
import pytest

from app import sources, subscribe


HASH = "a" * 40
MAGNET = f"magnet:?xt=urn:btih:{HASH}"


def rss(seed_tag=""):
    return (f"<rss><item><title>DEMO-1 1080p</title><link>{MAGNET}</link>"
            f"<description>1 GB</description><nyaa:infoHash>{HASH}</nyaa:infoHash>"
            f"{seed_tag}</item></rss>")


@pytest.mark.parametrize("value,want", [
    (None, None), (0, 0), ("0", 0), (" 12 ", 12), (7, 7),
    ("", None), ("unknown", None), (-1, None), (False, None),
    (True, None), (0.0, 0), (5.0, 5), (0.5, None),
    (float("nan"), None), (float("inf"), None),
])
def test_seeder_count_preserves_only_known_nonnegative_integers(value, want):
    assert sources.seeder_count({"seeders": value}) == want


def test_javbus_adapter_reports_unknown(monkeypatch):
    async def fake(*args, **kwargs):
        return [{"magnet": MAGNET, "title": "DEMO-1"}]
    monkeypatch.setattr(sources.javbus, "search_magnets", fake)
    rows = asyncio.run(sources.fetch_javbus("DEMO-1", None))
    assert rows[0]["seeders"] is None
    assert sources.rank(rows[0])[0] == 0


def test_bt4g_parser_reports_unknown():
    rows = sources.parse_bt4g(rss())
    assert rows[0]["seeders"] is None
    assert sources.rank(rows[0])[0] == 0


@pytest.mark.parametrize("tag,want", [
    ("", None), ("<nyaa:seeders>0</nyaa:seeders>", 0),
    ("<nyaa:seeders>4</nyaa:seeders>", 4),
])
def test_sukebei_parser_preserves_missing_and_explicit_zero(tag, want):
    row = sources.parse_sukebei(rss(tag))[0]
    assert row["seeders"] == want
    assert sources.rank(row)[0] == int(want == 0)


@pytest.mark.parametrize("first,second,want", [
    (None, 0, 0), (0, None, 0), (None, 5, 5), (5, None, 5),
    (0, 5, 5), (5, 0, 5), (3, 7, 7), (7, 3, 7),
])
def test_merge_keeps_known_counts_regardless_of_source_order(first, second, want):
    rows = [{"magnet": MAGNET, "seeders": first},
            {"magnet": MAGNET, "seeders": second}]
    merged = sources.dedupe(rows)
    assert len(merged) == 1
    assert merged[0]["seeders"] == want


def test_merge_of_missing_and_null_remains_unknown():
    for rows in itertools.permutations([
        {"magnet": MAGNET}, {"magnet": MAGNET, "seeders": None},
    ]):
        assert sources.dedupe(rows)[0]["seeders"] is None


def test_subscription_preserves_null_and_uses_shared_rank():
    unknown = {"magnet": MAGNET, "seeders": None, "has_subtitle": True}
    dead = {"magnet": "magnet:?xt=urn:btih:" + "b" * 40, "seeders": 0,
            "has_subtitle": True}
    assert subscribe.pick_candidate([dead, unknown]) is unknown
    chosen, error = subscribe._choose([dead, unknown], "DEMO-1", 0, None)
    assert not error
    assert chosen["magnet"] == MAGNET
    assert chosen["seeders"] is None


def test_real_adapters_feed_search_without_unknown_becoming_dead(monkeypatch):
    """真实 javbus 适配器/两种 RSS 解析经 search 合并后仍遵守三态。"""
    async def fake_javbus(*args, **kwargs):
        return [{"magnet": MAGNET, "title": "DEMO-1"}]
    monkeypatch.setattr(sources.javbus, "search_magnets", fake_javbus)
    monkeypatch.setattr(sources, "configured", lambda: ["javbus", "sukebei", "bt4g"])

    dead_hash = "b" * 40
    unknown_hash = "c" * 40

    def response(request):
        if "sukebei" in request.url.host:
            return httpx.Response(200, text=rss("<nyaa:seeders>0</nyaa:seeders>")
                                  .replace(HASH, dead_hash))
        return httpx.Response(200, text=rss().replace(HASH, unknown_hash))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            return await sources.search("DEMO-1", client=client)

    got = asyncio.run(run())
    assert len(got) == 3
    assert [row["seeders"] for row in got] == [None, None, 0]
    assert all(sources.rank(row)[0] == 0 for row in got[:2])
    assert sources.rank(got[-1])[0] == 1
