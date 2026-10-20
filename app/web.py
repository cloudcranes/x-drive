"""展示页 + 实时看板（瀑布流卡片）：读 /data/state.json，单页展示已转存记录与健康状态。

零第三方依赖（标准库 http.server），与 bot 共用同一镜像，command 覆盖入口。
环境变量：WEB_PORT(8080)、WEB_BIND(0.0.0.0)、WEB_AUTH(用户名:密码，可选，
设置后浏览器会弹原生登录框，401 时返回登录页)、STATE_PATH、PARSE_BASE_URL。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import core

log = logging.getLogger("x-drive.web")

STATE = os.getenv("STATE_PATH", "/data/state.json")
METRICS = os.getenv("METRICS_PATH", "/data/metrics.json")
PENDING_DIR = os.getenv("PENDING_DIR", "/data/pending")
BIND = os.getenv("WEB_BIND", "0.0.0.0")
PORT = int(os.getenv("WEB_PORT", "8080"))
AUTH = os.getenv("WEB_AUTH", "").strip()
HTML_PATH = Path(__file__).with_name("webui.html")
LOGIN_PATH = Path(__file__).with_name("login.html")

THUMB_MAX = 6 * 1024 * 1024

SESSION_COOKIE = "xd_session"
SESSION_TTL = 7 * 24 * 3600


def make_session(ttl: int = SESSION_TTL) -> str:
    """登录成功后下发的签名串：过期时间 + HMAC(密钥=WEB_AUTH)。"""
    exp = int(time.time()) + ttl
    mac = hmac.new(AUTH.encode(), str(exp).encode(), hashlib.sha256).hexdigest()[:32]
    return f"{exp}.{mac}"


def session_ok(value: str) -> bool:
    if not AUTH or not value:
        return False
    exp, _, mac = value.partition(".")
    if not exp.isdigit() or int(exp) < time.time():
        return False
    expect = hmac.new(AUTH.encode(), exp.encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(mac, expect)


def check_credentials(given: str) -> bool:
    user, _, password = given.partition(":")
    want_user, _, want_password = AUTH.partition(":")
    return hmac.compare_digest(user, want_user) and hmac.compare_digest(password, want_password)


def pending_count() -> int:
    try:
        return len([p for p in Path(PENDING_DIR).glob("*.json")])
    except OSError:
        return 0


def state_readable() -> bool:
    try:
        json.loads(Path(STATE).read_text(encoding="utf-8"))
        return True
    except FileNotFoundError:
        return True          # 还没有任何归档，属于正常
    except (OSError, json.JSONDecodeError):
        return False


def health() -> dict:
    """容器 healthcheck：state 可解析 + 待办数量 + 归档计数（不含具体内容）。"""
    ok_state = state_readable()
    counts = {"tasks": 0, "files": 0}
    try:
        state = core.load_state(STATE)
        counts["tasks"] = len(state)
        counts["files"] = sum(len(v.get("files") or []) for v in state.values())
    except Exception:
        pass
    return {
        "ok": ok_state,
        "state_readable": ok_state,
        "pending": pending_count(),
        **counts,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def metrics_snapshot() -> dict:
    from . import metrics as m

    data = m.snapshot(METRICS)
    data["pending"] = pending_count()
    return data


def collect() -> dict:
    """把 state 摊成看板要的列表：每条 = 一个番号。"""
    state = core.load_state(STATE)
    items = []
    for key, item in state.items():
        files = item.get("files") or []
        size = int(item.get("size") or 0)
        items.append(
            {
                "id": key,
                "code": item.get("code") or key,
                "link": item.get("link") or "",
                "path": item.get("path") or "",
                "at": item.get("at") or "",
                "files": files,
                "count": len(files),
                "size": size,
                "size_text": core.human_size(size),
                "source": item.get("source") or "",
            }
        )
    items.sort(key=lambda t: t["at"] or "", reverse=True)
    total = sum(i["size"] for i in items)
    return {
        "tasks": items,
        "stats": {
            "tasks": len(items),
            "files": sum(i["count"] for i in items),
            "size": total,
            "size_text": core.human_size(total) if total else "0MB",
            "last": items[0]["at"] if items else "",
        },
        "health": {"parse": True},
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "x-drive-web"
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        extra = dict(extra or {})
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # 允许调用方覆盖，避免出现两个 Content-Length / Cache-Control
        self.send_header("Cache-Control", extra.pop("Cache-Control", "no-store"))
        for key, value in extra.items():
            if key.lower() == "content-length":
                continue
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not AUTH:
            return True
        # cookie 会话优先（登录页 POST /api/login 后下发），失败再退回 Basic
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE and session_ok(value.strip()):
                return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            given = base64.b64decode(header[6:]).decode("utf-8", "replace")
        except Exception:
            return False
        return check_credentials(given)

    def _login(self, page: bool = True) -> None:
        """未登录时：页面给自定义登录页（200，避免浏览器弹原生框），接口给 401 JSON。"""
        if page:
            try:
                body = LOGIN_PATH.read_bytes()
            except OSError:
                body = "<h1>需要登录</h1>".encode()
            self._send(200, body, "text/html; charset=utf-8")
            return
        self._json({"ok": False, "error": "unauthorized", "login": "/"}, 401)

    def do_POST(self) -> None:  # noqa: N802 (标准库命名)
        path = urlsplit(self.path).path
        if path == "/api/login":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(min(length, 4096)) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._json({"ok": False, "error": "bad request"}, 400)
                return
            creds = f"{payload.get('user','')}:{payload.get('password','')}"
            if not AUTH or check_credentials(creds):
                cookie = (
                    f"{SESSION_COOKIE}={make_session()}; Path=/; Max-Age={SESSION_TTL}; "
                    "HttpOnly; SameSite=Lax"
                )
                self._json({"ok": True}, extra={"Set-Cookie": cookie})
            else:
                self._json({"ok": False, "error": "账号或密码不正确"}, 401)
            return
        if path == "/api/logout":
            self._json(
                {"ok": True},
                extra={"Set-Cookie": f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"},
            )
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_HEAD(self) -> None:  # noqa: N802 (标准库命名)
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802 (标准库命名)
        # 健康检查不要求认证，否则容器 healthcheck 永远 401
        if urlsplit(self.path).path == "/api/health":
            self._json(health())
            return
        if not self._authorized():
            path = urlsplit(self.path).path
            self._login(page=path in ("/", "/index.html"))
            return
        parts = urlsplit(self.path)
        path = parts.path
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
        if path == "/api/session":
            # 已通过认证才会到这里；auth 表示是否启用了登录
            self._json({"ok": True, "auth": bool(AUTH), "user": AUTH.partition(":")[0] if AUTH else ""})
            return
        if path == "/api/metrics":
            self._json(metrics_snapshot())
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def _json(self, payload: dict, code: int = 200, extra: dict | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self._send(code, body, "application/json; charset=utf-8", extra)

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
