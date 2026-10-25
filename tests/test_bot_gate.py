"""bot 侧必须遵守恢复闸门（E4），且统计口径与清理口径分开（E5 收尾）。

两条都不是「单元正确」就够的：
  · bot 与 web 是两个进程，内存暂停开关互相看不见，只认库内标记与旁路文件；
  · 上传成功但与记账失败并存时，清理要清、metrics 不能记 ok。
不连网络、不碰 /data：库与状态都在 tmp_path。
"""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from app import main, store, workspace


def cfg_for(tmp_path):
    return main.Config(
        token="x", allowed={1}, remote="gdrive", drive_root="X-Drive", max_mb=10,
        concurrency=1, state_path=str(tmp_path / "state.json"),
        tmp_root=str(tmp_path / "dl"), rclone_config="/dev/null",
        pending_dir=str(tmp_path / "pending"),
        metrics_path=str(tmp_path / "metrics.json"),
        state_dir=str(tmp_path / "work"), jobs_db=str(tmp_path / "jobs.db"),
    )


# ─────────────────────────────────────────── 闸门判据

def test_gate_open_without_any_mark(tmp_path):
    gate = main.scheduling_gate(cfg_for(tmp_path))
    assert gate["open"] is True and gate["reason"] == ""


def test_gate_reads_side_mark_written_by_another_process(tmp_path):
    """旁路文件由 web 侧写，bot 只读：两个进程靠它对齐。"""
    cfg = cfg_for(tmp_path)
    store.write_reconcile_mark({"source": "/data/backup-1.db"}, cfg.jobs_db)

    gate = main.scheduling_gate(cfg)

    assert gate["open"] is False
    assert "待对账" in gate["reason"]
    assert gate["mark"]["file"] is True, "应来自旁路文件"
    assert gate["mark"]["source"] == "/data/backup-1.db"


def test_gate_reads_db_mark_too(tmp_path):
    cfg = cfg_for(tmp_path)
    store.Store(cfg.jobs_db).state_put({store.RECONCILE_KEY: {"required": True}})

    gate = main.scheduling_gate(cfg)

    assert gate["open"] is False and gate["mark"]["db"] is True


def test_gate_closes_after_mark_cleared(tmp_path):
    """对账完成（两处都清）后 bot 必须立刻恢复放行，不能靠重启才生效。"""
    cfg = cfg_for(tmp_path)
    store.write_reconcile_mark({}, cfg.jobs_db)
    assert main.scheduling_gate(cfg)["open"] is False

    store.clear_reconcile_mark(cfg.jobs_db)
    assert main.scheduling_gate(cfg)["open"] is True


def test_gate_fails_closed_when_db_is_not_a_database(tmp_path):
    """库文件坏掉时按「停」处理：读不出状态 ≠ 可以开工。"""
    cfg = cfg_for(tmp_path)
    with open(cfg.jobs_db, "wb") as fh:
        fh.write(b"this is not sqlite")

    gate = main.scheduling_gate(cfg)

    assert gate["open"] is False, "读不出标记时必须拦，不能放行"


# ─────────────────────────────────────────── 启动补跑受闸门约束

