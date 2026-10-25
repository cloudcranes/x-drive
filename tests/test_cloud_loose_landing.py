"""单文件种子：文件直接躺在收件目录根下（实测 MIMK-267）。

迅雷对**单文件**种子不会建「种子同名目录」，文件直接落在收件目录里。
早先 measure() 只统计子目录，于是这种落地永远等不到 —— 任务卡在 cloud，
0/0，看起来像「在下载」（其实早就下完了）。

这两例守住：
  · measure 要认收件目录根下的文件（记在 LOOSE_KEY 下）；
  · 整理时源目录是**收件目录本身**（source 为空），目标目录名用番号。
"""
from __future__ import annotations

import pytest

from app import cloudingest, cloudorganize


class _Entry:
    def __init__(self, name, size=0):
        self.name = name
        self.size = size


class _Backend:
    """最小假后端：只实现 measure/plan 用到的那几个方法。"""

    def __init__(self, dirs=None, files=None):
        self._dirs = dirs or {}
        self._files = files or {}

    def list_dirs(self, path):
        return list(self._dirs.get(path, []))

    def list_files(self, path):
        return [_Entry(n, s) for n, s in self._files.get(path, {}).items()]


def test_measure_sees_loose_files():
    b = _Backend(files={"/inbox": {"MIMK-267ch.mp4": 5_282_620_174}})
    items = cloudingest.measure(b, "/inbox")
    assert cloudingest.LOOSE_KEY in items
    assert items[cloudingest.LOOSE_KEY]["bytes"] == 5_282_620_174


def test_measure_loose_key_never_collides_with_real_dirname():
    """固定的键不能跟真实目录名撞 —— 用 "=" 开头，目录名不会有。"""
    assert cloudingest.LOOSE_KEY.startswith("=")
    b = _Backend(dirs={"/inbox": ["=loose"]},
                 files={"/inbox": {"a.mp4": 1}})
    items = cloudingest.measure(b, "/inbox")
    # 两者会合并到同一个键 —— 极端情况，但要保证不崩
    assert cloudingest.LOOSE_KEY in items


def test_plan_with_empty_source_uses_folder_code():
    """没有落地目录名时，目标目录名用任务里的番号。"""
    p = cloudorganize.plan("", [{"name": "MIMK-267ch.mp4", "size": 100}],
                           folder_code="MIMK-267")
    assert p.source == ""
    assert p.target == "MIMK-267"
    assert p.move == ["MIMK-267ch.mp4"]


def test_plan_without_folder_code_falls_back_to_filename():
    """没给番号时退回「从文件名认番号」，认不出就是 unsorted。"""
    p = cloudorganize.plan("", [{"name": "MIMK-267ch.mp4", "size": 100}])
    assert p.target in ("MIMK-267", "unsorted")
    # 非媒体文件（没有可归档的主体）本来就该被拒
    with pytest.raises(cloudorganize.OrganizeError):
        cloudorganize.plan("", [{"name": "random.bin", "size": 100}])


def test_cleanup_of_loose_files_keeps_the_inbox():
    """文件直接躺在收件目录时，清理只删搬走/广告那几个文件，收件目录留着。"""
    purged = []

    class B:
        def list_dirs(self, path):
            return []

        def list_files(self, path):
            return []                 # 媒体已经 move，源路径不存在

        def purge(self, path):
            purged.append(path)
            return "已删除"

    plan = cloudorganize.plan("", [{"name": "MIMK-267ch.mp4", "size": 100}],
                              folder_code="MIMK-267")
    cloudorganize.cleanup_source(B(), "inbox/task", plan, "X-Drive")
    assert purged == [], "已搬走的媒体不应再向源路径发删除请求"


def test_cleanup_of_normal_dir_keeps_empty_shell():
    """普通形态也保留源目录壳，回读与清理间迟到媒体不被连带删除。"""
    purged = []

    class B:
        def list_dirs(self, path):
            return []

        def list_files(self, path):
            return []                 # 媒体已经 move，源路径不存在

        def purge(self, path):
            purged.append(path)
            return "已删除"

    plan = cloudorganize.plan("kss55.cc@MAAN-1199",
                              [{"name": "MAAN-1199.mp4", "size": 100}],
                              folder_code="MAAN-1199")
    cloudorganize.cleanup_source(B(), "inbox/task", plan, "X-Drive")
    assert purged == [], "无已知残留时只保留源目录壳"
