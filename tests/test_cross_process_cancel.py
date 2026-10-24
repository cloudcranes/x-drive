"""2.4 跨进程取消：Web 停止按钮停得了 bot 正在跑的任务。

取消请求必须落库：web 与 bot 是两个进程，进程内的取消事件互相看不见。
以前 Web 从数据库展示 bot 任务，停止接口却只认本进程内存，报「任务不存在」，
用户以为停了，bot 照跑。
"""
from __future__ import annotations

import time

import pytest

from app import main as bot, store, web


def _fresh(tmp_path, monkeypatch):
    st = store.Store(str(tmp_path / "jobs.db"))
    monkeypatch.setattr(web, "STORE", st)
    monkeypatch.setattr(web, "JOBS", {})
    monkeypatch.setattr(web, "CANCEL", {})
    monkeypatch.setattr(web, "LEASES", {})
    monkeypatch.setattr(web, "_lease_live", {})
    return st


# ---------------------------------------------------------------- store 层

def test_request_cancel_marks_and_clears(tmp_path):
    s = store.Store(str(tmp_path / "jobs.db"))
    s.start("j1", "DEMO-1", "magnet:a")
    assert not s.cancel_marked("j1")
    assert s.request_cancel("j1")
    assert s.cancel_marked("j1")
    assert s.clear_cancel("j1") is True
    assert not s.cancel_marked("j1")


def test_request_cancel_refuses_finished(tmp_path):
    s = store.Store(str(tmp_path / "jobs.db"))
    s.start("j1", "DEMO-1", "magnet:a")
    s.finish("j1", "done")
    assert not s.request_cancel("j1"), "已结束的任务无可取消"
    assert not s.cancel_marked("j1")


def test_cancel_mark_is_visible_across_process(tmp_path):
    """模拟另一个进程：两个 Store 实例开同一个库。"""
    path = str(tmp_path / "jobs.db")
    web_side, bot_side = store.Store(path), store.Store(path)
    web_side.start("j1", "DEMO-1", "magnet:a")
    assert bot_side.request_cancel("j1")
    assert web_side.cancel_marked("j1"), "库里标记，另一个进程要能看到"


# ---------------------------------------------------------------- web 层

def test_stop_job_accepts_job_owned_by_other_process(tmp_path, monkeypatch):
    """bot 在跑的任务（库里有、本进程没线程）也要能停。"""
    st = _fresh(tmp_path, monkeypatch)
    st.start("b1", "DEMO-1", "magnet:a", owner="bot")
    st.update("b1", stage="downloading")
    assert web.stop_job("b1") is True
    assert st.cancel_marked("b1"), "要写跨进程取消标记"
    assert st.get("b1")["stage"] == "downloading", "执行者自己收尾，接口只记标记"


def test_request_cancel_falls_back_to_other_process(tmp_path, monkeypatch):
    st = _fresh(tmp_path, monkeypatch)
    st.start("b1", "DEMO-1", "magnet:a")
    # 本进程内存里没有这条任务
    assert web.request_cancel("b1") is True
    assert web.cancel_requested("b1") is True


def test_web_cancel_seen_by_bot_and_vice_versa(tmp_path, monkeypatch):
    st = _fresh(tmp_path, monkeypatch)
    st.start("b1", "DEMO-1", "magnet:a")
    cfg = bot.Config.from_env()
    monkeypatch.setattr(bot, "task_store", lambda _cfg: st)
    assert bot.cancel_requested(cfg, "b1") is False
    web.request_cancel("b1")
    assert bot.cancel_requested(cfg, "b1") is True, "bot 要能消费 web 写下的标记"


def test_bot_require_lease_raises_cancelled(tmp_path, monkeypatch):
    st = _fresh(tmp_path, monkeypatch)
    st.start("b1", "DEMO-1", "magnet:a")
    st.request_cancel("b1")
    cfg = bot.Config.from_env()
    monkeypatch.setattr(bot, "task_store", lambda _cfg: st)
    monkeypatch.setattr(bot, "lease_alive", lambda _cfg, _j: True)
    with pytest.raises(bot.JobCancelled):
        bot.require_lease(cfg, "b1")


# ---------------------------------------------------------------- 提醒与状态

def test_stop_of_finished_job_reports_not_found(tmp_path, monkeypatch):
    st = _fresh(tmp_path, monkeypatch)
    st.start("j1", "DEMO-1", "magnet:a")
    st.finish("j1", "done")
    assert web.stop_job("j1") is False, "已结束的任务不再报成功"
