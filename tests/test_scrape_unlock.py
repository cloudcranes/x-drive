"""补刮接口的三个缺口：解锁、单附件重试、批量校验分页。

不连任何真实服务：state 用临时文件，rclone 与 MetaTube 全部打桩。
"""
from __future__ import annotations

import json

import pytest

from app import core, metadata


def _env(tmp_path, monkeypatch):
    from app import store, web
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    monkeypatch.setattr(web, "JOBS", {})
    monkeypatch.setattr(web, "LEASES", {})
    monkeypatch.setattr(web, "_lease_live", {})
    core.save_state(web.STATE, {})
    return core, web


def _record(title="旧标题", locked=()):
    return {"code": "SSIS-123", "path": "gdrive:X-Drive/SSIS-123",
            "files": ["SSIS123.mp4"], "meta": {"code": "SSIS-123", "title": title},
            "locked": sorted(locked), "manifest": {"files": [
                {"name": "SSIS123.mp4", "size": 100, "ext": ".mp4"}]}}


# ─────────────────────────────────────────── D1-a 解锁

def test_unlock_removes_field_from_locks(tmp_path, monkeypatch):
    """unlock 真的把字段从锁定集合里移除，并写回状态。"""
    core, web = _env(tmp_path, monkeypatch)
    core.save_state(web.STATE, {"SSIS-123": _record(locked=["title", "actors"])})

    out = web.scrape_sync("SSIS-123", unlock=["title"])

    assert out["ok"] is True
    assert out["locked"] == ["actors"], "title 应已解锁，actors 仍是锁定"
    assert out["unlocked"] == ["title"]
    assert core.load_state(web.STATE)["SSIS-123"]["locked"] == ["actors"], "要落盘"


def test_unlock_then_resync_can_overwrite(tmp_path, monkeypatch):
    """解锁之后重新刮削就能覆盖该字段——这正是解锁的意义。"""
    core, web = _env(tmp_path, monkeypatch)
    core.save_state(web.STATE, {"SSIS-123": _record(title="旧标题", locked=["title"])})
    monkeypatch.setattr(web.metatube, "configured", lambda: True)

    async def fresh(code):
        return {"code": "SSIS-123", "title": "新标题"}

    monkeypatch.setattr(web.metatube, "scrape", fresh)
    monkeypatch.setattr(web.metatube, "matches", lambda code, d: True)

    locked = web.scrape_sync("SSIS-123", resync=True)
    assert "title" not in locked["changed"], "锁定时不该被覆盖"

    web.scrape_sync("SSIS-123", unlock=["title"])
    out = web.scrape_sync("SSIS-123", resync=True)
    assert out["changed"].get("title") == "新标题"
    assert core.load_state(web.STATE)["SSIS-123"]["meta"]["title"] == "新标题"


def test_unlock_ignores_unknown_field_names(tmp_path, monkeypatch):
    """只认 LOCKABLE 里的字段：非法名忽略，不会写进锁定集合。"""
    core, web = _env(tmp_path, monkeypatch)
    core.save_state(web.STATE, {"SSIS-123": _record(locked=["title"])})

    out = web.scrape_sync("SSIS-123", unlock=["token; rm -rf /", "../x"])

    assert out["ok"] is True
    assert out["locked"] == ["title"]
    assert "unlocked" not in out or out["unlocked"] == []


def test_lock_and_unlock_same_field_is_an_error(tmp_path, monkeypatch):
    """同一字段既锁又解：报错，不静默挑一个。"""
    core, web = _env(tmp_path, monkeypatch)
    core.save_state(web.STATE, {"SSIS-123": _record(locked=["title"])})

    out = web.scrape_sync("SSIS-123", lock=["title"], unlock=["title"])

    assert out["ok"] is False
    assert out["error"] == "同一字段不能同时锁定和解锁"
    assert out["fields"] == ["title"]
    assert core.load_state(web.STATE)["SSIS-123"]["locked"] == ["title"], "报错时不许改状态"


def test_lock_still_adds_and_returns_locked(tmp_path, monkeypatch):
    """加锁的老行为不变，返回里始终带当前锁定集合。"""
    core, web = _env(tmp_path, monkeypatch)
    core.save_state(web.STATE, {"SSIS-123": _record()})

    out = web.scrape_sync("SSIS-123", lock=["title", "bogus"])

    assert out["ok"] is True
    assert out["locked"] == ["title"]
    assert core.load_state(web.STATE)["SSIS-123"]["locked"] == ["title"]


