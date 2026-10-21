"""aria2 RPC 客户端测试：全部走假 HTTP 层。"""
import asyncio
import json

import httpx
import pytest

from app import aria2


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)
    def json(self):
        return self._payload


class FakeClient:
    """按调用序列返回预设结果；序列用完后重复最后一个。"""
    def __init__(self, results):
        self.results = list(results)
        self.calls = []
    async def post(self, url, json=None, timeout=None):
        self.calls.append(json)
        payload = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        return FakeResp(payload)


def test_endpoint_normalization():
    assert aria2.Aria2("http://aria2:6800").endpoint == "http://aria2:6800/jsonrpc"
    assert aria2.Aria2("http://aria2:6800/").endpoint == "http://aria2:6800/jsonrpc"
    assert aria2.Aria2("http://aria2:6800/jsonrpc").endpoint == "http://aria2:6800/jsonrpc"


def test_token_prefix_only_when_secret():
    c = FakeClient([{"result": {"version": "1.37"}}])
    asyncio.run(aria2.Aria2("http://x", "").call(c, "getVersion"))
    assert c.calls[0]["params"] == []

    c2 = FakeClient([{"result": {"version": "1.37"}}])
    asyncio.run(aria2.Aria2("http://x", "s3cret").call(c2, "getVersion"))
    assert c2.calls[0]["params"][0] == "token:s3cret"


def test_rpc_error_raises():
    c = FakeClient([{"error": {"message": "Unauthorized"}}])
    with pytest.raises(aria2.RpcError):
        asyncio.run(aria2.Aria2("http://x").call(c, "getVersion"))


def test_add_magnet_sets_safe_options():
    c = FakeClient([{"result": "gid123"}])
    gid = asyncio.run(aria2.Aria2("http://x").add_magnet(c, "magnet:?xt=urn:btih:a", "/downloads/job"))
    assert gid == "gid123"
    opts = c.calls[0]["params"][1]
    assert opts["dir"] == "/downloads/job"
    assert opts["seed-time"] == "0", "下完不做种"
    assert opts["bt-stop-timeout"] == "180", "无进度要能自己退出"
    assert c.calls[0]["params"][0] == ["magnet:?xt=urn:btih:a"]


def test_wait_returns_on_complete():
    c = FakeClient([
        {"result": {"status": "active", "totalLength": "100", "completedLength": "40", "downloadSpeed": "1000"}},
        {"result": {"status": "complete", "totalLength": "100", "completedLength": "100"}},
    ])
    seen = []
    async def prog(pct, speed, status):
        seen.append(round(pct, 2))
    st = asyncio.run(aria2.Aria2("http://x", poll=0).wait(c, "g", progress=prog))
    assert st["status"] == "complete"
    assert seen and seen[-1] == 1.0


def test_wait_raises_on_error():
    c = FakeClient([{"result": {"status": "error", "errorMessage": "no peers"}}])
    with pytest.raises(RuntimeError, match="no peers"):
        asyncio.run(aria2.Aria2("http://x", poll=0).wait(c, "g"))


def test_wait_timeout():
    c = FakeClient([{"result": {"status": "active", "totalLength": "100", "completedLength": "1"}}])
    with pytest.raises(RuntimeError, match="超时"):
        asyncio.run(aria2.Aria2("http://x", poll=0, timeout=0.01).wait(c, "g"))


def test_wait_follows_metadata_to_real_task():
    """BT 任务在 aria2 里会被拆成「元数据 gid → 真实下载 gid」。

    只盯第一个 gid 会看到它立刻 complete（此时一个字节媒体都还没下），
    上层据此就去整理目录，结果什么都找不到。必须顺着 followedBy 追下去。
    """
    aria = aria2.Aria2("http://x", "s", poll=0, timeout=5)
    order = []

    async def call(client, method, *args):
        assert method == "tellStatus"
        gid = args[0]
        order.append(gid)
        if gid == "meta0":
            return {"status": "complete", "totalLength": "0", "completedLength": "0",
                    "followedBy": ["real1"]}
        return {"status": "complete", "totalLength": "1000", "completedLength": "1000",
                "downloadSpeed": "0",
                "files": [{"path": "/d/a.mp4", "completedLength": "1000",
                           "length": "1000", "selected": "true"}]}

    aria.call = call
    status = asyncio.run(aria.wait(None, "meta0"))
    assert "real1" in order, "必须跟踪 followedBy 指向的真实任务"
    assert status["status"] == "complete"
    assert int(status["completedLength"]) == 1000


