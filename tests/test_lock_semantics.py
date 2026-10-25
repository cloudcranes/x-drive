"""E8：锁定空值必须被尊重，且预览与实际写入用同一套判定。

两条已确证的缺陷：
  1. 旧 merge 只在旧值**非空**时尊重锁定，于是用户「清空并锁定」之后，
     下一次刮削又把值填回去；
  2. scrape_preview 自己写了一套跳过逻辑（无条件跳过锁字段），而 scrape_sync
     走 metadata.merge —— 两处判据分叉时，预览显示「不会覆盖」，实际却覆盖了。

这里同时钉住「同源」这个不变量：对同一输入，preview 报出的 changes 必须与
真正 sync 之后落盘的 meta 完全吻合。
"""
from __future__ import annotations

import pytest

from app import core, metadata


def _env(tmp_path, monkeypatch):
    from app import store, web
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    monkeypatch.setattr(web, "JOBS", {})
    monkeypatch.setattr(web, "LEASES", {})
    monkeypatch.setattr(web, "_lease_live", {})
    # 手动刮削走两个源（javbus 字段全、metatube 覆盖 provider 多）——
    # 这里把 javbus 桩掉：不桩的话测试会真的联网，拿到真数据，断言就不准了。
    async def no_javbus(code, path=None, client=None, timeout=0, refresh=False):
        return None

    monkeypatch.setattr(web.meta, "lookup", no_javbus)
    core.save_state(web.STATE, {})
    return core, web


# ─────────────────────────────────────────── merge 语义

def test_locked_empty_string_is_preserved():
    """用户清空并锁定：下次刮到的值不许把空字符串顶掉。"""
    out = metadata.merge({"summary": ""}, {"summary": "被填回"}, locks={"summary"})
    assert out["summary"] == ""


def test_locked_empty_list_is_preserved():
    out = metadata.merge({"actors": []}, {"actors": ["某演员"]}, locks={"actors"})
    assert out["actors"] == []


def test_locked_zero_is_preserved():
    out = metadata.merge({"year": 0}, {"year": 2024}, locks={"year"})
    assert out["year"] == 0


def test_locked_nonempty_value_is_preserved():
    out = metadata.merge({"title": "旧标题"}, {"title": "新标题"}, locks={"title"})
    assert out["title"] == "旧标题"


def test_unlock_then_value_follows_fresh():
    """解锁之后才允许被覆盖 —— 这正是解锁的意义。"""
    out = metadata.merge({"summary": "旧"}, {"summary": "新"}, locks=set())
    assert out["summary"] == "新"


def test_lock_does_not_block_first_time_value():
    """记录里还没有这个字段时，锁定不能把首次刮到的内容挡在门外。"""
    out = metadata.merge({}, {"title": "首次刮到的标题"}, locks={"title"})
    assert out["title"] == "首次刮到的标题"


def test_unlocked_empty_value_does_not_clobber():
    """未锁定字段的既有行为不变：空值不覆盖有效值。"""
    out = metadata.merge({"title": "有效"}, {"title": ""})
    assert out["title"] == "有效"


# ─────────────────────────────────────────── 预览与实际同源

def _fresh(monkeypatch, web, payload):
    """让 metatube.scrape 返回固定数据（不连网络）。"""
    monkeypatch.setattr(web.metatube, "configured", lambda: True)
    monkeypatch.setattr(web.metatube, "matches", lambda code, d: True)

    async def fake(code, provider="", client=None):
        return dict(payload)

    monkeypatch.setattr(web.metatube, "scrape", fake)


@pytest.mark.parametrize("locks", [[], ["summary"], ["title"], ["title", "summary"]])
def test_preview_and_sync_agree(tmp_path, monkeypatch, locks):
    """同一输入下，预览报的差异必须等于实际写入的结果（逐字段逐一核对）。"""
    core, web = _env(tmp_path, monkeypatch)
    old = {"code": "SSIS-123", "title": "旧标题", "summary": "旧简介",
           "actors": ["甲"]}
    core.save_state(web.STATE, {"SSIS-123": {"code": "SSIS-123", "meta": dict(old),
                                             "locked": sorted(locks)}})
    fresh = {"code": "SSIS-123", "title": "新标题", "summary": "新简介",
             "actors": ["乙"]}
    _fresh(monkeypatch, web, fresh)

    preview = web.scrape_preview("SSIS-123")
    assert preview["ok"] is True

    web.scrape_sync("SSIS-123", resync=True)          # 不 confirm，只写站内记录
    saved = core.load_state(web.STATE)["SSIS-123"]["meta"]

    for key in metadata.LOCKABLE:
        p = (preview["changes"].get(key) or {}).get("to", "（无差异）")
        actual = saved.get(key, "（无差异）")
        if key not in preview["changes"]:
            assert saved.get(key) == old.get(key), \
                f"{key} 预览说不变，实际却变成 {saved.get(key)!r}"
        else:
            assert actual == p, f"{key} 预览说会变成 {p!r}，实际是 {actual!r}"


def test_preview_shows_no_change_for_locked_empty_field(tmp_path, monkeypatch):
    """清空并锁定后，预览也不能说要变。"""
    core, web = _env(tmp_path, monkeypatch)
    core.save_state(web.STATE, {"SSIS-123": {
        "code": "SSIS-123", "meta": {"summary": ""}, "locked": ["summary"]}})
    _fresh(monkeypatch, web, {"code": "SSIS-123", "summary": "新简介"})

    preview = web.scrape_preview("SSIS-123")

    assert "summary" not in preview["changes"], preview


def test_preview_reports_first_time_field(tmp_path, monkeypatch):
    """记录里没有的字段，预览要如实报「会新增」。"""
    core, web = _env(tmp_path, monkeypatch)
    core.save_state(web.STATE, {"SSIS-123": {
        "code": "SSIS-123", "meta": {"code": "SSIS-123"}, "locked": ["title"]}})
    _fresh(monkeypatch, web, {"code": "SSIS-123", "title": "首次标题"})

    preview = web.scrape_preview("SSIS-123")

    # 标题补上番号前缀（站内惯例：番号 + 标题）
    assert preview["changes"]["title"]["to"] == "SSIS-123 首次标题"
