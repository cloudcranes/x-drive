"""whatslink 辅助解析：成功、无数据、异常都不该拖垮主流程。"""
import asyncio

import httpx
import pytest

from app import codes, whatslink


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, payload=None, exc=None):
        self.payload = payload
        self.exc = exc
        self.params = None
    async def get(self, url, params=None, headers=None, timeout=None):
        self.params = params
        if self.exc:
            raise self.exc
        return FakeResp(self.payload)


OK = {
    "error": "", "type": "FOLDER", "file_type": "folder",
    "name": "ABC-123 完整版", "size": 129302391, "count": 11,
    "screenshots": [{"time": 0, "screenshot": "https://whatslink.info/image/aaa"}],
}


def test_lookup_ok():
    info = asyncio.run(whatslink.lookup(FakeClient(OK), "magnet:?xt=urn:btih:aa"))
    assert info["name"] == "ABC-123 完整版"
    assert info["size"] == 129302391
    assert info["screenshots"] == ["https://whatslink.info/image/aaa"]


def test_lookup_skips_non_magnet():
    client = FakeClient(OK)
    assert asyncio.run(whatslink.lookup(client, "https://a.com/b.mp4")) is None
    assert client.params is None, "直链不该请求第三方"


def test_lookup_unknown_returns_none():
    payload = {"error": "", "type": "UNKNOWN", "size": 0, "name": ""}
    assert asyncio.run(
        whatslink.lookup(FakeClient(payload), "magnet:?xt=urn:btih:aa", retries=0)
    ) is None


def test_lookup_error_field_returns_none():
    payload = {"error": "magnet invalid", "type": "UNKNOWN", "size": 0}
    assert asyncio.run(
        whatslink.lookup(FakeClient(payload), "magnet:?xt=urn:btih:aa", retries=0)
    ) is None


def test_lookup_network_error_returns_none():
    client = FakeClient(exc=httpx.ConnectError("no net"))
    assert asyncio.run(
        whatslink.lookup(client, "magnet:?xt=urn:btih:aa", retries=0)
    ) is None


def test_guess_code_from_name():
    assert whatslink.guess_code({"name": "[组] SSIS-123 [1080p]"}) == "SSIS-123"
    assert whatslink.guess_code({"name": "video"}) == ""
    assert whatslink.guess_code(None) == ""


def test_describe():
    text = whatslink.describe(OK)
    assert "ABC-123" in text and "11 个文件" in text
    assert whatslink.describe(None) == ""


def test_magnet_query_not_double_escaped():
    """params= 会把 magnet 里的 ?/& 一起转义导致服务端 500，必须保留分隔符。"""
    from urllib.parse import parse_qs, urlsplit

    captured = {}

    class C:
        async def get(self, url, headers=None, timeout=None):
            captured["url"] = url
            return FakeResp(OK)

    magnet = "magnet:?xt=urn:btih:08ada5a7a6183aae1e09d831df6748d566095a10&dn=Sintel&tr=udp%3A%2F%2Fx%3A1"
    asyncio.run(whatslink.lookup(C(), magnet))
    # 服务端拿到的是解码后的值；关键是不能把 ? 和 & 变成 %3F/%26
    raw_query = urlsplit(captured["url"]).query
    assert "%3Fxt%3D" not in raw_query, "? 和 = 不能被转义"
    assert "&dn=Sintel" in raw_query, "& 必须保留为参数分隔"
    assert parse_qs(raw_query)["url"][0].startswith("magnet:?xt=urn:btih:")


def test_lookup_retries_transient_500():
    """服务端偶发 500 时重试应成功。"""
    class Flaky:
        def __init__(self):
            self.n = 0
        async def get(self, url, headers=None, timeout=None):
            self.n += 1
            if self.n < 3:
                return FakeResp({}, status=500)
            return FakeResp(OK)

    # 把退避缩短，测试别真等 4.5 秒
    original = whatslink.asyncio.sleep
    whatslink.asyncio.sleep = lambda *_: original(0)
    try:
        client = Flaky()
        info = asyncio.run(whatslink.lookup(client, "magnet:?xt=urn:btih:aa", retries=2))
    finally:
        whatslink.asyncio.sleep = original
    assert info and info["name"] == "ABC-123 完整版"
    assert client.n == 3
