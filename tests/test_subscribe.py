"""订阅解析与筛选：全部走本地 fixture，不联网。"""
import asyncio
import time
from pathlib import Path

import pytest

from app import core, store, subscribe

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8", errors="ignore")


# ---------------------------------------------------------------- 演员页

def test_parse_actor_items():
    items = subscribe.parse_actor_items(_fixture("javdb_actor.html"))
    assert len(items) >= 10
    first = items[0]
    assert first["code"].startswith(("MIDA", "MIZD", "MIAA"))
    assert len(first["date"]) == 10
    assert first["path"].startswith("/v/")
    assert isinstance(first["has_magnet"], bool)


def test_parse_actor_items_marks_magnet_flag():
    items = subscribe.parse_actor_items(_fixture("javdb_actor.html"))
    assert any(i["has_magnet"] for i in items), "应能识别出含磁链的条目"
    assert not all(i["has_magnet"] for i in items), "也该有没磁链的，标记才有意义"


def test_parse_actor_items_ignores_junk():
    assert subscribe.parse_actor_items("<html></html>") == []
    assert subscribe.parse_actor_items("") == []


# ---------------------------------------------------------------- 磁力

def test_parse_magnets():
    mags = subscribe.parse_magnets(_fixture("javdb_detail.html"))
    assert len(mags) >= 2
    m = mags[0]
    assert m["magnet"].startswith("magnet:?xt=urn:btih:")
    assert "&amp;" not in m["magnet"], "转义要还原，否则迅雷不认"
    assert m["size_mb"] > 0 and m["files"] >= 1


def test_parse_magnets_dedupes():
    mags = subscribe.parse_magnets(_fixture("javdb_detail.html"))
    keys = [m["magnet"].split("&dn=")[0].lower() for m in mags]
    assert len(keys) == len(set(keys)), "同一条磁力不该出现两次"


def test_pick_magnet_prefers_chinese_subtitle():
    mags = [
        {"magnet": "magnet:?xt=urn:btih:aaa", "size_mb": 1000, "files": 1, "tags": ["高清"]},
        {"magnet": "magnet:?xt=urn:btih:bbb", "size_mb": 800, "files": 1, "tags": ["中文字幕"]},
    ]
    assert subscribe.pick_magnet(mags)["magnet"].endswith("bbb")


def test_pick_magnet_falls_back_to_largest():
    mags = [
        {"magnet": "magnet:?xt=urn:btih:small", "size_mb": 100, "files": 1, "tags": []},
        {"magnet": "magnet:?xt=urn:btih:big", "size_mb": 5000, "files": 1, "tags": []},
    ]
    assert subscribe.pick_magnet(mags)["magnet"].endswith("big")


def test_pick_magnet_empty():
    assert subscribe.pick_magnet([]) is None


# ---------------------------------------------------------------- 游标与去重

def test_candidates_skip_old_by_cursor():
    """游标之前的片子一律不抓，否则会把演员几百部历史片全下一遍。"""
    items = [
        {"code": "A-1", "date": "2026-01-01", "has_magnet": True},
        {"code": "A-2", "date": "2026-06-01", "has_magnet": True},
    ]
    got = subscribe.candidates(items, "2026-03-01", set())
    assert [i["code"] for i in got] == ["A-2"]


def test_candidates_skip_known_codes():
    items = [{"code": "A-1", "date": "2026-06-01", "has_magnet": True}]
    assert subscribe.candidates(items, "2026-01-01", {"A-1"}) == []


def test_candidates_skip_without_magnet():
    items = [{"code": "A-1", "date": "2026-06-01", "has_magnet": False}]
    assert subscribe.candidates(items, "2026-01-01", set()) == []


def test_candidates_oldest_first():
    """先下最早的，游标才能稳步前移，不会漏掉中间那部。"""
    items = [
        {"code": "A-3", "date": "2026-09-01", "has_magnet": True},
        {"code": "A-1", "date": "2026-07-01", "has_magnet": True},
        {"code": "A-2", "date": "2026-08-01", "has_magnet": True},
    ]
    got = subscribe.candidates(items, "2026-06-01", set())
    assert [i["code"] for i in got] == ["A-1", "A-2", "A-3"]


# ---------------------------------------------------------------- actor id

