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
import asyncio
import shutil
import subprocess
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from . import aria2, codes, core, fetcher, pipeline

log = logging.getLogger("x-drive.web")

STATE = os.getenv("STATE_PATH", "/data/state.json")
METRICS = os.getenv("METRICS_PATH", "/data/metrics.json")
PENDING_DIR = os.getenv("PENDING_DIR", "/data/pending")
ARIA2_RPC = os.getenv("ARIA2_RPC", "http://aria2:6800").strip()
RCLONE_REMOTE = os.getenv("RCLONE_REMOTE", "gdrive").strip()
DRIVE_ROOT = os.getenv("DRIVE_ROOT", "X-Drive").strip().strip("/")
RCLONE_CONFIG = os.getenv("RCLONE_CONFIG", "/config/rclone/rclone.conf")
DAILY_UPLOAD_GB = int(os.getenv("DAILY_UPLOAD_GB", "0") or 0)
ARIA2_SECRET = os.getenv("ARIA2_RPC_SECRET", "").strip()
TMP_ROOT = os.getenv("TMP_ROOT", "/downloads")

BIND = os.getenv("WEB_BIND", "0.0.0.0")
PORT = int(os.getenv("WEB_PORT", "8080"))
AUTH = os.getenv("WEB_AUTH", "").strip()
HTML_PATH = Path(__file__).with_name("webui.html")
LOGIN_PATH = Path(__file__).with_name("login.html")


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


# ---------------------------------------------------------------- 下载任务

