"""测试隔离的两道防线：遗留线程不能变成僵尸，测试不能连真实迅雷面板。

两次真实事故：
  1. `web._threads.clear()` 只清字典不停线程 —— 那些线程从字典里消失，
     conftest 再也找不到它们、停不掉它们，继续在下一个用例的桩下推进。
  2. 于是它推进到下载那一步时，如果下一个用例没打桩 `pipeline.download`，
     它就真的去连**真实迅雷面板**：容器内实测撞上「平台上已有同一个种子在
     下载」（任务 VP4con1layjQOyCStyRiFu82A1），把毫不相干的恢复用例搞红。
     更糟的是它会真的占用平台的下载位。

这两条防线各自用一个用例钉住：都是行为级，不看源码字符串。
"""
from __future__ import annotations

import threading
import time

import pytest

from app import web


def test_clearing_threads_dict_does_not_hide_running_thread(tmp_path, monkeypatch):
    """`web._threads.clear()` 之后，线程仍在跑 —— 防线必须按名字兜住它。

    先复现「清字典 → 线程变隐形」，再验证 conftest 的兜底扫描仍能找到它。
    """
    from tests import conftest as cft

    started = threading.Event()
    release = threading.Event()

    def fake_run(job_id, source, code):
        started.set()
        release.wait(5)

    monkeypatch.setattr(web, "run_web_job", fake_run)
    monkeypatch.setattr(web, "STORE", type("S", (), {
        "path": str(tmp_path / "jobs.db"),
        "get": lambda self, jid: {"total": 0, "code": "A-1"},
        "update": lambda self, jid, **kw: None,
        "update_cas": lambda self, jid, owner, **kw: True,
        "claim_owner": lambda self, jid, token: True,
    })())
    monkeypatch.setattr(web, "_lease_live", {})
    monkeypatch.setattr(web, "LEASES", {})
    monkeypatch.setattr(web, "WAITING", {"j1": ("magnet:a", "A-1")})
    monkeypatch.setattr(web, "JOBS", {})

    web.admit("j1")
    assert started.wait(5), "任务线程没起来"
    web._threads.clear()                       # fixture 干的事：只清字典
    alive = [t for t in threading.enumerate()
             if t.name.startswith("job-") and t.is_alive()]
    assert alive, "线程应该还在跑 —— 它只是从字典里消失了（这正是问题所在）"
    assert not web._threads, "字典确实被清空了"

    # conftest 的兜底扫描：按名字找，而不是只信字典。
    # 断言点是「取消标记打到了正确的 job_id」—— 线程能不能退出取决于它自己
    # 是否检查 CANCEL（真实任务线程会），这不是这里要测的。
    cft._drain_job_threads(web, grace=0.2)
    with web._jobs_lock:
        marked = set(web.CANCEL.keys())
    assert "j1" in marked, f"取消标记没打到 job_id：{marked}"
    release.set()


def test_real_xunlei_is_blocked_in_tests():
    """测试环境的迅雷客户端指向连不通的地址，漏网的真实调用会立刻失败。"""
    from app import xunlei

    assert web._XL is not None
    assert web._XL.base.startswith("http://127.0.0.1:"), \
        f"测试环境不该指向真实面板：{web._XL.base}"
    # 9 号端口是 discard 端口，连它必然失败
    assert web._XL.base != "http://127.0.0.1:2345", \
        "测试环境竟然指向了真实迅雷面板"


def test_drain_marks_threads_found_by_name_not_only_by_dict(tmp_path, monkeypatch):
    """_threads 是空的时，只要还有活的 job-* 线程，也必须被找到并打上取消标记。

    「线程名 → job_id」的换算是关键：取消标记按 job_id 索引，名字找得到却
    按名字打标记的话，线程查 CANCEL[job_id] 就查不到，标记等于没打。
    """
    from tests import conftest as cft

    stop = threading.Event()

    def worker():
        # 真实任务线程会检查 CANCEL[job_id]；这里照做，才能验证标记确实生效
        while not stop.is_set():
            with web._jobs_lock:
                if web.CANCEL.get("ghost"):
                    return
            time.sleep(0.01)

    t = threading.Thread(target=worker, name="job-ghost")
    t.start()
    try:
        with web._threads_lock:
            web._threads.clear()             # 字典里没有它
        cft._drain_job_threads(web, grace=2.0)
        assert not t.is_alive(), "按名字兜底找到的线程没收到取消标记"
    finally:
        stop.set()
        t.join(2)