def test_actor_id_from_url():
    assert subscribe.actor_id_of("https://javdb.com/actors/YnZ1K") == "YnZ1K"
    assert subscribe.actor_id_of("javdb.com/actors/YnZ1K?x=1") == "YnZ1K"


def test_actor_id_bare():
    assert subscribe.actor_id_of("YnZ1K") == "YnZ1K"


def test_actor_id_rejects_junk():
    assert subscribe.actor_id_of("") == ""
    assert subscribe.actor_id_of("hello world") == ""


# ---------------------------------------------------------------- check 流程

def test_check_returns_empty_on_fetch_failure():
    class Boom:
        async def get(self, *a, **kw):
            raise OSError("no net")

    out = asyncio.run(subscribe.check("YnZ1K", "2026-01-01", client=Boom()))
    assert out["new"] == []
    assert out["error"]


def test_check_respects_max_size():
    """单片体积超过上限要跳过，并把游标推过去，避免下次卡在同一部。"""
    actor_html = _fixture("javdb_actor.html")

    class Fake:
        async def get(self, url, **kw):
            class R:
                status_code = 200
            r = R()
            r.text = actor_html if "/actors/" in url else _fixture("javdb_detail.html")
            return r

        async def aclose(self):
            pass

    out = asyncio.run(subscribe.check("YnZ1K", "2026-01-01", max_per_run=3,
                                      max_size_gb=0.0001, client=Fake()))
    assert out["new"] == [], "体积上限极小，什么都不该下"
    assert out["cursor"] > "2026-01-01", "跳过的也要推进游标"


def test_check_advances_cursor_without_new_items():
    """没有新片时也要把游标推到本页最新，避免每次重复扫同一批老条目。"""
    class Fake:
        async def get(self, url, **kw):
            class R:
                status_code = 200
            r = R()
            r.text = _fixture("javdb_actor.html")
            return r

        async def aclose(self):
            pass

    out = asyncio.run(subscribe.check("YnZ1K", "2099-01-01", client=Fake()))
    assert out["new"] == []
    assert out["cursor"] >= "2026-10-20"


# ---------------------------------------------------------------- web 侧集成

@pytest.fixture
def subs_env(tmp_path, monkeypatch):
    """订阅调度的统一环境：独立 subs/backlog/jobs，默认「发现 0 部、没有磁力」。

    打桩目标是新调度用到的两个函数（发现 / 取磁力），不是旧的 check()。
    """
    from app import store, web

    monkeypatch.setattr(web, "SUBS", str(tmp_path / "subs.json"))
    # 订阅与待办现在存在任务库里，需要独立的库，否则会写进真实 /data
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    monkeypatch.setattr(web, "BACKLOG", str(tmp_path / "backlog.json"))
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    monkeypatch.setattr(web, "JOBS", {})
    monkeypatch.setattr(web, "WAITING", {})
    monkeypatch.setattr(web, "_queued_at", {})
    monkeypatch.setattr(web, "diskspace_room_ok", lambda: True)
    calls = {"discover": [], "best": []}

    async def fake_discover(sub_id, cursor, client=None, kind="actor", pages=2):
        calls["discover"].append({"id": sub_id, "kind": kind, "cursor": cursor})
        return {"items": [], "cursor": cursor, "error": "", "seen": 0, "pages_done": 1}

    async def fake_best(code, max_size_gb=0, client=None, tried=None):
        calls["best"].append(code)
        return {"code": code, "magnet": f"magnet:?xt=urn:btih:{code.lower()}",
                "size_mb": 100, "tags": [], "has_subtitle": False, "is_hd": False}, ""

    monkeypatch.setattr(subscribe, "discover_javbus", fake_discover)
    monkeypatch.setattr(subscribe, "fetch_best", fake_best)
    return calls

def test_add_sub_sets_cursor_to_today(tmp_path, monkeypatch):
    """新订阅的游标必须是今天：否则会把演员几百部历史片全下一遍。"""
    from app import store, web

    monkeypatch.setattr(web, "SUBS", str(tmp_path / "subs.json"))
    # 订阅与待办现在存在任务库里，需要独立的库，否则会写进真实 /data
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    out = web.add_sub("https://javdb.com/actors/YnZ1K", max_per_run=2)
    assert out["ok"]
    import datetime
    assert out["sub"]["cursor"] == datetime.date.today().isoformat()
    assert out["sub"]["max_per_run"] == 2
    assert out["sub"]["enabled"] is True