def test_resume_pending_skips_when_gate_closed(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    store.write_reconcile_mark({}, cfg.jobs_db)
    ran = []

    async def fake_run_task(*a, **k):        # 一旦被调用就说明闸门没起作用
        ran.append(1)
        return "ok"

    monkeypatch.setattr(main, "run_task", fake_run_task)
    pending = {"k1": {"url": "magnet:?xt=urn:btih:" + "ab" * 20, "chat_id": None}}
    app = SimpleNamespace(bot_data={
        "cfg": cfg, "pending": pending, "state": {}, "http": object()})

    asyncio.run(main.resume_pending(app))

    assert ran == [], "待对账期间不许补跑"
    assert "k1" in pending, "跳过不等于丢弃：对账后还要能补跑"


def test_resume_pending_runs_when_gate_open(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    ran = []

    async def fake_run_task(*a, **k):
        ran.append(1)
        return "ok"

    monkeypatch.setattr(main, "run_task", fake_run_task)
    pending = {"k1": {"url": "magnet:?xt=urn:btih:" + "ab" * 20, "chat_id": None}}
    app = SimpleNamespace(bot_data={
        "cfg": cfg, "pending": pending, "state": {}, "http": object()})

    asyncio.run(main.resume_pending(app))

    assert ran == [1], "闸门开放时补跑照旧"


# ─────────────────────────────────────────── outcome 与 cleaned 分开

def _stub_success_path(monkeypatch, tmp_path, calls):
    """最小可用的成功链路：假下载 / 假整理 / 假上传 / 假刮削。"""

    async def fake_download(xl, client, source, dest, progress=None, **opts):
        dest.mkdir(parents=True, exist_ok=True)
        # 必须打归属标记：workspace.drop 只清「有标记」的目录，没有标记会静默跳过，
        # 「成功路径确实清理了」就变成空断言。
        # 目录在建之前可能已被上层创建，用 adopt（专为「目录已存在」的重名/预建
        # 场景设计）补标记；drop() 只认带标记的目录，缺了它清理断言就是空的。
        workspace.adopt(dest, workspace.key_of(dest.name), source, task_id="T")
        (dest / "raw.mp4").write_bytes(b"\x00" * (1 << 20))
        return {"task": "T", "dir": dest, "waited": 0.0, "reused": False}

    def fake_stage(source_dir, work, key):
        work.mkdir(parents=True, exist_ok=True)
        for p in source_dir.glob("*"):
            if p.is_file():
                (work / p.name).write_bytes(p.read_bytes())
        return list(work.glob("*"))

    async def fake_rclone(cfg, *args, timeout=180.0):
        if args[0] == "copy":
            calls["copied"] = sorted(p.name for p in os.scandir(args[1]) if p.is_file())
        return 0, "", ""

    async def fake_link(cfg, target):
        return "https://drive.example/x"

    async def fake_scrape(code, provider="", client=None):
        return {"code": code, "title": "标题"}

    monkeypatch.setattr(main.pipeline, "download", fake_download)
    monkeypatch.setattr(main.pipeline, "stage_copy", fake_stage)
    monkeypatch.setattr(main, "rclone", fake_rclone)
    monkeypatch.setattr(main, "folder_link", fake_link)
    monkeypatch.setattr(main.metatube, "configured", lambda: True)
    monkeypatch.setattr(main.metatube, "scrape", fake_scrape)
    monkeypatch.setattr(main, "_STORE", None)


def _run(tmp_path):
    cfg = cfg_for(tmp_path)
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data={
        "cfg": cfg, "state": {}, "sem": asyncio.Semaphore(1), "http": object()}))
    msg = SimpleNamespace(chat_id=7, text="")

    async def edit(t):
        msg.text = t
    msg.edit_text = edit
    reply = asyncio.run(main.deliver(ctx, "SSIS-777 magnet:?xt=urn:btih:" + "cd" * 20, msg))
    return reply, cfg


def _last_outcome(cfg):
    with open(cfg.metrics_path, encoding="utf-8") as fh:
        data = json.load(fh)
    rows = data.get("recent_timings") or []
    assert rows, f"没有耗时记录：{data}"
    return rows[-1]["outcome"]


def test_outcome_is_ok_on_clean_success(tmp_path, monkeypatch):
    calls = {}
    _stub_success_path(monkeypatch, tmp_path, calls)
    reply, cfg = _run(tmp_path)

    assert "✅" in reply, reply
    assert _last_outcome(cfg) == "ok"


def test_outcome_is_fail_when_task_bookkeeping_raises(tmp_path, monkeypatch):
    """归档落盘但任务终态失败：如实报告、metrics记fail、原件留存。"""
    calls = {}
    _stub_success_path(monkeypatch, tmp_path, calls)

    # Patch the real execution write interface, not the legacy unfenced finish.
    def boom(self, job_id, owner, stage, error=""):
        raise RuntimeError("任务表写不进去")

    monkeypatch.setattr(type(main.task_store(cfg_for(tmp_path))), "finish_cas", boom)
    reply, cfg = _run(tmp_path)

    assert "⚠️" in reply, f"应当如实报失败：{reply}"
    assert _last_outcome(cfg) == "fail", "同一任务不能一边记 failed 一边记 ok"
    key = workspace.key_of("SSIS-777")
    assert workspace.staging(cfg.tmp_root, key).is_dir(), "任务记账失败应保留原件"
    assert workspace.workspace(cfg.state_dir, key).is_dir(), "任务记账失败应保留工作区"
