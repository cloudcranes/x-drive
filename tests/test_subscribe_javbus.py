"""订阅走 JavBus API：分页、重叠回查、磁力筛选、上限。"""
import asyncio

import pytest

from app import javbus, subscribe

def movie(code, date, tags=None):
    return {"code": code, "date": date, "title": f"T {code}", "tags": tags or []}

@pytest.fixture
def fake_javbus(monkeypatch):
    state = {"pages": {}, "magnets": {}}

    async def list_movies(filter_type, filter_value, page=1, client=None):
        assert filter_type in ("star", "series")
        return state["pages"].get(page, [])

    async def magnets_for(code, client=None):
        return state["magnets"].get(code, [])

    monkeypatch.setattr(javbus, "list_movies", list_movies)
    monkeypatch.setattr(javbus, "magnets_for", magnets_for)
    return state

def test_new_codes_after_cursor_are_picked(fake_javbus):
    # 游标之前的那部要真的比游标早，否则「按日期从旧到新」会先拿它
    fake_javbus["pages"] = {1: [movie("SSIS-200", "2026-10-20"), movie("SSIS-100", "2025-12-01")]}
    fake_javbus["magnets"] = {"SSIS-200": [{"magnet": "magnet:?xt=urn:btih:aa",
                                            "size_bytes": 2 << 30, "has_subtitle": True,
                                            "is_hd": True}]}
    out = asyncio.run(subscribe.check_javbus("w5a", "2026-01-01", kind="actor"))
    assert [i["code"] for i in out["new"]] == ["SSIS-200"]
    assert out["new"][0]["size_mb"] == 2048
    assert out["cursor"] == "2026-10-20"

def test_cursor_day_is_rescanned(fake_javbus):
    """同一天的作品要重叠回查：晚出磁力的不能被游标跨过去。"""
    fake_javbus["pages"] = {1: [movie("SSIS-200", "2026-10-20")]}
    fake_javbus["magnets"] = {"SSIS-200": [{"magnet": "magnet:?xt=urn:btih:bb", "size_bytes": 1}]}
    out = asyncio.run(subscribe.check_javbus("w5a", "2026-10-20", kind="actor"))
    assert [i["code"] for i in out["new"]] == ["SSIS-200"]

def test_known_codes_and_no_magnet_are_skipped(fake_javbus):
    fake_javbus["pages"] = {1: [movie("SSIS-1", "2026-10-20"), movie("SSIS-2", "2026-10-20")]}
    fake_javbus["magnets"] = {"SSIS-1": [{"magnet": "magnet:?xt=urn:btih:aa", "size_bytes": 1}]}
    out = asyncio.run(subscribe.check_javbus("w5a", "2026-01-01",
                                            known_codes={"SSIS-1"}, kind="actor"))
    assert out["new"] == [] and out["skipped"] == 1

def test_max_size_gb_filters(fake_javbus):
    fake_javbus["pages"] = {1: [movie("SSIS-3", "2026-10-20")]}
    fake_javbus["magnets"] = {"SSIS-3": [{"magnet": "magnet:?xt=urn:btih:aa",
                                          "size_bytes": 8 << 30}]}
    out = asyncio.run(subscribe.check_javbus("w5a", "2026-01-01",
                                            max_size_gb=2, kind="actor"))
    assert out["new"] == [] and out["skipped"] == 1

def test_two_pages_are_scanned(fake_javbus):
    fake_javbus["pages"] = {1: [movie("SSIS-9", "2026-10-20")],
                            2: [movie("SSIS-8", "2026-10-21")]}
    fake_javbus["magnets"] = {c: [{"magnet": f"magnet:?xt=urn:btih:{c.lower()}",
                                   "size_bytes": 1 << 30}] for c in ("SSIS-9", "SSIS-8")}
    out = asyncio.run(subscribe.check_javbus("w5a", "2026-01-01", max_per_run=5,
                                            pages=2, kind="actor"))
    assert [i["code"] for i in out["new"]] == ["SSIS-9", "SSIS-8"], "旧的先下"

def test_empty_list_reports_error(fake_javbus):
    out = asyncio.run(subscribe.check_javbus("bad-id", "2026-01-01", kind="actor"))
    assert out["new"] == [] and "没有返回作品" in out["error"]

def test_check_dispatches_by_source(fake_javbus, monkeypatch):
    called = {}

    async def fake_jb(*a, **kw):
        called["javbus"] = True
        return {"new": [], "cursor": "", "error": "", "skipped": 0}

    monkeypatch.setattr(subscribe, "check_javbus", fake_jb)
    asyncio.run(subscribe.check("w5a", "", kind="actor", source="javbus"))
    assert called.get("javbus") is True

def test_new_sub_defaults_to_javbus_source():
    sub = subscribe.new_sub("w5a", "乙白さやか", "2026-10-22", kind="actor")
    assert sub["source"] == "javbus"
    old = subscribe.new_sub("YnZ1K", "x", "2026-10-22", kind="actor", source="javdb")
    assert old["source"] == "javdb"

