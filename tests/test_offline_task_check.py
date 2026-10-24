"""迅雷离线任务状态检查：解析失败要早点报，别干等。

起因（2026-10-24 实测，MIMK-267）：用户挑的磁力 0 个做种，迅雷云盘回
    phase = PHASE_TYPE_ERROR, message = "解析失败，请重试或更换链接"
但目录那边什么都不会出现 —— 没有这个检查，任务会一直卡在 cloud 阶段
（默认等两小时），用户只看到「卡住了」，完全不知道迅雷早说不行了。

对照：CD2 的 ListAllOfflineFiles / GetOfflineQuotaInfo 对迅雷云盘不支持
（"cloud account not found"），只有官方 API 的 /drive/v1/tasks 查得到。
"""
from __future__ import annotations

import pytest

from app import cloudingest
from app import xunleipan as xp


def test_info_hash_of():
    assert xp.info_hash_of(
        "magnet:?xt=urn:btih:D18B6256A58185252F4A3B8767B28FB38D059821&dn=x"
    ) == "d18b6256a58185252f4a3b8767b28fb38d059821"
    assert xp.info_hash_of("not a magnet") == ""


def test_offline_tasks_filters_only_offline(monkeypatch):
    """只认 type=offline 的任务（其它是 event-deletion / user#runner 那些）。"""
    fake = {
        "tasks": [
            {"type": "offline", "id": "A", "name": "x.mp4", "phase": "PHASE_TYPE_RUNNING",
             "progress": {"percent": 30}, "message": "", "params": {"url": "magnet:?x"}},
            {"type": "event-deletion", "id": "B", "name": "", "phase": "",
             "progress": {}, "message": "", "params": {}},
        ]
    }
    monkeypatch.setattr(xp, "_get", lambda *a, **k: fake)
    tasks = xp.offline_tasks()
    assert len(tasks) == 1
    assert tasks[0]["id"] == "A"
    assert tasks[0]["phase"] == "PHASE_TYPE_RUNNING"


def test_offline_task_by_hash_matches(monkeypatch):
    fake = {
        "tasks": [
            {"type": "offline", "id": "A", "name": "", "phase": "PHASE_TYPE_ERROR",
             "progress": 0, "message": "解析失败，请重试或更换链接",
             "params": {"url": "magnet:?xt=urn:btih:ABCDEF"}},
        ]
    }
    monkeypatch.setattr(xp, "_get", lambda *a, **k: fake)
    t = xp.offline_task_by_hash("abcdef")
    assert t and t["id"] == "A"
    assert t["message"] == "解析失败，请重试或更换链接"
    assert xp.offline_task_by_hash("nosuchhash") is None


def test_check_offline_task_reports_error(monkeypatch):
    """迅雷说失败了 → 给出原因。"""
    monkeypatch.setattr(xp, "offline_task_by_hash",
                        lambda ih, **k: {"phase": "PHASE_TYPE_ERROR",
                                         "message": "解析失败，请重试或更换链接",
                                         "error_detail": ""})
    why = cloudingest.check_offline_task("magnet:?xt=urn:btih:AA")
    assert "解析失败" in why


def test_check_offline_task_silent_while_running(monkeypatch):
    """还在跑 → 不报（空串）。"""
    monkeypatch.setattr(xp, "offline_task_by_hash",
                        lambda ih, **k: {"phase": "PHASE_TYPE_RUNNING", "message": ""})
    assert cloudingest.check_offline_task("magnet:?xt=urn:btih:AA") == ""


def test_check_offline_task_silent_when_not_found(monkeypatch):
    """查不到任务 → 当「还在跑」，不能把正常任务判死。"""
    monkeypatch.setattr(xp, "offline_task_by_hash", lambda ih, **k: None)
    assert cloudingest.check_offline_task("magnet:?xt=urn:btih:AA") == ""


def test_check_offline_task_ignores_query_errors(monkeypatch):
    """查状态本身出错（网络/鉴权）→ 忽略，不能因此把任务判死。"""

    def boom(*a, **k):
        raise RuntimeError("网络超时")

    monkeypatch.setattr(xp, "offline_task_by_hash", boom)
    assert cloudingest.check_offline_task("magnet:?xt=urn:btih:AA") == ""


def test_wait_landing_fails_fast_on_offline_error(monkeypatch):
    """迅雷报解析失败 → 立刻抛，别干等。"""
    import asyncio

    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        return "解析失败，请重试或更换链接"

    monkeypatch.setattr(cloudingest, "check_offline_task", boom)
    monkeypatch.setattr(cloudingest, "measure", lambda *a, **k: {})

    with pytest.raises(cloudingest.LandingTimeout) as ei:
        asyncio.run(cloudingest.wait_landing(
            object(), "/inbox", {}, poll=0.01, timeout=30, stable=1,
            source="magnet:?xt=urn:btih:AA"))
    assert "解析失败" in str(ei.value)
    assert calls["n"] == 1, "第一轮就该查一次"


def test_wait_landing_without_source_never_queries(monkeypatch):
    """没给 source → 不查（省掉官方 API 调用）。"""
    import asyncio

    def boom(*a, **k):
        raise AssertionError("不该查离线任务")

    monkeypatch.setattr(cloudingest, "check_offline_task", boom)
    monkeypatch.setattr(cloudingest, "measure", lambda *a, **k: {})

    with pytest.raises(cloudingest.LandingTimeout):
        asyncio.run(cloudingest.wait_landing(
            object(), "/inbox", {}, poll=0.01, timeout=0.05, stable=1))
