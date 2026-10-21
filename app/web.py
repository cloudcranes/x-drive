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

from . import aria2, cleanup, codes, core, diskspace, fetcher, meta, pipeline, retry, subscribe

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
# 上传并发与分片大小：默认值来自实测（见 upload_folder 注释），可通过 .env 覆盖
UPLOAD_TRANSFERS = max(1, int(os.getenv("UPLOAD_TRANSFERS", "4") or 4))
UPLOAD_CHUNK = os.getenv("UPLOAD_CHUNK", "64M").strip() or "64M"
# 失败自动重试：次数与首次退避秒数（指数增长，封顶 60s）
RETRIES = max(0, int(os.getenv("RETRIES", "2") or 2))
RETRY_BASE_DELAY = float(os.getenv("RETRY_BASE_DELAY", "3") or 3)
# 连续这么久没有任何新字节就放弃（死种），0 表示不检测
STALL_TIMEOUT = float(os.getenv("STALL_TIMEOUT", "900") or 0)
# 订阅自动化：订阅存在 data/subs.json，后台每隔 SUBS_INTERVAL 秒查一次
SUBS = os.getenv("SUBS_PATH", "/data/subs.json")
SUBS_INTERVAL = max(300.0, float(os.getenv("SUBS_INTERVAL", "3600") or 3600))
# 归档回收：默认关闭（0），需要时再开，避免误删
CLEANUP_MAX_GB = float(os.getenv("CLEANUP_MAX_GB", "0") or 0)
CLEANUP_KEEP_RECENT = int(os.getenv("CLEANUP_KEEP_RECENT", "10") or 10)
CLEANUP_OLDER_DAYS = float(os.getenv("CLEANUP_OLDER_DAYS", "0") or 0)
TMP_ROOT = os.getenv("TMP_ROOT", "/downloads")
# 磁盘空间闸门：下载前确认放得下，放不下就排队（写满会导致 aria2 报错并丢残片）
DISK_RESERVE_GB = float(os.getenv("DISK_RESERVE_GB", "5") or 0)
DISK_CHECK_INTERVAL = max(5.0, float(os.getenv("DISK_CHECK_INTERVAL", "30") or 30))
# 排队最久等多久（秒），0 表示不限
DISK_QUEUE_TIMEOUT = float(os.getenv("DISK_QUEUE_TIMEOUT", "0") or 0)

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
    data["timing"] = m.timing_snapshot(METRICS)
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
    """把目录推到 Drive/<番号>，返回 (分享链接, 字节数)。

    transfers/chunk 可调：实测 300MB 单文件上 transfers=4 的中位耗时
    28s，transfers=2 是 52s（各 3 次，网络抖动大所以看中位数）。
    """
    target = f"{RCLONE_REMOTE}:{DRIVE_ROOT}/{folder}"
    rclone("mkdir", target, timeout=120.0)
    rc, _, err = rclone(
        "copy", str(work), target,
        "--transfers", str(UPLOAD_TRANSFERS),
        "--drive-chunk-size", UPLOAD_CHUNK,
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
        aria = aria2.Aria2(ARIA2_RPC, ARIA2_SECRET, poll=3.0, stall_timeout=STALL_TIMEOUT)
        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
            pipeline.cleanup(work)
            work.mkdir(parents=True, exist_ok=True)
            job_update(job_id, stage="downloading", percent=0.0, speed=0)

            async def on_progress(percent, speed, status):
                if cancel_requested(job_id):
                    raise Cancelled("已停止")
                job_update(job_id, percent=round(percent * 100, 1), speed=speed or 0)

            await pipeline.download(aria, client, source, work, progress=on_progress)

    started = time.monotonic()
    clock = {"download": 0.0, "organize": 0.0, "upload": 0.0}
    done_code, done_size = code, 0
    policy = retry.RetryPolicy(retries=RETRIES, base_delay=RETRY_BASE_DELAY)
    try:
        # ── 下载（可重试）：BT 断流、tracker 抖动都很常见，重试往往能续上
        while True:
            phase = time.monotonic()
            try:
                asyncio.run(main())
                clock["download"] += time.monotonic() - phase
                break
            except Cancelled:
                raise
            except Exception as exc:
                clock["download"] += time.monotonic() - phase
                if not policy.can_retry(str(exc)):
                    raise
                policy.record(str(exc))
                wait = round(policy.delay(), 1)
                log.warning("下载失败，%s 秒后重试（第 %d 次）：%s", wait, policy.attempts, exc)
                job_update(job_id, stage="downloading", retries=policy.attempts,
                           error=f"{exc}｜{wait}s 后重试", percent=0.0, speed=0)
                if not retry.backoff_sleep(policy, cancel_check=lambda: cancel_requested(job_id)):
                    raise Cancelled("已停止")

        if cancel_requested(job_id):
            raise Cancelled("已停止")

        job_update(job_id, stage="organizing")
        phase = time.monotonic()
        names = " ".join(p.name for p in work.glob("*"))
        folder, files = pipeline.organize(work, code or codes.detect(names) or "")
        size = pipeline.total_size(files)
        done_code, done_size = folder, size
        clock["organize"] = time.monotonic() - phase

        if DAILY_UPLOAD_GB and size > DAILY_UPLOAD_GB * 1024**3:
            raise RuntimeError(f"单任务 {core.human_size(size)} 超过 DAILY_UPLOAD_GB 限制")

        # ── 上传（可重试）：网络中断、Drive 5xx 都可以再推一次
        up_policy = retry.RetryPolicy(retries=RETRIES, base_delay=RETRY_BASE_DELAY)
        while True:
            job_update(job_id, stage="uploading", folder=folder, size=size, percent=100.0,
                       retries=policy.attempts + up_policy.attempts)
            phase = time.monotonic()
            try:
                link, _ = upload_folder(work, folder)
                clock["upload"] += time.monotonic() - phase
                break
            except Exception as exc:
                clock["upload"] += time.monotonic() - phase
                if not up_policy.can_retry(str(exc)):
                    raise
                up_policy.record(str(exc))
                wait = round(up_policy.delay(), 1)
                log.warning("上传失败，%s 秒后重试（第 %d 次）：%s", wait, up_policy.attempts, exc)
                job_update(job_id, stage="uploading", error=f"{exc}｜{wait}s 后重试",
                           retries=policy.attempts + up_policy.attempts)
                if not retry.backoff_sleep(up_policy, cancel_check=lambda: cancel_requested(job_id)):
                    raise Cancelled("已停止")

        policy.history.extend(up_policy.history)

        # 元数据是锦上添花：查不到也要把归档记录写进去，不能因此丢记录
        info = None
        try:
            info = asyncio.run(meta.lookup(folder))
        except Exception as exc:
            log.warning("查 %s 的元数据失败：%s", folder, exc)

        core.update_state(STATE, {
            folder: {
                "code": folder,
                "path": f"{RCLONE_REMOTE}:{DRIVE_ROOT}/{folder}",
                "link": link or "",
                "files": [f.name for f in files],
                "size": size,
                "source": source[:300],
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "meta": info or {},
            }
        })
        job_update(job_id, stage="done", link=link or "", code=folder, size=size,
                   retries=policy.attempts, error="",
                   timing={k: round(v, 2) for k, v in clock.items()},
                   throughput=(round(size / clock["upload"] / 1048576, 2) if clock["upload"] else None))
    except Cancelled:
        log.info("任务已停止：%s", job_id)
        job_update(job_id, stage="failed", error="已停止")
    except Exception as exc:
        log.exception("网页任务失败")
        job_update(job_id, stage="failed", error=str(exc)[:300])
    finally:
        outcome = "ok" if clock["upload"] else ("download_fail" if not clock["download"] else "upload_fail")
        record_timing(job_id, done_code, done_size, clock, outcome, started)
        with _jobs_lock:
            CANCEL.pop(job_id, None)
        pipeline.cleanup(work)


def record_timing(job_id: str, code: str, size: int, clock: dict, outcome: str, started: float) -> None:
    """把阶段耗时写进 metrics.json；失败也记，否则只看到成功的样本会失真。"""
    from . import metrics as m

    try:
        m.record_timing(METRICS, {
            "code": code or job_id,
            "size": size,
            "outcome": outcome,
            "download": clock.get("download", 0),
            "organize": clock.get("organize", 0),
            "upload": clock.get("upload", 0),
            "total": time.monotonic() - started,
        })
    except Exception as exc:      # 统计失败不能影响任务本身
        log.warning("记录耗时失败：%s", exc)


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


# 等着被放行的任务：job_id -> (source, code)。放行后从队列移除。
WAITING: dict[str, tuple[str, str]] = {}
_waiting_lock = threading.Lock()
# 任务什么时候开始排队的，用于 DISK_QUEUE_TIMEOUT
_queued_at: dict[str, float] = {}

def _pending_jobs() -> list[dict]:
    """排队中 + 下载中的任务快照，算已占用的空间额度用。"""
    with _jobs_lock:
        return [dict(j) for j in JOBS.values() if j.get("stage") in ("queued", "downloading")]

def admit(job_id: str) -> bool:
    """现在放得下吗？放得下就从队列里拿掉、记上额度、启动线程。"""
    with _waiting_lock:
        item = WAITING.get(job_id)
        if item is None:
            return False
        with _jobs_lock:
            job = dict(JOBS.get(job_id) or {})
        need = int(job.get("total") or 0)
        reserve = int(DISK_RESERVE_GB * diskspace.GB)
        free = diskspace.free_bytes(TMP_ROOT)
        # 已经许给「已在下载」的任务的额度要先扣掉，否则几个任务会同时
        # 判定「放得下」然后一起开下，合起来把盘吃穿。排队中的不算，
        # 否则队列一长额度就被还没开始的任务吃光，队头永远轮不到。
        already = diskspace.reserved_by(_pending_jobs())
        usable = None if free is None else free - max(0, already)
        if not diskspace.fits(need, usable, reserve):
            return False
        WAITING.pop(job_id, None)
        _queued_at.pop(job_id, None)
        # 立刻转成 downloading，这样下一次 admit 就会把这份额度算进去
        with _jobs_lock:
            JOBS.setdefault(job_id, {}).update(
                {"stage": "downloading", "error": "", "queued_position": 0})
    source, code = item
    threading.Thread(target=run_web_job, args=(job_id, source, code), daemon=True).start()
    return True

def pump_queue() -> int:
    """把队列里放得下的任务依次放行，返回放行数量。

    按提交顺序处理：先来的先拿空间，避免大任务一直插队把小任务饿死。
    """
    released = 0
    with _waiting_lock:
        order = list(WAITING.keys())
    for job_id in order:
        if admit(job_id):
            released += 1
    _refresh_queue_positions()
    return released

def _refresh_queue_positions() -> None:
    """给排队任务标上位置，UI 上能看出排第几个。"""
    with _waiting_lock:
        order = list(WAITING.keys())
    free = diskspace.free_bytes(TMP_ROOT)
    reserve_gb = DISK_RESERVE_GB
    for idx, job_id in enumerate(order, start=1):
        with _jobs_lock:
            job = dict(JOBS.get(job_id) or {})
        need = int(job.get("total") or 0)
        if free is None:
            hint = "等待磁盘空间确认"
        else:
            short = need + int(reserve_gb * diskspace.GB) - free
            hint = ("等待磁盘空间" if short <= 0
                    else f"等待磁盘空间（还差 {core.human_size(max(0, short))}）")
        job_update(job_id, stage="queued", queued_position=idx, error=hint)

def _queue_loop() -> None:
    """后台调度：定期重算空间，够一个就放一个。"""
    while True:
        try:
            pump_queue()
            if DISK_QUEUE_TIMEOUT:
                now = time.time()
                with _waiting_lock:
                    expired = [j for j, t in _queued_at.items()
                               if now - t > DISK_QUEUE_TIMEOUT]
                    for j in expired:
                        WAITING.pop(j, None)
                        _queued_at.pop(j, None)
                for job_id in expired:
                    job_update(job_id, stage="failed",
                               error=f"排队超过 {int(DISK_QUEUE_TIMEOUT)}s 仍未等到磁盘空间")
        except Exception:
            log.exception("磁盘空间调度出错")
        time.sleep(DISK_CHECK_INTERVAL)

_job_seq = 0
_job_seq_lock = threading.Lock()

def _new_job_id() -> str:
    """毫秒级时间戳在同一毫秒内会撞车，加一个自增序号保证唯一。"""
    global _job_seq
    with _job_seq_lock:
        _job_seq += 1
        seq = _job_seq
    return f"j{int(time.time() * 1000) % 10**10}{seq % 100:02d}"

def start_job(source: str, code: str, info: dict | None = None) -> str:
    job_id = _new_job_id()
    job_update(job_id, source=source[:300], code=code, stage="queued", percent=0.0, speed=0)
    if info:
        job_update(
            job_id, name=info.get("name") or "", total=info.get("size") or 0,
            screenshots=info.get("screenshots") or [],
        )
    with _waiting_lock:
        WAITING[job_id] = (source, code)
        _queued_at[job_id] = time.time()
    # 立刻试一次：空间够就不用等下一个调度周期
    if not admit(job_id):
        _refresh_queue_positions()
    return job_id


def backfill_meta(limit: int = 50) -> dict:
    """给还没有元数据的归档补抓。逐个来，失败的跳过，不影响别的。

    老归档是在这个功能之前入库的，没有 meta 字段；补一次之后就都有缓存了。
    """
    state = core.load_state(STATE)
    todo = [(k, (v.get("code") or k)) for k, v in state.items()
            if isinstance(v, dict) and not v.get("meta")]
    done, failed = [], []
    for key, code in todo[:max(0, limit)]:
        try:
            info = asyncio.run(meta.lookup(code))
        except Exception as exc:
            log.warning("补抓 %s 元数据失败：%s", code, exc)
            info = None
        if not info:
            failed.append(code)
            continue
        core.update_state(STATE, {key: {**state[key], "meta": info}})
        done.append(code)
    return {"ok": True, "updated": done, "failed": failed,
            "remaining": max(0, len(todo) - len(done) - len(failed))}

# ---------------------------------------------------------------- 订阅自动化

def _resolve_key(data: dict, value: str) -> str:
    """把「订阅键」或「裸 ID」都解析成真实的键。

    界面上传的是 key（形如 actor:YnZ1K），但老数据和命令行里可能只给裸 ID，
    两种都认，免得到处要用户记住前缀。
    """
    if not value:
        return ""
    if value in data:
        return value
    for key, sub in data.items():
        if (sub.get("id") or "") == value:
            return key
    return ""


def subs_snapshot() -> list[dict]:
    out = []
    for key, sub in subscribe.load_subs(SUBS).items():
        item = dict(sub)
        item.setdefault("key", key)
        out.append(item)
    return sorted(out, key=lambda d: d.get("added_at") or "", reverse=True)


def add_sub(actor: str, max_per_run: int = 1, max_size_gb: float = 0,
            name: str = "", kind: str = "actor") -> dict:
    """加一个订阅（演员或番号系列）。游标设成今天，只抓之后出的新片，不回填历史。"""
    if kind not in ("actor", "series"):
        kind = "actor"
    sub_id = subscribe.sub_id_of(actor, kind)
    if not sub_id:
        label = "演员页" if kind == "actor" else "系列页"
        return {"ok": False, "error": f"需要一个 javdb {label} ID 或链接"}
    data = subscribe.load_subs(SUBS)
    # 演员和系列分属两套 ID 空间，键要带上 kind，避免撞车
    key = f"{kind}:{sub_id}"
    if key in data:
        return {"ok": False, "error": f"{sub_id} 已经订阅过了"}
    sub = subscribe.new_sub(sub_id, name or sub_id,
                            cursor=time.strftime("%Y-%m-%d"),
                            max_per_run=max_per_run, max_size_gb=max_size_gb,
                            kind=kind)
    sub["key"] = key
    data[key] = sub
    subscribe.save_subs(SUBS, data)
    return {"ok": True, "sub": sub}


def remove_sub(key: str) -> dict:
    data = subscribe.load_subs(SUBS)
    key = _resolve_key(data, key)
    if not key:
        return {"ok": False, "error": "订阅不存在"}
    data.pop(key)
    subscribe.save_subs(SUBS, data)
    return {"ok": True}


def toggle_sub(key: str, enabled: bool) -> dict:
    data = subscribe.load_subs(SUBS)
    key = _resolve_key(data, key)
    if not key:
        return {"ok": False, "error": "订阅不存在"}
    data[key]["enabled"] = bool(enabled)
    subscribe.save_subs(SUBS, data)
    return {"ok": True, "sub": data[key]}


def known_codes_now() -> set[str]:
    """已经归档 + 正在跑的番号，自动下载前要拿这个去重。"""
    codes = set()
    try:
        codes |= {str(k).upper() for k in core.load_state(STATE)}
    except Exception:
        pass
    codes |= {c.upper() for c in active_codes() if c}
    return codes


def run_subscriptions(only: str = "") -> dict:
    """查所有启用的订阅，把新片提交下载。返回每个订阅的结果。

    这是后台任务：单个订阅出问题只记在该条目上，不影响其它订阅，也不抛异常。
    """
    if not diskspace_room_ok():
        return {"ok": False, "error": "磁盘空间不足，本轮跳过", "results": []}

    data = subscribe.load_subs(SUBS)
    results = []
    known = known_codes_now()
    only_key = _resolve_key(data, only) if only else ""
    for key, sub in list(data.items()):
        if only and key != only_key:
            continue
        if not sub.get("enabled", True):
            continue
        out = asyncio.run(subscribe.check(
            sub.get("id") or key, sub.get("cursor") or "",
            sub.get("max_per_run") or 1, sub.get("max_size_gb") or 0,
            known_codes=known, kind=sub.get("kind") or "actor"))
        started = []
        for item in out["new"]:
            code = item["code"]
            if code.upper() in known:
                continue
            job_id = start_job(item["magnet"], code,
                               {"name": item.get("title") or code,
                                "size": int(item.get("size_mb") or 0) * 1024 * 1024,
                                "screenshots": []})
            known.add(code.upper())
            started.append({"code": code, "job": job_id, "size_mb": item["size_mb"]})
        # 游标推进要落盘：否则下次还会重复扫同一批
        data[key]["cursor"] = out["cursor"]
        data[key]["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if started:
            last = f"新片 {len(started)} 部"
        elif out["error"]:
            last = out["error"]
        elif out.get("skipped"):
            last = f"跳过 {out['skipped']} 部（超体积上限或没有磁力）"
        else:
            last = "没有新片"
        data[key]["last_result"] = last
        results.append({"key": key, "kind": sub.get("kind") or "actor",
                        "id": sub.get("id") or key,
                        "name": sub.get("name") or key,
                        "started": started, "error": out["error"]})
    subscribe.save_subs(SUBS, data)
    return {"ok": True, "results": results}


def diskspace_room_ok() -> bool:
    """有空间才跑自动下载；不够就整轮跳过，等下次。"""
    free = diskspace.free_bytes(TMP_ROOT)
    reserve = int(DISK_RESERVE_GB * diskspace.GB)
    return free is None or free > reserve


def _subs_loop() -> None:
    while True:
        try:
            res = run_subscriptions()
            n = sum(len(r.get("started") or []) for r in res.get("results") or [])
            if n:
                log.info("订阅轮询：新提交 %d 个任务", n)
        except Exception:
            log.exception("订阅轮询出错")
        time.sleep(SUBS_INTERVAL)


def build_cleanup_plan(dry_run: bool = True) -> dict:
    """算出该删哪些归档；dry_run 时只给清单不执行。"""
    state = core.load_state(STATE)
    archives = []
    for key, item in state.items():
        archives.append({
            "id": key,
            "code": item.get("code") or key,
            "size": int(item.get("size") or 0),
            "at": item.get("at") or "",
        })
    plan = cleanup.plan_cleanup(
        archives,
        keep_recent=CLEANUP_KEEP_RECENT,
        max_bytes=int(CLEANUP_MAX_GB * 1024**3),
        active_codes=active_codes(),
        older_than_days=CLEANUP_OLDER_DAYS,
        dry_run=dry_run,
    )
    result = plan.as_dict()
    if not dry_run:
        result["removed"] = purge_archives([i["code"] for i in plan.delete])
    return result


def purge_archives(codes_to_drop: list[str]) -> list[dict]:
    """真正删除：Drive 目录 + state 记录。任一失败就保留记录，便于重试。"""
    done = []
    for code in codes_to_drop:
        if not code:
            continue
        target = f"{RCLONE_REMOTE}:{DRIVE_ROOT}/{code}"
        rc, _, err = rclone("purge", target, timeout=1800.0)
        if rc != 0:
            log.warning("删除 %s 失败：%s", code, err.strip()[:200])
            continue
        state = core.load_state(STATE)
        if code in state:
            state.pop(code, None)
            core.save_state(STATE, state)
        done.append({"code": code, "ok": True})
    return done


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
                # 元数据只读缓存，不在渲染时联网；没抓过的用 backfill 补
                "meta": item.get("meta") or meta.cached(code) or {},
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
        if path == "/api/cleanup/run":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(min(length, 8192)) or b"{}") or {}
            except (ValueError, json.JSONDecodeError):
                payload = {}
            # 必须显式传 confirm，否则一律按 dry-run 处理，防止误删
            if not payload.get("confirm"):
                self._json({"ok": True, **build_cleanup_plan(dry_run=True),
                            "hint": "需要 confirm=true 才会真正删除"}, 200)
                return
            plan = build_cleanup_plan(dry_run=False)
            self._json({"ok": True, **plan})
            return
        if path == "/api/subs/add":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(min(length, 8192)) or b"{}") or {}
            except (ValueError, json.JSONDecodeError):
                payload = {}
            self._json(add_sub(payload.get("actor") or "",
                               int(payload.get("max_per_run") or 1),
                               float(payload.get("max_size_gb") or 0),
                               payload.get("name") or "",
                               payload.get("kind") or "actor"))
            return
        if path == "/api/subs/toggle":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(min(length, 4096)) or b"{}") or {}
            except (ValueError, json.JSONDecodeError):
                payload = {}
            self._json(toggle_sub(payload.get("id") or "", bool(payload.get("enabled"))))
            return
        if path == "/api/subs/remove":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(min(length, 4096)) or b"{}") or {}
            except (ValueError, json.JSONDecodeError):
                payload = {}
            self._json(remove_sub(payload.get("id") or ""))
            return
        if path == "/api/subs/run":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(min(length, 4096)) or b"{}") or {}
            except (ValueError, json.JSONDecodeError):
                payload = {}
            self._json(run_subscriptions(payload.get("id") or ""))
            return
        if path == "/api/archive/delete":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(min(length, 8192)) or b"{}") or {}
            except (ValueError, json.JSONDecodeError):
                payload = {}
            code = (payload.get("code") or "").strip()
            if not code:
                self._json({"ok": False, "error": "缺少番号"}, 400)
                return
            state = core.load_state(STATE)
            if code not in state:
                self._json({"ok": False, "error": f"{code} 不在归档记录里"}, 404)
                return
            target = f"{RCLONE_REMOTE}:{DRIVE_ROOT}/{code}"
            # 删除不可逆：没显式 confirm 只回报将删什么
            if not payload.get("confirm"):
                self._json({"ok": True, "code": code, "target": target,
                            "files": state[code].get("files") or [],
                            "size": state[code].get("size") or 0,
                            "hint": "需要 confirm=true 才会真正删除 Drive 目录与归档记录"}, 200)
                return
            done = purge_archives([code])
            if not done:
                self._json({"ok": False, "error": f"删除 {code} 失败，记录已保留可重试"}, 502)
                return
            self._json({"ok": True, "deleted": done})
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
        if path == "/api/subs":
            self._json({"ok": True, "subs": subs_snapshot()})
            return
        if path == "/api/meta/backfill":
            # 给老归档补元数据：会联网抓，所以显式触发而不是渲染时自动跑
            limit = int((parse_qs(parts.query).get("limit", ["50"]) or ["50"])[0] or 50)
            self._json(backfill_meta(limit=limit))
            return
        if path == "/api/cleanup/preview":
            self._json({"ok": True, **build_cleanup_plan(dry_run=True)})
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
        # 只放行白名单：截图来自 whatslink，封面来自 javdb 的图床
        allowed = ("whatslink.info", "www.whatslink.info", "jdbstatic.com")
        if not url or not any(host == h or host.endswith("." + h) for h in allowed):
            self._send(400, b"bad proxy url", "text/plain; charset=utf-8")
            return
        referer = ("https://whatslink.info/" if "whatslink" in host
                   else "https://javdb.com/")
        req_headers = {"User-Agent": "Mozilla/5.0", "Referer": referer}
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
    # 磁盘空间调度线程：排队任务要能被自动放行，不能只靠提交时那一次判断
    threading.Thread(target=_queue_loop, daemon=True, name="disk-queue").start()
    # 订阅轮询线程：定时查演员页，有新的自动提交
    threading.Thread(target=_subs_loop, daemon=True, name="subs").start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
