"""系列订阅与多条件筛选功能测试。

覆盖：
1. 完整系列订阅 vs 追新订阅游标初始化与切换
2. 关键词过滤 (include_keywords / exclude_keywords)
3. 资源条件过滤 (require_subtitle, require_hd, min_seeders, min_size_gb, max_size_gb)
4. 多页全量翻页 (full_series=True)
5. 接口 /api/subs/add 与 /api/subs/update
"""
import pytest
import time
from app import subscribe, web, sources, javbus, backlog


@pytest.fixture
def subs_env(tmp_path, monkeypatch):
    from app import store, web
    p = tmp_path / "subs.json"
    s = tmp_path / "state.json"
    b = tmp_path / "backlog.json"
    db = tmp_path / "jobs.db"
    monkeypatch.setattr(web, "SUBS", str(p))
    monkeypatch.setattr(web, "STATE", str(s))
    monkeypatch.setattr(web, "BACKLOG", str(b))
    monkeypatch.setattr(web, "STORE", store.Store(str(db)))
    monkeypatch.setattr(web, "JOBS", {})
    monkeypatch.setattr(web, "WAITING", {})
    monkeypatch.setattr(web, "_queued_at", {})
    monkeypatch.setattr(web, "cloud_ready", lambda: (True, "ok"))
    web._store_inst = None
    store.Store._instance = None
    return tmp_path


def test_sub_filters_normalization():
    raw = {
        "full_series": "true",
        "min_size_gb": "2.5",
        "max_size_gb": 15,
        "min_seeders": "5",
        "require_subtitle": 1,
        "require_hd": "yes",
        "exclude_keywords": " VR , 4K ",
        "include_keywords": "中字, 破解",
    }
    cleaned = subscribe.sub_filters({"filters": raw})
    assert cleaned["full_series"] is True
    assert cleaned["min_size_gb"] == 2.5
    assert cleaned["max_size_gb"] == 15.0
    assert cleaned["min_seeders"] == 5
    assert cleaned["require_subtitle"] is True
    assert cleaned["require_hd"] is True
    assert cleaned["exclude_keywords"] == "VR,4K"
    assert cleaned["include_keywords"] == "中字,破解"


def test_match_item_keywords():
    # 正常通过
    ok, why = subscribe.match_item_keywords("SSIS-123", "测试标题 中字",
                                           include_keywords="中字", exclude_keywords="VR")
    assert ok is True
    assert why == ""

    # 包含关键词不匹配
    ok, why = subscribe.match_item_keywords("SSIS-123", "测试标题 无字幕",
                                           include_keywords="中字")
    assert ok is False
    assert "中字" in why

    # 命中排除关键词
    ok, why = subscribe.match_item_keywords("SSIS-123", "测试标题 VR版",
                                           exclude_keywords="VR, 4K")
    assert ok is False
    assert "VR" in why


def test_series_full_vs_new_cursor(subs_env):
    # 追新模式：游标为今天
    res_new = web.add_sub("series1", kind="series", filters={"full_series": False})
    assert res_new["ok"] is True
    today = time.strftime("%Y-%m-%d")
    assert res_new["sub"]["cursor"] == today

    # 完整系列模式：游标为空，从头抓起
    res_full = web.add_sub("series2", kind="series", filters={"full_series": True})
    assert res_full["ok"] is True
    assert res_full["sub"]["cursor"] == ""


def test_update_sub_switch_to_full_series(subs_env):
    web.add_sub("series3", kind="series", filters={"full_series": False})
    key = "javbus:series:series3"
    assert web.subs_snapshot()[0]["cursor"] != ""

    # 切换为完整系列：游标重置为空
    res = web.update_sub(key, {"filters": {"full_series": True}})
    assert res["ok"] is True
    assert res["sub"]["cursor"] == ""
    assert res["sub"]["filters"]["full_series"] is True


