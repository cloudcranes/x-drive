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
import time
from types import SimpleNamespace

import pytest

from app import main, store
from app.storage import base as storage_base


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

def test_status_reports_cloud_capacity_not_local_disk(tmp_path, monkeypatch):
    """空间指标问的是**云盘**：本地下载取消后，报本地磁盘剩余没有参考价值。"""
    cfg = cfg_for(tmp_path)
    monkeypatch.setattr(main.cloudadd, "ready", lambda get: (True, "ok"))
    monkeypatch.setattr(main.storage, "quota", lambda: storage_base.Quota(
        total=20 * 1024 ** 4, used=40 * 1024 ** 3, free=19 * 1024 ** 4))

    sent = _run(tmp_path, monkeypatch, cfg, "on_status")

    text = sent[0]
    assert "云盘剩余" in text
    assert "20.00TB" in text
    assert "磁盘" not in text, "不该再报本地磁盘"


def test_status_capacity_unknown_does_not_claim_zero(tmp_path, monkeypatch):
    """问不到容量要说「无法探测」，不能写 0B（那会被读成「云盘满了」）。"""
    cfg = cfg_for(tmp_path)
    monkeypatch.setattr(main.cloudadd, "ready", lambda get: (True, "ok"))
    monkeypatch.setattr(main.storage, "quota", lambda: storage_base.Quota())

    sent = _run(tmp_path, monkeypatch, cfg, "on_status")

    assert "无法探测" in sent[0]
    assert "0B" not in sent[0]


def test_status_shows_active_today_and_gate_closed(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    st = store.Store(cfg.jobs_db)
    st.start("j1", "SSIS-123", "magnet:a")
    st.update("j1", stage="downloading")
    # 今日完成/失败按**任务表**算：网页侧归档的任务不进 metrics.json 的 total，
    # 拿 metrics 统计会把网页上的成果全漏掉（曾经恒显示「今日完成 0」）。
    now = time.time()
    st.start("j2", "SSIS-124", "magnet:b")
    st.update("j2", stage="done")
    st.start("j3", "SSIS-125", "magnet:c")
    st.update("j3", stage="failed")
    # metrics.json 里故意写一份过时的计数：它不该影响 /status 的数字
    os.makedirs(os.path.dirname(cfg.metrics_path), exist_ok=True)
    with open(cfg.metrics_path, "w", encoding="utf-8") as fh:
        json.dump({"total": {"ok": 99, "error": 99}, "updated": "today"}, fh)
    # 闸门关闭
    store.write_reconcile_mark({"source": "b.db"}, cfg.jobs_db)
    # 云添加可用（现在只有这一条下载链路，状态里要报它）
    monkeypatch.setattr(main.cloudadd, "ready", lambda get: (True, "ok"))

    sent = _run(tmp_path, monkeypatch, cfg, "on_status")

    text = sent[0]
    assert "进行中 1 个" in text
    assert "今日完成 1 · 失败 1" in text
    assert "云添加 可用" in text
    assert "闸门关闭" in text


def test_status_today_counts_ignore_other_days(tmp_path, monkeypatch):
    """"今日"只算当天建的：昨天完成的不算今天的完成。"""
    cfg = cfg_for(tmp_path)
    st = store.Store(cfg.jobs_db)
    old = time.time() - 86400 * 3
    st.start("j1", "SSIS-123", "magnet:a")
    st.update("j1", stage="done")
    # 把这条挪到三天前
    with st._conn() as conn:
        conn.execute("UPDATE jobs SET created_at=?, updated_at=? WHERE id=?",
                     (old, old, "j1"))

    assert main.today_counts(cfg) == (0, 0)


def test_status_today_counts_survive_broken_db(tmp_path, monkeypatch):
    """任务表读不出来时返回 0，不抛 —— /status 是只读命令，不能把 bot 带走。"""
    cfg = cfg_for(tmp_path)
    monkeypatch.setattr(main, "task_store", lambda cfg: (_ for _ in ()).throw(
        RuntimeError("db locked")))
    assert main.today_counts(cfg) == (0, 0)


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


# ─────────────────────────────────────────── 命令清单与菜单

def test_command_menu_covers_every_registered_command():
    """菜单里要包含全部命令，且说明非空（Telegram 会拒绝空说明）。"""
    names = [name for name, _ in main.command_menu()]
    for expected in ("find", "status", "subs", "list", "retry", "del", "help"):
        assert expected in names, f"菜单少了 /{expected}"
    for name, desc in main.command_menu():
        assert desc.strip(), f"/{name} 缺说明"
    assert len(names) == len(set(names)), "菜单里有重名命令"


def test_command_menu_matches_handler_registry():
    """清单里的每个处理器都得真实存在 —— 加了命令忘了写处理器要当场发现。"""
    import app.main as m
    for name, handler_name, _ in m.COMMANDS + m.EXTRA_COMMANDS:
        assert hasattr(m, handler_name), f"/{name} 指向不存在的处理器 {handler_name}"


def test_commands_lists_all_commands_in_chat(tmp_path, monkeypatch):
    """/commands 在会话里列出全部命令（客户端不显示 `/` 菜单时的兜底）。"""
    cfg = cfg_for(tmp_path)

    sent = _run(tmp_path, monkeypatch, cfg, "on_commands")

    text = sent[0]
    assert "命令列表" in text
    for name, _ in main.command_menu():
        assert f"/{name}" in text, f"列表里少了 /{name}"


def test_commands_unauthorized_user_is_silent(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    sent = []
    monkeypatch.setattr(main, "_STORE", None)

    async def reply_text(text, **kw):
        sent.append(text)
        return SimpleNamespace()

    msg = SimpleNamespace(reply_text=reply_text, chat_id=7, text="")
    update = SimpleNamespace(effective_message=msg,
                             effective_user=SimpleNamespace(id=999))
    ctx = SimpleNamespace(args=[], application=SimpleNamespace(bot_data={
        "cfg": cfg, "state": {}, "http": object(), "pending": {}}))

    asyncio.run(main.on_commands(update, ctx))
    assert sent == [], "未授权用户不该收到命令列表"


def test_sync_commands_registers_menu_with_telegram(tmp_path, monkeypatch):
    """启动时把清单同步到 Telegram 菜单。"""
    cfg = cfg_for(tmp_path)
    calls = []

    class FakeBot:
        async def set_my_commands(self, commands):
            calls.append(list(commands))

    app = SimpleNamespace(bot=FakeBot())
    asyncio.run(main.sync_commands(app))

    assert calls, "启动时必须注册一次菜单"
    assert calls[0] == main.command_menu()


def test_sync_commands_failure_does_not_raise(tmp_path, monkeypatch):
    """菜单注册失败不能把 bot 带走 —— 它只是给人看的列表。"""
    class FakeBot:
        async def set_my_commands(self, commands):
            raise RuntimeError("network down")

    app = SimpleNamespace(bot=FakeBot())
    asyncio.run(main.sync_commands(app))   # 不抛就算过

