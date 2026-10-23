"""磁盘空间闸门在任务队列上的行为：不够就排队，够了自动放行。"""
import threading

import pytest

from app import diskspace as ds
from app import store, web

GB = 1024 ** 3

# 注入可热更新参数的覆盖值：这些参数已从模块常量迁到 get_setting，
# monkeypatch 常量已失效。这里 monkeypatch get_setting，让它对指定 key 返回覆盖值。
_INJECTED = {}


def _inject(monkeypatch, **overrides):
    real = web.get_setting
    _INJECTED.update(overrides)
    monkeypatch.setattr(web, "get_setting",
                       lambda key, d=None: _INJECTED[key] if key in _INJECTED else real(key))


@pytest.fixture(autouse=True)
def clean_state(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    _INJECTED.clear()
    web.JOBS.clear()
    web.WAITING.clear()
    web._queued_at.clear()
    yield
    _INJECTED.clear()
    web.JOBS.clear()
    web.WAITING.clear()
    web._queued_at.clear()


@pytest.fixture
def no_threads(monkeypatch):
    """拦住真正开线程跑下载，只记录「谁被放行了」。

    真的跑 run_web_job 会去连迅雷然后立刻失败，把 stage 写成 failed，
    后面的断言就全乱了 —— 这里只关心闸门有没有放行。
    """
    started = []
    monkeypatch.setattr(web, "run_web_job",
                        lambda job_id, source, code: started.append((job_id, source, code)))
    return started


def test_job_runs_immediately_when_space_is_plenty(monkeypatch, no_threads):
    _inject(monkeypatch, DISK_RESERVE_GB=1)
    monkeypatch.setattr(ds, "free_bytes", lambda _: 100 * GB)
    job_id = web.start_job("magnet:?xt=urn:btih:a", "ABC-1",
                           {"name": "x", "size": 2 * GB})
    assert no_threads == [(job_id, "magnet:?xt=urn:btih:a", "ABC-1")]
    assert job_id not in web.WAITING


def test_job_queues_when_disk_is_short(monkeypatch, no_threads):
    """剩 3G、要下 5G、留 1G：放不下，必须排队而不是硬下。"""
    _inject(monkeypatch, DISK_RESERVE_GB=1)
    monkeypatch.setattr(ds, "free_bytes", lambda _: 3 * GB)
    job_id = web.start_job("magnet:?xt=urn:btih:a", "ABC-1",
                           {"name": "x", "size": 5 * GB})
    assert no_threads == [], "空间不够不能开下载"
    assert job_id in web.WAITING
    job = web.JOBS[job_id]
    assert job["stage"] == "queued"
    assert "磁盘空间" in job["error"]


def test_queued_job_is_released_when_space_frees_up(monkeypatch, no_threads):
    _inject(monkeypatch, DISK_RESERVE_GB=1)
    monkeypatch.setattr(ds, "free_bytes", lambda _: 1 * GB)
    job_id = web.start_job("magnet:?xt=urn:btih:a", "ABC-1",
                           {"name": "x", "size": 5 * GB})
    assert job_id in web.WAITING

    # 用户清掉了旧档案，空间回来了
    monkeypatch.setattr(ds, "free_bytes", lambda _: 50 * GB)
    assert web.pump_queue() == 1
    assert job_id not in web.WAITING
    assert no_threads == [(job_id, "magnet:?xt=urn:btih:a", "ABC-1")]


def test_reservations_are_counted_so_tasks_do_not_overcommit(monkeypatch, no_threads):
    """剩 10G、留 1G：两个 6G 的任务只能放一个 —— 第二个会把总占用算进去。"""
    _inject(monkeypatch, DISK_RESERVE_GB=1)
    monkeypatch.setattr(ds, "free_bytes", lambda _: 10 * GB)
    first = web.start_job("magnet:?xt=urn:btih:a", "ABC-1", {"name": "a", "size": 6 * GB})
    second = web.start_job("magnet:?xt=urn:btih:b", "ABC-2", {"name": "b", "size": 6 * GB})
    assert first not in web.WAITING, "第一个放得下"
    assert second in web.WAITING, "第二个不该被放行，否则两个合起来吃穿预留"


def test_unknown_size_is_admitted(monkeypatch, no_threads):
    """whatslink 查不到大小 → 放行，并保持 total=0。"""
    _inject(monkeypatch, DISK_RESERVE_GB=1)
    monkeypatch.setattr(ds, "free_bytes", lambda _: 10 * GB)
    job_id = web.start_job("magnet:?xt=urn:btih:a", "ABC-1", None)
    assert job_id not in web.WAITING
    assert len(no_threads) == 1


def test_queue_positions_are_exposed(monkeypatch, no_threads):
    """排队位置按「还没放行的」顺序算：放行了的就不占位置。"""
    _inject(monkeypatch, DISK_RESERVE_GB=1)
    monkeypatch.setattr(ds, "free_bytes", lambda _: 1 * GB)
    a = web.start_job("magnet:?xt=urn:btih:a", "ABC-1", {"name": "a", "size": 5 * GB})
    b = web.start_job("magnet:?xt=urn:btih:b", "ABC-2", {"name": "b", "size": 5 * GB})
    c = web.start_job("magnet:?xt=urn:btih:c", "ABC-3", {"name": "c", "size": 5 * GB})
    # 三个都放不下，全在队列里，位置是 1/2/3
    assert [web.JOBS[x]["queued_position"] for x in (a, b, c)] == [1, 2, 3]

    # 空间回来：按提交顺序先放 a，剩下的重新排位
    monkeypatch.setattr(ds, "free_bytes", lambda _: 6 * GB)
    web.pump_queue()
    assert a not in web.WAITING, "队头先放行"
    assert web.JOBS[b]["queued_position"] == 1
    assert web.JOBS[c]["queued_position"] == 2


def test_job_ids_are_unique_within_same_millisecond(monkeypatch, no_threads):
    """并发提交时毫秒时间戳会撞车，撞了就会互相覆盖任务记录。"""
    _inject(monkeypatch, DISK_RESERVE_GB=1)
    monkeypatch.setattr(ds, "free_bytes", lambda _: 100 * GB)
    ids = [web.start_job("magnet:?xt=urn:btih:x", f"C{i}", {"name": "n", "size": GB})
           for i in range(20)]
    assert len(set(ids)) == 20
    assert len(web.JOBS) == 20


def test_disk_probe_failure_does_not_block(monkeypatch, no_threads):
    """探测不到剩余空间时放行：卡死所有任务比偶尔写满更糟。"""
    _inject(monkeypatch, DISK_RESERVE_GB=5)
    monkeypatch.setattr(ds, "free_bytes", lambda _: None)
    job_id = web.start_job("magnet:?xt=urn:btih:a", "ABC-1", {"name": "a", "size": 5 * GB})
    assert job_id not in web.WAITING


def test_gate_sees_reservations_after_restart(tmp_path, monkeypatch):
    """重启后内存是空的，闸门仍必须看到任务表里已放行的任务。

    以前 _pending_jobs() 只读内存镜像：恢复一批任务时全部判定「盘还够」，
    一起开下把盘写满 —— 这正是重启后最容易撞上的场景。
    """
    _inject(monkeypatch, DISK_RESERVE_GB=1)
    monkeypatch.setattr(web, "run_web_job", lambda *a: None)
    db = str(tmp_path / "jobs.db")
    first = store.Store(db)
    first.start("old1", "ABC-1", "magnet:a")
    first.update("old1", stage="downloading", total=6 * GB)

    # 第二个进程（重启后）：内存镜像清空，库里那条还在
    monkeypatch.setattr(web, "STORE", store.Store(db))
    web.JOBS.clear()
    monkeypatch.setattr(ds, "free_bytes", lambda _: 10 * GB)
    job_id = web.start_job("magnet:?xt=urn:btih:b", "ABC-2", {"name": "b", "size": 6 * GB})
    assert job_id in web.WAITING, "已在下载的 6G 要先扣掉，剩下的放不下第二个"


def test_stage_footprints_counted_per_stage():
    """整理阶段要额外一份副本；只补上传的不再新增占用。"""
    assert ds.footprint({"stage": "downloading", "total": 100}) == 100
    assert ds.footprint({"stage": "organizing", "total": 100}) == 100
    assert ds.footprint({"stage": "uploading", "total": 100}) == 0
    assert ds.footprint({"stage": "queued", "total": 100}) == 0
    assert ds.footprint({"stage": "downloading"}) == 0


def test_unknown_size_still_reserves_something(monkeypatch, no_threads):
    """体积完全未知也要留出保守额度，否则「未知」等于「不占盘」直接放行。"""
    _inject(monkeypatch, DISK_RESERVE_GB=1)
    _inject(monkeypatch, DEFAULT_SIZE_GB=4)
    monkeypatch.setattr(ds, "free_bytes", lambda _: 3 * GB)
    job_id = web.start_job("magnet:?xt=urn:btih:z", "ZZZ-9", None)
    assert job_id in web.WAITING, "3G 空间放不下默认 4G 的预留"
