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