def test_add_sub_rejects_duplicate(tmp_path, monkeypatch):
    from app import web

    monkeypatch.setattr(web, "SUBS", str(tmp_path / "subs.json"))
    # 订阅与待办现在存在任务库里，需要独立的库，否则会写进真实 /data
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    assert web.add_sub("YnZ1K")["ok"]
    second = web.add_sub("YnZ1K")
    assert not second["ok"] and "已经订阅过" in second["error"]


def test_add_sub_rejects_bad_input(tmp_path, monkeypatch):
    from app import web

    monkeypatch.setattr(web, "SUBS", str(tmp_path / "subs.json"))
    # 订阅与待办现在存在任务库里，需要独立的库，否则会写进真实 /data
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    assert not web.add_sub("")["ok"]
    assert not web.add_sub("随便写点什么")["ok"]


def test_toggle_and_remove_sub(tmp_path, monkeypatch):
    from app import web

    monkeypatch.setattr(web, "SUBS", str(tmp_path / "subs.json"))
    # 订阅与待办现在存在任务库里，需要独立的库，否则会写进真实 /data
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    web.add_sub("YnZ1K")
    assert web.toggle_sub("YnZ1K", False)["sub"]["enabled"] is False
    assert web.toggle_sub("YnZ1K", True)["sub"]["enabled"] is True
    assert web.remove_sub("YnZ1K")["ok"]
    assert web.subs_snapshot() == []


def test_run_subscriptions_skips_disabled(subs_env):
    from app import web

    web.add_sub("YnZ1K")
    web.toggle_sub("YnZ1K", False)
    out = web.run_subscriptions()
    assert out["ok"] and subs_env["discover"] == [], "关掉的订阅不该被查"


def test_run_subscriptions_starts_jobs_and_advances_cursor(subs_env, monkeypatch):
    from app import core, web

    monkeypatch.setattr(web, "STATE", str(web.Path(web.SUBS).parent / "state.json"))
    core.save_state(web.STATE, {"OLD-1": {"code": "OLD-1"}})
    web.add_sub("YnZ1K")

    async def fake_discover(sub_id, cursor, client=None, kind="actor", pages=2):
        return {"items": [{"code": "NEW-1", "date": "2026-10-20", "title": "新"}],
                "cursor": "2026-10-20", "error": "", "seen": 1, "pages_done": 1}
    monkeypatch.setattr(subscribe, "discover_javbus", fake_discover)
    started = []
    monkeypatch.setattr(web, "start_job",
                        lambda src, code, info=None: started.append((code, src)) or "j1")
    out = web.run_subscriptions()
    assert started and started[0][0] == "NEW-1"
    assert out["results"][0]["started"][0]["code"] == "NEW-1"
    assert web.subs_snapshot()[0]["cursor"] == "2026-10-20", "游标要落盘"


def test_run_subscriptions_dedupes_archived(subs_env, monkeypatch):
    """already 归档的番号不该重复下。"""
    from app import core, web

    monkeypatch.setattr(web, "STATE", str(web.Path(web.SUBS).parent / "state.json"))
    core.save_state(web.STATE, {"DUP-1": {"code": "DUP-1"}})
    web.add_sub("YnZ1K")

    async def fake_discover(sub_id, cursor, client=None, kind="actor", pages=2):
        return {"items": [{"code": "DUP-1", "date": "2026-10-20"}],
                "cursor": "2026-10-20", "error": "", "seen": 1, "pages_done": 1}
    monkeypatch.setattr(subscribe, "discover_javbus", fake_discover)
    started = []
    monkeypatch.setattr(web, "start_job",
                        lambda src, code, info=None: started.append(code) or "j1")
    web.run_subscriptions()
    assert started == [], "已归档的番号必须跳过"