# ─────────────────────────────────────────── D1-b 单附件重试

def _fake_rclone(monkeypatch, web, rc_by_name=None):
    sent = []

    def fake(cmd, src, dst, **kw):
        name = dst.rsplit("/", 1)[-1]
        sent.append(name)
        rc = (rc_by_name or {}).get(name, 0)
        return rc, "", "" if rc == 0 else "boom"

    monkeypatch.setattr(web, "rclone", fake)
    monkeypatch.setattr(web, "NFO_ON", True)
    monkeypatch.setattr(web, "POSTER_ON", False)
    return sent


def test_sync_attachments_only_pushes_named_file(tmp_path, monkeypatch):
    """only=["movie.nfo"] 时只推 movie.nfo，别的附件不推。"""
    core, web = _env(tmp_path, monkeypatch)
    sent = _fake_rclone(monkeypatch, web)

    out = web.sync_attachments("SSIS-123", _record(), only=["movie.nfo"])

    assert sent == ["movie.nfo"], f"只该推一个文件，实际推了 {sent}"
    assert out["synced"] == ["movie.nfo"]
    assert out["sync_failed"] == []
    assert "metadata.json" in out["skipped"], "没被点名的要列进 skipped"


def test_sync_attachments_without_only_pushes_everything(tmp_path, monkeypatch):
    """only 为空 / None 时行为与原来一致：全部重推。"""
    core, web = _env(tmp_path, monkeypatch)

    sent = _fake_rclone(monkeypatch, web)
    full = web.sync_attachments("SSIS-123", _record())
    assert set(sent) == {"movie.nfo", "metadata.json", "manifest.json"}
    assert "skipped" not in full

    sent.clear()
    web.sync_attachments("SSIS-123", _record(), only=[])
    assert set(sent) == {"movie.nfo", "metadata.json", "manifest.json"}


def test_sync_attachments_only_rejects_arbitrary_names(tmp_path, monkeypatch):
    """only 是白名单：路径穿越和不相干文件名都不收，不会拼进远端路径。"""
    core, web = _env(tmp_path, monkeypatch)
    sent = _fake_rclone(monkeypatch, web)

    out = web.sync_attachments("SSIS-123", _record(),
                               only=["../../etc/passwd", "SSIS123.mp4", "x.nfo"])

    assert sent == [], "非法名字一个都不许推"
    assert out["synced"] == []


def test_sync_attachments_never_pushes_video(tmp_path, monkeypatch):
    """视频永不重传：即使只有里点了视频名，也不推。"""
    core, web = _env(tmp_path, monkeypatch)
    sent = _fake_rclone(monkeypatch, web)

    web.sync_attachments("SSIS-123", _record(), only=["SSIS123.mp4"])

    assert sent == []
    assert "SSIS123.mp4" not in sent


def test_scrape_sync_passes_only_through(tmp_path, monkeypatch):
    """scrape_sync 把 only 透传给 sync_attachments（需要 confirm）。"""
    core, web = _env(tmp_path, monkeypatch)
    sent = _fake_rclone(monkeypatch, web)
    core.save_state(web.STATE, {"SSIS-123": _record()})

    out = web.scrape_sync("SSIS-123", confirm=True, only=["metadata.json"])

    assert out["ok"] is True
    assert sent == ["metadata.json"]
    assert out["synced"] == ["metadata.json"]


def test_scrape_sync_without_confirm_does_not_push(tmp_path, monkeypatch):
    core, web = _env(tmp_path, monkeypatch)
    sent = _fake_rclone(monkeypatch, web)
    core.save_state(web.STATE, {"SSIS-123": _record()})

    web.scrape_sync("SSIS-123", only=["metadata.json"])

    assert sent == [], "没有 confirm 不许动 Drive"


# ─────────────────────────────────────────── D1-c 批量校验分页

def _many(core, web, n):
    core.save_state(web.STATE, {f"T-{i:03d}": {"code": f"T-{i:03d}",
                                               "files": ["a.mp4"]} for i in range(n)})