def test_wait_rejects_complete_but_short():
    """aria2 说 complete 但字节数不满（文件被删/被换），不能当成功。"""
    aria = aria2.Aria2("http://x", "s", poll=0, timeout=5)

    async def call(client, method, *args):
        return {"status": "complete", "totalLength": "1000", "completedLength": "10",
                "downloadSpeed": "0"}

    aria.call = call
    with pytest.raises(aria2.Incomplete):
        asyncio.run(aria.wait(None, "g1"))


def test_wait_progress_reaches_real_task():
    """进度回调要能反映真实任务的百分比，而不是元数据阶段的 0%。"""
    aria = aria2.Aria2("http://x", "s", poll=0, timeout=5)
    states = []

    async def call(client, method, *args):
        gid = args[0]
        if gid == "meta0":
            return {"status": "complete", "totalLength": "0", "completedLength": "0",
                    "followedBy": ["real1"]}
        return {"status": "complete", "totalLength": "1000", "completedLength": "1000",
                "downloadSpeed": "0"}

    async def progress(percent, speed, status):
        states.append(round(percent, 2))

    aria.call = call
    asyncio.run(aria.wait(None, "meta0", progress=progress))
    assert max(states) == 1.0, f"应看到真实任务 100%，实际 {states}"


def test_abort_pauses_instead_of_deleting_record():
    """失败收尾必须「停下来但保留记录」，删记录会让 aria2 继续往被删的目录里写。"""
    calls = []

    class C:
        async def post(self, url, json=None, timeout=None):
            calls.append((json["method"], json["params"]))
            if json["method"] == "aria2.tellStatus":
                return FakeResp({"result": {"status": "active", "followedBy": ["child1"]}})
            return FakeResp({"result": "OK"})

    aria = aria2.Aria2("http://x", "s")
    asyncio.run(aria.abort(C(), "g1"))

    methods = [m for m, _ in calls]
    assert "aria2.forcePause" in methods
    assert "aria2.forceRemove" not in methods
    assert "aria2.removeDownloadResult" not in methods, "不能删记录"
    paused = {p[-1] for m, p in calls if m == "aria2.forcePause"}
    assert paused == {"g1", "child1"}, f"父任务和 followedBy 子任务都要停：{paused}"


def test_abort_survives_missing_gid():
    class C:
        async def post(self, url, json=None, timeout=None):
            return FakeResp({"error": {"code": 1, "message": "GID g1 is not found"}})

    aria = aria2.Aria2("http://x", "s")
    asyncio.run(aria.abort(C(), "g1"))     # 不应抛异常打断收尾


def test_wait_gives_up_on_stalled_torrent():
    """死种会一直挂着直到总超时（2小时），必须提前放弃。"""
    aria = aria2.Aria2("http://x", "s", poll=0, timeout=10**6, stall_timeout=0)
    # 手工模拟：始终没有新字节，把 stall_timeout 改成 0 秒使其立刻触发
    aria.stall_timeout = 0.001
    calls = {"n": 0}

    async def call(client, method, *args):
        calls["n"] += 1
        # 第一次给一点进展，之后完全不动
        done = "100" if calls["n"] > 1 else "0"
        return {"status": "active", "totalLength": "1000", "completedLength": done,
                "downloadSpeed": "0", "connections": "3"}

    aria.call = call
    with pytest.raises(aria2.Stalled) as exc:
        asyncio.run(aria.wait(None, "g1"))
    assert "没有任何进展" in str(exc.value)


def test_stall_detection_ignores_metadata_phase():
    """元数据/校验阶段没有字节进展是正常的，不能误判为死种。"""
    aria = aria2.Aria2("http://x", "s", poll=0, timeout=3, stall_timeout=0.001)
    calls = {"n": 0}

    async def call(client, method, *args):
        calls["n"] += 1
        if calls["n"] <= 2:
            # 元数据阶段：totalLength=0，不算进展
            return {"status": "active", "totalLength": "0", "completedLength": "0",
                    "downloadSpeed": "0", "connections": "5"}
        return {"status": "complete", "totalLength": "1000", "completedLength": "1000",
                "downloadSpeed": "0"}

    aria.call = call
    status = asyncio.run(aria.wait(None, "g1"))
    assert status["status"] == "complete"


def test_stall_timeout_zero_disables_check():
    """配置成 0 应该关掉检测（留给需要无脑等待的场景）。"""
    aria = aria2.Aria2("http://x", "s", poll=0, timeout=2, stall_timeout=0)
    calls = {"n": 0}

    async def call(client, method, *args):
        calls["n"] += 1
        if calls["n"] > 50:
            return {"status": "complete", "totalLength": "1", "completedLength": "1"}
        return {"status": "active", "totalLength": "1000", "completedLength": "0",
                "downloadSpeed": "0"}

    aria.call = call
    status = asyncio.run(aria.wait(None, "g1"))
    assert status["status"] == "complete"
