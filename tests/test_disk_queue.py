"""磁盘空间闸门在任务队列上的行为：不够就排队，够了自动放行。"""
import threading

import pytest

from app import diskspace as ds
from app import web

GB = 1024 ** 3


@pytest.fixture(autouse=True)
def clean_state():
    web.JOBS.clear()
    web.WAITING.clear()
    web._queued_at.clear()
    yield
    web.JOBS.clear()
    web.WAITING.clear()
    web._queued_at.clear()


@pytest.fixture
def no_threads(monkeypatch):
    """拦住真正开线程跑下载，只记录「谁被放行了」。

    真的跑 run_web_job 会去连 aria2 然后立刻失败，把 stage 写成 failed，
    后面的断言就全乱了 —— 这里只关心闸门有没有放行。
    """
    started = []
    monkeypatch.setattr(web, "run_web_job",
                        lambda job_id, source, code: started.append((job_id, source, code)))
    return started


def test_job_runs_immediately_when_space_is_plenty(monkeypatch, no_threads):
    monkeypatch.setattr(web, "DISK_RESERVE_GB", 1)
    monkeypatch.setattr(ds, "free_bytes", lambda _: 100 * GB)
    job_id = web.start_job("magnet:?xt=urn:btih:a", "ABC-1",
                           {"name": "x", "size": 2 * GB})
    assert no_threads == [(job_id, "magnet:?xt=urn:btih:a", "ABC-1")]
    assert job_id not in web.WAITING


def test_job_queues_when_disk_is_short(monkeypatch, no_threads):
    """剩 3G、要下 5G、留 1G：放不下，必须排队而不是硬下。"""
    monkeypatch.setattr(web, "DISK_RESERVE_GB", 1)
    monkeypatch.setattr(ds, "free_bytes", lambda _: 3 * GB)
    job_id = web.start_job("magnet:?xt=urn:btih:a", "ABC-1",
                           {"name": "x", "size": 5 * GB})
    assert no_threads == [], "空间不够不能开下载"
    assert job_id in web.WAITING
    job = web.JOBS[job_id]
    assert job["stage"] == "queued"
    assert "磁盘空间" in job["error"]


def test_queued_job_is_released_when_space_frees_up(monkeypatch, no_threads):
    monkeypatch.setattr(web, "DISK_RESERVE_GB", 1)
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
    monkeypatch.setattr(web, "DISK_RESERVE_GB", 1)
    monkeypatch.setattr(ds, "free_bytes", lambda _: 10 * GB)
    first = web.start_job("magnet:?xt=urn:btih:a", "ABC-1", {"name": "a", "size": 6 * GB})
    second = web.start_job("magnet:?xt=urn:btih:b", "ABC-2", {"name": "b", "size": 6 * GB})
    assert first not in web.WAITING, "第一个放得下"
    assert second in web.WAITING, "第二个不该被放行，否则两个合起来吃穿预留"


def test_unknown_size_is_admitted(monkeypatch, no_threads):
    """whatslink 查不到大小 → 放行，并保持 total=0。"""
    monkeypatch.setattr(web, "DISK_RESERVE_GB", 1)
    monkeypatch.setattr(ds, "free_bytes", lambda _: 10 * GB)
    job_id = web.start_job("magnet:?xt=urn:btih:a", "ABC-1", None)
    assert job_id not in web.WAITING
    assert len(no_threads) == 1


def test_queue_positions_are_exposed(monkeypatch, no_threads):
    """排队位置按「还没放行的」顺序算：放行了的就不占位置。"""
    monkeypatch.setattr(web, "DISK_RESERVE_GB", 1)
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
    monkeypatch.setattr(web, "DISK_RESERVE_GB", 1)
    monkeypatch.setattr(ds, "free_bytes", lambda _: 100 * GB)
    ids = [web.start_job("magnet:?xt=urn:btih:x", f"C{i}", {"name": "n", "size": GB})
           for i in range(20)]
    assert len(set(ids)) == 20
    assert len(web.JOBS) == 20


def test_disk_probe_failure_does_not_block(monkeypatch, no_threads):
    """探测不到剩余空间时放行：卡死所有任务比偶尔写满更糟。"""
    monkeypatch.setattr(web, "DISK_RESERVE_GB", 5)
    monkeypatch.setattr(ds, "free_bytes", lambda _: None)
    job_id = web.start_job("magnet:?xt=urn:btih:a", "ABC-1", {"name": "a", "size": 5 * GB})
    assert job_id not in web.WAITING