def test_verify_all_reports_paging_contract(tmp_path, monkeypatch):
    """返回里必须有 offset/limit/total/next_offset/scanned。"""
    core, web = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(web, "verify_archive",
                        lambda code: {"ok": True, "code": code, "state": "校验通过"})
    _many(core, web, 5)

    out = web.verify_all(limit=2)

    assert out["ok"] is True
    assert (out["offset"], out["limit"], out["total"], out["next_offset"]) == (0, 2, 5, 2)
    assert out["scanned"] == 2
    assert len(out["items"]) == 2
    assert out["summary"] == {"校验通过": 2}, "summary 只统计本页"


def test_verify_all_paging_covers_everything(tmp_path, monkeypatch):
    """一路按 next_offset 翻页，能且只能覆盖全部记录，且不重复。"""
    core, web = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(web, "verify_archive",
                        lambda code: {"ok": True, "code": code, "state": "校验通过"})
    _many(core, web, 7)

    seen, offset, pages = [], 0, 0
    while offset is not None:
        page = web.verify_all(limit=3, offset=offset)
        assert page["scanned"] <= 3
        seen.extend(i["code"] for i in page["items"])
        offset = page["next_offset"]
        pages += 1
        assert pages < 10, "翻页没有终止"

    assert pages == 3
    assert sorted(seen) == sorted(f"T-{i:03d}" for i in range(7))
    assert len(seen) == len(set(seen)), "翻页不许重复"


def test_verify_all_next_offset_none_at_end(tmp_path, monkeypatch):
    core, web = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(web, "verify_archive",
                        lambda code: {"ok": True, "code": code, "state": "校验通过"})
    _many(core, web, 2)

    out = web.verify_all(limit=5)
    assert out["next_offset"] is None
    assert out["scanned"] == 2 and out["total"] == 2


def test_verify_all_offset_past_end_is_empty(tmp_path, monkeypatch):
    """offset 超出范围返回空页，而不是回退到第一批。"""
    core, web = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(web, "verify_archive",
                        lambda code: {"ok": True, "code": code, "state": "校验通过"})
    _many(core, web, 3)

    out = web.verify_all(limit=2, offset=10)
    assert out["items"] == [] and out["scanned"] == 0
    assert out["next_offset"] is None
    assert out["total"] == 3


def test_verify_all_bad_arguments_do_not_crash(tmp_path, monkeypatch):
    core, web = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(web, "verify_archive",
                        lambda code: {"ok": True, "code": code, "state": "校验通过"})
    _many(core, web, 2)

    out = web.verify_all(limit=0, offset=-5)
    assert out["offset"] == 0 and out["limit"] == 1
    assert out["scanned"] == 1


# ─────────────────────────────────────────── 路由透传（真起 http.server）

def test_routes_pass_unlock_only_offset(tmp_path, monkeypatch):
    """三个新参数都要经 HTTP 路由透传到实现函数，不是只改函数签名。"""
    import http.client
    import threading

    from app import web

    core, web = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(web, "AUTH", "")
    core.save_state(web.STATE, {"SSIS-123": _record(locked=["title", "actors"])})

    seen = {}

    def fake_sync(code, fields=None, lock=None, resync=False, confirm=False,
                  unlock=None, only=None):
        seen.update({"code": code, "lock": lock, "unlock": unlock, "only": only})
        return {"ok": True}

    def fake_verify_all(limit=50, offset=0):
        seen.update({"limit": limit, "offset": offset})
        return {"ok": True}

    monkeypatch.setattr(web, "scrape_sync", fake_sync)
    monkeypatch.setattr(web, "verify_all", fake_verify_all)
    httpd = web.serve("127.0.0.1", 0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def post(path, body):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("POST", path, body=json.dumps(body),
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        data = r.read()
        c.close()
        return json.loads(data)

    try:
        post("/api/scrape/sync", {"code": "SSIS-123", "unlock": ["title"],
                                  "only": ["movie.nfo"]})
        assert seen["unlock"] == ["title"], "unlock 没透传到 scrape_sync"
        assert seen["only"] == ["movie.nfo"], "only 没透传到 scrape_sync"

        post("/api/archive/verify", {"limit": 10, "offset": 20})
        assert (seen["limit"], seen["offset"]) == (10, 20), "offset 没透传到 verify_all"
    finally:
        httpd.shutdown()
        httpd.server_close()
