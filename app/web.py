"""展示页 + 实时看板：读 /data/state.json，单页展示已转存记录与健康状态。

零第三方依赖（标准库 http.server），与 bot 共用同一镜像，command 覆盖入口。
环境变量：WEB_PORT(8080)、WEB_BIND(0.0.0.0)、WEB_AUTH(用户名:密码，可选，
设置后浏览器会弹原生登录框，401 时提示「需要登录」)，
STATE_PATH、PARSE_BASE_URL。
"""
from __future__ import annotations

import base64
import hmac
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

LOGIN_PAGE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>x-drive · 需要登录</title>
<style>
 body{margin:0;min-height:100vh;display:grid;place-items:center;color:#e8edf4;
   background:#0a0d12 radial-gradient(700px 420px at 50% 0%,rgba(61,220,151,.14),transparent 60%);
   font:15px/1.6 system-ui,-apple-system,"Noto Sans SC",sans-serif}
 .card{width:min(340px,88vw);text-align:center;padding:34px 26px;border:1px solid rgba(255,255,255,.09);
   border-radius:16px;background:rgba(255,255,255,.045)}
 h1{margin:0 0 6px;font-size:24px;letter-spacing:-.02em}
 h1 span{color:#3ddc97}
 p{margin:0 0 20px;color:#8b96a8;font-size:13.5px}
 a{display:inline-block;padding:9px 18px;border-radius:9px;text-decoration:none;
   color:#0a0d12;background:#3ddc97;font-weight:600;font-size:13.5px}
 code{background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.09);
   border-radius:6px;padding:2px 6px;font-size:12.5px;color:#8b96a8}
</style>
<div class="card">
  <h1>x-<span>drive</span></h1>
  <p>此页面需要登录。弹出登录框后输入账号密码，或点下方按钮重试。</p>
  <a href="/">重新登录</a>
  <p style="margin:16px 0 0"><code id="hint"></code></p>
</div>
<script>
  // 浏览器通常会自动弹原生登录框；未弹出时提示用户手动触发
  setTimeout(() => {
    fetch(location.href, {cache: "no-store"}).catch(() => {});
    document.getElementById("hint").textContent =
      "若未弹出登录框，请刷新页面";
  }, 300);
</script>
</html>"""

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
            given = base64.b64decode(header[6:]).decode("utf-8", "replace")
        except Exception:
            return False
        user, _, password = given.partition(":")
        want_user, _, want_password = AUTH.partition(":")
        return hmac.compare_digest(user, want_user) and hmac.compare_digest(
            password, want_password
        )

    def do_GET(self) -> None:  # noqa: N802 (标准库命名)
        if not self._authorized():
            body = LOGIN_PAGE.encode()
            self._send(401, body, "text/html; charset=utf-8",
                       {"WWW-Authenticate": 'Basic realm="x-drive", charset="UTF-8"'})
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
