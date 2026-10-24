"""CloudDrive2 适配器：对着进程内的假 gRPC 服务跑真协议。

覆盖的坑（都是「不测就会在线上才发现」的那类）：
  · 上传是异步任务 → upload_dir 必须等 Status_Finished 才返回；
  · 任务报 Status_Error → 抛 StorageError（可重试），不能当成功；
  · 目录/文件名会被归一化（全角）→ ensure_dir 要返回真实名；
  · token 过期 → 重登一次（不循环）；
  · 改名撞车 → 明确失败，绝不覆盖。
"""
from __future__ import annotations

import os
import time

import pytest

from app.storage import clouddrive2 as cd2mod
from app.storage.base import AuthError, StorageError
from tests.cd2_fake import FakeCd2


@pytest.fixture()
def fake():
    server = FakeCd2().start()
    try:
        yield server
    finally:
        server.stop()


def backend(fake, **kw):
    kw.setdefault("chunk_mb", 1)
    kw.setdefault("task_timeout", 20)
    kw.setdefault("call_timeout", 10)
    return cd2mod.CloudDrive2(fake.url, fake.user, fake.password, **kw)


def _dir(tmp_path, name="work", files=None):
    work = tmp_path / name
    work.mkdir()
    for fname, size in (files or {}).items():
        (work / fname).write_bytes(b"x" * size)
    return work


# ─────────────────────────────────────────── 目录

def test_ensure_dir_returns_normalized_name(fake):
    """CD2 会把 `*?:/\\<>|"` 转全角：记账必须用返回的真实名。"""
    cd2 = backend(fake)
    real = cd2.ensure_dir("X-Drive/SSIS-555")
    assert real == "SSIS-555"
    assert "/X-Drive/SSIS-555" in fake.dirs

    real2 = cd2.ensure_dir("X-Drive/A:B")
    assert real2 == "A：B", "半角冒号转全角后才是真实目录名"
    assert "/X-Drive/A：B" in fake.dirs


def test_ensure_dir_is_idempotent(fake):
    cd2 = backend(fake)
    assert cd2.ensure_dir("X-Drive/A") == "A"
    assert cd2.ensure_dir("X-Drive/A") == "A"      # 第二次走 FindFileByPath 分支
    assert len([d for d in fake.dirs if d.endswith("/A")]) == 1


def test_list_files_and_dirs_merge_stream_batches(fake):
    fake.page_size = 2                             # 强制分成多批
    for i in range(5):
        fake.add_file(f"/X-Drive/A/f{i}.mp4", 10 + i)
    fake.add_file("/X-Drive/A/sub/x.mp4", 1)
    cd2 = backend(fake)
    files = cd2.list_files("X-Drive/A")
    assert [f.name for f in files] == [f"f{i}.mp4" for i in range(5)]
    assert [f.size for f in files] == [10, 11, 12, 13, 14]
    assert cd2.list_dirs("X-Drive/A") == ["sub"]


# ─────────────────────────────────────────── 上传

def test_upload_dir_waits_until_task_finished(fake, tmp_path):
    work = _dir(tmp_path, files={"SSIS555.mp4": 2 * 1024 * 1024 + 1024,
                                 "movie.nfo": 20})
    cd2 = backend(fake)
    seen = []
    size = cd2.upload_dir(work, "X-Drive/SSIS-555", progress=lambda r, n: seen.append(r))

    assert size == 2 * 1024 * 1024 + 1024 + 20
    # 返回即代表云端已落地：假服务的 files 里必须真有这两个文件
    assert fake.files["/X-Drive/SSIS-555/SSIS555.mp4"] == 2 * 1024 * 1024 + 1024
    assert fake.files["/X-Drive/SSIS-555/movie.nfo"] == 20
    assert seen and seen[-1] == pytest.approx(1.0)


def test_upload_streams_multiple_chunks(fake, tmp_path):
    """chunk_mb=1 时 2.5MB 要分 3 条 WriteFileRequest（不能整块塞内存）。"""
    work = _dir(tmp_path, files={"big.mp4": 2 * 1024 * 1024 + 512 * 1024})
    cd2 = backend(fake, chunk_mb=1)
    cd2.upload_dir(work, "X-Drive/CODE")
    assert fake.files["/X-Drive/CODE/big.mp4"] == 2 * 1024 * 1024 + 512 * 1024


def test_upload_error_task_raises_storage_error(fake, tmp_path):
    fake.fail_upload = True
    work = _dir(tmp_path, files={"a.mp4": 8})
    cd2 = backend(fake)
    with pytest.raises(StorageError) as err:
        cd2.upload_dir(work, "X-Drive/CODE")
    assert "cloud said no" in str(err.value)


