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

    **必须按线程名兜底扫一遍**，不能只信 web._threads：有些 fixture 会直接
    `web._threads.clear()`（清字典但不停线程），那些线程就从字典里消失了，
    这里再也找不到它们 —— 于是它们变成不可见的僵尸，继续在下一例的桩下推进。
    下一例若恰好没打桩某个符号，它就会真的去连迅雷面板（容器内实测撞上
    「平台上已有同一个种子在下载」，把无关的恢复用例搞红）。所以这里按名字
    兜住所有 job-* 线程。

    从线程名反解 job_id 来打取消标记：线程名是 `job-<job_id>`，而 CANCEL 是按
    job_id 索引的。只按名字找到却按名字打标记没用 —— 那个线程查的是
    CANCEL[job_id]，标记打错地方它就停不下来。
    """
    with web._threads_lock:
        pending = {job_id: t for job_id, t in web._threads.items()
                   if t.is_alive() and t is not threading.current_thread()}
    # 兜底：字典里没有但还活着的 job-* 线程（被 clear 掉的那批）
    for t in threading.enumerate():
        if not t.name.startswith("job-") or not t.is_alive():
            continue
        if t is threading.current_thread():
            continue
        # 线程名形如 "job-<job_id>"：用它反解 job_id，取消标记才打对地方
        job_id = t.name[len("job-"):]
        pending.setdefault(job_id, t)
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


def _block_real_xunlei(web) -> None:
    """把网页端的迅雷客户端指向一个**必定连不通**的地址。

    为什么需要：某些用例会真的走 `admit()` 起后台任务线程，线程在断言结束后
    仍在跑。等它推进到下载那一步时，如果下一个用例没打桩 `pipeline.download`，
    它就会真的去连**真实迅雷面板** —— 容器内实测撞上「平台上已有同一个种子
    在下载」，把毫不相干的恢复用例搞红；更糟的是它会真的占用平台的下载位。

    这里把 `web._XL`（惰性创建、模块级缓存）换成指向本地保留端口的实例：
    漏网的调用会立刻连接失败并露出调用栈，而不是悄悄连上真面板。
    真的需要面板的用例自己会重新设置 `web._XL` 或 monkeypatch 掉它。
    """
    from app import xunlei
    web._XL = xunlei.Xunlei("http://127.0.0.1:9", "", web.TMP_ROOT, "")


@pytest.fixture(autouse=True)
def isolate_module_state():
    """每个用例前后都把 web 的模块级内存状态复位。

    只碰内存镜像，不动任何库文件：用例自己 monkeypatch 的 STORE 仍然照常用。
    """
    from app import main as bot
    from app import web

    _block_real_xunlei(web)
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
