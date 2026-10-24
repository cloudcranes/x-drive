"""多源合并搜索：候选池要合并、去重、排序，失败与慢源都不能影响结果。

对应方向一：以前 `search()` 是先到先得（第一个有结果的源即采用），
候选池只有一个源的大小。这里钉住合并语义：

  · 多源都有结果时候选被合并，不是只取第一个源的；
  · 合并后仍按 rank 排序（字幕 → 高清 → 做种 → 体积）；
  · 同一 infoHash 出现在两个源里只保留一条；
  · 一个源抛异常时其余源结果照常返回；
  · 全部源为空 → []；
  · 某个源慢/超时时不影响其他源的响应。

全部离线：三个源一律打桩，不连真实站点。
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app import sources


def _item(magnet: str, *, title: str = "x", size: int = 0, sub: bool = False,
          hd: bool = False, seeders: int = 0, source: str = "x") -> dict:
    return {"magnet": magnet, "title": title, "size_bytes": size, "size_text": "",
            "has_subtitle": sub, "is_hd": hd, "seeders": seeders, "source": source}


def _install(monkeypatch, mapping: dict[str, object]) -> list[str]:
    """把 REGISTRY 换成给定的桩，返回记录调用顺序的列表。"""
    calls: list[str] = []

    def make(name: str, result):
        async def fetch(code, client):
            calls.append(name)
            if isinstance(result, BaseException):
                raise result
            if callable(result):
                return await result(code, client)
            return result
        return fetch

    for name, result in mapping.items():
        monkeypatch.setitem(sources.REGISTRY, name, make(name, result))
    monkeypatch.setattr(sources, "configured", lambda: list(mapping))
    return calls


# ─────────────────────────────────────────── 合并

def test_candidates_from_all_sources_are_merged(monkeypatch):
    """多源都有结果时候选被合并，不是只取第一个源的。"""
    _install(monkeypatch, {
        "javbus": [_item("magnet:?xt=urn:btih:" + "a" * 40, source="javbus")],
        "sukebei": [_item("magnet:?xt=urn:btih:" + "b" * 40, source="sukebei")],
        "bt4g": [_item("magnet:?xt=urn:btih:" + "c" * 40, source="bt4g")],
    })

    got = asyncio.run(sources.search("SSIS-123"))

    assert len(got) == 3, f"三个源的候选都该在：{got}"
    assert {i["source"] for i in got} == {"javbus", "sukebei", "bt4g"}


def test_merged_result_is_ranked(monkeypatch):
    """合并后仍按 rank 排序：有种子 → 字幕 → 高清 → 做种 → 体积。

    「有种子」排在字幕前面是 2026-10-24 加的：0 种子的种子迅雷云盘会
    「解析失败」，选了就是白等两小时（实测 MIMK-267）。
    """
    plain = _item("magnet:?xt=urn:btih:" + "1" * 40, size=10 ** 10, seeders=99,
                  source="javbus")
    with_sub = _item("magnet:?xt=urn:btih:" + "2" * 40, sub=True, seeders=5,
                      source="sukebei")
    _install(monkeypatch, {"javbus": [plain], "sukebei": [with_sub]})

    got = asyncio.run(sources.search("SSIS-123"))

    # 两条都是活种，字幕仍然赢 —— 只有死种才排到最后
    assert got[0]["has_subtitle"] is True, "带字幕的应排前，哪怕体积小种子少"


def test_dead_seed_sorts_last_even_with_subtitle(monkeypatch):
    """0 种子的排最后 —— 哪怕带字幕、高清、体积更大（实测 MIMK-267 的坑）。"""
    dead = _item("magnet:?xt=urn:btih:" + "1" * 40, size=10 ** 10, seeders=0,
                 sub=True, hd=True, source="javbus")
    live = _item("magnet:?xt=urn:btih:" + "2" * 40, size=1, seeders=1,
                 source="sukebei")
    _install(monkeypatch, {"javbus": [dead], "sukebei": [live]})

    got = asyncio.run(sources.search("SSIS-123"))

    assert got[0]["seeders"] == 1, "有种子的应排前面"


def test_rank_prefers_seeders_within_same_flags(monkeypatch):
    """同样有字幕同样高清时，做种多的排前。"""
    few = _item("magnet:?xt=urn:btih:" + "3" * 40, sub=True, hd=True, seeders=2,
                source="sukebei")
    many = _item("magnet:?xt=urn:btih:" + "4" * 40, sub=True, hd=True, seeders=50,
                 source="bt4g")
    _install(monkeypatch, {"sukebei": [few], "bt4g": [many]})

    got = asyncio.run(sources.search("SSIS-123"))

    assert got[0]["seeders"] == 50


def test_limit_caps_merged_candidates(monkeypatch):
    """limit 语义不变：合并后取前 N 条。"""
    _install(monkeypatch, {
        "javbus": [_item(f"magnet:?xt=urn:btih:{i:040d}", source="javbus")
                   for i in range(5)],
        "sukebei": [_item(f"magnet:?xt=urn:btih:{i:040d}", source="sukebei")
                    for i in range(5, 10)],
    })

    got = asyncio.run(sources.search("SSIS-123", limit=3))

    assert len(got) == 3


# ─────────────────────────────────────────── 去重

def test_same_infohash_across_sources_kept_once(monkeypatch):
    """同一 infoHash 出现在两个源里只保留一条。"""
    same = "a066ceeb97cd20e847a8155ed5e2b91ff61ee557"
    _install(monkeypatch, {
        "javbus": [_item(f"magnet:?xt=urn:btih:{same}", source="javbus")],
        "sukebei": [_item(f"magnet:?xt=urn:btih:{same}", source="sukebei")],
    })

    got = asyncio.run(sources.search("SSIS-123"))

    assert len(got) == 1, f"同一 infoHash 出现两次：{got}"


def test_infohash_dedupe_is_case_insensitive(monkeypatch):
    """去重不分大小写。"""
    upper = "A066CEEB97CD20E847A8155ED5E2B91FF61EE557"
    lower = "a066ceeb97cd20e847a8155ed5e2b91ff61ee557"
    _install(monkeypatch, {
        "javbus": [_item(f"magnet:?xt=urn:btih:{upper}", source="javbus")],
        "sukebei": [_item(f"magnet:?xt=urn:btih:{lower}", source="sukebei")],
    })

    got = asyncio.run(sources.search("SSIS-123"))

    assert len(got) == 1


def test_dedupe_keeps_richer_fields_from_other_source(monkeypatch):
    """同一资源在多个源出现时，要保留信息更全的那份，不能被空字段盖掉。

    javbus 与 bt4g 都不给做种数（恒为 0），只有 sukebei 给。先到先留会让
    bt4g 那条的 seeders=0 覆盖掉真实做种数，排序时这条就被当成没人做种。
    """
    same = "a066ceeb97cd20e847a8155ed5e2b91ff61ee557"
    _install(monkeypatch, {
        "javbus": [_item(f"magnet:?xt=urn:btih:{same}", seeders=0, source="javbus")],
        "sukebei": [_item(f"magnet:?xt=urn:btih:{same}", seeders=42, source="sukebei")],
    })

    got = asyncio.run(sources.search("SSIS-123"))

    assert len(got) == 1
    assert got[0]["seeders"] == 42, f"真实做种数被空字段盖掉了：{got[0]}"


def test_dedupe_keeps_subtitle_flag_from_any_source(monkeypatch):
    """任一源说带字幕/高清，就按带字幕/高清算。"""
    same = "b066ceeb97cd20e847a8155ed5e2b91ff61ee557"
    _install(monkeypatch, {
        "javbus": [_item(f"magnet:?xt=urn:btih:{same}", sub=False, source="javbus")],
        "sukebei": [_item(f"magnet:?xt=urn:btih:{same}", sub=True, source="sukebei")],
    })

    got = asyncio.run(sources.search("SSIS-123"))

    assert len(got) == 1
    assert got[0]["has_subtitle"] is True, "带字幕的标记被丢了"


# ─────────────────────────────────────────── 容错

def test_one_broken_source_does_not_hide_others(monkeypatch):
    """一个源抛异常时其余源结果照常返回。"""
    _install(monkeypatch, {
        "javbus": httpx.ConnectError("tls eof"),
        "sukebei": [_item("magnet:?xt=urn:btih:" + "b" * 40, source="sukebei")],
        "bt4g": httpx.ReadTimeout("slow"),
    })

    got = asyncio.run(sources.search("SSIS-123"))

    assert len(got) == 1
    assert got[0]["source"] == "sukebei"


def test_all_sources_empty_returns_empty_list(monkeypatch):
    """全部源为空 → []。"""
    _install(monkeypatch, {"javbus": [], "sukebei": [], "bt4g": []})

    assert asyncio.run(sources.search("SSIS-123")) == []


def test_all_sources_broken_returns_empty_list(monkeypatch):
    """全部源都炸 → []，不抛。"""
    _install(monkeypatch, {
        "javbus": httpx.ConnectError("x"),
        "sukebei": httpx.ConnectError("y"),
        "bt4g": httpx.ConnectError("z"),
    })

    assert asyncio.run(sources.search("SSIS-123")) == []


def test_empty_code_returns_empty_list(monkeypatch):
    """空番号连源都不问。"""
    calls = _install(monkeypatch, {"javbus": []})

    assert asyncio.run(sources.search("")) == []
    assert calls == [], "空番号不该打任何源"


def test_slow_source_does_not_block_other_sources(monkeypatch):
    """慢源不能拖住整次搜索：预算用尽就采用已回来的结果。

    这是合并语义的硬要求。合并必然要等齐各源，但「等齐」不等于「无限等」：
    给一次搜索一个总预算，到点就用手上有的候选，其余源取消掉。
    慢源的候选因此可能缺席 —— 宁可少一条候选，也不能让一次搜索卡住。
    """

    async def slow(code, client):
        await asyncio.sleep(30)          # 远超搜索预算
        return [_item("magnet:?xt=urn:btih:" + "f" * 40, source="slow")]

    _install(monkeypatch, {
        "javbus": slow,
        "sukebei": [_item("magnet:?xt=urn:btih:" + "b" * 40, source="sukebei")],
    })
    monkeypatch.setattr(sources, "SEARCH_DEADLINE", 0.5)

    started = time.monotonic()
    got = asyncio.run(sources.search("SSIS-123"))
    elapsed = time.monotonic() - started

    assert [i["source"] for i in got] == ["sukebei"], "快源的候选必须照常返回"
    assert elapsed < 3, f"慢源拖住了整次搜索：{elapsed:.1f}s"


def test_slow_source_is_cancelled_not_left_running(monkeypatch):
    """预算用尽后，慢源要被取消掉，不能在后台继续挂着。"""

    cancelled = []

    async def slow(code, client):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return []

    _install(monkeypatch, {"javbus": slow, "sukebei": []})
    monkeypatch.setattr(sources, "SEARCH_DEADLINE", 0.3)

    asyncio.run(sources.search("SSIS-123"))

    assert cancelled == [True], "慢源没有被取消"


def test_sources_are_queried_concurrently(monkeypatch):
    """各源并发问：总耗时约等于最慢的那个源，不是各源耗时之和。

    串行的话三个源各 5 秒就是 15 秒；并发则约 5 秒。慢源把整次搜索拖长
    是并发的固有代价（合并语义要求等齐），但不该是**相加**。
    """

    async def slow_a(code, client):
        await asyncio.sleep(0.5)
        return [_item("magnet:?xt=urn:btih:" + "a" * 40, source="a")]

    async def slow_b(code, client):
        await asyncio.sleep(0.5)
        return [_item("magnet:?xt=urn:btih:" + "b" * 40, source="b")]

    async def slow_c(code, client):
        await asyncio.sleep(0.5)
        return [_item("magnet:?xt=urn:btih:" + "c" * 40, source="c")]

    _install(monkeypatch, {"javbus": slow_a, "sukebei": slow_b, "bt4g": slow_c})

    started = time.monotonic()
    got = asyncio.run(sources.search("SSIS-123"))
    elapsed = time.monotonic() - started

    assert len(got) == 3
    assert elapsed < 1.2, \
        f"三个各 0.5 秒的源串行要 1.5 秒，实际 {elapsed:.2f}s —— 没并发"


def test_unknown_source_name_is_skipped(monkeypatch):
    """REGISTRY 里没有的名字直接跳过，不报错。"""
    monkeypatch.setattr(sources, "configured", lambda: ["bogus", "javbus"])
    monkeypatch.setitem(sources.REGISTRY, "javbus",
                        lambda code, client: _async([]))

    assert asyncio.run(sources.search("SSIS-123")) == []


def _async(value):
    async def run(*a, **k):
        return value
    return run


# ─────────────────────────────────────────── 客户端归属

def test_provided_client_is_not_closed(monkeypatch):
    """传进来的 client 归调用方所有，search 不能关它。"""
    closed = []

    class FakeClient:
        async def aclose(self):
            closed.append(True)

    _install(monkeypatch, {"javbus": [_item("magnet:?xt=urn:btih:" + "a" * 40)]})

    asyncio.run(sources.search("SSIS-123", client=FakeClient()))

    assert closed == [], "外部传入的 client 不该被关掉"