def test_upload_timeout_raises(fake, tmp_path, monkeypatch):
    work = _dir(tmp_path, files={"a.mp4": 8})
    cd2 = backend(fake, task_timeout=0.2)
    cd2.ensure_dir("X-Drive/CODE")
    # 任务永远不完成：把它从列表里摘掉（模拟「CD2 挂了、任务一直排队」）
    monkeypatch.setattr(cd2mod.CloudDrive2, "_uploads", lambda self: [])
    monkeypatch.setattr(cd2mod.CloudDrive2, "_find", lambda self, path: None)
    with pytest.raises(StorageError) as err:
        cd2.upload_dir(work, "X-Drive/CODE")
    assert "超时" in str(err.value)


# ─────────────────────────────────────────── 改名 / 删除

def test_rename_dir_moves_directory(fake):
    cd2 = backend(fake)
    cd2.ensure_dir("X-Drive/OLD-1")
    fake.add_file("/X-Drive/OLD-1/a.mp4", 3)
    cd2.rename_dir("X-Drive/OLD-1", "NEW-1")
    assert "/X-Drive/NEW-1" in fake.dirs and "/X-Drive/OLD-1" not in fake.dirs
    assert fake.files["/X-Drive/NEW-1/a.mp4"] == 3


def test_rename_refuses_existing_target(fake):
    cd2 = backend(fake)
    cd2.ensure_dir("X-Drive/A")
    cd2.ensure_dir("X-Drive/B")
    with pytest.raises(StorageError) as err:
        cd2.rename_dir("X-Drive/A", "B")
    assert "目标已存在" in str(err.value)


def test_purge_removes_tree(fake):
    cd2 = backend(fake)
    fake.add_file("/X-Drive/CODE/a.mp4", 1)
    fake.add_file("/X-Drive/CODE/sub/b.nfo", 1)
    assert cd2.purge("X-Drive/CODE") == "已删除"
    assert not [d for d in fake.dirs if d.startswith("/X-Drive/CODE")]
    assert not [f for f in fake.files if f.startswith("/X-Drive/CODE")]


def test_purge_falls_back_to_trash(fake, monkeypatch):
    cd2 = backend(fake)
    fake.add_file("/X-Drive/CODE/a.mp4", 1)

    from app.storage.proto import CloudDrive_pb2 as pb

    def refuse_permanent(req, _ctx, permanent=True):
        if permanent:
            return pb.FileOperationResult(success=False, errorMessage="not supported")
        return pb.FileOperationResult(success=True, resultFilePaths=list(req.path))

    fake.delete_many = refuse_permanent
    assert "回收站" in cd2.purge("X-Drive/CODE")


# ─────────────────────────────────────────── 分享 / 自检 / 鉴权

def test_link_modes(fake):
    fake.add_file("/X-Drive/A/only.mp4", 5)
    assert backend(fake, link_mode="none").link("X-Drive/A") is None
    url = backend(fake, link_mode="static").link("X-Drive/A")
    assert url and "/static/" in url and "only.mp4" in url
    tpl = backend(fake, link_mode="template", link_template="http://nas/x/{path}")
    assert tpl.link("X-Drive/A") == "http://nas/x/X-Drive/A"


def test_check_reports_space(fake):
    ok, detail = backend(fake).check()
    assert ok and "可读写" in detail and "GB" in detail


def test_check_bad_password_is_explicit(fake):
    cd2 = cd2mod.CloudDrive2(fake.url, "u", "wrong", chunk_mb=1, call_timeout=5)
    ok, detail = cd2.check()
    assert not ok and ("登录失败" in detail or "鉴权" in detail)


def test_token_expiry_triggers_one_relogin(fake):
    fake.expire_token_after = 2
    cd2 = backend(fake)
    cd2.ensure_dir("X-Drive/A")            # 首次登录拿到 t1
    fake.token_serial = 2                  # 服务端只认 t2，旧 token 立刻失效
    assert cd2.list_files("X-Drive/A") == []   # 触发一次 UNAUTHENTICATED → 重登
    assert cd2.ensure_dir("X-Drive/A") == "A"


def test_auth_error_is_distinct_from_storage_error(fake):
    cd2 = cd2mod.CloudDrive2(fake.url, "u", "wrong", chunk_mb=1, call_timeout=5)
    with pytest.raises(AuthError):
        cd2.ensure_dir("X-Drive/A")