@pytest.mark.anyio
async def test_fetch_best_with_filters(monkeypatch):
    candidates = [
        # 体积过小
        {"code": "TEST-1", "magnet": "magnet:?xt=urn:btih:1", "size_mb": 500,
         "has_subtitle": True, "is_hd": True, "seeders": 10, "name": "TEST-1 small"},
        # 命中排除词
        {"code": "TEST-1", "magnet": "magnet:?xt=urn:btih:2", "size_mb": 4000,
         "has_subtitle": True, "is_hd": True, "seeders": 10, "name": "TEST-1 VR special"},
        # 无字幕
        {"code": "TEST-1", "magnet": "magnet:?xt=urn:btih:3", "size_mb": 4500,
         "has_subtitle": False, "is_hd": True, "seeders": 10, "name": "TEST-1 raw"},
        # 做种数不足
        {"code": "TEST-1", "magnet": "magnet:?xt=urn:btih:4", "size_mb": 4800,
         "has_subtitle": True, "is_hd": True, "seeders": 1, "name": "TEST-1 low seed"},
        # 完美匹配
        {"code": "TEST-1", "magnet": "magnet:?xt=urn:btih:5", "size_mb": 5000,
         "has_subtitle": True, "is_hd": True, "seeders": 20, "name": "TEST-1 perfect"},
    ]

    async def fake_search(code, client=None, limit=None):
        return list(candidates)

    monkeypatch.setattr(sources, "search", fake_search)

    # 1. 严格筛选条件
    filters = {
        "min_size_gb": 1.0,  # 1GB = 1024MB，排除 500MB
        "exclude_keywords": "VR",  # 排除 VR
        "require_subtitle": True,  # 必须字幕
        "min_seeders": 5,  # 做种数 >= 5
    }
    best, why = await subscribe.fetch_best("TEST-1", max_size_gb=10, filters=filters)
    assert best is not None
    assert best["magnet"] == "magnet:?xt=urn:btih:5"
    assert why == ""

    # 2. 如果要求不存在的条件（如做种 >= 50），应返回 None 及说明
    strict_filters = dict(filters, min_seeders=50)
    best2, why2 = await subscribe.fetch_best("TEST-1", max_size_gb=10, filters=strict_filters)
    assert best2 is None
    assert "做种数不足" in why2


@pytest.mark.anyio
async def test_discover_javbus_full_series(monkeypatch):
    """测试完整系列模式下自动翻多页直到没有新数据。"""
    pages_requested = []

    async def fake_list_movies(filter_type, sub_id, page=1, client=None):
        pages_requested.append(page)
        if page <= 3:
            return [{"code": f"SERIES-{page}", "date": f"2026-0{page}-01", "has_magnet": True}]
        return []

    monkeypatch.setattr(javbus, "list_movies", fake_list_movies)

    # 完整系列模式：应翻满 3 页直到第 4 页为空退出
    res = await subscribe.discover_javbus("s_test", cursor="", kind="series", full_series=True)
    assert len(res["items"]) == 3
    assert set(it["code"] for it in res["items"]) == {"SERIES-1", "SERIES-2", "SERIES-3"}
    assert 4 in pages_requested


def test_web_add_and_update_sub_filters(subs_env):
    # 添加带过滤条件的系列订阅
    res = web.add_sub("seriesabc", kind="series", name="测试系列", max_per_run=2, max_size_gb=12.0,
                      filters={"full_series": True, "require_subtitle": True, "min_seeders": 3, "exclude_keywords": "VR"})
    assert res["ok"] is True
    sub = res["sub"]
    assert sub["cursor"] == ""
    assert sub["filters"]["full_series"] is True
    assert sub["filters"]["require_subtitle"] is True
    assert sub["filters"]["min_seeders"] == 3
    assert sub["filters"]["exclude_keywords"] == "VR"

    # 更新条件
    up_res = web.update_sub(sub["key"], {"filters": {"require_hd": True, "min_size_gb": 2.0}})
    assert up_res["ok"] is True
    assert up_res["sub"]["filters"]["require_hd"] is True
    assert up_res["sub"]["filters"]["min_size_gb"] == 2.0


def test_run_subscriptions_with_item_keywords_filter(subs_env, monkeypatch):
    web.add_sub("serieskw", kind="series", filters={"include_keywords": "中字", "exclude_keywords": "VR"})

    async def fake_discover(sub_id, cursor, client=None, kind="actor", pages=2, **kwargs):
        return {
            "items": [
                {"code": "MATCH-1", "title": "系列第1部 中字版", "date": "2026-10-01"},
                {"code": "SKIP-VR", "title": "系列第2部 中字 VR版", "date": "2026-10-02"},
                {"code": "SKIP-RAW", "title": "系列第3部 生肉版", "date": "2026-10-03"},
            ],
            "cursor": "2026-10-03", "error": "", "seen": 3, "pages_done": 1,
        }

    async def fake_best(code, max_size_gb=0, client=None, tried=None, filters=None):
        return {"code": code, "magnet": f"magnet:?xt=urn:btih:{code}", "size_mb": 2000,
                "has_subtitle": True, "is_hd": True}, ""

    monkeypatch.setattr(subscribe, "discover_javbus", fake_discover)
    monkeypatch.setattr(subscribe, "fetch_best", fake_best)
    started = []
    def fake_start(src, code, info=None):
        started.append(code)
        web._store().start("job1", code, src)
        return "job1"
    monkeypatch.setattr(web, "start_job", fake_start)

    res = web.run_subscriptions()
    assert res["ok"] is True
    # 只有 MATCH-1 被提交
    assert started == ["MATCH-1"]
    # 检查待办状态
    todo = web.backlog_load()
    key = "javbus:series:serieskw"
    assert todo[key]["MATCH-1"]["state"] == backlog.QUEUED
    assert todo[key]["SKIP-VR"]["state"] == backlog.SKIPPED
    assert todo[key]["SKIP-RAW"]["state"] == backlog.SKIPPED
