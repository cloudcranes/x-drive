"""全量测试的共用隔离：清掉模块级的任务/租约内存状态，并收掉上一例遗留的心跳线程。

为什么必须有这个文件：`app/web.py` 里的 JOBS / WAITING / _queued_at / CANCEL /
LEASES / _lease_live 都是**模块级**字典，pytest 在同一进程里跑完所有用例，
它们不会自己清空。而大量用例又都用同一个 job_id（`"j1"`）和同一套租约机制，
于是上一例留下的心跳线程会在下一例里继续为 `"j1"` 续租、失租：

  · `lease_owner("j1")` 复用了上一例的 token（LEASES 未清）；
  · 上一例的心跳线程还在 `while _lease_live.get("j1")` 里循环，一旦它 beat 失败，
    就把 `_lease_live["j1"]` 置为 False —— 那是**共用**的标志位，
    于是下一例的 `lease_alive()` 立刻变假，`require_lease()` 抛 Cancelled，
    stage 被写成 cancelled、重试次数被打断。

这类污染会让全量随机红（单跑任何单个文件都是绿的），所以在这里统一兜住：
每个用例开始前把内存状态清空，并唤醒上一例的线程让它们自己退出。
"""
from __future__ import annotations

import threading
import time

import pytest

# 上一例遗留的心跳线程最多等这么久退出（web 侧有唤醒 Event，通常几毫秒就退完了）
_THREAD_GRACE = 1.0


def _stop_heartbeats(web) -> None:
    """让遗留心跳线程立刻退出，并清掉租约内存状态。

    心跳线程等的是 `_lease_wake[job_id]` 这个 Event（timeout=心跳间隔），
    所以 set() 一下它就当场醒、看到标志位已清就退场 —— 不用等满 40 秒。
    生产代码里另有 token 归属校验（见 web.lease_begin 的 pulse），
    即使有线程赶不及退出，也不会再影响下一个用例。
    """
    with web._lease_lock:
        wakes = list(web._lease_wake.values())
        web._lease_wake.clear()
        web._lease_live.clear()
        web.LEASES.clear()
    for stop in wakes:
        stop.set()
    deadline = time.time() + _THREAD_GRACE
    while time.time() < deadline:
        if not [t for t in threading.enumerate()
                if t.name.startswith("lease-") and t.is_alive()]:
            return
        time.sleep(0.005)


def _stop_bot_heartbeats(bot) -> None:
    """bot 侧同一套租约内存状态：唤醒事件 + 代际校验，唤醒即退场。"""
    with bot._lease_lock:
        wakes = list(bot._lease_wake.values())
        bot._lease_wake.clear()
        bot._lease_live.clear()
        bot.LEASES.clear()
    for stop in wakes:
        stop.set()
    deadline = time.time() + _THREAD_GRACE
    while time.time() < deadline:
        if not [t for t in threading.enumerate()
                if t.name.startswith("bot-lease-") and t.is_alive()]:
            return
        time.sleep(0.005)


def _drain_job_threads(web, grace: float = 5.0) -> None:
    """把上一例遗留在跑的任务线程收干净，避免它醒着去调**下一例**的桩。

    这是全量随机红的第二个来源：某些用例会真的走 `admit()` 起一条后台任务线程，
    断言完就结束，线程却还在 `run_web_job` 里跑。等它继续推进时，`pipeline.download`
    之类的符号已经被下一例 monkeypatch 成新的假实现 —— 于是它调用了**别人的**
    计数桩，把下一例的 `calls["download"]` 多加一次（实测 3 变成 4）。

    做法：先给这些任务打上取消标记，再等它们退出；超时也放行（不能因为兜底
    逻辑把测试挂死），但取消标记 + 租约清空已足够让它们在下一个检查点停手。
    """
    with web._threads_lock:
        pending = {job_id: t for job_id, t in web._threads.items()
                   if t.is_alive() and t is not threading.current_thread()}
    if not pending:
        return
    with web._jobs_lock:
        for job_id in pending:
            web.CANCEL[job_id] = True
    deadline = time.time() + grace
    for thread in pending.values():
        thread.join(max(0.0, deadline - time.time()))


def _clear_memory_state(web) -> None:
    """把所有模块级内存状态归零（顺序：先清标志位，再清容器）。"""
    with web._jobs_lock:
        web.JOBS.clear()
        web.CANCEL.clear()
    with web._waiting_lock:
        web.WAITING.clear()
        web._queued_at.clear()
    with web._threads_lock:
        web._threads.clear()
    web.set_paused(False)


@pytest.fixture(autouse=True)
def isolate_module_state():
    """每个用例前后都把 web 的模块级内存状态复位。

    只碰内存镜像，不动任何库文件：用例自己 monkeypatch 的 STORE 仍然照常用。
    """
    from app import main as bot
    from app import web

    _stop_heartbeats(web)
    _drain_job_threads(web)
    _clear_memory_state(web)
    # bot 侧同一套租约机制，同样会跨用例残留
    _stop_bot_heartbeats(bot)
    yield
    _stop_heartbeats(web)
    _drain_job_threads(web)
    _clear_memory_state(web)
    _stop_bot_heartbeats(bot)