def test_check_probes_inside_archive_root(fake, tmp_path):
    """CD2 的 "/" 是聚合根，建不了目录：探针必须落在归档根目录下。"""
    cd2 = backend(fake)
    cd2.ensure_dir("X-Drive")
    ok, detail = cd2.check(probe_path="X-Drive")
    assert ok and "/X-Drive" in detail
    assert not [d for d in fake.dirs if d.endswith("_xdrive_healthcheck")], "探针要自己清掉"


def test_check_says_why_root_is_not_writable(fake):
    """根目录建不了就明说「能登录但建不了目录」，别报成连不上。"""
    cd2 = backend(fake)
    real = cd2.ensure_dir

    def refuse(path):
        if path.strip("/") == "_xdrive_healthcheck":
            raise StorageError("clouddrive2 建目录失败（_xdrive_healthcheck）：create_folder is not supported")
        return real(path)

    cd2.ensure_dir = refuse
    ok, detail = cd2.check(probe_path="")
    assert not ok and "建不了目录" in detail and "create_folder" in detail


def test_space_is_asked_at_archive_root(fake):
    """配额要问归档根那一层：问 "/" 拿到的是聚合根，会给出上千万 GB 的假数字。"""
    asked = []
    real = backend(fake)

    def space(req, _ctx):
        from app.storage.proto import CloudDrive_pb2 as pb
        asked.append(req.path)
        return pb.SpaceInfo(totalSpace=2 << 40, usedSpace=1 << 40, freeSpace=1 << 40)

    fake.get_space = space
    ok, detail = real.check(probe_path="X-Drive")
    assert ok and "1.0TB" in detail, detail
    assert "/X-Drive" in asked and "/" not in asked


# ─────────────────────────────────────────── 云添加（离线下载）

def test_offline_add_accepts_and_reports_nothing(fake):
    """实测：AddOfflineFiles 对假磁力也回 success —— 返回值不能当成功。"""
    cd2 = backend(fake)
    cd2.ensure_dir("迅雷云盘/我的资源")
    cd2.offline_add("magnet:?xt=urn:btih:" + "0" * 40, "迅雷云盘/我的资源")
    assert fake.offline_accepted == ["magnet:?xt=urn:btih:" + "0" * 40]
    # 目录里什么都没有：这就是「接口说成功但没落地」
    assert cd2.list_dirs("迅雷云盘/我的资源") == []


def test_offline_add_rejects_empty(fake):
    with pytest.raises(StorageError) as err:
        backend(fake).offline_add("   ", "迅雷云盘")
    assert "链接为空" in str(err.value)


def test_offline_add_accepts_multiple_urls(fake):
    backend(fake).offline_add(["magnet:?xt=urn:btih:" + "a" * 40,
                               "magnet:?xt=urn:btih:" + "b" * 40], "迅雷云盘")
    assert fake.offline_accepted == ["magnet:?xt=urn:btih:" + "a" * 40
                                     + "\n" + "magnet:?xt=urn:btih:" + "b" * 40]


def test_landed_files_become_visible(fake):
    cd2 = backend(fake)
    cd2.ensure_dir("迅雷云盘/我的资源")
    cd2.offline_add("magnet:?xt=urn:btih:" + "c" * 40, "迅雷云盘/我的资源")
    fake.land_offline("迅雷云盘/我的资源", {"MIMK-290.mp4": 4096, "movie.nfo": 12},
                      name="MIMK-290")
    files = {f.name: f.size for f in cd2.list_files("迅雷云盘/我的资源/MIMK-290")}
    assert files == {"MIMK-290.mp4": 4096, "movie.nfo": 12}


def test_offline_supported_reads_capability_bit(fake):
    cd2 = backend(fake)
    cd2.ensure_dir("迅雷云盘/我的资源")
    ok, detail = cd2.offline_supported("迅雷云盘/我的资源")
    assert ok and "可离线下载" in detail
    assert "不可查询进度" in detail, "这台 CD2 不支持管理，要如实说明"


def test_offline_supported_false_when_bit_off(fake):
    fake.can_offline = False
    cd2 = backend(fake)
    cd2.ensure_dir("迅雷云盘/我的资源")
    ok, detail = cd2.offline_supported("迅雷云盘/我的资源")
    assert not ok and "不支持离线下载" in detail


def test_cloud_roots_lists_clouds(fake):
    roots = backend(fake).cloud_roots()
    assert [r["name"] for r in roots] == ["迅雷云盘"]
    assert roots[0]["path"] == "/迅雷云盘"
