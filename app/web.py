"""展示页 + 实时看板（瀑布流卡片）：读 /data/state.json，单页展示已转存记录与健康状态。

零第三方依赖（标准库 http.server），与 bot 共用同一镜像，command 覆盖入口。
环境变量：WEB_PORT(8080)、WEB_BIND(0.0.0.0)、WEB_AUTH(用户名:密码，可选，
设置后浏览器会弹原生登录框，401 时返回登录页)、STATE_PATH、PARSE_BASE_URL。
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import re
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from . import core

log = logging.getLogger("x-drive.web")

STATE = os.getenv("STATE_PATH", "/data/state.json")
PARSE = os.getenv("PARSE_BASE_URL", "http://parse:8000").rstrip("/")
BIND = os.getenv("WEB_BIND", "0.0.0.0")
PORT = int(os.getenv("WEB_PORT", "8080"))
AUTH = os.getenv("WEB_AUTH", "").strip()
HTML_PATH = Path(__file__).with_name("webui.html")
LOGIN_PATH = Path(__file__).with_name("login.html")

STATUS_RE = re.compile(
    r"^https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/([^/]+)/status(?:es)?/(\d+)"
)
THUMB_MAX = 6 * 1024 * 1024

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


def tweet_info(source: str, tweet_id: str) -> tuple[str, str]:
    """从原始链接解析 (作者账号, 推文地址)；解析不出来就用 id 兜底。"""
    match = STATUS_RE.match(source or "")
    if match:
        handle, tid = match.group(1), match.group(2)
        return handle, f"https://x.com/{handle}/status/{tid}"
    if tweet_id:
        return "", f"https://x.com/i/status/{tweet_id}"
    return "", (source or "")


def thumb_url(kind: str, original: str, tweet_id: str, name: str) -> str:
    """同源缩略图地址，避免浏览器直连 pbs.twimg.com。"""
    query = "&".join(
        f"{key}={quote(str(value), safe='')}"
        for key, value in (("u", original), ("k", kind), ("t", tweet_id), ("f", name))
    )
    return f"/thumb?{query}"


def collect() -> dict:
    state = core.load_state(STATE)
    tasks = []
    for key, item in state.items():
        files = item.get("files") or []
        images, videos = core.split_counts(files)
        source = item.get("source") or ""
        handle, url = tweet_info(source, key)
        media = []
        for name in sorted(files):
            ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
            kind = "image" if ext in core.IMAGE_EXTS else ("video" if ext in core.VIDEO_EXTS else None)
            if not kind:
                continue
            original = (item.get("orig") or {}).get(name) or ""
            media.append(
                {
                    "kind": kind,
                    "name": name,
                    "thumb": thumb_url(kind, original, key, name) if original else "",
                    "raw": original,
                }
            )
        tasks.append(
            {
                "id": key,
                "title": item.get("title") or "",
                "author": item.get("author") or "",
                "handle": handle,
                "url": url,
                "link": item.get("link") or "",
                "path": item.get("path") or "",
                "at": item.get("at") or "",
                "files": files,
                "images": images,
                "videos": videos,
                "media": media,
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

    def _login(self) -> None:
        try:
            body = LOGIN_PATH.read_bytes()
        except OSError:
            body = "<h1>需要登录</h1>".encode()
        self._send(
            401,
            body,
            "text/html; charset=utf-8",
            {"WWW-Authenticate": 'Basic realm="x-drive", charset="UTF-8"'},
        )

    def do_GET(self) -> None:  # noqa: N802 (标准库命名)
        # 健康检查不要求认证，否则容器 healthcheck 永远 401
        if urlsplit(self.path).path == "/api/health":
            self._json({"ok": True, "parse": parse_online()})
            return
        if not self._authorized():
            self._login()
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
        if path == "/thumb":
            self._thumb(parse_qs(parts.query))
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def _thumb(self, query: dict) -> None:
        """同源代理缩略图：只放行 twimg 域名，避免开放代理被滥用。"""
        url = (query.get("u") or [""])[0]
        host = (urlsplit(url).hostname or "").lower()
        if not url or not (host == "twimg.com" or host.endswith(".twimg.com")):
            self._send(400, b"bad thumb url", "text/plain; charset=utf-8")
            return
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
                    ),
                    "Referer": "https://x.com/",
                    "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*;q=0.8",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read(THUMB_MAX)
                ctype = resp.headers.get("Content-Type") or "image/jpeg"
            self._send(200, data, ctype, {"Cache-Control": "public, max-age=86400"})
        except Exception as exc:  # 单张缩略图失败不该拖垮页面
            self._send(502, f"thumb failed: {exc}".encode(), "text/plain; charset=utf-8")

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
