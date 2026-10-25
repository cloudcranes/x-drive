"""真正的跨进程执行交错：两个 OS 进程共用一个任务库。

前面的 test_execution_fencing.py 用 copy_context() 在**同一进程**里模拟两个
执行者 —— 它能验证「不借别人的 token」，但验证不了真正跨进程的那几件事：

  · 租约心跳与接管在两个进程之间真的能看见（各自的内存状态互不相通）；
  · 旧进程在**自己**的进程里发起的写入，SQLite 层面能不能挡住；
  · 旧进程收尾时会不会踩掉新进程的取消标记；
  · 旧进程退出时会不会把新进程正在跑的任务写成终态。

所以这里用 multiprocessing 起真进程：两边开同一个库文件，各自领取、
各自写入，然后让旧进程在新进程接管之后继续动作。

跑法：pytest tests/test_execution_handoff_process.py
"""
from __future__ import annotations

import multiprocessing as mp
import os
import time
from pathlib import Path

import pytest

from app import store


# ─────────────────────────────────────────── 子进程里的动作

def _child_claim(db_path: str, job_id: str, ready, release, results) -> None:
    """子进程 A：领取租约，等新进程接管后继续动作。

    它模拟「旧执行者的耗时调用返回了」：拿到新阶段的控制权后，
    依次尝试写进度、写失败终态、清取消标记、释放租约。

    每一步的结果都回传给父进程 —— 父进程据此确认「A 真的试过了，
    是被挡下来的」，而不是「A 压根没动」。
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    from app import store as store_mod

    if os.environ.get("XDRIVE_UNFENCED"):
        # 反证用：把 CAS 退化成无条件写（等价于修复前的行为）
        def unguarded(self, job_id, owner, **fields):
            self.update(job_id, **fields)
            return True
        store_mod.Store.update_cas = unguarded

    st = store_mod.Store(db_path)
    token = f"procA:{os.getpid()}"
    claimed = st.claim_owner(job_id, token)
    ready.set()
    release.wait(20)

    result = {
        "claimed": claimed,
        "late_progress": st.update_cas(job_id, token, percent=99, stage="uploading"),
        "late_failure": st.update_cas(job_id, token, stage="failed", error="A late failure"),
        "late_done": st.update_cas(job_id, token, stage="done"),
        "cleared_cancel": st.clear_cancel(job_id, owner=token),
    }
    st.release(job_id, token)
    result["released"] = True
    results.put(result)


def _child_takeover(db_path: str, job_id: str, ready, results) -> None:
    """子进程 B：等 A 的心跳过期后接管这条任务。"""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    from app import store as store_mod

    st = store_mod.Store(db_path)
    # A 的心跳已经过期（用 now 参数直接推进，避免真等 120s）
    token = f"procB:{os.getpid()}"
    claimed = st.claim_owner(job_id, token, now=time.time() + store_mod.STALE_BEAT + 1)
    wrote = st.update_cas(job_id, token, stage="uploading", percent=42, error="B active")
    st.request_cancel(job_id)
    results.put({"claimed": claimed, "wrote": wrote, "owner": token})
    ready.set()


# ─────────────────────────────────────────── 用例

def _await(results, key: str, timeout: float = 15.0) -> dict:
    """等一条含指定键的子进程结果。

    队列是 FIFO 且两个子进程都往里放，按「到达顺序 == 期望顺序」来取会
    偶发取错（B 先放）。这里按**内容**识别：反复取，取到带这个键的那条为止。
    """
    deadline = time.monotonic() + timeout
    seen: list[dict] = []
    while time.monotonic() < deadline:
        try:
            item = results.get(timeout=max(0.1, deadline - time.monotonic()))
        except Exception as exc:            # noqa: BLE001 队列空/超时
            if type(exc).__name__ == "Empty":
                continue
            raise
        if key in item:
            return item
        seen.append(item)
    raise AssertionError(f"没等到含 {key!r} 的结果，已收到：{seen}")


@pytest.mark.skipif(os.name == "nt", reason="跨进程用例用 fork/spawn，Windows 上另走")
def test_old_process_cannot_overwrite_new_owner_across_processes(tmp_path):
    db_path = str(tmp_path / "jobs.db")
    st = store.Store(db_path)
    st.start("j1", "DEMO-1", "magnet:a")

    ctx = mp.get_context("spawn")
    ready_a = ctx.Event()
    release_a = ctx.Event()
    ready_b = ctx.Event()
    results = ctx.Queue()

    proc_a = ctx.Process(target=_child_claim, args=(db_path, "j1", ready_a, release_a, results))
    proc_a.start()
    assert ready_a.wait(20), "子进程 A 没起来"
    assert st.get("j1")["exec_owner"].startswith("procA:")

    proc_b = ctx.Process(target=_child_takeover, args=(db_path, "j1", ready_b, results))
    proc_b.start()
    assert ready_b.wait(20), "子进程 B 没起来"
    proc_b.join(20)
    assert proc_b.exitcode == 0
    b_result = _await(results, "claimed")
    assert b_result["claimed"] is True, "B 应当接管成功"
    assert b_result["wrote"] is True, "B 应当写进自己的阶段"

    after_takeover = st.get("j1")
    assert after_takeover["exec_owner"].startswith("procB:"), "B 应当已经接管"
    assert after_takeover["stage"] == "uploading"
    assert after_takeover["error"] == "B active"

    # 现在放 A 继续：它的迟到写入必须全部打不进去
    release_a.set()
    proc_a.join(20)
    assert proc_a.exitcode == 0
    a_result = _await(results, "late_failure")

    # 先确认 A 真的**试过**了（否则下面的断言可能是空跑）
    assert a_result["claimed"] is True, "A 应当领到过租约"
    assert a_result["late_progress"] is False, "A 的迟到进度必须被挡下"
    assert a_result["late_failure"] is False, "A 的迟到失败终态必须被挡下"
    assert a_result["late_done"] is False, "A 的迟到完成必须被挡下"
    assert a_result["cleared_cancel"] is False, "A 不该清掉 B 的取消标记"

    final = st.get("j1")
    assert final["stage"] == "uploading", f"A 覆盖了新阶段：{final}"
    assert final["error"] == "B active", f"A 覆盖了新状态：{final}"
    assert final["percent"] == 42, f"A 覆盖了新进度：{final}"
    assert final["exec_owner"].startswith("procB:"), "A 不该抢回租约"
    assert st.cancel_marked("j1"), "A 不该清掉 B 的取消标记"

    # A 的 release 也不能把 B 的租约还回去
    assert st.get("j1")["exec_owner"].startswith("procB:")


@pytest.mark.skipif(os.name == "nt", reason="跨进程用例用 spawn，Windows 上另走")
def test_process_b_owns_terminal_state_after_takeover(tmp_path):
    """接管之后，终态只能由新持有者写。"""
    db_path = str(tmp_path / "jobs.db")
    st = store.Store(db_path)
    st.start("j1", "DEMO-1", "magnet:a")

    ctx = mp.get_context("spawn")
    ready_a = ctx.Event()
    release_a = ctx.Event()
    ready_b = ctx.Event()
    results = ctx.Queue()

    proc_a = ctx.Process(target=_child_claim, args=(db_path, "j1", ready_a, release_a, results))
    proc_a.start()
    assert ready_a.wait(20)

    proc_b = ctx.Process(target=_child_takeover, args=(db_path, "j1", ready_b, results))
    proc_b.start()
    assert ready_b.wait(20)
    proc_b.join(20)

    release_a.set()
    proc_a.join(20)
    a_result = _await(results, "late_failure")
    assert a_result["late_failure"] is False, "A 的迟到失败必须被挡下"

    # 现在由 B（真正的持有者）写终态 —— 应当写进去
    owner_b = st.get("j1")["exec_owner"]
    assert st.update_cas("j1", owner_b, stage="done", error="") is True
    assert st.get("j1")["stage"] == "done"
    # A 的 token 已经失效，用它写终态必须失败
    assert st.update_cas("j1", "procA:stale", stage="failed", error="stale") is False
    assert st.get("j1")["stage"] == "done"


@pytest.mark.skipif(os.name == "nt", reason="跨进程用例用 spawn，Windows 上另走")
def test_two_live_processes_cannot_both_hold_the_lease(tmp_path):
    """两个都活着的执行者不能同时持租（心跳没过期时接管要失败）。"""
    db_path = str(tmp_path / "jobs.db")
    st = store.Store(db_path)
    st.start("j1", "DEMO-1", "magnet:a")

    assert st.claim_owner("j1", "A") is True
    # A 心跳还新鲜：B 不能抢
    assert st.claim_owner("j1", "B") is False
    assert st.get("j1")["exec_owner"] == "A"
    # A 续租后仍然抢不走
    assert st.beat("j1", "A") is True
    assert st.claim_owner("j1", "B") is False
    # A 心跳过期后 B 才能接
    assert st.claim_owner("j1", "B", now=time.time() + store.STALE_BEAT + 1) is True
    assert st.get("j1")["exec_owner"] == "B"


@pytest.mark.skipif(os.name == "nt", reason="跨进程用例用 spawn，Windows 上另走")
def test_cancel_mark_survives_across_processes(tmp_path):
    """跨进程取消标记：B 写的，A 收尾时清不掉。"""
    db_path = str(tmp_path / "jobs.db")
    st = store.Store(db_path)
    st.start("j1", "DEMO-1", "magnet:a")

    ctx = mp.get_context("spawn")
    ready_a = ctx.Event()
    release_a = ctx.Event()
    ready_b = ctx.Event()
    results = ctx.Queue()

    proc_a = ctx.Process(target=_child_claim, args=(db_path, "j1", ready_a, release_a, results))
    proc_a.start()
    assert ready_a.wait(20)

    proc_b = ctx.Process(target=_child_takeover, args=(db_path, "j1", ready_b, results))
    proc_b.start()
    assert ready_b.wait(20)
    proc_b.join(20)

    release_a.set()
    proc_a.join(20)
    a_result = _await(results, "cleared_cancel")
    assert a_result["cleared_cancel"] is False, "A 应当清不掉 B 的取消标记"

    assert st.cancel_marked("j1"), "B 写下的取消标记必须还在"
    # 新持有者自己收尾时可以清
    owner_b = st.get("j1")["exec_owner"]
    assert st.clear_cancel("j1", owner=owner_b) is True
    assert not st.cancel_marked("j1")