def test_discovery_keeps_every_item_on_the_page(fake_javbus):
    """发现只负责「这一页有什么」：3 部都要在 items 里，交给待办表去排。"""
    fake_javbus["pages"] = {1: [movie("TEST-1", "2026-10-01"),
                                movie("TEST-2", "2026-10-02"),
                                movie("TEST-3", "2026-10-03")]}
    out = asyncio.run(subscribe.discover_javbus("w5a", "2026-09-30", kind="actor"))
    assert [i["code"] for i in out["items"]] == ["TEST-1", "TEST-2", "TEST-3"]
    assert out["cursor"] == "2026-10-03"

def test_discovery_dedupes_across_pages(fake_javbus):
    """翻页重叠时同一部只留一条，否则待办表里会重复。"""
    fake_javbus["pages"] = {1: [movie("TEST-1", "2026-10-01")],
                            2: [movie("TEST-1", "2026-10-01")]}
    out = asyncio.run(subscribe.discover_javbus("w5a", "2026-09-30", kind="actor", pages=2))
    assert [i["code"] for i in out["items"]] == ["TEST-1"]

def test_cursor_stops_before_work_without_magnet(fake_javbus):
    """没有磁力的作品不能被游标跨过去，得留在待查里。"""
    fake_javbus["pages"] = {1: [movie("TEST-1", "2026-10-01"),
                                movie("TEST-2", "2026-10-02")]}
    fake_javbus["magnets"] = {"TEST-1": [{"magnet": "magnet:?xt=urn:btih:aa",
                                          "size_bytes": 1 << 30}]}
    out = asyncio.run(subscribe.check_javbus("w5a", "2026-09-30", max_per_run=5,
                                             kind="actor"))
    assert [i["code"] for i in out["new"]] == ["TEST-1"]
    assert out["skipped"] == 1
    # 游标表示「发现到哪儿」，安全性由待办表保证：没磁力那部仍在 items 里，不会丢
    assert [i["code"] for i in out["items"]] == ["TEST-1", "TEST-2"]
    assert out["backlog"] == 2

def test_todays_release_waits_for_next_round(fake_javbus):
    """今天出的片当天还会补磁力，不急着推进游标。"""
    import time
    today = time.strftime("%Y-%m-%d")
    fake_javbus["pages"] = {1: [movie("TEST-9", today)]}
    fake_javbus["magnets"] = {"TEST-9": [{"magnet": "magnet:?xt=urn:btih:cc",
                                          "size_bytes": 1 << 30}]}
    out = asyncio.run(subscribe.check_javbus("w5a", "2026-01-01", kind="actor"))
    # 今天的片先不下（可能还在补磁力），但必须被记录下来
    assert out["new"] == [] and [i["code"] for i in out["items"]] == ["TEST-9"]


def test_backlog_deeper_than_pages_keeps_cursor(fake_javbus):
    """积压超过两页时不能宣布「发现完了」：游标必须停住，下一轮继续往回翻。

    以前翻两页就把游标推到最新日期，中间那段永久漏掉 —— 订阅空几周后必然发生。
    """
    pages = {1: [{"code": "NEW-3", "date": "2026-03-03"},
                 {"code": "NEW-2", "date": "2026-03-02"}],
             2: [{"code": "NEW-1", "date": "2026-03-01"}]}
    fake_javbus["pages"] = pages
    out = asyncio.run(subscribe.discover_javbus("w5a", "2026-01-01", kind="actor", pages=2))
    assert [i["code"] for i in out["items"]] == ["NEW-1", "NEW-2", "NEW-3"]
    assert out["caught_up"] is False
    assert out["cursor"] == "2026-01-01", "没翻到旧边界，游标不能动"


def test_reaching_old_boundary_advances_cursor(fake_javbus):
    """翻到旧游标之前就说明追上了：这时才前移游标。"""
    fake_javbus["pages"] = {1: [{"code": "NEW-3", "date": "2026-03-03"},
                                {"code": "OLD-9", "date": "2025-12-31"}]}
    out = asyncio.run(subscribe.discover_javbus("w5a", "2026-01-01", kind="actor", pages=2))
    assert out["caught_up"] is True
    assert out["cursor"] == "2026-03-03"
    assert [i["code"] for i in out["items"]] == ["NEW-3"], "早于游标的不再收"


def test_first_run_sets_baseline_without_backfilling(fake_javbus):
    """首次订阅（无游标）只定基线：拿到一页，游标设成最新，不回填历史。"""
    fake_javbus["pages"] = {1: [{"code": "A-1", "date": "2026-01-01"},
                                {"code": "A-2", "date": "2026-01-05"}]}
    out = asyncio.run(subscribe.discover_javbus("w5a", "", kind="actor", pages=2))
    assert out["caught_up"] is True
    assert out["cursor"] == "2026-01-05"
