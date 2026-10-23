"""rclone 适配器：argv 必须与改造前逐字一致，错误要分对类。

这个后端是现网在跑的那条路，任何「顺手优化」都可能让上传变慢或报错，
所以这里把每条命令的形状钉住（参数顺序都算契约）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.storage.base import AuthError, StorageError
from app.storage.rclone import Rclone


class Recorder:
    def __init__(self, replies=None):
        self.calls: list[tuple[tuple, dict]] = []
        self.replies = list(replies or [])

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.replies:
            return self.replies.pop(0)
        return 0, "", ""


def _backend(rec, remote="gdrive"):
    return Rclone(run=rec, remote=remote)


# ─────────────────────────────────────────── 命令形状

def test_upload_dir_keeps_original_argv(tmp_path):
    rec = Recorder()
    backend = _backend(rec)
    (tmp_path / "a.mp4").write_bytes(b"x" * 7)

    size = backend.upload_dir(tmp_path, "X-Drive/SSIS-555", transfers=4, chunk="64M")

    assert size == 7
    mkdir, copy = rec.calls[0], rec.calls[1]
    assert mkdir[0] == ("mkdir", "gdrive:X-Drive/SSIS-555")
    assert mkdir[1] == {"timeout": 120.0}
    # 参数顺序就是老代码的顺序，别动
    assert copy[0] == (
        "copy", str(tmp_path), "gdrive:X-Drive/SSIS-555",
        "--transfers", "4", "--drive-chunk-size", "64M",
        "--retries", "2", "--timeout", "30m", "--drive-stop-on-upload-limit",
    )
    assert copy[1] == {"timeout": 7200.0}


def test_upload_file_uses_copyto(tmp_path):
    rec = Recorder()
    local = tmp_path / "movie.nfo"
    local.write_text("x")
    _backend(rec).upload_file(local, "X-Drive/A/movie.nfo")
    assert rec.calls[0][0] == ("copyto", str(local), "gdrive:X-Drive/A/movie.nfo")
    assert rec.calls[0][1] == {"timeout": 300.0}


def test_list_files_parses_lsjson():
    rec = Recorder([(0, json.dumps([{"Name": "a.mp4", "Size": 3},
                                    {"Name": "b.nfo", "Size": 0}]), "")])
    files = _backend(rec).list_files("X-Drive/A")
    assert [(f.name, f.size) for f in files] == [("a.mp4", 3), ("b.nfo", 0)]
    assert rec.calls[0][0] == ("lsjson", "gdrive:X-Drive/A", "--files-only")


def test_list_dirs_trims_slashes():
    rec = Recorder([(0, "A/\nB/\n", "")])
    assert _backend(rec).list_dirs("X-Drive") == ["A", "B"]


def test_link_only_returns_http():
    rec = Recorder([(0, "https://drive.example/x", "")])
    assert _backend(rec).link("X-Drive/A") == "https://drive.example/x"
    rec = Recorder([(0, "", "no backend support")])
    assert _backend(rec).link("X-Drive/A") is None


def test_rename_dir_moves_then_rmdir():
    rec = Recorder()
    _backend(rec).rename_dir("X-Drive/OLD", "NEW")
    assert rec.calls[0][0] == ("move", "gdrive:X-Drive/OLD", "gdrive:X-Drive/NEW")
    assert rec.calls[1][0] == ("rmdir", "gdrive:X-Drive/OLD")


def test_purge_uses_purge():
    rec = Recorder()
    assert _backend(rec).purge("X-Drive/A") == "已删除"
    assert rec.calls[0][0] == ("purge", "gdrive:X-Drive/A")


# ─────────────────────────────────────────── 错误分类

def test_auth_errors_are_not_retried():
    rec = Recorder([(1, "", "Failed to create file system: didn't find section in config file"),
                    (1, "", "401 Unauthorized")])
    backend = _backend(rec)
    with pytest.raises(AuthError):
        backend.list_dirs("X-Drive")


def test_transient_error_is_storage_error():
    rec = Recorder([(1, "", "connection reset by peer")])
    with pytest.raises(StorageError) as err:
        _backend(rec).list_dirs("X-Drive")
    assert not isinstance(err.value, AuthError)


def test_check_mkdir_then_rmdir_probe():
    rec = Recorder([(0, "", ""), (0, "", ""), (0, "", "")])
    ok, detail = _backend(rec).check()
    assert ok and "可读写" in detail
    assert rec.calls[1][0] == ("mkdir", "gdrive:_healthcheck")
    assert rec.calls[2][0] == ("rmdir", "gdrive:_healthcheck")


def test_check_reports_readonly():
    rec = Recorder([(0, "", ""), (1, "", "403 Forbidden")])
    ok, detail = _backend(rec).check()
    assert not ok and "不可写" in detail
