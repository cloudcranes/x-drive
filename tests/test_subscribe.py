"""订阅解析与筛选：全部走本地 fixture，不联网。"""
import asyncio
from pathlib import Path

from app import subscribe

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
    assert "&amp;" not in m["magnet"], "转义要还原，否则 aria2 不认"
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

def test_add_sub_sets_cursor_to_today(tmp_path, monkeypatch):
    """新订阅的游标必须是今天：否则会把演员几百部历史片全下一遍。"""
    from app import web

    monkeypatch.setattr(web, "SUBS", str(tmp_path / "subs.json"))
    out = web.add_sub("https://javdb.com/actors/YnZ1K", max_per_run=2)
    assert out["ok"]
    import datetime
    assert out["sub"]["cursor"] == datetime.date.today().isoformat()
    assert out["sub"]["max_per_run"] == 2
    assert out["sub"]["enabled"] is True


def test_add_sub_rejects_duplicate(tmp_path, monkeypatch):
    from app import web

    monkeypatch.setattr(web, "SUBS", str(tmp_path / "subs.json"))
    assert web.add_sub("YnZ1K")["ok"]
    second = web.add_sub("YnZ1K")
    assert not second["ok"] and "已经订阅过" in second["error"]


def test_add_sub_rejects_bad_input(tmp_path, monkeypatch):
    from app import web

    monkeypatch.setattr(web, "SUBS", str(tmp_path / "subs.json"))
    assert not web.add_sub("")["ok"]
    assert not web.add_sub("随便写点什么")["ok"]


def test_toggle_and_remove_sub(tmp_path, monkeypatch):
    from app import web

    monkeypatch.setattr(web, "SUBS", str(tmp_path / "subs.json"))
    web.add_sub("YnZ1K")
    assert web.toggle_sub("YnZ1K", False)["sub"]["enabled"] is False
    assert web.toggle_sub("YnZ1K", True)["sub"]["enabled"] is True
    assert web.remove_sub("YnZ1K")["ok"]
    assert web.subs_snapshot() == []


def test_run_subscriptions_skips_disabled(tmp_path, monkeypatch):
    from app import web

    monkeypatch.setattr(web, "SUBS", str(tmp_path / "subs.json"))
    web.add_sub("YnZ1K")
    web.toggle_sub("YnZ1K", False)
    called = []
    async def fake_check(*a, **kw):
        called.append(1)
        return {"new": [], "cursor": "", "error": ""}
    monkeypatch.setattr(subscribe, "check", fake_check)
    out = web.run_subscriptions()
    assert out["ok"] and called == [], "关掉的订阅不该被查"


def test_run_subscriptions_starts_jobs_and_advances_cursor(tmp_path, monkeypatch):
    from app import core, web

    monkeypatch.setattr(web, "SUBS", str(tmp_path / "subs.json"))
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    core.save_state(web.STATE, {"OLD-1": {"code": "OLD-1"}})
    web.add_sub("YnZ1K")
    async def fake_check(*a, **kw):
        return {"new": [{"code": "NEW-1", "date": "2026-10-20",
                         "magnet": "magnet:?xt=urn:btih:cc", "size_mb": 100, "tags": []}],
                "cursor": "2026-10-20", "error": ""}
    monkeypatch.setattr(subscribe, "check", fake_check)
    started = []
    monkeypatch.setattr(web, "start_job",
                        lambda src, code, info=None: started.append((code, src)) or "j1")
    out = web.run_subscriptions()
    assert started and started[0][0] == "NEW-1"
    assert out["results"][0]["started"][0]["code"] == "NEW-1"
    assert web.subs_snapshot()[0]["cursor"] == "2026-10-20", "游标要落盘"


def test_run_subscriptions_dedupes_archived(tmp_path, monkeypatch):
    """already 归档的番号不该重复下。"""
    from app import core, web

    monkeypatch.setattr(web, "SUBS", str(tmp_path / "subs.json"))
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    core.save_state(web.STATE, {"DUP-1": {"code": "DUP-1"}})
    web.add_sub("YnZ1K")
    async def fake_check(*a, **kw):
        return {"new": [{"code": "DUP-1", "date": "2026-10-20",
                         "magnet": "magnet:?xt=urn:btih:dd", "size_mb": 100, "tags": []}],
                "cursor": "2026-10-20", "error": ""}
    monkeypatch.setattr(subscribe, "check", fake_check)
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


def test_last_result_says_skipped_not_empty(tmp_path, monkeypatch):
    from app import web

    monkeypatch.setattr(web, "SUBS", str(tmp_path / "subs.json"))
    web.add_sub("YnZ1K")

    async def fake_check(*a, **kw):
        return {"new": [], "cursor": "2026-10-21", "error": "", "skipped": 2}

    monkeypatch.setattr(subscribe, "check", fake_check)
    web.run_subscriptions()
    assert "跳过 2 部" in web.subs_snapshot()[0]["last_result"]