def test_run_subscriptions_skips_when_disk_short(tmp_path, monkeypatch):
    """空间不够时整轮跳过，不要硬下。"""
    from app import diskspace as ds
    from app import web

    monkeypatch.setattr(web, "SUBS", str(tmp_path / "subs.json"))
    # 订阅与待办现在存在任务库里，需要独立的库，否则会写进真实 /data
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    web.add_sub("YnZ1K")
    monkeypatch.setattr(web, "DISK_RESERVE_GB", 100)
    monkeypatch.setattr(ds, "free_bytes", lambda _: 1 * 1024**3)
    called = []
    async def fake_check(*a, **kw):
        called.append(1)
        return {"new": [], "cursor": "", "error": ""}
    monkeypatch.setattr(subscribe, "check", fake_check)
    out = web.run_subscriptions()
    assert not out["ok"] and "磁盘空间不足" in out["error"]
    assert called == []


def test_webui_has_subscription_panel():
    """前端要有订阅入口和面板，且不允许用错误的提示类名。"""
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "app" / "webui.html").read_text(
        encoding="utf-8")
    for hook in ('id="subpanel"', 'id="subs-open"', 'id="subs-list"',
                 'id="sub-add"', 'id="sub-run"', "/api/subs/add", "/api/subs/run"):
        assert hook in html, f"缺少 {hook}"
    assert '"bad")' not in html, "提示类名应为 err，不是 bad"
    assert "回填历史" in html, "界面要说明不会回填历史"


def test_check_counts_skipped_by_size():
    """被体积上限挡下的要计数，否则界面只会说「没有新片」，看不出是拦掉了。"""
    actor_html = _fixture("javdb_actor.html")

    class Fake:
        async def get(self, url, **kw):
            class R:
                status_code = 200
            r = R()
            r.text = actor_html if "/actors/" in url else _fixture("javdb_detail.html")
            return r

        async def aclose(self):
            pass

    out = asyncio.run(subscribe.check("YnZ1K", "2026-01-01", max_per_run=3,
                                      max_size_gb=0.0001, client=Fake()))
    assert out["new"] == [] and out["skipped"] >= 1


def test_last_result_explains_why_nothing_was_downloaded(subs_env, monkeypatch):
    """没下到东西时要说明原因（待办里有几部在等），不能只写「没有新片」。"""
    from app import web

    web.add_sub("YnZ1K")

    async def fake_discover(sub_id, cursor, client=None, kind="actor", pages=2):
        return {"items": [{"code": "WAIT-1", "date": "2020-01-01"}],
                "cursor": "2020-01-01", "error": "", "seen": 1, "pages_done": 1}

    async def fake_best(code, max_size_gb=0, client=None, tried=None):
        return None, "还没有磁力"

    monkeypatch.setattr(subscribe, "discover_javbus", fake_discover)
    monkeypatch.setattr(subscribe, "fetch_best", fake_best)
    web.run_subscriptions()
    last = web.subs_snapshot()[0]["last_result"]
    assert "跳过 1 部" in last and "还没有磁力" in last


# ---------------------------------------------------------------- 番号系列订阅

def test_parse_series_items():
    """系列页和演员页结构一致，同一套解析要都能吃。"""
    items = subscribe.parse_items(_fixture("javdb_series.html"))
    assert len(items) >= 20
    assert items[0]["code"].startswith("SIRO-")
    assert len(items[0]["date"]) == 10
    assert items[0]["path"].startswith("/v/")


def test_code_with_trailing_letters():
    """VSPDS-221AI 这种结尾带字母的番号，只认结尾数字会整页漏掉。"""
    html = ('<div class="item"><a href="/v/Aq20gM" class="box">'
            '<div class="video-title"><strong>VSPDS-221AI</strong> 标题</div>'
            '<div class="meta"> 2026-10-09 </div>'
            '<div class="tags">含磁鏈</div></a></div>')
    items = subscribe.parse_items(html)
    assert len(items) == 1 and items[0]["code"] == "VSPDS-221AI"


def test_series_page_title():
    name = subscribe.page_title(_fixture("javdb_series.html"))
    assert name and "JavDB" not in name


def test_sub_id_of_series():
    assert subscribe.sub_id_of("https://javdb.com/series/rY2v", "series") == "rY2v"
    assert subscribe.sub_id_of("rY2v", "series") == "rY2v"
    assert subscribe.sub_id_of("/series/9xwV", "series") == "9xwV"


