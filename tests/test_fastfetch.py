"""分块并发下载：拼出来的字节必须与源文件**逐字节一致**。

拼错一个字节 → faststart 重排出的是坏文件 → 传回云盘 → 归档报废。
所以这里要比对完整内容，不只是长度。
"""
from __future__ import annotations

import hashlib
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app import fastfetch


class _Handler(BaseHTTPRequestHandler):
    payload = b""
    seen_ranges: list[str] = []
    lock = threading.Lock()

    def log_message(self, *a):        # 静音
        pass

    def do_GET(self):
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            start, end = rng[6:].split("-")
            start, end = int(start), int(end)
            chunk = self.payload[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(self.payload)}")
            self.send_header("Content-Length", str(len(chunk)))
            self.end_headers()
            self.wfile.write(chunk)
            with self.lock:
                self.seen_ranges.append(rng)
        else:
            self.send_response(200)
            self.send_header("Content-Length", str(len(self.payload)))
            self.end_headers()
            self.wfile.write(self.payload)


@pytest.fixture
def server():
    _Handler.payload = bytes(range(256)) * 40000        # 10.24MB
    _Handler.seen_ranges = []
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/a.bin"
    httpd.shutdown()


def test_parallel_fetch_reassembles_exact_bytes(server, tmp_path):
    """分块拉回来的文件必须与源**逐字节一致**。"""
    dest = tmp_path / "out.bin"
    got = fastfetch.fetch(server, dest, concurrency=4, chunk=1024 * 1024)
    assert got == len(_Handler.payload)
    assert hashlib.md5(dest.read_bytes()).hexdigest() == \
        hashlib.md5(_Handler.payload).hexdigest(), "拼出来的字节不对"
    assert len(_Handler.seen_ranges) >= 2, "应该真的分了多段"


def test_parallel_fetch_uses_multiple_ranges(server, tmp_path):
    """要真的用 Range 分段（而不是整片拉一次）。"""
    dest = tmp_path / "out.bin"
    fastfetch.fetch(server, dest, concurrency=4, chunk=1024 * 1024)
    assert len(_Handler.seen_ranges) >= 4, _Handler.seen_ranges


def test_fetch_falls_back_when_no_range_support(tmp_path):
    """服务端不支持 Range → 退回单连接整片拉，结果仍要正确。"""
    class _Plain(BaseHTTPRequestHandler):
        payload = b"x" * 50000

        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(self.payload)))
            self.end_headers()
            self.wfile.write(self.payload)

    httpd = HTTPServer(("127.0.0.1", 0), _Plain)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/b.bin"
    dest = tmp_path / "out.bin"
    got = fastfetch.fetch(url, dest, concurrency=4, chunk=1024 * 1024)
    assert got == 50000
    assert dest.read_bytes() == b"x" * 50000
    httpd.shutdown()


def test_fetch_raises_when_a_chunk_exhausts_retries(tmp_path):
    """某段彻底失败 → 整个下载失败（**不**把残缺文件当成品传回去）。

    探测阶段成功（206 + Content-Range），但**中途**某段开始一直 503 ——
    那正是「并发调太高被 CDN 限流」的样子。
    """
    state = {"probe_ok": True, "served": 0}

    class _Bad(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            rng = self.headers.get("Range", "")
            if rng == "bytes=0-1":
                # 探测：正常回答
                self.send_response(206)
                self.send_header("Content-Range", "bytes 0-1/100000")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ab")
                return
            # 正式下载：全部 503
            state["served"] += 1
            self.send_response(503)
            self.end_headers()

    httpd = HTTPServer(("127.0.0.1", 0), _Bad)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/c.bin"
    dest = tmp_path / "out.bin"
    with pytest.raises(fastfetch.FetchError):
        fastfetch.fetch(url, dest, concurrency=2, chunk=1024 * 1024, retries=2)
    assert state["served"] >= 2, "应该重试过"
    httpd.shutdown()
