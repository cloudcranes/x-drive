"""订阅/心愿单的自动下载也要能用上多个搜索源。

背景：机器自动下载原先只问 javbus 一个源，而人手动 /find 走的是合并多源的
sources.search。于是「JavBus 没收录、但 sukebei 有」的番号：手动搜得到，
订阅却永远下不来 —— 用户只会以为订阅坏了。

这里锁住 fetch_best 的契约：
  · 多源候选能被选中（核心场景）
  · 体积上限仍然过滤，且说明讲得清
  · tried（已试过的死种）仍然跳过
  · 三种说明文案（还没有磁力 / 都超过上限 / 都试过了）继续成立
  · 排序口径：字幕 → 高清 → 做种数 → 体积

不打桩真实站点：sources.search 被整段替换成桩，只喂它该返回的候选。
"""
from __future__ import annotations

import asyncio

import pytest

from app import sources, subscribe


def cand(magnet, size_gb=1.0, sub=False, hd=False, seeders=0, source="javbus",
         title=""):
    """一条候选，字段与 sources.search 的返回保持一致。"""
    return {"magnet": magnet, "title": title,
            "size_bytes": int(size_gb * 1024 ** 3), "size_text": f"{size_gb}GB",
            "has_subtitle": sub, "is_hd": hd, "seeders": seeders,
            "source": source}


@pytest.fixture
def fake_search(monkeypatch):
    """把候选来源换成桩：按番号返回候选。

    两个入口都打桩：
      · sources.search —— 新实现走多源合并；
      · javbus.magnets_for —— 旧实现只问这个源。
    两者喂**同一份**候选，所以旧代码上用例会得到真实断言失败（而不是打桩报错），
    新代码上则通过；测试因此既能证伪旧行为，也能守住新行为。
    """
    state = {"items": {}, "calls": []}

    async def search(code, client=None, limit=8):
        state["calls"].append({"code": code, "limit": limit})
        return list(state["items"].get(code, []))

    async def magnets_for(code, client=None):
        state["calls"].append({"code": code, "limit": None})
        return list(state["items"].get(code, []))

    monkeypatch.setattr(sources, "search", search)
    monkeypatch.setattr(subscribe.javbus, "magnets_for", magnets_for)
    return state


# ─────────────────────────────────────────── 核心场景：多源候选

def test_picks_candidate_from_non_javbus_source(fake_search):
    """JavBus 没有、sukebei 有：订阅流程也要能选中它。"""
    fake_search["items"]["SSIS-123"] = [
        cand("magnet:?xt=urn:btih:sukebei01", size_gb=2, source="sukebei")]

    item, why = asyncio.run(subscribe.fetch_best("SSIS-123"))

    assert item is not None, why
    assert item["magnet"] == "magnet:?xt=urn:btih:sukebei01"
    assert item["source"] == "sukebei", "候选来源要带出来，便于排查"


def test_search_actually_goes_through_multi_source_registry(fake_search):
    """候选必须来自 sources.search（多源合并），而不是单问一个源。

    两条桩都会记账，所以要看的是**走了哪一条**：旧实现只问 javbus.magnets_for，
    这里的 limit 就是 None；新实现问 sources.search，limit 是候选池大小。
    """
    fake_search["items"]["SSIS-123"] = [
        cand("magnet:?xt=urn:btih:aa", source="javbus")]

    asyncio.run(subscribe.fetch_best("SSIS-123"))

    limits = [c["limit"] for c in fake_search["calls"]]
    assert any(l is not None for l in limits), \
        f"fetch_best 没走 sources.search（多源合并），只问了单源：{fake_search['calls']}"


def test_no_magnets_gives_same_message(fake_search):
    """没有任何源有磁力：说明仍是「还没有磁力」。"""
    item, why = asyncio.run(subscribe.fetch_best("SSIS-123"))

    assert item is None
    assert why == "还没有磁力"


# ─────────────────────────────────────────── 体积上限仍然有效

def test_max_size_gb_filters_and_explains(fake_search):
    """超限候选被排除，说明要讲清「N 条候选都超过上限」。"""
    fake_search["items"]["SSIS-123"] = [
        cand("magnet:?xt=urn:btih:big1", size_gb=8),
        cand("magnet:?xt=urn:btih:big2", size_gb=9)]

    item, why = asyncio.run(subscribe.fetch_best("SSIS-123", max_size_gb=2))

    assert item is None
    assert why == "2 条候选都超过上限 2GB"