def test_sub_id_of_rejects_wrong_kind_url():
    """给的是演员链接却按系列订阅，不能瞎认。"""
    assert subscribe.sub_id_of("https://javdb.com/actors/YnZ1K", "series") == "YnZ1K"


def test_add_series_sub_keeps_kind(tmp_path, monkeypatch):
    from app import web

    monkeypatch.setattr(web, "SUBS", str(tmp_path / "subs.json"))
    # 订阅与待办现在存在任务库里，需要独立的库，否则会写进真实 /data
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    out = web.add_sub("https://javdb.com/series/rY2v", max_per_run=1,
                      max_size_gb=6, kind="series")
    assert out["ok"]
    assert out["sub"]["kind"] == "series"
    assert out["sub"]["id"] == "rY2v"
    assert out["sub"]["key"] == "javdb:series:rY2v", "链接来自 javdb，键要跟着来源走"


def test_actor_and_series_same_id_do_not_collide(tmp_path, monkeypatch):
    """两套 ID 空间独立，同一个字符串不该互相覆盖。"""
    from app import web

    monkeypatch.setattr(web, "SUBS", str(tmp_path / "subs.json"))
    # 订阅与待办现在存在任务库里，需要独立的库，否则会写进真实 /data
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    assert web.add_sub("abc123", kind="actor")["ok"]
    assert web.add_sub("abc123", kind="series")["ok"]
    keys = {s["key"] for s in web.subs_snapshot()}
    assert keys == {"javbus:actor:abc123", "javbus:series:abc123"}, "裸 ID 走默认来源"


def test_run_subscriptions_uses_kind(subs_env):
    """系列订阅要按系列页去查，不能还去抓演员页。"""
    from app import web

    web.add_sub("rY2v", kind="series")
    web.run_subscriptions()
    assert len(subs_env["discover"]) == 1 and subs_env["discover"][0]["kind"] == "series", \
        "系列订阅要按系列页发现"


def test_remove_by_bare_id_still_works(tmp_path, monkeypatch):
    """界面传 key，但裸 ID 也要能删，免得老数据删不掉。"""
    from app import web

    monkeypatch.setattr(web, "SUBS", str(tmp_path / "subs.json"))
    # 订阅与待办现在存在任务库里，需要独立的库，否则会写进真实 /data
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    web.add_sub("rY2v", kind="series")
    assert web.remove_sub("rY2v")["ok"]
    assert web.subs_snapshot() == []


def test_webui_has_kind_selector():
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "app" / "webui.html").read_text(
        encoding="utf-8")
    assert 'id="sub-kind"' in html
    assert 'data-kind="series"' in html
    assert "番号系列" in html
    assert "kind: subKind" in html, "添加时要带上类型"
    assert "sub.key" in html, "操作要用带类型前缀的 key"


def test_run_subscriptions_fills_name_from_page(subs_env, monkeypatch):
    """只填裸 ID 时，检查一次后应显示能看懂的名字，而不是一堆字母。"""
    from app import web

    web.add_sub("rY2v", kind="series")
    assert web.subs_snapshot()[0]["name"] == "rY2v"

    async def fake_discover(sub_id, cursor, client=None, kind="actor", pages=2):
        return {"items": [], "cursor": cursor, "error": "", "seen": 0, "pages_done": 1,
                "name": "【初撮り】ネットでAV応募→AV体験撮影"}

    monkeypatch.setattr(subscribe, "discover_javbus", fake_discover)
    web.run_subscriptions()
    assert web.subs_snapshot()[0]["name"] == "【初撮り】ネットでAV応募→AV体験撮影"


def test_run_subscriptions_keeps_custom_name(subs_env, monkeypatch):
    """用户自己起过名字就不要覆盖。"""
    from app import web

    web.add_sub("rY2v", kind="series", name="我的系列")

    async def fake_discover(sub_id, cursor, client=None, kind="actor", pages=2):
        return {"items": [], "cursor": cursor, "error": "", "seen": 0, "pages_done": 1,
                "name": "页面上的名字"}

    monkeypatch.setattr(subscribe, "discover_javbus", fake_discover)
    web.run_subscriptions()
    assert web.subs_snapshot()[0]["name"] == "我的系列"