JOBS: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def job_update(job_id: str, **fields) -> None:
    with _jobs_lock:
        record = JOBS.setdefault(job_id, {"id": job_id, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        record.update(fields)


def jobs_snapshot() -> list[dict]:
    with _jobs_lock:
        items = [dict(j) for j in JOBS.values()]
    return sorted(items, key=lambda j: j["at"], reverse=True)[:20]


def active_codes() -> set[str]:
    """正在跑的番号（排队/下载/整理/上传），用于提交前去重。"""
    with _jobs_lock:
        return {
            (j.get("code") or "").upper()
            for j in JOBS.values()
            if j.get("stage") in ("queued", "downloading", "organizing", "uploading") and j.get("code")
        }


def archived_codes() -> set[str]:
    try:
        return {str(k).upper() for k in core.load_state(STATE)}
    except Exception:
        return set()


CANCEL: dict[str, threading.Event] = {}


def request_cancel(job_id: str) -> bool:
    """标记任务取消；下载线程在下一个进度回调里抛异常收尾。"""
    with _jobs_lock:
        if job_id not in JOBS:
            return False
    CANCEL.setdefault(job_id, threading.Event()).set()
    return True


def cancel_requested(job_id: str) -> bool:
    event = CANCEL.get(job_id)
    return bool(event and event.is_set())


class Cancelled(RuntimeError):
    """用户主动停止任务。"""


def rclone(*args: str, timeout: float = 7200.0) -> tuple[int, str, str]:
    cmd = ["rclone", "--config", RCLONE_CONFIG, *args]
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", "rclone 超时"
    return done.returncode, done.stdout.decode(errors="replace"), done.stderr.decode(errors="replace")


def upload_folder(work: Path, folder: str) -> tuple[str | None, int]:
    """把目录推到 Drive/<番号>，返回 (分享链接, 字节数)。"""
    target = f"{RCLONE_REMOTE}:{DRIVE_ROOT}/{folder}"
    rclone("mkdir", target, timeout=120.0)
    rc, _, err = rclone(
        "copy", str(work), target,
        "--transfers", "2", "--drive-chunk-size", "64M",
        "--retries", "2", "--timeout", "30m",
        "--drive-stop-on-upload-limit",
    )
    if rc != 0:
        raise RuntimeError(f"rclone 上传失败：{err.strip()[:300]}")
    rc, out, _ = rclone("link", target, timeout=120.0)
    return (out.strip() if rc == 0 and out.strip().startswith("http") else None), 0


def run_web_job(job_id: str, source: str, code: str) -> None:
    """在独立线程里跑完整流程（下载走 aria2 RPC，上传走 rclone）。"""
    work = pipeline.job_dir(TMP_ROOT, code or job_id)

    async def main() -> None:
        aria = aria2.Aria2(ARIA2_RPC, ARIA2_SECRET, poll=3.0)
        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
            pipeline.cleanup(work)
            work.mkdir(parents=True, exist_ok=True)
            job_update(job_id, stage="downloading", percent=0.0, speed=0)

            async def on_progress(percent, speed, status):
                if cancel_requested(job_id):
                    raise Cancelled("已停止")
                job_update(job_id, percent=round(percent * 100, 1), speed=speed or 0)

            await pipeline.download(aria, client, source, work, progress=on_progress)

    try:
        asyncio.run(main())
        if cancel_requested(job_id):
            raise Cancelled("已停止")
        job_update(job_id, stage="organizing")
        names = " ".join(p.name for p in work.glob("*"))
        folder, files = pipeline.organize(work, code or codes.detect(names) or "")
        size = pipeline.total_size(files)

        if DAILY_UPLOAD_GB and size > DAILY_UPLOAD_GB * 1024**3:
            raise RuntimeError(f"单任务 {core.human_size(size)} 超过 DAILY_UPLOAD_GB 限制")

        job_update(job_id, stage="uploading", folder=folder, size=size, percent=100.0)
        link, _ = upload_folder(work, folder)

        core.update_state(STATE, {
            folder: {
                "code": folder,
                "path": f"{RCLONE_REMOTE}:{DRIVE_ROOT}/{folder}",
                "link": link or "",
                "files": [f.name for f in files],
                "size": size,
                "source": source[:300],
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        })
        job_update(job_id, stage="done", link=link or "", code=folder, size=size)
    except Cancelled:
        log.info("任务已停止：%s", job_id)
        job_update(job_id, stage="failed", error="已停止")
    except Exception as exc:
        log.exception("网页任务失败")
        job_update(job_id, stage="failed", error=str(exc)[:300])
    finally:
        with _jobs_lock:
            CANCEL.pop(job_id, None)
        pipeline.cleanup(work)


def peek(magnet: str) -> dict | None:
    """同步查磁力元信息（网页预览用）。"""
    from . import whatslink

    async def main():
        async with httpx.AsyncClient(follow_redirects=True) as client:
            return await whatslink.lookup(client, magnet)

    try:
        info = asyncio.run(main())
    except Exception as exc:
        log.warning("whatslink 预览失败：%s", exc)
        return None
    return info


def start_job(source: str, code: str, info: dict | None = None) -> str:
    job_id = f"j{int(time.time() * 1000) % 10**10}"
    job_update(job_id, source=source[:300], code=code, stage="queued", percent=0.0, speed=0)
    if info:
        job_update(
            job_id, name=info.get("name") or "", total=info.get("size") or 0,
            screenshots=info.get("screenshots") or [],
        )
    threading.Thread(target=run_web_job, args=(job_id, source, code), daemon=True).start()
    return job_id


def aria_tasks() -> dict:
    """实时队列（来自 aria2），供页面展示正在下载的内容。"""
    async def main() -> dict:
        aria = aria2.Aria2(ARIA2_RPC, ARIA2_SECRET)
        async with httpx.AsyncClient() as client:
            return await aria.list_tasks(client)

    try:
        return asyncio.run(main())
    except Exception as exc:
        return {"error": str(exc)[:200], "active": [], "waiting": [], "stopped": [], "stat": {}}


def series_of(code: str) -> str:
    """系列名 = 番号去掉最后一段数字（SSIS-123 → SSIS，FC2-PPV-4422110 → FC2-PPV）。"""
    parts = [p for p in (code or "").split("-") if p]
    if len(parts) >= 2 and parts[-1].isdigit():
        return "-".join(parts[:-1])
    return parts[0] if parts else "其他"


def kind_of(files: list[str]) -> str:
    """卡片/筛选用的一句话文件构成：视频 2 · 图片 1 / 视频 / 图片 3。"""
    videos = sum(1 for f in files if Path(f).suffix.lower() in fetcher.VIDEO_EXT)
    images = len(files) - videos
    if videos and images:
        return f"视频 {videos} · 图片 {images}"
    if videos:
        return "视频" if videos == 1 else f"视频 {videos}"
    if images:
        return "图片" if images == 1 else f"图片 {images}"
    return "空目录"


def collect() -> dict:
    """把 state 摊成看板要的列表：每条 = 一个番号。"""
    state = core.load_state(STATE)
    items = []
    for key, item in state.items():
        files = item.get("files") or []
        size = int(item.get("size") or 0)
        code = item.get("code") or key
        at = item.get("at") or ""
        kind = kind_of(files)
        items.append(
            {
                "id": key,
                "code": code,
                "series": series_of(code),
                "link": item.get("link") or "",
                "path": item.get("path") or "",
                "at": at,
                "month": at[:7],
                "files": files,
                "count": len(files),
                "size": size,
                "size_text": core.human_size(size),
                "kind": kind,
                "cat": "video" if kind.startswith("视频") else "image",
                "source": item.get("source") or "",
                "is_magnet": (item.get("source") or "").lower().startswith("magnet:"),
            }
        )
    items.sort(key=lambda t: t["at"] or "", reverse=True)
    total = sum(i["size"] for i in items)
    videos = sum(i["size"] for i in items if i["cat"] == "video")
    return {
        "tasks": items,
        "stats": {
            "tasks": len(items),
            "files": sum(i["count"] for i in items),
            "size": total,
            "size_text": core.human_size(total),
            "size_video_text": core.human_size(videos),
            "series": len({i["series"] for i in items}),
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

    def _drain(self) -> None:
        """读掉请求体：鉴权失败直接返回时不读，残留字节会被当成下一个请求解析，日志里刷 400。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 0:
                self.rfile.read(min(length, 1 << 16))
        except (ValueError, OSError):
            pass

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
        if path == "/api/download":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(min(length, 8192)) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._json({"ok": False, "error": "bad request"}, 400)
                return
            text = (payload.get("text") or "").strip()
            sources = core.extract_urls(text)
            if not sources:
                self._json({"ok": False, "error": "需要一个磁力或直链"}, 400)
                return
            code = codes.detect(text) or ""
            # 先去重：命中的话不必再花几秒查 whatslink
            dedupe = payload.get("dedupe", True)
            if code and dedupe:
                key = codes.safe_folder(code).upper()
                if key in active_codes():
                    self._json({"ok": False, "code": code, "error": f"{code} 正在下载，等它跑完再提交", "duplicate": "active"}, 409)
                    return
                if key in archived_codes():
                    self._json({"ok": False, "code": code, "error": f"{code} 已经归档过了，勾掉「去重」可强制重下", "duplicate": "archived"}, 409)
                    return
            preview = None
            for src in sources:
                if src.lower().startswith("magnet:"):
                    preview = peek(src)
                    if preview and not code:
                        guessed = codes.detect(preview.get("name") or "")
                        if guessed:
                            code = guessed
                    break
            if code and dedupe:
                # 番号是查完磁力才认出来的，再挡一次
                key = codes.safe_folder(code).upper()
                if key in active_codes():
                    self._json({"ok": False, "code": code, "error": f"{code} 正在下载，等它跑完再提交", "duplicate": "active"}, 409)
                    return
                if key in archived_codes():
                    self._json({"ok": False, "code": code, "error": f"{code} 已经归档过了，勾掉「去重」可强制重下", "duplicate": "archived"}, 409)
                    return
            ids = [start_job(src, code, preview if len(sources) == 1 else None) for src in sources]
            self._json({
                "ok": True, "jobs": ids, "code": code,
                "count": len(ids), "preview": preview,
            })
            return
        if path == "/api/job/stop":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                job_id = (json.loads(self.rfile.read(min(length, 4096)) or b"{}") or {}).get("id", "")
            except (ValueError, json.JSONDecodeError):
                job_id = ""
            if not job_id or not request_cancel(job_id):
                self._json({"ok": False, "error": "任务不存在"}, 404)
                return
            self._json({"ok": True})
            return
        if path == "/api/jobs/clear":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            with _jobs_lock:
                for job_id in [k for k, v in JOBS.items() if v.get("stage") in ("done", "failed")]:
                    JOBS.pop(job_id, None)
            self._json({"ok": True})
            return
        if path == "/api/aria2/remove":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                gid = (json.loads(self.rfile.read(min(length, 4096)) or b"{}") or {}).get("gid", "")
            except (ValueError, json.JSONDecodeError):
                gid = ""
            if not gid:
                self._json({"ok": False, "error": "缺少 gid"}, 400)
                return

            async def remove() -> None:
                aria = aria2.Aria2(ARIA2_RPC, ARIA2_SECRET)
                async with httpx.AsyncClient() as client:
                    await aria.remove(client, gid)

            try:
                asyncio.run(remove())
                self._json({"ok": True})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)[:200]}, 502)
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
        if path == "/proxy":
            self._proxy_image(parse_qs(parts.query))
            return
        if path == "/api/peek":
            magnet = parse_qs(parts.query).get("magnet", [""])[0]
            if not magnet.lower().startswith("magnet:"):
                self._json({"ok": False, "error": "需要一个磁力链接"}, 400)
                return
            info = peek(magnet)
            self._json({"ok": bool(info), "info": info}, 200 if info else 502)
            return
        if path == "/api/jobs":
            self._json({"jobs": jobs_snapshot()})
            return
        if path == "/api/aria2":
            self._json(aria_tasks())
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def _proxy_image(self, query: dict) -> None:
        """同源转发 whatslink 的截图，避免浏览器直连第三方域名。"""
        url = (query.get("u") or [""])[0]
        host = (urlsplit(url).hostname or "").lower()
        if not url or host not in ("whatslink.info", "www.whatslink.info"):
            self._send(400, b"bad proxy url", "text/plain; charset=utf-8")
            return
        req_headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://whatslink.info/"}
        for attempt in range(3):          # 上游偶发 500，重试两次基本就稳
            try:
                req = urllib.request.Request(url, headers=req_headers)
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = resp.read(4 * 1024 * 1024)
                    ctype = resp.headers.get("Content-Type") or "image/jpeg"
                self._send(200, data, ctype, {"Cache-Control": "public, max-age=86400"})
                return
            except Exception as exc:
                if attempt == 2:
                    self._send(502, f"proxy failed: {exc}".encode(), "text/plain; charset=utf-8")
                else:
                    time.sleep(0.4 * (attempt + 1))

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