def test_max_size_gb_keeps_compliant_candidate(fake_search):
    """超限的剔掉、合规的留下 —— 不能因为首选超限就整部放弃。"""
    fake_search["items"]["SSIS-123"] = [
        cand("magnet:?xt=urn:btih:big", size_gb=8),
        cand("magnet:?xt=urn:btih:ok", size_gb=1)]

    item, why = asyncio.run(subscribe.fetch_best("SSIS-123", max_size_gb=2))

    assert item is not None, why
    assert item["magnet"] == "magnet:?xt=urn:btih:ok"


# ─────────────────────────────────────────── tried 机制仍然有效

def test_tried_magnets_are_skipped(fake_search):
    """已试过并失败的磁力要跳过，换下一条。"""
    fake_search["items"]["SSIS-123"] = [
        cand("magnet:?xt=urn:btih:dead", size_gb=1),
        cand("magnet:?xt=urn:btih:alive", size_gb=1)]

    item, why = asyncio.run(
        subscribe.fetch_best("SSIS-123", tried=["magnet:?xt=urn:btih:dead"]))

    assert item is not None, why
    assert item["magnet"] == "magnet:?xt=urn:btih:alive"


def test_all_tried_gives_same_message(fake_search):
    """全都试过了：说明仍是「候选都试过了」。"""
    fake_search["items"]["SSIS-123"] = [
        cand("magnet:?xt=urn:btih:dead1"), cand("magnet:?xt=urn:btih:dead2")]

    item, why = asyncio.run(
        subscribe.fetch_best("SSIS-123",
                             tried=["magnet:?xt=urn:btih:dead1",
                                    "magnet:?xt=urn:btih:dead2"]))

    assert item is None
    assert why == "候选都试过了"


def test_tried_and_too_big_together_reports_size(fake_search):
    """试过的与超限的混在一起：超限的说明优先（用户能据此调上限）。"""
    fake_search["items"]["SSIS-123"] = [
        cand("magnet:?xt=urn:btih:dead", size_gb=1),
        cand("magnet:?xt=urn:btih:big", size_gb=8)]

    item, why = asyncio.run(
        subscribe.fetch_best("SSIS-123", max_size_gb=2,
                             tried=["magnet:?xt=urn:btih:dead"]))

    assert item is None
    assert why == "1 条候选都超过上限 2GB"


# ─────────────────────────────────────────── 排序口径

def test_subtitle_wins_over_everything(fake_search):
    """字幕是内容需求，排在最前。"""
    fake_search["items"]["SSIS-123"] = [
        cand("magnet:?xt=urn:btih:plain", size_gb=9, seeders=100),
        cand("magnet:?xt=urn:btih:subs", size_gb=1, sub=True, seeders=0)]

    item, _ = asyncio.run(subscribe.fetch_best("SSIS-123"))

    assert item["magnet"] == "magnet:?xt=urn:btih:subs"


def test_hd_wins_over_seeders_when_no_subtitle(fake_search):
    """没有字幕可比时，高清优先于做种数。"""
    fake_search["items"]["SSIS-123"] = [
        cand("magnet:?xt=urn:btih:manyseeds", size_gb=9, seeders=100),
        cand("magnet:?xt=urn:btih:hd", size_gb=1, hd=True, seeders=0)]

    item, _ = asyncio.run(subscribe.fetch_best("SSIS-123"))

    assert item["magnet"] == "magnet:?xt=urn:btih:hd"


def test_seeders_win_over_size(fake_search):
    """字幕高清都一样时，做种数优先于体积：有人做的种才下得下来。"""
    fake_search["items"]["SSIS-123"] = [
        cand("magnet:?xt=urn:btih:big", size_gb=9, seeders=0),
        cand("magnet:?xt=urn:btih:seeded", size_gb=1, seeders=50)]

    item, _ = asyncio.run(subscribe.fetch_best("SSIS-123"))

    assert item["magnet"] == "magnet:?xt=urn:btih:seeded"


def test_seeders_field_is_reported(fake_search):
    """选中的候选要带上做种数与来源，排查时有据可依。"""
    fake_search["items"]["SSIS-123"] = [
        cand("magnet:?xt=urn:btih:aa", seeders=42, source="sukebei")]

    item, _ = asyncio.run(subscribe.fetch_best("SSIS-123"))

    assert item["seeders"] == 42
    assert item["source"] == "sukebei"
    assert item["candidates"] == 1


def test_candidates_count_reflects_usable_pool(fake_search):
    """candidates 是过滤后（体积与 tried 之后）的可用条数。"""
    fake_search["items"]["SSIS-123"] = [
        cand("magnet:?xt=urn:btih:a"), cand("magnet:?xt=urn:btih:b"),
        cand("magnet:?xt=urn:btih:big", size_gb=8)]

    item, _ = asyncio.run(
        subscribe.fetch_best("SSIS-123", max_size_gb=2,
                             tried=["magnet:?xt=urn:btih:a"]))

    assert item["candidates"] == 1
