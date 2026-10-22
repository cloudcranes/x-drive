"""磁盘空间闸门的逻辑测试：不依赖真实磁盘，全部走纯函数或 mock。"""
import shutil

import pytest

from app import diskspace as ds

GB = 1024 ** 3


# ---------------------------------------------------------------- free_bytes

def test_free_bytes_reads_real_path(tmp_path):
    free = ds.free_bytes(tmp_path)
    assert free is not None and free > 0


def test_free_bytes_returns_none_on_error(monkeypatch):
    """探测失败不能抛异常，调用方据此放行。"""
    def boom(_):
        raise OSError("statvfs failed")

    monkeypatch.setattr(shutil, "disk_usage", boom)
    assert ds.free_bytes("/nonexistent") is None


# ---------------------------------------------------------------- fits

def test_fits_when_plenty_of_room():
    assert ds.fits(need=5 * GB, free=38 * GB, reserve=5 * GB)


def test_does_not_fit_when_reserve_would_be_eaten():
    """剩 10G、要下 6G、预留 5G：扣完只剩 4G，低于预留，不该放行。"""
    assert not ds.fits(need=6 * GB, free=10 * GB, reserve=5 * GB)


def test_fits_exactly_at_boundary():
    """扣完刚好等于预留，算够（不苛刻到差一个字节也排队）。"""
    assert ds.fits(need=5 * GB, free=10 * GB, reserve=5 * GB)


def test_one_byte_short_does_not_fit():
    assert not ds.fits(need=5 * GB + 1, free=10 * GB, reserve=5 * GB)


def test_unknown_size_passes_when_room_exists():
    """磁力查不到大小时放行：卡死一个能下的任务比偶尔写满更糟。"""
    assert ds.fits(need=0, free=10 * GB, reserve=5 * GB)


def test_unknown_size_still_blocked_when_below_reserve():
    """大小未知也要尊重预留线：剩余本就低于预留时不能再放行。

    这不是苛刻 —— 到这个地步磁盘该告警了，继续塞任务只会更快写满。
    """
    assert not ds.fits(need=0, free=1 * GB, reserve=5 * GB)


def test_unknown_free_passes():
    """剩余空间探测失败也放行，同样的道理。"""
    assert ds.fits(need=50 * GB, free=None, reserve=5 * GB)


def test_negative_need_treated_as_zero():
    assert ds.fits(need=-1, free=10 * GB, reserve=5 * GB)


def test_negative_reserve_ignored():
    assert ds.fits(need=9 * GB, free=10 * GB, reserve=-5 * GB)


# ---------------------------------------------------------------- reserved_by

def test_reserved_counts_only_downloading():
    """只算真正在下的；排队的不占额度，否则队列一长队头永远轮不到。"""
    jobs = [
        {"stage": "queued", "total": 4 * GB},
        {"stage": "downloading", "total": 3 * GB},
        {"stage": "uploading", "total": 9 * GB},   # 已经落盘，不重复占额度
        {"stage": "done", "total": 9 * GB},
        {"stage": "failed", "total": 9 * GB},
    ]
    assert ds.reserved_by(jobs) == 3 * GB


def test_reserved_ignores_bad_values():
    jobs = [
        {"stage": "downloading", "total": None},
        {"stage": "downloading", "total": "abc"},
        {"stage": "downloading"},
        {"stage": "downloading", "total": 1 * GB},
    ]
    assert ds.reserved_by(jobs) == 1 * GB


def test_reserved_empty():
    assert ds.reserved_by([]) == 0