def test_add_sub_accepts_short_javbus_star_id(tmp_path, monkeypatch):
    """JavBus 演员 ID 可能只有三位（w5a），不能按 JavDB 的四位起算拒收。"""
    from app import web

    monkeypatch.setattr(web, "SUBS", str(tmp_path / "subs.json"))
    # 订阅与待办现在存在任务库里，需要独立的库，否则会写进真实 /data
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    out = web.add_sub("w5a")
    assert out["ok"] and out["sub"]["id"] == "w5a"
    assert out["sub"]["source"] == "javbus"
    out2 = web.add_sub("https://javbus.com/star/w5b", kind="actor")
    assert out2["ok"] and out2["sub"]["key"] == "javbus:actor:w5b"


def test_legacy_sub_key_is_migrated_not_duplicated(tmp_path, monkeypatch):
    """老数据只有 kind:id，迁移成带来源的键，并且不会多出一条订阅。"""
    from app import subscribe, web

    path = str(tmp_path / "subs.json")
    monkeypatch.setattr(web, "SUBS", path)
    # 空库 → 首次运行会从旧 subs.json 导入，正是这里要验证的升级路径
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    subscribe.save_subs(path, {
        "actor:w5a": {"id": "w5a", "source": "javbus", "kind": "actor",
                      "name": "乙白", "cursor": "2026-01-01", "enabled": True},
    })
    out = web.add_sub("w5a")
    assert out["ok"] is False and "已经订阅" in out["error"]
    keys = {s["key"] for s in web.subs_snapshot()}
    assert keys == {"javbus:actor:w5a"}, "迁移后只该有一条"


# ---------------------------------------------------------------- 来源识别（批次4）

def test_source_of_reads_host_from_url():
    assert subscribe.source_of("https://javdb.com/actors/YnZ1K") == "javdb"
    assert subscribe.source_of("https://www.javbus.com/star/w5a") == "javbus"
    assert subscribe.source_of("w5a") == "", "裸 ID 不带站点信息"


def test_add_sub_follows_link_source(subs_env):
    """链接指向 javdb 就存 javdb：以前存成 javbus，查出来永远是空的。"""
    from app import web

    out = web.add_sub("https://javdb.com/actors/YnZ1K")
    assert out["ok"] and out["sub"]["source"] == "javdb"
    assert out["sub"]["key"] == "javdb:actor:YnZ1K"

    bare = web.add_sub("w5a")
    assert bare["ok"] and bare["sub"]["source"] == "javbus", "裸 ID 用默认来源"


def test_same_id_across_sources_do_not_collide(subs_env):
    """同一个字符串在不同站点是不同对象，键要分开。"""
    from app import web

    assert web.add_sub("https://javdb.com/actors/abcd")["ok"]
    assert web.add_sub("https://javbus.com/star/abcd")["ok"]
    keys = {s["key"] for s in web.subs_snapshot()}
    assert keys == {"javdb:actor:abcd", "javbus:actor:abcd"}


# ---------------------------------------------------------------- 落盘与对账（批次2）

def test_cursor_and_backlog_commit_together(subs_env, monkeypatch):
    """发现结果与游标必须在同一个事务里落库：中间崩一次就是永久漏订。"""
    from app import store, web

    async def fake_discover(sub_id, cursor, client=None, kind="actor", pages=2):
        return {"items": [{"code": "CCC-1", "date": "2020-01-01"}],
                "cursor": "2020-01-01", "error": "", "seen": 1, "pages_done": 1}

    monkeypatch.setattr(subscribe, "discover_javbus", fake_discover)
    web.add_sub("w5a")
    # 写待办时炸掉：游标这次绝不可以落库
    original = store.Store.state_put
    calls = {"n": 0}

    def boom(self, updates, conn=None):
        if "backlog" in updates:
            calls["n"] += 1
            raise RuntimeError("磁盘写坏了")
        return original(self, updates, conn=conn)

    monkeypatch.setattr(store.Store, "state_put", boom)
    with pytest.raises(RuntimeError):
        web.run_subscriptions()
    monkeypatch.setattr(store.Store, "state_put", original)
    assert calls["n"] == 1
    sub = web.subs_snapshot()[0]
    assert sub["cursor"] != "2020-01-01", "待办没写进去，游标就不能前移"


