"""2.5/2.6 Bot 恢复走共享任务表 + 统一执行准入。

两个真实缺口：
  · 新任务只写 jobs 表，恢复却只读旧 pending 文件 —— bot 单独重启后中断的
    任务没人接，番号还被占着，/retry 撞唯一索引；
  · 新消息与 /retry 不查调度闸门 —— 恢复后待对账期间，bot 照着旧快照开工。
"""
from __future__ import annotations

import asyncio

import pytest

from app import cloudadd, main, store


def _cloud_ok(monkeypatch):
    """云添加可用（bot 现在只有这一条下载链路）。"""
    monkeypatch.setattr(cloudadd, "ready", lambda get: (True, "ok"))


def cfg_for(tmp_path):
    return main.Config(
        token="x", allowed={1}, remote="gdrive", drive_root="X-Drive", max_mb=10,
        concurrency=1, state_path=str(tmp_path / "state.json"),
        tmp_root=str(tmp_path / "dl"), rclone_config="/dev/null",
        pending_dir=str(tmp_path / "pending"),
        metrics_path=str(tmp_path / "metrics.json"),
        state_dir=str(tmp_path / "work"), jobs_db=str(tmp_path / "jobs.db"),
    )


# ─────────────────────────────────────────── 2.6 统一准入

def test_run_task_is_held_when_gate_closed(tmp_path, monkeypatch):
    """闸门关闭时新消息不开工：任务留在任务表等网页放行。"""
    cfg = cfg_for(tmp_path)
    _cloud_ok(monkeypatch)
    monkeypatch.setattr(main, "scheduling_gate",
                        lambda _c: {"open": False, "reason": "库处于待对账状态", "mark": {}})
    started = []
    monkeypatch.setattr(main, "claim_task",
                        lambda c, k, s, ch: started.append(k) or "j1")
    out = asyncio.run(main.run_task(cfg, None, {}, "magnet:?xt=urn:btih:" + "a" * 40,
                                    lambda t: asyncio.sleep(0), force=True, chat_id=1))
    assert "调度闸门关闭" in out
    assert started == [], "闸门关闭时不许领任务开工"


def test_run_task_claims_when_gate_open(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    _cloud_ok(monkeypatch)
    monkeypatch.setattr(main, "scheduling_gate",
                        lambda _c: {"open": True, "reason": "", "mark": {}})
    monkeypatch.setattr(main, "claim_task", lambda c, k, s, ch: "")
    out = asyncio.run(main.run_task(cfg, None, {}, "magnet:?xt=urn:btih:" + "a" * 40,
                                    lambda t: asyncio.sleep(0), force=True, chat_id=1))
    assert "任务表里已有未结束任务" in out


# ─────────────────────────────────────────── 2.5 沿用原 job_id

def test_run_task_resume_reuses_job_id(tmp_path, monkeypatch):
    """续跑沿用原任务记录：不新建（否则同番号撞唯一索引，旧任务永远占坑）。"""
    cfg = cfg_for(tmp_path)
    st = store.Store(cfg.jobs_db)
    st.start("old1", "MIMK-290", "magnet:?xt=urn:btih:" + "b" * 40)
    st.update("old1", stage="downloading")
    _cloud_ok(monkeypatch)
    monkeypatch.setattr(main, "scheduling_gate",
                        lambda _c: {"open": True, "reason": "", "mark": {}})
    monkeypatch.setattr(main, "task_store", lambda _c: st)
    claimed = []
    monkeypatch.setattr(main, "claim_task",
                        lambda c, k, s, ch: claimed.append(k) or "new9")
    monkeypatch.setattr(main, "lease_begin", lambda _c, _j: False)   # 让它快速收尾

    asyncio.run(main.run_task(cfg, None, {}, "magnet:?xt=urn:btih:" + "b" * 40,
                              lambda t: asyncio.sleep(0), force=True, chat_id=1,
                              resume_id="old1"))

    assert claimed == [], "resume_id 时不该新建任务"
    assert st.get("old1")["stage"] == "downloading", "原记录还在，没被新 id 顶掉"


def test_resume_pending_uses_jobs_table_not_only_pending_files(tmp_path, monkeypatch):
    """主恢复源是共享任务表：没有 pending 文件也照样续跑。"""
    cfg = cfg_for(tmp_path)
    st = store.Store(cfg.jobs_db)
    st.start("b1", "MIMK-290", "magnet:?xt=urn:btih:" + "c" * 40)
    st.update("b1", stage="downloading")

    class FakeApp:
        bot_data = {}
        bot = None

    app = FakeApp()
    app.bot_data = {"cfg": cfg, "pending": {}, "state": {}, "http": None}
    ran: list[str] = []

    async def fake_run_task(cfg_, client, state, text, progress, **kw):
        ran.append(kw.get("resume_id") or "")
        return "ok"

    monkeypatch.setattr(main, "run_task", fake_run_task)
    monkeypatch.setattr(main, "scheduling_gate",
                        lambda _c: {"open": True, "reason": "", "mark": {}})
    monkeypatch.setattr(main, "task_store", lambda _c: st)
    # 前置自检：mock 必须真的挂上（顺序污染时能看到是哪一层失灵）
    assert main.task_store(cfg) is st, "task_store mock 未生效"
    assert main.scheduling_gate(cfg)["open"] is True
    assert [j["id"] for j in st.unfinished_any()] == ["b1"], "库里该有待续任务"
    asyncio.run(main.resume_pending(app))
    assert ran == ["b1"], f"应从任务表续跑原 job_id，实为 {ran}"
