"""2.3 CD2 move_file 的两步补偿：MoveFile 成功、RenameFile 失败不留孤儿。

move_file 是「移动 + 改名」两步远端操作，不是原子的：移动成功改名失败时
文件已在目标目录、用旧名，而调用方的回滚清单里还没有它 —— 不补偿就是
一个谁都不知道的孤儿文件，重试还会撞「目标已存在」。
"""
from __future__ import annotations

import pytest

from app.storage.base import StorageError
from app.storage.clouddrive2 import CloudDrive2


class _Res:
    def __init__(self, ok: bool, err: str = ""):
        self.success = ok
        self.errorMessage = err
        self.resultFilePaths = []


class FakeChan:
    """按方法名派发的假 channel；RenameFile / 第二次 MoveFile 可注入失败。"""

    def __init__(self, rename_ok: bool = False, rollback_ok: bool = True):
        self.rename_ok = rename_ok
        self.rollback_ok = rollback_ok
        self.calls: list[str] = []

    def call(self, method, request, response_cls, **kw):
        self.calls.append(method)
        if method == "MoveFile":
            moves = self.calls.count("MoveFile")
            if moves >= 2 and not self.rollback_ok:
                return _Res(False, "回滚被拒")
            return _Res(True, "")
        if method == "RenameFile":
            return _Res(self.rename_ok, "" if self.rename_ok else "改名被拒")
        if method == "FindFileByPath":
            return _Res(False, "not found")     # _find 会当 None 处理
        return _Res(True, "")


def make(chan):
    cd2 = CloudDrive2("http://127.0.0.1:1", channel=chan)
    cd2.ensure_dir = lambda path: path.rsplit("/", 1)[-1]      # 不测建目录
    cd2._find = lambda path: None                              # 目标不存在
    return cd2


def test_rename_failure_rolls_back_to_source():
    chan = FakeChan(rename_ok=False, rollback_ok=True)
    cd2 = make(chan)
    with pytest.raises(StorageError) as err:
        cd2.move_file("inbox/old.mp4", "archive/new.mp4")
    assert "已回滚到原目录" in str(err.value)
    assert chan.calls.count("MoveFile") == 2, "第二次 MoveFile 是补偿回滚"


def test_rename_and_rollback_failure_reports_actual_location():
    chan = FakeChan(rename_ok=False, rollback_ok=False)
    cd2 = make(chan)
    with pytest.raises(StorageError) as err:
        cd2.move_file("inbox/old.mp4", "archive/new.mp4")
    text = str(err.value)
    assert "archive/old.mp4" in text, "回滚失败必须报出文件实际位置（旧名）"
    assert "人工处理" in text


def test_normal_move_needs_no_compensation():
    chan = FakeChan(rename_ok=True)
    cd2 = make(chan)
    cd2.move_file("inbox/old.mp4", "archive/new.mp4")
    assert chan.calls == ["MoveFile", "RenameFile"]