def test_failed_job_goes_back_to_pending(subs_env, monkeypatch):
    """提交后下载失败：必须放回待办，否则这部片永久漏掉。"""
    from app import backlog as bl
    from app import store, web

    async def fake_discover(sub_id, cursor, client=None, kind="actor", pages=2):
        return {"items": [{"code": "DDD-1", "date": "2020-01-01"}],
                "cursor": "2020-01-01", "error": "", "seen": 1, "pages_done": 1}

    async def fake_best(code, max_size_gb=0, client=None, tried=None):
        return {"code": code, "magnet": "magnet:?xt=urn:btih:dd", "size_mb": 100,
                "tags": [], "has_subtitle": False, "is_hd": False}, ""

    monkeypatch.setattr(subscribe, "discover_javbus", fake_discover)
    monkeypatch.setattr(subscribe, "fetch_best", fake_best)
    web.add_sub("w5a")
    out = web.run_subscriptions()
    job_id = out["results"][0]["started"][0]["job"]
    todo = web.backlog_load()
    assert todo["javbus:actor:w5a"]["DDD-1"]["state"] == bl.QUEUED
    assert todo["javbus:actor:w5a"]["DDD-1"]["job_id"] == job_id

    # 下载失败
    web.STORE.update(job_id, stage="failed", error="没有可用视频")
    web.reconcile_backlog(todo)
    assert todo["javbus:actor:w5a"]["DDD-1"]["state"] == bl.PENDING
    assert "没有可用视频" in todo["javbus:actor:w5a"]["DDD-1"]["note"]


def test_archived_code_closes_the_backlog_item(tmp_path, monkeypatch):
    """归档成功后待办要收口，不能每轮都回炉重试。"""
    from app import backlog as bl
    from app import web

    async def fake_best(code, max_size_gb=0, client=None, tried=None):
        return {"code": code, "magnet": "magnet:?xt=urn:btih:ee", "size_mb": 100,
                "tags": [], "has_subtitle": False, "is_hd": False}, ""

    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    monkeypatch.setattr(subscribe, "fetch_best", fake_best)
    todo = {"javbus:actor:w5a": {"EEE-1": {"date": "2020-01-01", "state": bl.QUEUED,
                                           "job_id": "gone", "tries": 0, "next_at": 0,
                                           "title": "", "note": ""}}}
    core.save_state(web.STATE, {"EEE-1": {"size": 1}})
    changed = web.reconcile_backlog(todo)
    assert changed[0]["result"] == "archived"
    assert todo["javbus:actor:w5a"]["EEE-1"]["state"] == bl.DONE


# ---------------------------------------------------------------- 待办中心与愿望单

def _iso_env(tmp_path, monkeypatch):
    from app import backlog as bl, store, web
    monkeypatch.setattr(web, "SUBS", str(tmp_path / "subs.json"))
    monkeypatch.setattr(web, "BACKLOG", str(tmp_path / "backlog.json"))
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    monkeypatch.setattr(web, "JOBS", {})
    monkeypatch.setattr(web, "WAITING", {})
    monkeypatch.setattr(web, "_queued_at", {})
    monkeypatch.setattr(web, "run_web_job", lambda *a: None)
    monkeypatch.setattr(web, "diskspace_room_ok", lambda: True)
    monkeypatch.setattr(web, "series_median", lambda code, state: 0)
    core.save_state(web.STATE, {})
    return bl, web


def test_backlog_view_explains_state_and_job(tmp_path, monkeypatch):
    """待办中心要能说清「为什么还没下、关联哪个任务」。"""
    bl, web = _iso_env(tmp_path, monkeypatch)
    todo = {"javbus:actor:w5a": {
        "AAA-1": {"date": "2020-01-01", "state": bl.PENDING, "tries": 2,
                  "next_at": time.time() + 600, "title": "待查", "note": "还没有磁力"},
        "AAA-2": {"date": "2020-01-02", "state": bl.QUEUED, "tries": 0, "next_at": 0,
                  "title": "在跑", "note": "", "job_id": "jX"},
    }}
    web.STORE.start("jX", "AAA-2", "magnet:a")
    web.STORE.update("jX", stage="downloading")
    web.subs_save({}, todo)

    view = web.backlog_view()
    rows = {r["code"]: r for r in view["items"]}
    assert "分钟后再查" in rows["AAA-1"]["why"]
    assert rows["AAA-1"]["tries"] == 2
    assert rows["AAA-2"]["job_stage"] == "downloading"
    assert view["counts"][bl.PENDING] == 1 and view["counts"][bl.QUEUED] == 1


