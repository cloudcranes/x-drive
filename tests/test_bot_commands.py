"""bot 两个只读命令 /status 与 /subs：纯本地读取、零副作用、读不到给回话。

测试目标：
  · /status 在有空任务、有今日记录、闸门关闭时输出关键数字与关键词；
  · /status 空状态不崩溃、给合理文案；
  · /subs 读到订阅表时列名称；读不到时回「还没有订阅」；
  · /subs 只读：调用前后订阅表内容不变。

全部离线：update/ctx 用 SimpleNamespace 造，reply_text 打桩记文本，不连 Telegram。
"""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from app import main, store


def cfg_for(tmp_path):
    return main.Config(
        token="x", allowed={1}, remote="gdrive", drive_root="X-Drive", max_mb=10,
        concurrency=1, state_path=str(tmp_path / "state.json"),
        tmp_root=str(tmp_path / "dl"), rclone_config="/dev/null",
        pending_dir=str(tmp_path / "pending"),
        metrics_path=str(tmp_path / "metrics.json"),
        state_dir=str(tmp_path / "work"), jobs_db=str(tmp_path / "jobs.db"),
    )


def _run(tmp_path, monkeypatch, cfg, handler_name):
    """驱动一个 on_* 命令处理器，返回它 reply_text 出去的文本。"""
    sent = []
    monkeypatch.setattr(main, "_STORE", None)   # 每个用例独立的任务库

    async def reply_text(text, **kw):
        sent.append(text)
        return SimpleNamespace()

    msg = SimpleNamespace(reply_text=reply_text, chat_id=7, text="")
    update = SimpleNamespace(effective_message=msg, effective_user=SimpleNamespace(id=1))
    ctx = SimpleNamespace(args=[], application=SimpleNamespace(bot_data={
        "cfg": cfg, "state": {}, "http": object(), "pending": {}}))

    asyncio.run(getattr(main, handler_name)(update, ctx))
    return sent


# ─────────────────────────────────────────── /status

def test_status_shows_active_today_and_gate_closed(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    st = store.Store(cfg.jobs_db)
    st.start("j1", "SSIS-123", "magnet:a")
    st.update("j1", stage="downloading")
    # 今日 metrics：ok 2、error 1
    os.makedirs(os.path.dirname(cfg.metrics_path), exist_ok=True)
    with open(cfg.metrics_path, "w", encoding="utf-8") as fh:
        json.dump({"total": {"ok": 2, "error": 1}, "updated": "today"}, fh)
    # 闸门关闭
    store.write_reconcile_mark({"source": "b.db"}, cfg.jobs_db)
    # 云添加可用（现在只有这一条下载链路，状态里要报它）
    monkeypatch.setattr(main.cloudadd, "ready", lambda get: (True, "ok"))

    sent = _run(tmp_path, monkeypatch, cfg, "on_status")

    text = sent[0]
    assert "进行中 1 个" in text
    assert "今日完成 2 · 失败 1" in text
    assert "云添加 可用" in text
    assert "闸门关闭" in text


def test_status_empty_state_does_not_crash(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)

    sent = _run(tmp_path, monkeypatch, cfg, "on_status")

    text = sent[0]
    assert "状态" in text
    assert "进行中 0 个" in text
    assert "今日完成 0 · 失败 0" in text


def test_status_unauthorized_user_is_silent(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    sent = []
    monkeypatch.setattr(main, "_STORE", None)

    async def reply_text(text, **kw):
        sent.append(text)
        return SimpleNamespace()

    msg = SimpleNamespace(reply_text=reply_text, chat_id=7, text="")
    update = SimpleNamespace(effective_message=msg,
                             effective_user=SimpleNamespace(id=999))  # 不在白名单
    ctx = SimpleNamespace(args=[], application=SimpleNamespace(bot_data={
        "cfg": cfg, "state": {}, "http": object(), "pending": {}}))

    asyncio.run(main.on_status(update, ctx))
    assert sent == [], "未授权用户不该收到任何回执"


# ─────────────────────────────────────────── /subs

def test_subs_lists_subscriptions(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    st = store.Store(cfg.jobs_db)
    st.state_put({"subs": {
        "javbus:actor:w1": {"name": "某演员", "id": "w1", "kind": "actor",
                            "enabled": True, "last_run": "2026-10-22T10:00:00",
                            "last_result": "发现 3 部"},
        "javbus:series:s9": {"name": "某系列", "id": "s9", "kind": "series",
                             "enabled": True, "last_run": "", "last_result": ""},
    }})

    sent = _run(tmp_path, monkeypatch, cfg, "on_subs")

    text = sent[0]
    assert "订阅 2 个" in text
    assert "某演员" in text and "演员" in text
    assert "某系列" in text and "系列" in text
    assert "2026-10-22T10:00:00" in text


def test_subs_empty_returns_friendly_message(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)

    sent = _run(tmp_path, monkeypatch, cfg, "on_subs")

    assert sent[0] == "还没有订阅。"


def test_subs_is_read_only(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    st = store.Store(cfg.jobs_db)
    st.state_put({"subs": {"javbus:actor:w1": {"name": "某演员", "id": "w1",
                                                "kind": "actor", "enabled": True}}})
    before = json.dumps(st.state_get("subs"), sort_keys=True)

    _run(tmp_path, monkeypatch, cfg, "on_subs")

    after = json.dumps(store.Store(cfg.jobs_db).state_get("subs"), sort_keys=True)
    assert before == after, "只读命令不得改动订阅表"
