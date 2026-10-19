"""展示页 + 实时看板：读 /data/state.json，单页展示已转存记录与健康状态。

零第三方依赖（标准库 http.server），与 bot 共用同一镜像，command 覆盖入口。
环境变量：WEB_PORT(8080)、WEB_BIND(0.0.0.0)、WEB_AUTH(user:pass，可选)、
STATE_PATH、PARSE_BASE_URL。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import core

log = logging.getLogger("x-drive.web")

STATE = os.getenv("STATE_PATH", "/data/state.json")
PARSE = os.getenv("PARSE_BASE_URL", "http://parse:8000").rstrip("/")
BIND = os.getenv("WEB_BIND", "0.0.0.0")
PORT = int(os.getenv("WEB_PORT", "8080"))
AUTH = os.getenv("WEB_AUTH", "").strip()
HTML_PATH = Path(__file__).with_name("webui.html")

_health = {"at": 0.0, "parse": False}


def parse_online(ttl: float = 15.0) -> bool:
    if time.time() - _health["at"] < ttl:
        return bool(_health["parse"])
    ok = False
    try:
        with urllib.request.urlopen(f"{PARSE}/", timeout=3) as resp:
            ok = resp.status == 200
    except Exception:  # 展示页不该因探测失败而报错
        ok = False
    _health.update(at=time.time(), parse=ok)
    return ok


def collect() -> dict:
    state = core.load_state(STATE)
    tasks = []
    for key, item in state.items():
        files = item.get("files") or []
        images, videos = core.split_counts(files)
        tasks.append(
            {
                "id": key,
                "title": item.get("title") or "",
                "author": item.get("author") or "",
                "source": item.get("source") or "",
                "link": item.get("link") or "",
                "path": item.get("path") or "",
                "at": item.get("at") or "",
                "files": files,
                "images": images,
                "videos": videos,
            }
        )
    tasks.sort(key=lambda t: t["at"] or "", reverse=True)
    return {
        "tasks": tasks,
        "stats": {
            "tasks": len(tasks),
            "images": sum(t["images"] for t in tasks),
            "videos": sum(t["videos"] for t in tasks),
            "last": tasks[0]["at"] if tasks else "",
        },
        "health": {"parse": parse_online()},
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "x-drive-web"
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not AUTH:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            given = base64.b64decode(header[6:]).decode()
        except Exception:
            return False
        return given == AUTH

    def do_GET(self) -> None:  # noqa: N802 (标准库命名)
        if not self._authorized():
            self._send(401, b"auth required", "text/plain; charset=utf-8",
                       {"WWW-Authenticate": 'Basic realm="x-drive"'})
            return
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            try:
                body = HTML_PATH.read_bytes()
            except OSError as exc:
                self._send(500, f"webui.html 读取失败: {exc}".encode(), "text/plain; charset=utf-8")
                return
            self._send(200, body, "text/html; charset=utf-8")
            return
        if path == "/api/tasks":
            self._json(collect())
            return
        if path == "/api/health":
            self._json({"ok": True, "parse": parse_online()})
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def _json(self, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self._send(200, body, "application/json; charset=utf-8")

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s %s", self.address_string(), fmt % args)


def serve(host: str = BIND, port: int = PORT) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), Handler)
    log.info("展示页 http://%s:%s  state=%s auth=%s", host, httpd.server_address[1], STATE, bool(AUTH))
    return httpd


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    httpd = serve()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
