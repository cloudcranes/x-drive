"""视频代理：转发云盘直链，必须支持 Range（<video> 靠它做拖动定位）。

真的起一个本地 HTTP 服务当上游，检查三件事：
  · 整片请求回 200 + 完整 Content-Length
  · Range 请求回 206 + Content-Range
  · 白名单外的地址一律 400（不做任意 URL 转发）
"""
from __future__ import annotations

import http.server
import threading

import pytest

from app import web

BODY = b"x" * (256 * 1024)


class _Upstream(http.server.BaseHTTPRequestHandler):
    """假云盘直链服务：认识 Range。"""

    def log_message(self, *a):
        pass

    def do_GET(self):
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            start = int(rng.split("=", 1)[1].split("-", 1)[0])
            end = min(start + 1024, len(BODY) - 1)
            data = BODY[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(BODY)}")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", "video/mp4")
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(BODY)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", "video/mp4")
        self.end_headers()
        self.wfile.write(BODY)


@pytest.fixture()
def upstream():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/a.mp4"
    finally:
        srv.shutdown()
        srv.server_close()


def test_video_proxy_allows_loopback(upstream):
    """127.0.0.1 在白名单里（测试用），生产是 host.docker.internal。"""
    assert "127.0.0.1" in web.VIDEO_HOSTS


def test_video_proxy_host_check_is_exact():
    """白名单是**精确匹配**，不是子串 —— 防止 evil-127.0.0.1.test 这类绕过。"""
    from urllib.parse import urlsplit
    for bad in ("http://evil-127.0.0.1.test/a.mp4",
                "http://127.0.0.1.evil.test/a.mp4",
                "http://example.com/a.mp4"):
        host = (urlsplit(bad).hostname or "").lower()
        assert host not in web.VIDEO_HOSTS, bad


def test_video_proxy_forwards_range_header(upstream, monkeypatch):
    """代理把浏览器的 Range 头原样转给上游 —— 不支持就播不了、拖不动。"""
    seen = {}

    class FakeResp:
        status = 206
        headers = {"Content-Type": "video/mp4", "Content-Range": "bytes 0-1023/262144",
                   "Content-Length": "1024", "Accept-Ranges": "bytes"}

        def read(self, n=-1):
            data = seen.setdefault("data", b"")
            if not data:
                data = b"y" * 1024
                seen["data"] = data
            out, data = data, b""
            return out

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen["range"] = req.headers.get("Range")
        return FakeResp()

    monkeypatch.setattr(web.urllib.request, "urlopen", fake_urlopen)

    # 直接调处理器：它需要 self.headers / self.wfile，用最小的假对象
    sent = {}
    handler = web.Handler.__new__(web.Handler)
    handler.headers = {"Range": "bytes=0-1023"}
    handler.wfile = type("W", (), {"write": lambda self, d: sent.setdefault("body", b"") or sent["body"].__iadd__(d)})()
    handler.send_response = lambda code: sent.setdefault("status", code)
    handler.send_header = lambda k, v: sent.setdefault("headers", {}).__setitem__(k, v)
    handler.end_headers = lambda: None

    handler._proxy_video({"u": [upstream]})

    assert seen.get("range") == "bytes=0-1023", "Range 头要原样转给上游"
    assert sent.get("status") == 206
    assert sent.get("headers", {}).get("Content-Range") == "bytes 0-1023/262144"


def test_video_proxy_rejects_non_whitelisted_url(monkeypatch):
    """白名单外的地址直接 400 —— 代理永远不做任意 URL 转发。"""
    called = []
    monkeypatch.setattr(web.urllib.request, "urlopen",
                        lambda *a, **k: called.append(1) or (_ for _ in ()).throw(AssertionError))

    sent = {}
    handler = web.Handler.__new__(web.Handler)
    handler.headers = {}
    handler.wfile = type("W", (), {"write": lambda self, d: None})()
    handler.send_response = lambda code: sent.setdefault("status", code)
    handler.send_header = lambda k, v: None
    handler.end_headers = lambda: None
    handler._send = lambda code, body, ctype, extra=None: sent.update(status=code)

    handler._proxy_video({"u": ["http://evil.test/a.mp4"]})

    assert sent.get("status") == 400
    assert called == [], "白名单外的地址不许发请求"
