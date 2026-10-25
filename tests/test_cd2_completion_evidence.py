"""无网络上传完成证据回归，使用实际 upload_dir/upload_file/_wait_upload。"""
from types import SimpleNamespace

import pytest

from app.storage import clouddrive2 as cd2
from app.storage.base import StorageError


class Clock:
    def __init__(self):
        self.now = 0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def backend(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(cd2, 'time', clock)
    backend = object.__new__(cd2.CloudDrive2)
    backend.chan = SimpleNamespace(task_timeout=3)
    backend._uploads = lambda: []
    return backend


@pytest.mark.parametrize('actual', [0, None, 9, 11])
def test_missing_task_requires_exact_nonzero_size(backend, actual):
    backend._find = lambda path: SimpleNamespace(size=actual, isDirectory=False)
    with pytest.raises(StorageError, match='超时'):
        backend._wait_upload('archive/main.mp4', 10)


def test_disappeared_task_exact_nonzero_file_is_verified(backend):
    backend._find = lambda path: SimpleNamespace(size=10, isDirectory=False)
    backend._wait_upload('archive/main.mp4', 10)


def test_upload_list_error_does_not_fall_back_to_existing_file(backend):
    def fail():
        raise StorageError('fixture query failure')
    backend._uploads = fail
    backend._find = lambda path: SimpleNamespace(size=10, isDirectory=False)
    with pytest.raises(StorageError, match='读取上传任务失败.*fixture query failure'):
        backend._wait_upload('archive/main.mp4', 10)


@pytest.mark.parametrize('actual', [0, None, 9, 11])
def test_finished_task_still_requires_exact_final_size(backend, actual):
    backend._uploads = lambda: [SimpleNamespace(
        destPath='/archive/main.mp4', statusEnum=cd2.FINISHED)]
    backend._find = lambda path: SimpleNamespace(size=actual, isDirectory=False)
    with pytest.raises(StorageError, match='完成核验失败'):
        backend._wait_upload('archive/main.mp4', 10)


def test_finished_exact_file_and_normalized_filename(backend):
    backend._uploads = lambda: [SimpleNamespace(
        destPath='/archive/a：b.mp4', statusEnum=cd2.FINISHED)]
    backend._find = lambda path: (SimpleNamespace(size=10, isDirectory=False)
                                 if path == 'archive/a：b.mp4' else None)
    backend._wait_upload('archive/a:b.mp4', 10)


def test_empty_file_requires_finished_evidence(backend):
    backend._find = lambda path: SimpleNamespace(size=0, isDirectory=False)
    with pytest.raises(StorageError, match='超时'):
        backend._wait_upload('archive/main.mp4', 0)
    backend._uploads = lambda: [SimpleNamespace(
        destPath='/archive/main.mp4', statusEnum=cd2.FINISHED)]
    backend._wait_upload('archive/main.mp4', 0)


def test_upload_dir_keeps_local_source_when_cd2_only_has_placeholder(backend, tmp_path):
    local = tmp_path / 'work'
    local.mkdir()
    source = local / 'main.mp4'
    source.write_bytes(b'0123456789')
    backend.chunk = 4
    backend.ensure_dir = lambda path: path
    backend._find = lambda path: SimpleNamespace(size=0, isDirectory=False)
    streamed = []
    def call(method, request, response_type, **kwargs):
        if method == 'CreateFile':
            return SimpleNamespace(fileHandle=1)
        if method == 'WriteToFileStream':
            streamed.extend(list(request()))
            return SimpleNamespace()
        raise AssertionError(method)
    backend.chan.call = call
    with pytest.raises(StorageError, match='超时'):
        backend.upload_dir(local, 'archive')
    assert sum(len(r.buffer) for r in streamed) == 10
    assert source.read_bytes() == b'0123456789'


def test_running_task_with_full_length_placeholder_never_completes(backend):
    backend._uploads = lambda: [SimpleNamespace(
        destPath='/archive/main.mp4', statusEnum=1, size=10)]
    calls = []
    def find(path):
        calls.append(path)
        return SimpleNamespace(size=10, isDirectory=False)
    backend._find = find
    with pytest.raises(StorageError, match='超时'):
        backend._wait_upload('archive/main.mp4', 10)
    assert calls == [], '匹配活跃任务必须等待 Finished，不能看预分配文件大小'


def test_missing_task_fallback_requires_two_consecutive_exact_reads(backend):
    backend.chan.task_timeout = 20
    sizes = iter([10, 0, 10, 10])
    seen = []
    def find(path):
        size = next(sizes)
        seen.append(size)
        return SimpleNamespace(size=size, isDirectory=False)
    backend._find = find
    backend._wait_upload('archive/main.mp4', 10)
    assert seen == [10, 0, 10, 10]
