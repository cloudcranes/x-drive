"""调度闸门：所有起线程的路径都必须过同一道「现在允许开工吗」，待对账标记写失败要硬失败。

三条已确证的缺陷各一组用例：
  E1  requeue()/start_job() 直达 admit()，闸门关着（暂停或待对账）也照样起线程；
  E2  restore_apply 写对账标记失败只 log.warning，仍返回 ok=True + reconcile_required=False；
  E3  restore_confirm 只清标记、放开暂停，没人把恢复出来的任务真正入队。

断言都看**行为**（有没有线程被放行、任务落在哪个队列/什么阶段、返回值里数得出来的
入队条数），不看源码字符串。全部用 tmp_path 的库与旁路文件，无网络。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from app import store, web

GB = 1024 ** 3


@pytest.fixture
def env(tmp_path, monkeypatch):
    """干净隔离：库、旁路标记目录、内存镜像都指向 tmp_path，模块级暂停开关复位。"""
    db = str(tmp_path / "jobs.db")
    monkeypatch.setattr(web, "STORE", store.Store(db))
    monkeypatch.setenv("JOBS_DB", db)
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(web, "JOBS", {})
    monkeypatch.setattr(web, "WAITING", {})
    monkeypatch.setattr(web, "_queued_at", {})
    web.set_paused(False)
    yield db
    web.set_paused(False)


@pytest.fixture
def started(monkeypatch):
    """拦住真的下载线程，只记录「谁被放行了」。"""
    seen: list[str] = []
    monkeypatch.setattr(web, "run_web_job",
                        lambda job_id, source, code: seen.append(job_id))
    return seen


def _queued_job(job_id: str = "j1", code: str = "ABC-1") -> None:
    """造一条「未完成」的任务并放进内存队列（等价于重启后 recover 出来的那份）。

    阶段用 deferred 而不是 queued：deferred 同样算「没跑完」、会被 recover 捞出来，
    但它不会被本用例**之后**的 restore_confirm 当成待对账任务顺手入队 ——
    那一步是用例主题以外的副作用，会把第二条断言的前提搅乱。
    """
    web._store().start(job_id, code, "magnet:a")
    web._store().update(job_id, stage="deferred", total=1 * GB)
    with web._waiting_lock:
        web.WAITING[job_id] = ("magnet:a", code)


# ─────────────────────────────────────────────── E1：闸门只有一个

def test_start_job_does_not_start_while_paused(env, started):
    """暂停期间新提交的任务：不开下载，但必须留在 WAITING 里、阶段仍是 queued。"""
    web.set_paused(True)

    job_id = web.start_job("magnet:?xt=urn:btih:a", "ABC-1", {"size": 1 * GB})

    assert job_id, "暂停不该让提交失败"
    assert started == [], "暂停期间绝不开下载"
    assert job_id in web.WAITING, "闸门关着的任务要留在队列等放行，不能丢"
    assert web._store().get(job_id)["stage"] == "queued", "不能落成终态"


def test_start_job_does_not_start_while_reconcile_pending(env, started):
    """待对账期间同理：库可能是旧快照，新任务只排队不开工。"""
    web._store().state_put({web.RECONCILE_KEY: {"required": True}})

    job_id = web.start_job("magnet:?xt=urn:btih:a", "ABC-1", {"size": 1 * GB})

    assert job_id and started == []
    assert job_id in web.WAITING


def test_requeue_holds_task_while_paused_and_pump_releases_after(env, started):
    """requeue 在闸门关闭时只排队（返回成功、留 WAITING、不入终态），放开后由 pump 放行。"""
    _queued_job()
    web.set_paused(True)

    assert web.requeue("j1", "magnet:a", "ABC-1") is True, "闸门关着不等于入队失败"
    assert started == []
    assert "j1" in web.WAITING
    assert web._store().get("j1")["stage"] == "queued"

    # 闸门仍然关着：泵线程也不许放行
    assert web.pump_queue() == 0 and started == []

    web.set_paused(False)                     # 对账确认 → 放开
    assert web.pump_queue() == 1
    assert started == ["j1"]
    assert "j1" not in web.WAITING
    assert web._store().get("j1")["stage"] == "downloading"


def test_requeue_holds_task_while_reconcile_pending(env, started):
    """待对账（只有库内标记，无内存暂停）时 requeue 同样只排队。"""
    _queued_job()
    web._store().state_put({web.RECONCILE_KEY: {"required": True}})

    web.requeue("j1", "magnet:a", "ABC-1")

    assert started == [] and "j1" in web.WAITING
    assert web.pump_queue() == 0

    # 对账确认也是「开工」路径：它清完标记后立刻把这条 deferred 任务放行，
    # 不需要等下一轮调度（这正是 E3 要求的显式入队）
    out = web.restore_confirm(confirm=True, force=True)
    assert out["ok"] is True and out["requeued"] == 1
    assert web.reconcile_pending() is False
    assert started == ["j1"], "确认后任务被真实放行，不需要再靠下一轮泵"


def test_admit_itself_refuses_while_gate_closed(env, started):
    """admit() 是唯一的开工入口：直接调它也不能绕过闸门（以前它完全不看暂停）。"""
    _queued_job()
    web.set_paused(True)

    assert web.admit("j1") is False
    assert started == [] and "j1" in web.WAITING


def test_defer_loop_path_stays_queued_while_gate_closed(env, started):
    """延期到期撞上闸门：任务留在队列，闸门放开后被放行（不丢、不落终态）。"""
    web._store().start("d1", "DEF-1", "magnet:d")
    web._store().defer("d1", time.time() - 1, "额度用尽")   # 已经到期
    assert web._store().claim_due("d1") is True   # 模拟 _defer_loop 的原子领取
    web.set_paused(True)                          # 闸门恰在此刻关闭（恢复窗口）

    web.requeue("d1", "magnet:d", "DEF-1")        # 闸门关着：只排队
    assert started == [] and "d1" in web.WAITING

    web.set_paused(False)
    assert web.pump_queue() == 1 and started == ["d1"]


def test_stop_job_removing_from_queue_wins_over_late_admit(env, started):
    """先摘队列的竞态：摘掉之后 admit 拿不到条目，绝不会再起线程。"""
    _queued_job()

    assert web.stop_job("j1") is True
    assert web.admit("j1") is False
    assert web.pump_queue() == 0
    assert started == []
    assert web._store().get("j1")["stage"] == "cancelled"


# ─────────────────────────────────────────────── E2：标记写失败是硬失败

def test_restore_apply_is_hard_failure_when_db_mark_write_fails(env, tmp_path, monkeypatch):
    """库内标记写失败：ok=False、带 error、保持暂停、且重启后仍不开工（旁路文件兜底）。"""
    web._store().start("old", "A-1", "magnet:a")
    web._store().finish("old", "done")
    bak = web.backup_now(str(tmp_path / "b.db"))["path"]

    real = store.Store.state_put

    def boom(self, updates, conn=None):
        if web.RECONCILE_KEY in updates:
            raise OSError("database is locked")
        return real(self, updates, conn)

    monkeypatch.setattr(store.Store, "state_put", boom)
    out = web.restore_apply(bak, confirm=True)
    monkeypatch.setattr(store.Store, "state_put", real)

    assert out["ok"] is False, "标记没写进去就不能报成功"
    assert out["trusted"] is False and out["reconcile_required"] is True
    assert out["error"] and "不可信" in out["error"]
    assert out["paused"] is True and web.paused() is True, "必须保持暂停"
    assert "file" in out["mark_written"], "库写不动时要有旁路文件标记"
    # 库内标记确实没写进去（缺陷的起点）
    assert real(web._store(), {}) is None
    assert web._store().state_get(web.RECONCILE_KEY) == {}
    # 重启语义：内存暂停丢了、库内也没有标记，闸门仍必须关着
    web.set_paused(False)
    assert web.reconcile_pending() is True, "重启后（内存暂停丢失）也要拦住"
    assert web.scheduling_open() is False
    assert web.pump_queue() == 0
    # 另一个进程（bot）只凭同一个库路径就能读到「库不可信」
    assert store.read_reconcile_mark(web.jobs_db_path())["required"] is True


def test_restore_apply_reports_untrusted_even_when_both_marks_fail(env, tmp_path, monkeypatch):
    """两处都写不进去：仍是硬失败，error 明说重启后拦不住（不假装安全）。"""
    web._store().start("old", "A-1", "magnet:a")
    web._store().finish("old", "done")
    bak = web.backup_now(str(tmp_path / "b.db"))["path"]

    real_put = store.Store.state_put

    def boom_put(self, updates, conn=None):
        if web.RECONCILE_KEY in updates:
            raise OSError("database is locked")
        return real_put(self, updates, conn)

    def boom_file(mark, db_path):
        raise OSError("磁盘满")

    monkeypatch.setattr(store.Store, "state_put", boom_put)
    monkeypatch.setattr(store, "write_reconcile_mark", boom_file)
    try:
        out = web.restore_apply(bak, confirm=True)
    finally:
        monkeypatch.setattr(store.Store, "state_put", real_put)

    assert out["ok"] is False and out["trusted"] is False
    assert out["mark_written"] == []
    assert "重启后无法阻止自动开工" in out["error"]
    assert web.paused() is True, "只能靠内存暂停时也必须停住"


def test_garbled_side_mark_keeps_gate_closed(env):
    """旁路标记文件损坏同样按待对账处理：读不出来不等于可以开工。"""
    Path(store.reconcile_mark_path(web.jobs_db_path())).write_text("{坏掉的", encoding="utf-8")

    assert web.reconcile_pending() is True
    assert web.scheduling_open() is False


def test_restore_confirm_clears_side_mark_too(env, tmp_path):
    """确认成功后两处标记一并清除：只清库内，旁路文件会一直把闸门关着。"""
    web._store().start("old", "A-1", "magnet:a")
    web._store().finish("old", "done")
    bak = web.backup_now(str(tmp_path / "b.db"))["path"]
    assert web.restore_apply(bak, confirm=True)["ok"] is True
    assert web.reconcile_pending() is True

    out = web.restore_confirm(confirm=True)

    assert out["ok"] is True
    assert web.reconcile_pending() is False
    assert web._store().state_get(web.RECONCILE_KEY) == {}
    side = Path(store.reconcile_mark_path(web.jobs_db_path()))
    assert not side.exists(), "旁路标记必须一起清掉"
    assert store.read_reconcile_mark(web.jobs_db_path())["required"] is False


# ─────────────────────────────────────────────── E3：确认对账后真的入队

def test_restore_confirm_requeues_unfinished_jobs(env, tmp_path, started):
    """恢复出来 1 个未完成任务：confirm(force) 之后它真的进队列并被放行。"""
    web._store().start("q1", "ABC-1", "magnet:a")        # 备份里它是未完成的
    bak = web.backup_now(str(tmp_path / "b.db"))["path"]
    web._store().finish("q1", "done")
    assert web.restore_apply(bak, confirm=True)["ok"] is True

    out = web.restore_confirm(confirm=True, force=True)

    assert out["ok"] is True
    assert out["requeued"] == 1 and out["requeue_failed"] == 0
    assert started == ["q1"], "确认后任务必须真的起来，而不是等「下一轮调度」"
    assert web._store().get("q1")["stage"] == "downloading"
    assert "入队" in out["hint"] and "下一轮调度" not in out["hint"], "提示文案要与实际一致"


def test_restore_confirm_keeps_tasks_queued_when_gate_closed(env, tmp_path, started, monkeypatch):
    """确认后闸门还关着：任务进 WAITING 等放行，结果里如实区分「入队」与「排队」。

    以前这里测的是「磁盘空间不够」（磁盘闸门已随本地下载取消），
    现在测的是同一套「held」语义：闸门关着时任务留在队列里等放行。
    闸门用「待对账」标记来关（restore_confirm 自己会清暂停标记）。
    """
    web._store().state_put({web.RECONCILE_KEY: {"required": True}})
    web._store().start("q1", "ABC-1", "magnet:a")
    bak = web.backup_now(str(tmp_path / "b.db"))["path"]
    web._store().finish("q1", "done")
    web.restore_apply(bak, confirm=True)

    # 确认后闸门放开（待对账标记被清掉），但调度仍被外部按住 →
    # 任务留在 WAITING，结果里如实说「排队」
    monkeypatch.setattr(web, "scheduling_open", lambda: False)
    out = web.restore_confirm(confirm=True, force=True)

    assert out["ok"] is True and out["requeued"] == 1 and out["held"] == 1
    assert started == [] and "q1" in web.WAITING
    assert web._store().get("q1")["stage"] == "queued"


def test_restore_confirm_reports_failures_without_stopping_others(env, tmp_path, started):
    """单条入队失败不中断其余任务，失败数如实报告。"""
    web._store().start("bad", "BAD-1", "magnet:bad")
    web._store().start("good", "GOOD-1", "magnet:good")
    bak = web.backup_now(str(tmp_path / "b.db"))["path"]
    web._store().finish("bad", "done")
    web._store().finish("good", "done")
    web.restore_apply(bak, confirm=True)

    real = web.requeue

    def flaky(job_id, source, code, stage="queued"):
        if job_id == "bad":
            raise RuntimeError("这一条坏了")
        return real(job_id, source, code, stage)

    web.requeue = flaky                 # 模块级替换，用例结束前还原
    try:
        out = web.restore_confirm(confirm=True, force=True)
    finally:
        web.requeue = real

    assert out["ok"] is True
    assert out["requeued"] == 1 and out["requeue_failed"] == 1
    assert any("bad" in e for e in out["errors"])
    assert started == ["good"], "另一条必须照常入队"
    assert "1 个入队失败" in out["hint"]


# ─────────────────────────────────────────────── E4：跨进程只读接口

def test_status_is_readable_from_another_store_instance(env, tmp_path):
    """bot 与 web 共用 /data：另一个进程按同一路径 new 一个 Store 就能读到待对账状态。"""
    web._store().start("old", "A-1", "magnet:a")
    web._store().finish("old", "done")
    bak = web.backup_now(str(tmp_path / "b.db"))["path"]
    web.restore_apply(bak, confirm=True)

    other = store.Store(web.jobs_db_path())          # 模拟 bot 进程自己打开库
    assert other.state_get(store.RECONCILE_KEY)["required"] is True
    mark = store.read_reconcile_mark(web.jobs_db_path())
    assert mark["required"] is True and mark["db"] is True and mark["db"]

    # 确认之后另一个进程也看到放行
    web.restore_confirm(confirm=True, force=True)
    assert store.read_reconcile_mark(web.jobs_db_path())["required"] is False


def test_read_reconcile_mark_marks_unreadable_db_as_untrusted(tmp_path):
    """库文件不是数据库时：判定为待对账并带上原因，绝不抛异常、绝不当成可开工。"""
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not a database")

    mark = store.read_reconcile_mark(str(broken))

    assert mark["required"] is True and mark["errored"], "读不出来必须按不可信处理"


def test_side_mark_file_is_written_atomically(env):
    """旁路标记能落到库同目录、能被独立读出（另一进程只看文件也能判断）。"""
    path = store.write_reconcile_mark({"source": "x.db"}, web.jobs_db_path())

    assert Path(path) == Path(store.reconcile_mark_path(web.jobs_db_path()))
    assert json.loads(Path(path).read_text(encoding="utf-8"))["required"] is True
    assert not list(Path(path).parent.glob("reconcile_required.json.*.tmp")), "临时文件不该留下"
