"""「路径不存在」跟「读失败」要分开（2026-10-24 实测的 MIMK-182 事故）。

起因：任务专属收件目录 `<inbox>/<键>` 是**提交时才会建**的，拍快照那一刻
它压根不存在。CD2 回
    find by path: "/迅雷云盘/我的资源/MIMK182" not found under "/迅雷云盘/我的资源"
这被当成「读失败」抛出去 → 任务在提交前就挂了（MIMK-182 就是这么挂的）。

分法：
  · NotFoundError  → 目录还没有 → 快照就是空的（真的空，不是降级）
  · 别的读失败     → 照旧抛，不许降级（曾经出过事：降级后把别人的目录当新落地）
"""
from __future__ import annotations

import pytest

from app import cloudingest
from app.storage.base import NotFoundError, StorageError


class _Entry:
    def __init__(self, name, size=0):
        self.name = name
        self.size = size


class _Backend:
    """假的存储后端。"""

    def __init__(self, dirs=None, files=None, dirs_error=None, files_error=None):
        self._dirs = dirs or []
        self._files = files or []
        self._dirs_error = dirs_error
        self._files_error = files_error

    def list_dirs(self, path):
        if self._dirs_error:
            raise self._dirs_error
        return list(self._dirs)

    def list_files(self, path):
        if self._files_error:
            raise self._files_error
        return [_Entry(n, s) for n, s in self._files]


def test_snapshot_strict_treats_missing_dir_as_empty():
    """目录还不存在 → 快照是空的，任务能往下走。"""
    b = _Backend(dirs_error=NotFoundError('find by path: "..." not found under "..."'))
    assert cloudingest.snapshot_strict(b, "/inbox/KEY") == {}


def test_snapshot_strict_still_raises_on_real_read_failure():
    """真读失败（不是「不存在」）→ 照旧抛，不许降级。

    降级过一次，后果是 inbox 里别人的目录全被当成「新落地」被搬走清掉。
    """
    b = _Backend(dirs_error=StorageError("clouddrive2 列目录失败：网络超时"))
    with pytest.raises(StorageError):
        cloudingest.snapshot_strict(b, "/inbox/KEY")


def test_snapshot_strict_reads_existing_dir():
    """目录在 → 快照记下子目录 + 根下文件（后者记在 LOOSE_KEY 下）。

    根下文件整体记一个键，是为了让快照与 measure() 对齐 ——
    否则单文件种子每轮都算「新增」，永远不稳定。
    """
    b = _Backend(dirs=["a", "b"], files=[("c.mp4", 10)])
    assert cloudingest.snapshot_strict(b, "/inbox/KEY") == {
        "a": -1, "b": -1, cloudingest.LOOSE_KEY: 10}


def test_snapshot_strict_missing_dir_skips_file_read():
    """目录不存在时不再读文件（那个目录本来就没东西可读）。

    反过来：目录在、但读**文件**失败 → 照旧抛。
    """
    b = _Backend(dirs_error=NotFoundError("not found"),
                 files_error=StorageError("读文件失败：超时"))
    assert cloudingest.snapshot_strict(b, "/inbox/KEY") == {}

    b2 = _Backend(dirs=[], files_error=StorageError("读文件失败：超时"))
    with pytest.raises(StorageError):
        cloudingest.snapshot_strict(b2, "/inbox/KEY")


def test_not_found_is_a_storage_error():
    """NotFoundError 仍是 StorageError —— 老的 except StorageError 不会漏。"""
    assert issubclass(NotFoundError, StorageError)


def test_cd2_not_found_message_recognized():
    """CD2 的各种「不存在」措辞都要认出来。"""
    from app.storage.clouddrive2 import _is_not_found
    for text in [
        'find by path: "/x" not found under "/y"',
        "NOT_FOUND:find by path",
        "路径不存在",
        "找不到该路径",
        "no such file or directory",
    ]:
        assert _is_not_found(text), text
    for text in ["permission denied", "网络超时", "connection reset"]:
        assert not _is_not_found(text), text
