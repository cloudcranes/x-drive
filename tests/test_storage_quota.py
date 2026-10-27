"""云盘容量：本地下载取消后，「还剩多少空间」只能问云端。

要钉住的几件事：
  · 三个后端都能报出容量（CD2 用 GetSpaceInfo，rclone 用 about，OpenList 用
    管理接口）；
  · 拿不到数时返回「不知道」而不是 0 —— 0 会被读成「云盘满了」；
  · CD2 必须问归档根那一层，不是聚合根（聚合根的数字离谱到 10553213.2GB）；
  · rclone 的 about 输出单位要正确还原成字节。
"""
from __future__ import annotations

import httpx
import pytest

from app.storage.base import Quota
from app.storage.clouddrive2 import CloudDrive2
from app.storage.openlist import OpenList
from app.storage.rclone import Rclone, _parse_size
from tests.cd2_fake import FakeCd2


# ─────────────────────────────────────────── rclone

def test_rclone_quota_parses_about_output():
    out = "Total:   15.000 TiB\nUsed:    1.234 GiB\nFree:    13.766 TiB\n"
    calls: list[tuple] = []

    def run(*args, **kwargs):
        calls.append(args)
        return 0, out, ""

    q = Rclone(run=run, remote="gdrive").quota()

    assert calls == [("about", "gdrive:")]
    assert q.known
    assert q.total == _parse_size("15.000 TiB")
    assert q.used == _parse_size("1.234 GiB")
    assert q.free == _parse_size("13.766 TiB")


def test_rclone_quota_failure_returns_unknown():
    """问不到就报「不知道」，不能报 0（0 会被读成「满了」）。"""
    def run(*args, **kwargs):
        return 1, "", "network unreachable"

    q = Rclone(run=run).quota()

    assert not q.known
    assert q.free == 0


@pytest.mark.parametrize("text,expect", [
    ("1.234 GiB", 1324997410),
    ("15.000 TiB", 16492674416640),
    ("500 MiB", 524288000),
    ("2.5 GB", 2500000000),
    ("0 B", 0),
    ("garbage", 0),
    ("", 0),
])
def test_parse_size_units(text, expect):
    assert _parse_size(text) == expect


# ─────────────────────────────────────────── CloudDrive2

def test_cd2_quota_reports_space():
    fake = FakeCd2().start()
    try:
        backend = CloudDrive2(fake.url, fake.user, fake.password, chunk_mb=1,
                              task_timeout=20)
        q = backend.quota("迅雷云盘/X-Drive")
    finally:
        fake.stop()

    assert q.known
    assert q.total == 100 << 30
    assert q.used == 40 << 30
    assert q.free == 60 << 30


def test_cd2_quota_asks_the_given_path_not_root():
    """必须问归档根那一层：CD2 的 "/" 是聚合根，数字离谱。"""
    fake = FakeCd2().start()
    try:
        backend = CloudDrive2(fake.url, fake.user, fake.password, chunk_mb=1,
                              task_timeout=20)
        backend.quota("迅雷云盘/X-Drive")
        asked = [p for p in fake.space_paths]
    finally:
        fake.stop()

    assert asked, "没问过容量"
    assert all("/" in p and "迅雷云盘" in p for p in asked), \
        f"问错了层：{asked}（聚合根的容量不能信）"


def test_cd2_quota_error_returns_unknown():
    """连不上时给「不知道」，别抛也别报 0。"""
    fake = FakeCd2().start()
    try:
        backend = CloudDrive2(fake.url, fake.user, fake.password, chunk_mb=1,
                              task_timeout=20)
        fake.stop()          # 服务端没了
        q = backend.quota("迅雷云盘/X-Drive")
    finally:
        try:
            fake.stop()
        except Exception:
            pass

    assert not q.known


# ─────────────────────────────────────────── OpenList

def test_openlist_quota_sums_storages():
    class Fake:
        def handler(self, request):
            if request.url.path == "/api/auth/login":
                return httpx.Response(200, json={"code": 200, "data": {"token": "t"}})
            assert request.url.path == "/api/admin/storage/list"
            return httpx.Response(200, json={"code": 200, "data": [
                {"total_capacity": 100, "used_capacity": 30, "free_capacity": 70},
                {"total_capacity": 50, "used_capacity": 10, "free_capacity": 40},
            ]})

    fake = Fake()
    client = httpx.Client(base_url="http://ol",
                          transport=httpx.MockTransport(fake.handler))
    backend = OpenList("http://ol", user="u", password="p", client=client)

    q = backend.quota()

    assert q.known
    assert q.total == 150
    assert q.used == 40
    assert q.free == 110


def test_openlist_quota_permission_denied_returns_unknown():
    """普通账号没管理员权限会 403 —— 那就说问不到，别报 0。"""
    class Fake:
        def handler(self, request):
            if request.url.path == "/api/auth/login":
                return httpx.Response(200, json={"code": 200, "data": {"token": "t"}})
            return httpx.Response(403, json={"code": 403, "message": "forbidden"})

    client = httpx.Client(base_url="http://ol",
                          transport=httpx.MockTransport(Fake().handler))
    backend = OpenList("http://ol", user="u", password="p", client=client)

    q = backend.quota()

    assert not q.known


# ─────────────────────────────────────────── storage.quota 门面

def test_storage_quota_returns_unknown_when_backend_broken(monkeypatch):
    from app import storage

    class Boom:
        kind = "rclone"

        def quota(self, probe_path=""):
            raise RuntimeError("炸了")

    monkeypatch.setattr(storage, "get", lambda which=None: Boom())

    q = storage.quota()
    assert not q.known


def test_storage_quota_returns_unknown_when_backend_lacks_method(monkeypatch):
    """契约外的后端没 quota 方法：不能炸，返回「不知道」。"""
    from app import storage

    class Old:
        kind = "rclone"

    monkeypatch.setattr(storage, "get", lambda which=None: Old())

    q = storage.quota()
    assert not q.known
