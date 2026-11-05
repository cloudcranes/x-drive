"""契约测试：CloudDrive2 实现上层统一存储行为。

上层（网页 / bot）只依赖 app/storage/base.py 里那十个方法的行为承诺：
建目录幂等、上传返回即落地、改名后路径对得上、删除后目录消失。
通过离线 gRPC 服务核验真实 CloudDrive2 适配器。
"""
from __future__ import annotations

import pytest

from app.storage.base import StorageError
from app.storage.clouddrive2 import CloudDrive2
from tests.cd2_fake import FakeCd2


@pytest.fixture
def env(request, tmp_path):
    fake = FakeCd2().start()
    request.addfinalizer(fake.stop)
    backend = CloudDrive2(fake.url, fake.user, fake.password, chunk_mb=1,
                          task_timeout=20)
    return backend, fake, tmp_path


def _write(tmp_path, files):
    work = tmp_path / "work"
    work.mkdir()
    for name, size in files.items():
        (work / name).write_bytes(b"z" * size)
    return work


def test_ensure_dir_is_idempotent(env):
    backend, _fake, _ = env
    assert backend.ensure_dir("X-Drive/CODE-1") == "CODE-1"
    assert backend.ensure_dir("X-Drive/CODE-1") == "CODE-1"


def test_upload_then_list_reports_same_bytes(env):
    backend, _fake, tmp_path = env
    work = _write(tmp_path, {"CODE1.mp4": 1024, "movie.nfo": 12})
    backend.upload_dir(work, "X-Drive/CODE-1")
    files = {f.name: f.size for f in backend.list_files("X-Drive/CODE-1")}
    assert files == {"CODE1.mp4": 1024, "movie.nfo": 12}


def test_rename_keeps_children(env):
    backend, _fake, tmp_path = env
    work = _write(tmp_path, {"a.mp4": 5})
    backend.upload_dir(work, "X-Drive/OLD")
    backend.rename_dir("X-Drive/OLD", "NEW")
    assert "NEW" in backend.list_dirs("X-Drive")
    assert [f.name for f in backend.list_files("X-Drive/NEW")] == ["a.mp4"]


def test_purge_removes_directory(env):
    backend, _fake, tmp_path = env
    work = _write(tmp_path, {"a.mp4": 5})
    backend.upload_dir(work, "X-Drive/GONE")
    assert backend.purge("X-Drive/GONE") in ("已删除", "已移入回收站")
    assert "GONE" not in backend.list_dirs("X-Drive")


def test_upload_file_single(env):
    backend, _fake, tmp_path = env
    local = tmp_path / "only.nfo"
    local.write_text("hello")
    backend.ensure_dir("X-Drive/SINGLE")
    backend.upload_file(local, "X-Drive/SINGLE/only.nfo")
    assert [f.name for f in backend.list_files("X-Drive/SINGLE")] == ["only.nfo"]


def test_listing_missing_dir_is_an_error(env):
    backend, _fake, _ = env
    with pytest.raises(StorageError):
        backend.list_files("X-Drive/does-not-exist")
