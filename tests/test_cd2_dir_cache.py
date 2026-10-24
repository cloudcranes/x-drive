"""CD2 目录缓存：刚建出来的目录，缓存没同步会导致「找不到」。

起因（2026-10-24 实测）：云添加任务 MAAN-1199 提交后 0.17 秒就失败：

    clouddrive2 列目录失败（/迅雷云盘/我的资源/MAAN1199）：
    clouddrive2 调用失败：find by path: "/迅雷云盘/我的资源/MAAN1199"
    not found under "/迅雷云盘/我的资源"

目录明明在（FindFileByPath 查得到），是 CD2 **服务端的目录缓存**还没
同步：GetSubFiles 会先按路径在缓存里解析，解析不到就回 NOT_FOUND。

修法两条：
  · 读目录报「找不到」时用 forceRefresh=True 重读一次；
  · 提交云添加报「找不到」时先 ForceExpireDirCache 再重试。
"""
from __future__ import annotations

import pytest

from app.storage import clouddrive2 as cd2
from app.storage.clouddrive2 import CloudDrive2


class _Entry:
    def __init__(self, name, is_dir=False, size=0):
        self.name = name
        self.isDirectory = is_dir
        self.size = size


class _Chan:
    """假 gRPC 通道：按脚本回放。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def call(self, method, request, response_cls, **kw):
        self.calls.append((method, request))
        if not self.script:
            raise AssertionError(f"没有预置响应：{method}")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _backend(chan):
    b = CloudDrive2.__new__(CloudDrive2)
    b.chan = chan
    return b


def test_entries_force_refresh_flag():
    """force=True 时请求里带 forceRefresh=True（这是绕缓存的关键）。"""
    class Reply:
        subFiles = []

        def __iter__(self):
            return iter([self])

    chan = _Chan([Reply()])
    b = _backend(chan)
    b._entries("/x", force=True)
    method, req = chan.calls[0]
    assert method == "GetSubFiles"
    assert req.forceRefresh is True
    assert req.path == "/x"


def test_entries_plain_has_no_force_refresh():
    class Reply:
        subFiles = []

        def __iter__(self):
            return iter([self])

    chan = _Chan([Reply()])
    b = _backend(chan)
    b._entries("/x")
    _method, req = chan.calls[0]
    assert req.forceRefresh is False


def test_entries_fresh_retries_with_force_on_not_found():
    """普通读报「找不到」→ 用 forceRefresh 重读一次并成功。"""

    class Reply:
        subFiles = [_Entry("MAAN1199", is_dir=True)]

        def __iter__(self):
            return iter([self])

    not_found = cd2.StorageError(
        'clouddrive2 调用失败：find by path: "/迅雷云盘/我的资源/MAAN1199" '
        'not found under "/迅雷云盘/我的资源"')
    chan = _Chan([not_found, Reply()])
    b = _backend(chan)
    out = b._entries_fresh("/迅雷云盘/我的资源/MAAN1199")
    assert [e.name for e in out] == ["MAAN1199"]
    assert chan.calls[0][1].forceRefresh is False
    assert chan.calls[1][1].forceRefresh is True


def test_entries_fresh_does_not_retry_on_other_errors():
    """别的错误不重试（省一次远端往返，也免得掩盖真因）。"""
    other = cd2.StorageError("clouddrive2 调用失败：permission denied")
    chan = _Chan([other])
    b = _backend(chan)
    with pytest.raises(cd2.StorageError) as ei:
        b._entries_fresh("/x")
    assert "permission denied" in str(ei.value)
    assert len(chan.calls) == 1, "不该重试"


def test_entries_fresh_reraises_original_when_force_also_fails():
    """强刷也失败 → 报原来那条（信息更准），别报成强刷的。"""
    not_found = cd2.StorageError("not found under /x")
    chan = _Chan([not_found, cd2.StorageError("not found under /x (forced)")])
    b = _backend(chan)
    with pytest.raises(cd2.StorageError) as ei:
        b._entries_fresh("/x")
    assert "forced" not in str(ei.value)


def test_offline_add_refreshes_cache_and_retries_on_not_found():
    """提交报「找不到」→ 先作废目录缓存再重试一次。"""

    class Result:
        success = True
        errorMessage = ""
        resultFilePaths = []

    chan = _Chan([
        cd2.StorageError('find by path: "/迅雷云盘/我的资源/MAAN1199" '
                         'not found under "/迅雷云盘/我的资源"'),
        Result(),
    ])
    b = _backend(chan)
    b._refresh_dir_cache = lambda path: chan.calls.append(("REFRESH", path))
    b.offline_add("magnet:?xt=urn:btih:abc", "/迅雷云盘/我的资源/MAAN1199")
    methods = [m for m, _ in chan.calls]
    assert methods.count("AddOfflineFiles") == 2, "应该重试一次"
    assert "REFRESH" in methods, "重试前要先作废目录缓存"


def test_offline_add_gives_up_after_one_retry():
    """重试一次还失败就抛（不无限重试）。"""
    chan = _Chan([
        cd2.StorageError("not found under /x"),
        cd2.StorageError("not found under /x"),
    ])
    b = _backend(chan)
    b._refresh_dir_cache = lambda path: None
    with pytest.raises(cd2.StorageError):
        b.offline_add("magnet:?x", "/x")
    assert len([m for m, _ in chan.calls if m == "AddOfflineFiles"]) == 2


def test_offline_add_success_first_time_no_refresh():
    class Result:
        success = True
        errorMessage = ""
        resultFilePaths = []

    chan = _Chan([Result()])
    b = _backend(chan)
    b._refresh_dir_cache = lambda path: (_ for _ in ()).throw(
        AssertionError("不该刷新缓存"))
    b.offline_add("magnet:?x", "/x")
    assert len(chan.calls) == 1