def test_backlog_retry_clears_backoff_and_tried(tmp_path, monkeypatch):
    """手动再试：退避与已试候选都清掉，否则点了也没变化。"""
    bl, web = _iso_env(tmp_path, monkeypatch)
    todo = {"javbus:actor:w5a": {
        "AAA-1": {"date": "", "state": bl.PENDING, "tries": 4,
                  "next_at": time.time() + 86400, "tried": ["aa", "bb"]}}}
    web.subs_save({}, todo)
    out = web.retry_backlog("AAA-1")
    assert out["ok"] is True
    item = web.backlog_load()["javbus:actor:w5a"]["AAA-1"]
    assert item["next_at"] == 0 and item["tries"] == 0
    assert item["note"] == "手动重试"


def test_backlog_ignore_marks_skipped(tmp_path, monkeypatch):
    """主动放弃不是失败：标 skipped，不再重试。"""
    bl, web = _iso_env(tmp_path, monkeypatch)
    web.subs_save({}, {"javbus:actor:w5a": {
        "AAA-1": {"date": "", "state": bl.PENDING, "tries": 0, "next_at": 0}}})
    assert web.ignore_backlog("AAA-1")["ok"] is True
    assert web.backlog_load()["javbus:actor:w5a"]["AAA-1"]["state"] == bl.SKIPPED


def test_wishlist_adds_codes_and_downloads_without_subscription(tmp_path, monkeypatch):
    """愿望单：没有订阅，光凭番号也能进待办并提交下载。"""
    bl, web = _iso_env(tmp_path, monkeypatch)
    out = web.wishlist_add("AAA-1，AAA-2  aaa-1", max_size_gb=5)
    assert out["added"] == ["AAA-1", "AAA-2"], "去重并规范化"
    assert web.backlog_load()[web.WISH_KEY]["AAA-1"]["max_size_gb"] == 5

    async def fake_best(code, max_size_gb=0, client=None, tried=None):
        return {"code": code, "magnet": "magnet:?xt=urn:btih:" + "0" * 40,
                "size_mb": 100, "tags": [], "has_subtitle": False, "is_hd": False}, ""

    monkeypatch.setattr(subscribe, "fetch_best", fake_best)
    res = web.run_subscriptions()
    wish = [r for r in res["results"] if r["key"] == web.WISH_KEY][0]
    assert {s["code"] for s in wish["started"]} == {"AAA-1", "AAA-2"}


def test_backlog_view_finds_job_outside_latest_200(tmp_path, monkeypatch):
    """待办里的任务不在最新 200 条里时，仍要按 job_id 精确查到它的真实状态。

    以前 backlog_view 用 recent(200) 兜底，任务一多老任务查不到 → 显示成
    「进行中」，实际它早就 failed 了。这里造 250 条把目标挤出窗口，验证
    job_stage 仍是真实值而不是空兜底。
    """
    bl, web = _iso_env(tmp_path, monkeypatch)
    web.STORE.start("TGT-1", "TGT-001", "magnet:tgt")
    web.STORE.finish("TGT-1", "failed")
    # 250 条更新的任务，把 TGT-1 挤出 recent(200) 窗口
    for i in range(250):
        jid = f"other-{i}"
        web.STORE.start(jid, f"O-{i:03d}", "magnet:b")
        web.STORE.finish(jid, "done")
    todo = {"javbus:actor:w5a": {
        "TGT-001": {"date": "2020-01-01", "state": bl.QUEUED, "tries": 0,
                    "next_at": 0, "title": "目标", "note": "", "job_id": "TGT-1"},
    }}
    web.subs_save({}, todo)

    view = web.backlog_view()
    row = {r["code"]: r for r in view["items"]}["TGT-001"]

    assert row["job_stage"] == "failed", \
        f"任务在 200 条之外被误判成进行中：{row['job_stage']}"
    assert "failed" in row["why"] or "失败" in row["why"], \
        f"why 应反映真实失败：{row['why']}"
