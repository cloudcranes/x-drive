"""看板 + 后台任务：读 /data/state.json 渲染卡片，并承接网页提交的下载/订阅/清理任务。

零第三方依赖（标准库 http.server），与 bot 共用同一镜像，command 覆盖入口。
环境变量：WEB_PORT(8080)、WEB_BIND(0.0.0.0)、WEB_AUTH(用户名:密码，可选，
设置后浏览器会弹原生登录框，401 时返回登录页)、STATE_PATH，以及文件后半部分
列出的下载/上传/订阅/清理相关项。
"""
from __future__ import annotations

import base64
from contextvars import ContextVar, copy_context
import datetime
import hashlib
import hmac
import json
import logging
import os
import re
import time
import asyncio
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit
from uuid import uuid4

import httpx

from . import (backlog, cleanup, cloudingest, cloudorganize, codes, core, diskspace, fetcher,
               find, imgcache, meta, metadata, metatube, notify, pipeline, preview, retry,
               settings as settings_mod, sources, storage, store, subscribe, workspace,
               xunlei, xunleipan)

log = logging.getLogger("x-drive.web")

STATE = os.getenv("STATE_PATH", "/data/state.json")
METRICS = os.getenv("METRICS_PATH", "/data/metrics.json")
PENDING_DIR = os.getenv("PENDING_DIR", "/data/pending")
XUNLEI_URL = os.getenv("XUNLEI_URL", "http://127.0.0.1:2345").strip()
XUNLEI_PARENT_ID = os.getenv("XUNLEI_PARENT_ID", "").strip()
XUNLEI_PAN_AUTH = os.getenv("XUNLEI_PAN_AUTH", "").strip()
XUNLEI_POLL = float(os.getenv("XUNLEI_POLL", "10") or 10)
XUNLEI_SETTLE = float(os.getenv("XUNLEI_SETTLE", "30") or 0)
# 面板里失败任务记录保留多少天；0 = 不自动清（只在手动点「清理失败」时清）
PANEL_CLEAN_DAYS = float(os.getenv("PANEL_CLEAN_DAYS", "0") or 0)
RCLONE_REMOTE = os.getenv("RCLONE_REMOTE", "gdrive").strip()
DRIVE_ROOT = os.getenv("DRIVE_ROOT", "X-Drive").strip().strip("/")
RCLONE_CONFIG = os.getenv("RCLONE_CONFIG", "/config/rclone/rclone.conf")
DAILY_UPLOAD_GB = int(os.getenv("DAILY_UPLOAD_GB", "0") or 0)
# 刮削附件：默认全开；关掉就退回原来的「只有视频」行为
SCRAPE_ON = os.getenv("SCRAPE", "1") not in ("0", "false", "False")
NFO_ON = os.getenv("SCRAPE_NFO", "1") not in ("0", "false", "False")
# 补刮能推到 Drive 的附件就这几个；单文件重试的 only 只认它们，别的名字一律不收
ATTACHMENT_NAMES = frozenset((metadata.JSON_NAME, metadata.NFO_NAME,
                              metadata.POSTER_NAME, metadata.MANIFEST_NAME))
POSTER_ON = os.getenv("SCRAPE_POSTER", "1") not in ("0", "false", "False")
# 上传并发与分片大小：默认值来自实测（见 upload_folder 注释），可通过 .env 覆盖
UPLOAD_TRANSFERS = max(1, int(os.getenv("UPLOAD_TRANSFERS", "4") or 4))
UPLOAD_CHUNK = os.getenv("UPLOAD_CHUNK", "64M").strip() or "64M"
# 失败自动重试：次数与首次退避秒数（指数增长，封顶 60s）
RETRIES = max(0, int(os.getenv("RETRIES", "2") or 2))
RETRY_BASE_DELAY = float(os.getenv("RETRY_BASE_DELAY", "3") or 3)
# 连续这么久没有任何新字节就放弃（死种），0 表示不检测
STALL_TIMEOUT = float(os.getenv("STALL_TIMEOUT", "900") or 0)
# 单个任务的总超时（秒），兜底用
MAGNET_TIMEOUT = float(os.getenv("MAGNET_TIMEOUT", "7200") or 7200)
# 订阅自动化：订阅存在 data/subs.json，后台每隔 SUBS_INTERVAL 秒查一次
SUBS = os.getenv("SUBS_PATH", "/data/subs.json")
# 订阅待办：发现到的作品先记这里，再按上限逐轮提交（避免漏项与队头阻塞）
BACKLOG = os.getenv("BACKLOG_PATH", "/data/backlog.json")
# 愿望单在待办表里的分组键：和订阅平级，共用调度与去重
WISH_KEY = "wishlist"
SUBS_INTERVAL = max(300.0, float(os.getenv("SUBS_INTERVAL", "3600") or 3600))
# 归档回收：默认关闭（0），需要时再开，避免误删
CLEANUP_MAX_GB = float(os.getenv("CLEANUP_MAX_GB", "0") or 0)
CLEANUP_KEEP_RECENT = int(os.getenv("CLEANUP_KEEP_RECENT", "10") or 10)
CLEANUP_OLDER_DAYS = float(os.getenv("CLEANUP_OLDER_DAYS", "0") or 0)
TMP_ROOT = os.getenv("TMP_ROOT", "/downloads")
PREVIEW_DB = os.getenv("PREVIEW_DB", str(Path(os.getenv("STATE_PATH", "/data/state.json")).parent / "preview.json"))
# 整理工作区：放改名后的文件，与迅雷的下载目录分开，失败也不动原件
STATE_DIR = os.getenv("STATE_DIR", str(Path(TMP_ROOT).parent / "work"))
# 磁盘空间闸门：下载前确认放得下，放不下就排队（写满会让迅雷报错并留一堆残片）
DISK_RESERVE_GB = float(os.getenv("DISK_RESERVE_GB", "5") or 0)
# 体积完全未知时的保守预留（同系列也没历史）：按一部普通片子估，而不是当成 0
DEFAULT_SIZE_GB = float(os.getenv("DEFAULT_SIZE_GB", "4") or 0)
DISK_CHECK_INTERVAL = max(5.0, float(os.getenv("DISK_CHECK_INTERVAL", "30") or 30))
# 排队最久等多久（秒），0 表示不限
DISK_QUEUE_TIMEOUT = float(os.getenv("DISK_QUEUE_TIMEOUT", "0") or 0)
# 每日 tick 的清理力度：任务表保留 30 天、待办保留 90 天且每个订阅最多留 500 条
# 已结束的任务与已结束的待办记录留着没用（只让表变大），pending 永不删。
DAILY_PRUNE_DAYS = float(os.getenv("DAILY_PRUNE_DAYS", "30") or 30)
DAILY_BACKLOG_PRUNE_DAYS = float(os.getenv("DAILY_BACKLOG_PRUNE_DAYS", "90") or 90)
DAILY_BACKLOG_KEEP = int(os.getenv("DAILY_BACKLOG_KEEP", "500") or 500)

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

# 迅雷客户端做成进程内单例：token、space、下载目录 id 都有缓存，
# 每个任务新建一个就要多打三次 GET，面板忙时还会拖慢提交
_XL: xunlei.Xunlei | None = None

def xunlei_client() -> xunlei.Xunlei:
    global _XL
    if _XL is None:
        _XL = xunlei.Xunlei(XUNLEI_URL, XUNLEI_PARENT_ID, TMP_ROOT, XUNLEI_PAN_AUTH)
    return _XL

# 任务表：重启不丢、同番号并发去重。JOBS 只是给旧代码留的内存镜像，
# 所有真实状态以 STORE 为准。
JOBS: dict[str, dict] = {}
_jobs_lock = threading.Lock()
def _store() -> store.Store:
    """任务表按需打开：模块导入时不该去碰 /data（测试与非容器环境都要能导入）。"""
    global STORE
    if STORE is None:
        STORE = store.Store(os.getenv("JOBS_DB", str(Path(STATE).parent / "jobs.db")))
    return STORE


STORE: store.Store | None = None

# 可热更新参数的单例：惰性绑定到 STORE，网页保存后立即失效本进程缓存。
_settings: settings_mod.Settings | None = None
_settings_lock = threading.Lock()


def _get_settings() -> settings_mod.Settings:
    """可热更新参数的访问器（惰性单例）。绑定到当前 STORE，库换了就重建。"""
    global _settings
    s = _store()
    with _settings_lock:
        if _settings is None or _settings._store is not s:
            _settings = settings_mod.Settings(s)
            # 绑定给 scrapers：METATUBE_* / JAVBUS_* / META_USE_JAVBUS
            # 在模块里是导入时读的环境变量，靠这个桥改成「网页改完即生效」。
            settings_mod.bind(_settings)
        return _settings


def get_setting(key: str):
    """读一个可热更新参数（state 表 > 环境变量 > 默认值，带 TTL 缓存）。"""
    try:
        return _get_settings().get(key)
    except Exception as exc:
        # 读不到设置不能拖垮主流程：回落到环境变量/默认值由 Settings 内部兜底，
        # 这里只防 Settings 本身抛异常（例如 store 打不开）。
        log.warning("读设置 %s 失败，回退默认：%s", key, exc)
        spec = settings_mod.SETTINGS_SPEC.get(key) or {}
        return spec.get("default")


def settings_snapshot() -> dict:
    """设置面板用：17 项的当前值 + 来源（settings/env/default）+ 分组说明。"""
    try:
        return _get_settings().snapshot()
    except Exception as exc:
        log.warning("读设置快照失败：%s", exc)
        return {}


def set_setting(key: str, value) -> bool:
    """写一个可热更新参数（网页保存）。返回是否成功。"""
    try:
        return _get_settings().set(key, value)
    except Exception as exc:
        log.warning("写设置 %s 失败：%s", key, exc)
        return False


def set_settings(updates: dict) -> dict:
    """批量写（网页一次保存多个）。返回 {ok, saved, failed}。

    存完立刻清掉存储后端实例缓存：URL/账号/密码一改，旧连接与旧 token 都得换，
    否则「保存成功但下一次上传还走旧配置」。
    """
    try:
        result = _get_settings().set_many(updates)
    except Exception as exc:
        log.warning("批量写设置失败：%s", exc)
        return {"ok": False, "saved": [], "failed": {"*": str(exc)[:120]}}
    if result.get("saved"):
        storage.reset()
    return result


# ── 存储后端：面板上的「当前后端」与「测试连接」

def storage_view() -> dict:
    """当前后端快照（不含任何密钥）。"""
    try:
        return {"ok": True, **storage.snapshot()}
    except Exception as exc:
        log.warning("读存储状态失败：%s", exc)
        return {"ok": False, "error": str(exc)[:200]}


def image_cache_view() -> dict:
    """图片缓存的现状（面板展示 + 清空入口用）。"""
    imgcache.purge_legacy()          # 老布局无法迁移，看到就清掉，别让它一直占地方
    info = imgcache.stats()
    info["ok"] = True
    info["limit_mb"] = round(img_cache_max_bytes() / 1048576, 1) if img_cache_max_bytes() else 0
    return info


def image_cache_clear(confirm: bool = False) -> dict:
    """清空本地图片缓存。缓存是纯派生数据，删了只会让下次重新取图。"""
    if not confirm:
        return {"ok": True, **imgcache.stats(), "hint": "需要 confirm=true 才会清空"}
    result = imgcache.clear()
    return {"ok": True, **result, "after": imgcache.stats()}


def find_cards(keyword: str) -> dict:
    """按关键词找片子（MIMK、SSIS 这类前缀或标题里的词）。

    与 /api/magnets 的区别：那个是「给我这个番号的磁力」（下载用），
    这个是「有哪些片」（发现用）。上游挂了也只回 ok:false。

    首页媒体墙已于 2026-10-25 移除（只展示已归档资源）；这个搜索框入口保留。
    """
    keyword = (keyword or "").strip()[:64]
    if not keyword:
        return {"ok": False, "query": "", "items": [], "error": "关键词为空"}
    try:
        return find.find(keyword)
    except Exception as exc:
        log.warning("关键词搜索失败：%s", exc)
        return {"ok": False, "query": keyword, "items": [], "error": str(exc)[:200]}


def storage_test() -> dict:
    """连通性自检。失败也回 200 + ok:false，面板直接展示原因。"""
    return storage.check()


def job_event(job_id: str, kind: str, detail: str = "") -> None:
    """记一条任务事件（时间线用）。**绝不抛异常**：观测不能拖累主流程。"""
    if not job_id or not kind:
        return
    try:
        owner = execution_owner(job_id)
        _store().event(job_id, kind, detail, owner=owner if owner and kind != "lease_lost" else None)
    except Exception as exc:          # 库打不开、磁盘满、表还没迁移……
        log.warning("记录任务 %s 的事件 %s 失败：%s", job_id, kind, exc)


def _stage(job_id: str, stage: str, detail: str = "") -> None:
    """阶段切换：先落库再记事件，时间线与任务表不会互相打架。"""
    job_update(job_id, stage=stage)
    job_event(job_id, "stage", detail or stage)


def _first_line(text: str, limit: int = 120) -> str:
    """错误原文取首行：多行堆栈塞进时间线没人看，库里也不用存整段。"""
    line = str(text or "").strip().splitlines()
    return (line[0] if line else "")[:limit]


def _event_deferred(job_id: str, until: float, note: str, why: str = "") -> None:
    """延期事件：把唤醒时刻写成可读时间，时间线上能直接看到「几点会再跑」。"""
    when = time.strftime("%m-%d %H:%M", time.localtime(until)) if until else "未知"
    job_event(job_id, "deferred",
              f"{when} 唤醒 · {note}" + (f" · {_first_line(why)}" if why else ""))


# Context belongs to one execution, not the mutable job -> latest-worker registry.
_EXECUTION = ContextVar("web_execution", default=None)


def execution_owner(job_id: str) -> str:
    current = _EXECUTION.get()
    return current[1] if current and current[0] == job_id and (
        current[2] is _store() or getattr(current[2], "path", None) == getattr(_store(), "path", object())) else ""


def job_update(job_id: str, **fields) -> bool:
    st = _store()
    owner = execution_owner(job_id)
    if not st.get(job_id) and not owner:
        st.start(job_id, str(fields.get("code") or ""), str(fields.get("source") or ""))
    # Even legacy/direct callers may only change an unleased row. A contextless
    # callback must never borrow the newest token from LEASES.
    if not st.update_cas(job_id, owner, **fields):
        return False
    with _jobs_lock:
        record = JOBS.setdefault(job_id, {"id": job_id, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        record.update(fields)
    return True


def _archive_lease(job_id: str, code: str = "") -> None:
    # Direct maintenance/tests on unleased records remain supported.
    if execution_owner(job_id) or _store().get(job_id).get("exec_owner"):
        require_lease(job_id, code)


# 任务接口对外承诺的字段。只增不改：前端与 bot 都按这张表读，
# 免得出现「内部写进去了、接口却查不到」的字段（排队位置曾整批丢掉）。
JOB_FIELDS = ("id", "code", "source", "stage", "percent", "speed", "total", "downloaded",
              "folder", "link", "error", "retries", "owner", "xunlei_task",
              "xunlei_ours", "keep_dir", "resume_from", "kept", "created_at", "updated_at")
# 每条任务随 /api/jobs 附带的最近事件条数（时间线够画完全程，也不把响应撑大）
JOB_EVENT_LIMIT = 50


def jobs_snapshot() -> list[dict]:
    """任务列表：业务字段 + extra 里可展示的那几项，统一在这里拼。"""
    items = _store().recent(20)
    out = []
    for j in items:
        row = {k: j.get(k) for k in JOB_FIELDS}
        extra = j.get("extra") or {}
        row["at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(j["created_at"]))
        row["cleanup_pending"] = extra.get("cleanup_pending") or []
        row["name"] = extra.get("name") or ""
        row["screenshots"] = extra.get("screenshots") or []
        row["infohash"] = extra.get("infohash") or ""
        row["defer_until"] = float(extra.get("defer_until") or 0)
        row["queued_position"] = int((_jobs_mirror(j.get("id")) or {}).get("queued_position") or 0)
        # 时间线的数据源：最近若干条事件（升序）。老任务没有事件时是空表，前端回退旧渲染
        row["events"] = _store().events(j.get("id"), limit=JOB_EVENT_LIMIT)
        out.append(row)
    return out


def clear_jobs(stage: str = "") -> dict:
    """清掉「已结束」的任务记录，返回删了哪些。

    这里必须删库，不能只清内存镜像：队列列表读的是 `store.recent()`，
    以前只 pop 了 JOBS 字典，页面上一条都不会少 —— 用户点了「清空已完成」
    看起来毫无反应，失败的记录更是永远清不掉。

    stage 可指定只清某一类（`failed` / `done` / `cancelled`），留空清全部已结束的。
    只删记录：已下载的文件、归档记录都不动。
    """
    st = _store()
    only = (stage or "").strip()
    removed: list[str] = []
    if only:
        if only not in st.FINISHED:
            return {"ok": False, "error": f"只能清理已结束的任务（{only} 不算）",
                    "allowed": list(st.FINISHED)}
        for job in st.recent(500):
            if job.get("stage") == only and st.delete(job["id"]):
                removed.append(job["id"])
    else:
        removed = st.clear_finished()
    with _jobs_lock:
        for job_id in removed:
            JOBS.pop(job_id, None)
    log.info("清空任务记录 %d 条%s", len(removed), f"（stage={only}）" if only else "")
    return {"ok": True, "deleted": removed, "count": len(removed)}


def delete_job(job_id: str, confirm: bool = False) -> dict:
    """删一条任务记录。每条都要单独确认：这是记录删除，不是停止任务。"""
    job_id = (job_id or "").strip()
    if not job_id:
        return {"ok": False, "error": "缺少任务 id"}
    st = _store()
    job = st.get(job_id)
    if not job:
        return {"ok": False, "error": "没有这条任务记录", "id": job_id}
    if job.get("stage") not in st.FINISHED:
        return {"ok": False, "id": job_id,
                "error": f"任务还在进行中（{job.get('stage')}），先停止再删记录"}
    if not confirm:
        return {"ok": True, "id": job_id, "stage": job.get("stage"),
                "code": job.get("code") or "", "hint": "需要 confirm=true 才会删除记录",
                "note": "只删任务记录，不动下载目录与已归档的文件"}
    ok = st.delete(job_id)
    with _jobs_lock:
        JOBS.pop(job_id, None)
    return {"ok": ok, "id": job_id, "deleted": ok,
            "error": "" if ok else "删除失败（记录可能已被清掉）"}


def _jobs_mirror(job_id: str) -> dict:
    """内存镜像：只放瞬时展示状态（排队位置、提示语），不落库。"""
    with _jobs_lock:
        return dict(JOBS.get(job_id) or {})


def active_codes() -> set[str]:
    """正在跑的番号：任务表为准，内存镜像兜底（补记录尚未落库的瞬间）。"""
    with _jobs_lock:
        mem = {(j.get("code") or "").upper() for j in JOBS.values()
               if j.get("stage") in store.ACTIVE and j.get("code")}
    return _store().active_codes() | mem


def recover_jobs() -> list[dict]:
    """重启后对账：把上次没跑完的任务捞出来，带上迅雷任务 id 供续接。

    返回待续任务，由调用方决定重新投递还是继续跟进（此处只读，不改状态）。
    """
    out = []
    for job in _store().unfinished():
        out.append({"id": job["id"], "code": job["code"], "source": job["source"],
                    "stage": job["stage"], "xunlei_task": job["xunlei_task"]})
    if out:
        log.info("发现 %d 个未完成任务，等待续接：%s",
                 len(out), "、".join(j["code"] or j["id"] for j in out))
    return out


def archived_codes() -> set[str]:
    try:
        return {str(k).upper() for k in core.load_state(STATE)}
    except Exception:
        return set()


def _dir_size(path: Path) -> int:
    """递归算一个目录的字节数。不跟符号链接：防循环，也不把别处的挂载算进来。"""
    total = 0
    try:
        for p in path.rglob("*"):
            try:
                if p.is_symlink():
                    continue
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total


def local_disk_inventory() -> dict:
    """本地残留盘点：只读列出 TMP_ROOT 与 STATE_DIR 下带归属标记的目录。

    背景：归档回收（rclone purge）删的是 Drive，本地一个字节都不动。磁盘满了
    用户点回收，以为腾了空间，本地还是满的。这个接口把两处本地占用的目录
    摊开来，让用户看到「哪些是已归档但没清干净的残留、哪些是失败保留的原件」。

    **只读、只报告**：不删任何目录、不删任何文件、不改任何状态。没有归属
    标记的目录一律跳过（与「没有标记的目录不接管、不清理」纪律一致）。
    返回里只有 hint 提示「清理需人工确认」，不提供删除入口。
    """
    archived = {workspace.key_of(k) for k in archived_codes()}
    items: list[dict] = []

    def scan(root: Path, bucket: str, subdir: str | None = None) -> None:
        base = root / subdir if subdir else root
        try:
            entries = list(base.iterdir())
        except OSError:
            return
        for d in entries:
            try:
                if not d.is_dir() or d.is_symlink():
                    continue
            except OSError:
                continue
            ev = workspace.evidence_of(d)
            owner = str(ev.get("owner") or "").strip()
            if not owner:                     # 没有归属标记：不报告、不接管
                continue
            key = workspace.key_of(owner)
            size = _dir_size(d)
            # 分类：key 在归档表里 = 上传成功但本地没清干净的残留；否则是失败保留。
            # 注意两边都按 key_of 归一（去连字符）后再比：目录名是 key_of(folder)，
            # 归档表 key 是 folder 本身（SSIS-123），直接比永远对不上。
            kind = "已归档残留" if key in archived else "失败保留"
            items.append({
                "bucket": bucket,
                "key": key,
                "path": str(d.relative_to(root)) if subdir is None
                        else f"{subdir}/{d.name}",
                "size": size,
                "kind": kind,
                "task": ev.get("task") or "",
                "btih": ev.get("btih") or "",
            })

    scan(Path(TMP_ROOT), "downloads")
    scan(Path(STATE_DIR), "workspace", subdir="state")

    items.sort(key=lambda r: (-r["size"], r["bucket"], r["key"]))
    total = sum(i["size"] for i in items)
    return {
        "ok": True,
        "items": items,
        "count": len(items),
        "total_size": total,
        "by_kind": {
            "已归档残留": sum(1 for i in items if i["kind"] == "已归档残留"),
            "失败保留": sum(1 for i in items if i["kind"] == "失败保留"),
        },
        "hint": "只报告，不删除。清理前请人工确认每个目录确属本程序产物。",
    }


# ── 图片代理：白名单 + 本地缓存（缓存的细节都在 app/imgcache.py）
IMG_HOSTS = ("whatslink.info", "www.whatslink.info", "jdbstatic.com", "javbus.com",
             "www.javbus.com", "pics.dmm.co.jp", "awsimgsrc.dmm.co.jp")
IMG_TIMEOUT = 6.0
IMG_ATTEMPTS = 3


def img_cache_max_bytes() -> int:
    """缓存上限（字节）。设置里 0 = 不限。"""
    try:
        mb = float(get_setting("IMG_CACHE_MB") or 0)
    except (TypeError, ValueError):
        mb = 0
    return int(mb * 1024 * 1024) if mb > 0 else 0


CANCEL: dict[str, threading.Event] = {}
# 上传进度回调按线程传递：upload_folder 的两参签名被测试大量 monkeypatch，
# 不适合加参数；CD2/OpenList 适配器有真实进度，就靠这个把进度送到 _upload_loop。
_UPLOAD_PROGRESS = threading.local()


def request_cancel(job_id: str) -> bool:
    """标记任务取消；执行线程在下一个进度回调/阶段边界抛异常收尾。

    **两处都记**：进程内事件（本进程立即生效）+ 任务表（跨进程生效）。
    Web 与 bot 是两个进程：只记内存时，Web 的停止按钮停不了 bot 正在跑的
    任务 —— 库里查得到任务，接口却说「任务不存在」。
    已经结束的任务返回 False：没有可取消的东西，重复停止不该报成功。
    """
    st = _store()
    marked = False
    try:
        marked = st.request_cancel(job_id)     # 库里查得到就受理（跨进程；终态 False）
    except Exception as exc:                   # 库暂时不可用时退回纯内存
        log.warning("写取消标记失败：%s", exc)
    with _jobs_lock:
        job = dict(JOBS.get(job_id) or {})
    # 本进程内存里还在跑才配对进程内事件（终态不许「取消成功」）
    in_proc_active = bool(job) and job.get("stage") not in ("done", "failed", "cancelled")
    if in_proc_active:
        CANCEL.setdefault(job_id, threading.Event()).set()
    return bool(marked or in_proc_active)


def cancel_requested(job_id: str) -> bool:
    event = CANCEL.get(job_id)
    if event and event.is_set():
        return True
    # 本进程没标，再看任务表：另一个进程（web / bot）可能刚请求了取消
    try:
        return bool(_store().cancel_marked(job_id))
    except Exception as exc:
        log.warning("读取消标记失败：%s", exc)
        return False


class Cancelled(RuntimeError):
    """用户主动停止任务。"""


class LossOfLease(RuntimeError):
    """租约被别的执行者接走：本线程必须安静退出。

    与用户取消（Cancelled）**必须分开**：取消要落 cancelled 终态，失租却绝对
    不能写共享状态 —— 接管者正持租在跑，旧线程把它覆盖成 cancelled/failed
    等于替别人取消任务（真实发生过：旧线程退出时任务写成 cancelled，
    「清空已完成」把还在跑的记录删掉）。require_lease / 心跳统一抛这个。
    """


def rclone(*args: str, timeout: float = 7200.0) -> tuple[int, str, str]:
    cmd = ["rclone", "--config", RCLONE_CONFIG, *args]
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", "rclone 超时"
    return done.returncode, done.stdout.decode(errors="replace"), done.stderr.decode(errors="replace")


# 把本模块这层 rclone 注入存储层：/api/storage/test 里 rclone 后端的自检走它，
# 于是测试里 monkeypatch `web.rclone` 的替身照样生效（别在 storage 里另起一套 subprocess）。
storage.set_runner(lambda *args, **kwargs: rclone(*args, **kwargs))


def storage_backend():
    """当前存储后端实例（app/storage 的注册表按配置指纹缓存）。"""
    return storage.get()


def upload_folder(work: Path, folder: str) -> tuple[str | None, int]:
    """把目录推到「归档根/<番号>」，返回 (分享链接, 字节数)。

    rclone 分支的 argv 与改造前逐字一致：现网就是它在跑，别顺手加 --progress
    之类（实测 transfers/chunk 的取值也写进了默认值，见 settings 注释）。
    OpenList / CloudDrive2 走 storage 适配器，语义见 app/storage/base.py：
    返回即代表云端已落地。
    """
    backend = storage_backend()
    root = storage.root()             # 同一份快照同时用于上传和记账，避免热更新夹在两次读取之间
    location = {"backend": backend.kind, "root": root,
                "rel": "/".join([root, folder.strip("/")])}
    if backend.kind == "rclone":
        location.update(root=DRIVE_ROOT, rel=f"{DRIVE_ROOT}/{folder}", remote=RCLONE_REMOTE)
    location["path"] = f"{location.get('remote') or backend.kind}:{location['rel']}"
    _UPLOAD_PROGRESS.location = location
    _UPLOAD_PROGRESS.location_work = str(work)
    _UPLOAD_PROGRESS.location_backend = backend
    if backend.kind != "rclone":
        rel = location["rel"]
        backend.ensure_dir(rel)
        size = backend.upload_dir(work, rel,
                                  transfers=get_setting("UPLOAD_TRANSFERS"),
                                  chunk=get_setting("UPLOAD_CHUNK"),
                                  progress=getattr(_UPLOAD_PROGRESS, "fn", None))
        return backend.link(rel), size

    target = f"{RCLONE_REMOTE}:{DRIVE_ROOT}/{folder}"
    rclone("mkdir", target, timeout=120.0)
    rc, _, err = rclone(
        "copy", str(work), target,
        "--transfers", str(get_setting("UPLOAD_TRANSFERS")),
        "--drive-chunk-size", get_setting("UPLOAD_CHUNK"),
        "--retries", "2", "--timeout", "30m",
        "--drive-stop-on-upload-limit",
    )
    if rc != 0:
        raise RuntimeError(f"rclone 上传失败：{err.strip()[:300]}")
    rc, out, _ = rclone("link", target, timeout=120.0)
    return (out.strip() if rc == 0 and out.strip().startswith("http") else None), 0


def _resume_stage(job: dict, work: Path, key: str) -> tuple[str, list[Path], int] | None:
    """中断在上传阶段时，复用工作区里已整理好的文件。

    判据看 resume_from 与落盘清单，而不是 stage：
    入队会把 stage 改回 queued，重启后只知道「排队中」，靠 stage 判断就永远续不上。
    清单比对用记录里存的文件名，文件缺失或出现非规范名都视为整理未完成，老老实实重跑。
    """
    if not job or job.get("resume_from") != "upload":
        return None
    folder = (job.get("folder") or "").strip()
    if not folder or not work.is_dir():
        return None
    manifest = [str(n) for n in (job.get("extra") or {}).get("files") or [] if n]
    # 比对用归一后的番号：目录名是 `SSIS-555`，文件名是 `SSIS555.mp4`
    stem = workspace.key_of(folder) or folder
    for name in manifest or []:
        path = work / name
        if not path.is_file():
            log.info("工作区 %s 缺少清单里的 %s，重新整理", work, name)
            return None
        if not workspace.key_of(path.stem).startswith(stem):
            log.info("工作区 %s 里的 %s 不是 %s 的规范名，重新整理", work, name, folder)
            return None
    files = sorted(work / n for n in manifest) if manifest else sorted(
        p for p in work.iterdir()
        if p.is_file() and p.suffix.lower() in (fetcher.VIDEO_EXT | fetcher.IMAGE_EXT))
    if not files:
        return None
    if not manifest and any(not workspace.key_of(p.stem).startswith(stem) for p in files):
        log.info("工作区 %s 里的文件不是 %s 的规范名，重新整理", work, folder)
        return None
    size = pipeline.total_size(files)
    log.info("复用已整理的工作区 %s：%s（%s）", work, folder, core.human_size(size))
    return folder, files, size


# ─────────────────────────────────────────── 云盘归档（不占本地磁盘）
#
# 链路：磁力 → 云盘离线下载 → 落地 → 远端整理 → 记账。
# 与本地链路的关键差别：没有本地文件，所以「整理」是远端的 move/rename/purge，
# 「上传」这一步根本不存在（文件本来就在云盘里）。

def cloud_ready() -> tuple[bool, str]:
    """云添加链路能不能用。返回 (可用, 说明)。**不抛异常**：面板要能直接显示原因。"""
    if not get_setting("CLOUD_INGEST"):
        return False, "未启用「云添加」（设置 → 存储）"
    inbox = str(get_setting("CLOUD_INBOX") or "").strip()
    if not inbox:
        return False, "没配「离线落地目录」"
    try:
        backend = storage.get()
    except Exception as exc:
        return False, f"存储后端不可用：{exc}"
    if backend.kind != "clouddrive2":
        return False, f"云添加只支持 CloudDrive2（当前 {backend.kind}）"
    probe = getattr(backend, "offline_supported", None)
    if probe is None:
        return False, "当前后端没有离线下载能力"
    try:
        ok, detail = probe(inbox)
    except Exception as exc:
        return False, f"探测失败：{exc}"
    return ok, detail


def cloud_capability() -> dict:
    """给面板看：这条链路能不能用、云盘有哪些、落地目录在哪。"""
    ok, detail = cloud_ready()
    info = {"ok": ok, "detail": detail,
            "inbox": str(get_setting("CLOUD_INBOX") or ""),
            "dest": str(get_setting("CLOUD_DEST") or "") or storage.root(),
            "backend": storage.kind(),
            "clouds": [], "error": ""}
    try:
        backend = storage.get()
        if hasattr(backend, "cloud_roots"):
            info["clouds"] = backend.cloud_roots()
    except Exception as exc:
        info["error"] = str(exc)[:200]
    return info


def _cloud_submit(backend, job_id: str, source: str, my_inbox: str) -> dict:
    """提交（或续接）。返回给 wait_landing 用的 {before, inbox}。

    2.1/2.2：同一链接已有提交记录时**不再重复投**。AddOfflineFiles 对假磁力
    也回 success（实测），重投只可能制造重复落地或撞同名目录；沿用提交前
    快照继续观察，落地已完成时 wait_landing 立刻就能收下。
    """
    prev_extra = _store().get(job_id).get("extra") or {}
    prev = prev_extra.get("cloud_submit") or {}
    if prev.get("url") == source and prev.get("inbox") == my_inbox \
            and prev.get("before") is not None:
        job_event(job_id, "stage", f"cloud 续接（有提交记录，不重复提交）到 {my_inbox}")
        return {"before": dict(prev.get("before") or {}), "inbox": my_inbox}
    job_event(job_id, "stage", f"cloud 提交到 {my_inbox}")
    submitted = cloudingest.submit(backend, source, my_inbox)
    job_update(job_id, extra={**prev_extra, "cloud_inbox": my_inbox, "cloud_url": source,
                              "cloud_submit": {"url": source, "inbox": my_inbox,
                                               "before": submitted["before"],
                                               "at": time.time()}})
    return submitted


def xlp_apply_credentials(text: str, kind: str = "") -> dict:
    """把粘来的凭据（一整段 JSON 或裸 token）拆开写进设置。

    浏览器 localStorage 的 `credentials_{clientid}` 就是一段 JSON，粘进来一次填齐：
    access_token / refresh_token / clientid（clientid 从 JWT 的 aud 自动取）。

    kind="cred" / "device" / "captcha" 时只处理那一类（前端是三个独立输入框）。
    """
    try:
        out = xunleipan.apply_credentials(text or "", kind=kind)
    except Exception as exc:                    # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}
    if out.get("error"):
        return {"ok": False, "error": out["error"]}
    # 写完立刻验一次：填错了当场就知道，不用等 12 小时后
    state = xlp_auth_status()
    missing = out.get("missing") or []
    return {"ok": bool(state.get("ok")), "saved": out.get("saved") or [],
            "error": state.get("error") or "", "detail": state.get("detail") or "",
            "expires_in": state.get("expires_in", 0),
            "has_refresh": state.get("has_refresh", False),
            "exchanged": out.get("exchanged") or "",
            "missing": missing}


def xlp_auth_status(refresh: bool = False) -> dict:
    """迅雷云盘登录状态：凭据齐不齐、各项配没配、token 还能用多久。

    refresh=True 时立刻用 refresh_token 续一次（写回设置）。
    账号密码直登**不支持**（迅雷端点回 currently not supported），
    所以首次凭据要从浏览器抄一次，之后靠 refresh_token 自己续。
    """
    parts = {}
    for key in ("XLPAN_CLIENT_ID", "XLPAN_TOKEN", "XLPAN_REFRESH_TOKEN",
                "XLPAN_DEVICE_ID", "XLPAN_CAPTCHA_TOKEN"):
        parts[key] = bool(str(get_setting(key) or "").strip())
    out = {"ok": False, "configured": xunleipan.configured(),
           "has_refresh": parts["XLPAN_REFRESH_TOKEN"],
           "parts": parts,
           "expires_in": 0, "detail": "", "error": ""}
    if not out["configured"]:
        out["detail"] = "缺 clientid 或 access_token"
        return out
    token = str(get_setting("XLPAN_TOKEN") or "")
    exp = xunleipan._token_expiry(token)
    if exp:
        out["expires_in"] = int(exp - time.time())
    if refresh:
        try:
            res = xunleipan.auto_refresh()
            out["ok"] = True
            out["detail"] = ("已续期" if not res.get("skipped") else "未临近过期，无需续期")
            if res.get("rotated"):
                out["detail"] += "（refresh_token 也已轮换并写回）"
            exp = xunleipan._token_expiry(str(get_setting("XLPAN_TOKEN") or ""))
            out["expires_in"] = int(exp - time.time()) if exp else 0
        except Exception as exc:                # noqa: BLE001
            out["error"] = str(exc)[:200]
            return out
    else:
        # 顺带探一次真实接口：只读列表，不带参数
        try:
            xunleipan.list_files("", limit=1)
            out["ok"] = True
            out["detail"] = "凭据可用"
        except Exception as exc:                # noqa: BLE001
            out["error"] = str(exc)[:200]
    return out


def cloud_direct_link(path: str = "", code: str = "") -> dict:
    """取云盘文件的直链：优先迅雷官方 API（外网可播 + 转码档），退回 CD2（内网）。

    官方 API 就是 greasyfork 脚本「迅雷云盘」用的那套（web_content_link +
    medias 各档画质），凭据在设置 → 存储；没配就用 CD2 的
    GetDownloadUrlPath（实测可用，但只在内网有意义）。直链有有效期，不缓存。
    """
    backend = None
    if code:
        state = core.load_state(STATE)
        key = codes.canonical(code) or (code or "").strip().upper()
        record = state.get(key) or {}
        if not record:
            return {"ok": False, "error": f"没有 {key} 的归档记录"}
        try:
            rel = archive_locator(record)
            backend = storage_backend()
            validate_archive_backend(backend, record)
            if path:
                if not path.strip("/").startswith(rel + "/"):
                    return {"ok": False, "error": "文件路径与归档定位不符"}
            else:
                entries = backend.list_files(rel)
                if not entries:
                    return {"ok": False, "error": f"{key} 归档目录是空的"}
                media_ext = (".mp4", ".mkv", ".avi", ".wmv", ".ts", ".mov", ".rmvb", ".iso")
                media = [e for e in entries if Path(e.name).suffix.lower() in media_ext]
                path = f"{rel}/{(media or entries)[0].name}"
        except Exception as exc:
            return {"ok": False, "error": f"列归档目录失败：{str(exc)[:120]}"}
    path = (path or "").strip("/")
    if not path or ".." in path or "\\" in path:
        return {"ok": False, "error": "缺少合法的文件路径"}

    # 1) 迅雷官方 API：外网可播的 web_content_link + 云播转码档
    is_xunlei = path.split("/", 1)[0] == "迅雷云盘"
    official_allowed = is_xunlei and (backend is None or backend.kind == "clouddrive2")
    if official_allowed and xunleipan.configured():
        try:
            info = xunleipan.direct_link(path)
            return {"ok": True, "via": "xunleipan", "path": path,
                    "name": info.get("name") or "", "link": info.get("link") or "",
                    "medias": info.get("medias") or []}
        except Exception as exc:                # noqa: BLE001 退回 CD2
            log.warning("官方 API 取直链失败，退回 CD2：%s", exc)
    # 2) CD2：内网直链（下载路径）
    try:
        backend = backend or storage_backend()
        probe = getattr(backend, "download_url", None)
        link = probe(path) if probe else backend.link(path)
    except Exception as exc:                    # noqa: BLE001
        return {"ok": False, "error": f"取直链失败：{str(exc)[:120]}"}
    if not link:
        return {"ok": False, "error": "这个后端取不到直链（可配迅雷官方 API 或 CD2_LINK_MODE）"}
    return {"ok": True, "via": backend.kind, "path": path,
            "name": path.rsplit("/", 1)[-1], "link": link, "medias": []}


def _cloud_run(job_id: str, source: str, code: str, key: str) -> dict:
    """云添加整条链路（同步执行，由 run_web_job 在线程里调）。"""
    _archive_lease(job_id, code)
    ok, why = cloud_ready()
    if not ok:
        raise RuntimeError(f"云添加不可用：{why}")
    backend = storage.get()
    inbox = str(get_setting("CLOUD_INBOX")).strip().strip("/")
    dest = str(get_setting("CLOUD_DEST") or "").strip().strip("/") or storage.root()
    # 任务专属收件目录：共享 inbox 曾让并发任务互相认领对方的落地目录
    # （各自把「新增目录」当自己的，然后搬走、清掉）。观察范围圈死在自己的
    # 子目录里，别人的落地、用户手动加的目录都进不来。
    my_inbox = cloudingest.task_inbox(inbox, key or job_id)

    job_update(job_id, stage="cloud", percent=0.0, speed=0)
    submitted = _cloud_submit(backend, job_id, source, my_inbox)

    def on_progress(snapshot: dict, rounds: int) -> None:
        require_lease(job_id)
        # 没有百分比：只报「已出现几个文件、多少字节」
        count = sum(int(v.get("count") or 0) for v in snapshot.values())
        total = sum(int(v.get("bytes") or 0) for v in snapshot.values())
        job_update(job_id, downloaded=total, total=total,
                   error="" if not count else f"云端落地中：{count} 个文件")

    async def wait() -> list:
        return await cloudingest.wait_landing(
            backend, my_inbox, submitted["before"],
            poll=float(get_setting("CLOUD_POLL") or 10),
            timeout=float(get_setting("CLOUD_LAND_TIMEOUT") or 7200),
            stable=int(get_setting("CLOUD_LAND_STABLE") or 3),
            progress=on_progress, cancel_check=lambda: cancel_requested(job_id),
            source=source)          # 迅雷解析失败时早点报，别干等两小时

    try:
        landed = asyncio.run(wait())
    except cloudingest.LandingTimeout as exc:
        raise RuntimeError(str(exc)) from exc
    if not landed:
        raise RuntimeError("云添加没有落地任何内容")

    results = []
    pending_cleanup: list[dict] = []             # 归档落盘后再清的源目录
    for item in landed:
        require_lease(job_id, code)              # 整理前确认租约还在自己手里
        job_event(job_id, "stage", f"landed {item.folder}（{item.total} 字节）")
        # 「文件直接躺在收件目录根下」的落地（单文件种子实测如此）：
        # 源目录就是收件目录本身，不是它下面的某个子目录。
        loose = item.folder == cloudingest.LOOSE_KEY
        source = "" if loose else item.folder
        plan = cloudorganize.plan(source, item.files, folder_code=code or "")
        moved = cloudorganize.apply(backend, my_inbox, plan, code or plan.target,
                                    dst_root=dest)
        # 1.5：归档记录自带定位（backend + root + rel），之后的核验/补刮/改名/删除
        # 都按这份记录找目录，不再跟着「当时的全局 STORAGE_ROOT」漂移。
        record = {
            "folder": moved["target"],
            "path": f"{backend.kind}:{dest}/{moved['target']}",
            "backend": backend.kind,
            "root": dest,
            "rel": f"{dest}/{moved['target']}",
            "files": moved["files"],
            "files_detail": [{"name": n, "size": next(
                (int(f.get("size") or 0) for f in item.files if f.get("name") == old), 0)}
                for old, n in zip(plan.move, moved["files"])],
            "size": moved["size"],
            "inbox": my_inbox if loose else f"{my_inbox}/{item.folder}",
        }
        job_update(job_id, extra={**(_store().get(job_id).get("extra") or {}),
                                  "cloud": record})
        results.append(record)
        pending_cleanup.append({"plan": plan, "src_root": my_inbox, "dst_root": dest})
    return {"landed": [x.folder for x in landed], "records": results,
            "inbox": my_inbox, "dest": dest, "_backend": backend,
            # 1.4：清理挪到记账之后，由调用方在归档落盘成功后执行
            "cleanup": [(c["plan"], c["src_root"], c["dst_root"]) for c in pending_cleanup]}


def scrape_attachments(work: Path, folder: str, files: list[Path],
                       job_id: str = "") -> dict:
    """在写盘前刮好元数据并生成附件。失败只登记，绝不阻断视频上传。

    可复用的核心（刮元数据、取封面、写 sidecar、返回上传清单）在
    metadata.build_attachments；这里只负责网页侧独有的部分：读开关、读任务的
    locked/scraped、把结果写回 job extra。

    返回 {"record", "files", "written", "failed", "pending"}：
      pending=True 表示元数据不完整（要补刮），任务状态里会带这个标记。
    """
    if not get_setting("SCRAPE"):
        return {"record": {}, "files": files, "written": [], "failed": [], "pending": False}

    code = workspace.key_of(folder) and (codes.canonical(folder) or folder)
    extra = ((_store().get(job_id) or {}).get("extra") or {}) if job_id else {}
    locked = set(extra.get("locked") or [])

    def scrape(code_: str) -> dict:
        found = asyncio.run(metatube.scrape(code_)) if metatube.configured() else {}
        # 番号对不上就不用：宁可留待补刮，也不把别人的元数据塞进归档
        if found and not metatube.matches(code_, found):
            log.warning("MetaTube 返回 %s 与 %s 不符，丢弃", found.get("code"), code_)
            return {}
        return found

    out = metadata.build_attachments(work, code, files, scrape, locks=locked,
                                     old=extra.get("scraped") or {}, want_nfo=get_setting("SCRAPE_NFO"),
                                     want_poster=get_setting("SCRAPE_POSTER"))
    if job_id:
        job_update(job_id, extra={
            **(_store().get(job_id).get("extra") or {}),
            "scraped": out["record"], "scrape_pending": out["pending"],
            "scrape_failed": out["failed"]})
    return out


def fetch_image(url: str, limit: int = 12 << 20) -> bytes:
    """取封面（实现与 bot 共用 metadata.fetch_image）。"""
    return metadata.fetch_image(url, limit=limit)


def _upload_progress(job_id: str):
    """上传进度回调：把适配器给的 0..1 写成任务的 percent，顺手响应取消。

    限流到约 1 秒一次：CD2 的轮询本身就是秒级，没必要每一跳都打一次库。
    """
    last = [0.0]

    def report(ratio: float, _note: str = "") -> None:
        require_lease(job_id)
        now = time.monotonic()
        if ratio < 1.0 and now - last[0] < 0.8:
            return
        last[0] = now
        try:
            job_update(job_id, stage="uploading",
                       percent=round(max(0.0, min(float(ratio), 1.0)) * 100, 1))
        except Exception:            # 进度只是展示，写库失败不能拖垮上传
            pass

    context = copy_context()
    return lambda *args, **kwargs: context.copy().run(report, *args, **kwargs)


def _upload_loop(job_id: str, work: Path, folder: str, size: int,
                 clock: dict, policy) -> str | None:
    """把工作区推到 Drive，可重试；返回分享链接。"""
    up_policy = retry.RetryPolicy(retries=get_setting("RETRIES"), base_delay=get_setting("RETRY_BASE_DELAY"))
    require_lease(job_id, folder)                # 上传前
    job_event(job_id, "stage", f"uploading {folder}")
    while True:
        require_lease(job_id, folder)            # 每次重试前
        job_update(job_id, stage="uploading", folder=folder, size=size, percent=100.0,
                   retries=policy.attempts + up_policy.attempts)
        phase = time.monotonic()
        _UPLOAD_PROGRESS.fn = _upload_progress(job_id)
        try:
            link, _ = upload_folder(work, folder)
            clock["upload"] += time.monotonic() - phase
            policy.history.extend(up_policy.history)
            return link
        except Exception as exc:
            clock["upload"] += time.monotonic() - phase
            if not up_policy.can_retry(str(exc)):
                raise
            up_policy.record(str(exc))
            wait = round(up_policy.delay(error=str(exc)), 1)
            log.warning("上传失败，%s 秒后重试（第 %d 次）：%s", wait, up_policy.attempts, exc)
            job_update(job_id, stage="uploading", error=f"{exc}｜{wait}s 后重试",
                       retries=policy.attempts + up_policy.attempts)
            job_event(job_id, "retry",
                      f"上传第 {up_policy.attempts} 次 · {wait}s 后重试 · {_first_line(exc)}")
            if not retry.backoff_sleep(up_policy, cancel_check=lambda: cancel_requested(job_id),
                                       error=str(exc)):
                raise Cancelled("已停止")
        finally:
            _UPLOAD_PROGRESS.fn = None


def _finish_cloud(job_id: str, source: str, out: dict) -> None:
    """云添加落地的记账：不做上传、不复制文件，只把远端结果写进归档表。

    与 `_finish` 的字段契约保持一致（`metadata.archive_record`），只是 files
    来自远端清单、path 是云盘路径。刮削附件这一步跳过：附件要写进云盘目录，
    云添加链路先不做（下一步再做，单独可回滚）。
    """
    records = out.get("records") or []
    if not records:
        raise RuntimeError("云添加没有产出可归档的目录")
    for rec in records:
        folder = rec["folder"]
        _archive_lease(job_id, folder)
        names = list(rec.get("files") or [])
        size = int(rec.get("size") or 0)
        try:
            info = asyncio.run(meta.lookup(folder))
        except Exception as exc:                # 元数据只是锦上添花
            log.warning("查 %s 的元数据失败：%s", folder, exc)
            info = None
        record = metadata.archive_record(
            folder, rec["path"], "", names, size, source,
            Path("/nonexistent-cloud"),          # 没有本地工作区，清单按远端回填
            scraped=info or {},
            manifest_files=names,
            pending=True,                        # 附件没推，标成待补
        )
        # 云端没有本地文件可 stat：清单大小直接用落地时读到的那份
        by_size = {str(f.get("name")): int(f.get("size") or 0)
                   for f in (rec.get("files_detail") or [])}
        if by_size:
            for entry in (record.get("manifest") or {}).get("files") or []:
                if entry.get("name") in by_size:
                    entry["size"] = by_size[entry["name"]]
            record["manifest"]["total_size"] = sum(
                int(e.get("size") or 0) for e in record["manifest"]["files"])
        record.update({k: rec[k] for k in ("backend", "root", "rel") if k in rec})
        record["cloud_inbox"] = rec.get("inbox") or ""
        _archive_lease(job_id, folder)
        core.update_state(STATE, {folder: record})
        job_event(job_id, "done", f"{folder} · {core.human_size(size)} · 云盘")
    last = records[-1]
    extra = {**(_store().get(job_id).get("extra") or {}), "cloud": last}
    extra.pop("cloud_submit", None)          # 成功后提交记录退场，重跑时可以重新投
    if not job_update(job_id, stage="done", code=last["folder"], folder=last["folder"],
                      size=int(last.get("size") or 0), link="", error="", resume_from="",
                      extra=extra):
        raise LossOfLease("归档完成状态未写入，本次执行停止")
    notify.job_done(last["folder"], core.human_size(int(last.get("size") or 0)), "")


def _uploaded_location(folder: str, work: Path) -> dict:
    location = getattr(_UPLOAD_PROGRESS, "location", None)
    if getattr(_UPLOAD_PROGRESS, "location_work", None) != str(work):
        location = None
    if location:
        location = dict(location)
        location["rel"] = "/".join([location["root"].strip("/"), folder]).strip("/")
    else:
        kind = storage.kind()
        root = DRIVE_ROOT if kind == "rclone" else storage.root()
        location = {"backend": kind, "root": root, "rel": f"{root}/{folder}"}
        if kind == "rclone":
            location["remote"] = RCLONE_REMOTE
    location["path"] = f"{location.get('remote') or location['backend']}:{location['rel']}"
    return location


def _finish(job_id: str, work: Path, folder: str, files: list[Path], size: int,
            link: str | None, source: str, clock: dict, policy) -> None:
    """记账：元数据查不到也写归档，不能因为刮削失败丢掉已上传的记录。"""
    # 目录名以服务端为准：CloudDrive2 会把 *?:/\<>|" 转全角、削掉尾部空白与点号。
    # 不跟着改的话，记录里的路径在远端根本不存在 —— 之后的校核、改名、删除全对不上。
    require_lease(job_id, folder)
    location = _uploaded_location(folder, work)
    if location["backend"] != "rclone":
        actual_backend = (getattr(_UPLOAD_PROGRESS, "location_backend", None)
                          if getattr(_UPLOAD_PROGRESS, "location_work", None) == str(work) else None)
        if actual_backend is not None:
            probe = getattr(actual_backend, "real_folder", None)
            actual = probe(location["rel"]) if probe else folder
        else:
            actual = storage.real_folder(folder)
        if actual and actual != folder:
            log.info("远端目录名被归一化：%s → %s", folder, actual)
            folder = actual
    require_lease(job_id, folder)                # 写归档前
    info = None
    try:
        info = asyncio.run(meta.lookup(folder))
    except Exception as exc:
        log.warning("查 %s 的元数据失败：%s", folder, exc)
    # 刮削结果在上传前的任务 extra 里（scraped/files/scrape_pending/locked/infohash）：
    # 归档记录要按它补齐，否则之后的 scrape/verify/dup_check 都无据可依。
    try:
        extra = ( _store().get(job_id) or {}).get("extra") or {}
        if isinstance(extra, str):            # 兜底：万一还是序列化过的
            extra = json.loads(extra or "{}")
    except Exception as exc:                  # 记不清就留空，别把记账搞挂
        log.warning("读任务 %s 的刮削记录失败：%s", job_id, exc)
        extra = {}
    from_extra = extra.get("files") or []
    names = ([f.name for f in files] + [str(n) for n in from_extra if n and str(n) not in
                                        [f.name for f in files]])
    # 记录组装与 bot 共用一份实现（字段契约见 metadata.archive_record）。
    # 清单按工作区实际文件生成；文件已不在工作区（如被清理）就保留条目、大小记 0。
    record = metadata.archive_record(
        folder, _uploaded_location(folder, work)["path"], link, names, size, source, work,
        scraped=info or extra.get("scraped") or {},
        manifest_files=names,
        infohash=str(extra.get("infohash") or ""),
        locks=extra.get("locked") or (),
        pending=bool(extra.get("scrape_pending")),
    )
    record.update({k: v for k, v in _uploaded_location(folder, work).items() if k != "path"})
    _archive_lease(job_id, folder)
    core.update_state(STATE, {folder: record})
    if not job_update(job_id, stage="done", link=link or "", code=folder, size=size,
                      retries=policy.attempts, error="", resume_from="",
                      timing={k: round(v, 2) for k, v in clock.items()},
                      throughput=(round(size / clock["upload"] / 1048576, 2) if clock["upload"] else None)):
        raise LossOfLease("归档完成状态未写入，本次执行停止")
    job_event(job_id, "done", " · ".join(x for x in (
        folder, core.human_size(size), link or "") if x))
    notify.job_done(folder, core.human_size(size), link or "")


# 进程身份：租约写在任务记录里，谁持租谁执行；进程重启后 id 变了，
# 旧租约自然会被判为过期（STALE_BEAT）而允许接手。
PROC_ID = f"web-{os.getpid()}-{int(time.time())}"
# 心跳间隔：取租约过期阈值的三分之一，漏两次都不会被误判为失联
HEARTBEAT_INTERVAL = store.STALE_BEAT / 3
# job_id -> 本次执行的租约 token。同一条记录可能被重试/恢复重新领取，
# 每次真正开始执行都换一个新 token，旧线程再 beat/release 就自然打不中。
LEASES: dict[str, str] = {}
# 心跳线程还在跑的任务；beat 失败即置 False，各阶段据此停手
_lease_live: dict[str, bool] = {}
# 心跳线程的唤醒信号：让它不必等满一个 interval 就能退出（lease_end / 换代 / 测试复位）
_lease_wake: dict[str, threading.Event] = {}
_lease_lock = threading.Lock()


def lease_owner(job_id: str) -> str:
    """本次执行对这个任务的租约名（不存在就创建）。

    token 唯一：以前是 f"{PROC_ID}:{job_id}"，同一 job 重领两次字符串完全相同，
    旧线程还能继续 beat，两个执行者就在同一目录上并行。
    """
    with _lease_lock:
        token = LEASES.get(job_id)
        if not token:
            token = f"{PROC_ID}:{job_id}:{uuid4().hex[:12]}"
            LEASES[job_id] = token
        return token


def lease_alive(job_id: str) -> bool:
    """本次执行的租约是否还有效：暂停过心跳（还没起心跳）也算在场。"""
    if not _lease_live.get(job_id, True):
        return False
    owner = execution_owner(job_id)
    if not owner:
        return False
    try:
        return bool(_store().beat(job_id, owner))
    except Exception as exc:      # 库暂时不可用不等于失租，别把任务误杀
        log.warning("检查任务 %s 的租约失败：%s", job_id, exc)
        return False


def lease_begin(job_id: str, token: str | None = None) -> str:
    """开始一次执行，起心跳线程，返回 token。

    token 省略就铸一个新的（本端自己 claim 的路径）；调用方已经用某个 token
    claim_owner 了就把它传进来 —— 领取用的 owner 必须和心跳用的同一个字符串，
    否则第一次 beat 就打空、被误判失租。
    """
    with _lease_lock:
        if token:
            LEASES[job_id] = token
        else:
            LEASES.pop(job_id, None)
    owner = token or lease_owner(job_id)
    _EXECUTION.set((job_id, owner, _store()))
    # 先续一次：顺带把「这次持有的租约真的在库里属于我」验证一遍
    if not _store().beat(job_id, owner):
        log.warning("任务 %s 的租约不在本线程手里，按失租处理", job_id)
        with _lease_lock:
            _lease_live[job_id] = False

    stop = threading.Event()

    def pulse() -> None:
        # 归属校验（generation check）：`_lease_live` 与 `LEASES` 都以 job_id 为键，
        # 同一 job 重新开始执行时会换一个新 token 覆盖同一格。旧线程若只凭标志位
        # 判断，就会在换代之后继续跑，失租时把**新的**那个标志位一起置 False ——
        # 表现为「刚领到租约就被判失租」，任务直接写成 cancelled。
        # 所以每轮先确认「这格 token 还是我铸的那个」，不是就安静退出。
        try:
            while _lease_live.get(job_id, False):
                if stop.wait(HEARTBEAT_INTERVAL):
                    break            # 被 lease_end / 换代 / 复位唤醒，立刻收工
                if not _lease_live.get(job_id, False):
                    break
                with _lease_lock:
                    if LEASES.get(job_id) != owner:
                        break        # 已被新一次执行取代，本线程退场
                try:
                    ok = _store().beat(job_id, owner)
                except Exception as exc:
                    log.warning("任务 %s 续租出错：%s", job_id, exc)
                    continue
                if not ok:
                    with _lease_lock:
                        # 只在自己仍持有这一格时才置失租，避免误杀后来者
                        if LEASES.get(job_id) == owner:
                            _lease_live[job_id] = False
                    log.warning("任务 %s 的租约已被他人持有，停止续租", job_id)
                    break
        finally:
            with _lease_lock:
                if _lease_wake.get(job_id) is stop:
                    _lease_wake.pop(job_id, None)

    with _lease_lock:
        previous = _lease_wake.get(job_id)
        _lease_wake[job_id] = stop
        _lease_live[job_id] = True
    if previous is not None:
        previous.set()               # 同一 job 上一次的心跳线程立刻退场
    threading.Thread(target=pulse, daemon=True, name=f"lease-{job_id}").start()
    return owner


def lease_end(job_id: str) -> None:
    """Release only this execution; never stop a replacement heartbeat."""
    owner = execution_owner(job_id)
    with _lease_lock:
        stop = None
        if owner and LEASES.get(job_id) == owner:
            _lease_live.pop(job_id, None)
            stop = _lease_wake.pop(job_id, None)
            LEASES.pop(job_id, None)
    if stop is not None:
        stop.set()
    if owner:
        try:
            _store().release(job_id, owner)
        except Exception as exc:
            log.warning("释放任务 %s 的租约失败：%s", job_id, exc)


def require_lease(job_id: str, code: str = "") -> None:
    """执行前确认租约仍属于自己；被接管就停手，绝不和另一个执行者抢同一目录。

    失租抛 LossOfLease（不是 Cancelled）：旧线程只退出，不写共享终态。
    """
    if not lease_alive(job_id):
        raise LossOfLease(f"任务 {code or job_id} 已由另一个执行者接手，本线程停止")
    if cancel_requested(job_id):
        raise Cancelled("已停止")


def run_web_job(job_id: str, source: str, code: str) -> None:
    """在独立线程里跑完整流程（下载投给迅雷，上传走 rclone）。

    下载目录（迅雷建）与工作区（我们整理）分开：
    下载原件只读，改名挑主体都在工作区做，失败时原件还在，便于续传。
    """
    _UPLOAD_PROGRESS.location = None
    _UPLOAD_PROGRESS.location_work = None
    _UPLOAD_PROGRESS.location_backend = None
    owner = execution_owner(job_id) or lease_owner(job_id)
    if not _store().beat(job_id, owner):
        log.warning("任务 %s 的租约已被接管，不再启动", job_id)
        return
    job_event(job_id, "claimed", owner)
    # 真正开始执行：沿用领取时的 token 起心跳（同一字符串，否则首跳就打空）
    lease_begin(job_id, owner)
    key = workspace.key_of(code or job_id)
    download_dir = workspace.staging(TMP_ROOT, key)
    work = workspace.workspace(STATE_DIR, key)

    # 云添加：磁力直接进云盘，落地后远端整理 —— 完全不碰本地磁盘与 rclone
    cloud_on, cloud_why = cloud_ready()
    if cloud_on:
        try:
            job_event(job_id, "stage", "cloud 云盘离线下载")
            out = _cloud_run(job_id, source, code, key)
            _finish_cloud(job_id, source, out)
            # 1.4：归档记录已落盘，才允许删源目录（ads 一起消失）。清理失败
            # 不影响任务成功 —— 目录留给重试，job extra 里挂着待清理标记。
            backend_ = out.get("_backend") or storage.get()
            leftover = []
            for plan, src_root, dst_root in out.get("cleanup") or []:
                try:
                    require_lease(job_id, code)
                    receipt = cloudorganize.cleanup_source(backend_, src_root, plan, dst_root)
                    job_event(job_id, "stage", f"cleaned {src_root}/{plan.source}（{receipt}）")
                except Exception as exc:        # noqa: BLE001 清理失败只挂账
                    if isinstance(exc, (LossOfLease, Cancelled)):
                        raise
                    log.warning("任务 %s 清理源目录 %s/%s 失败：%s",
                                job_id, src_root, plan.source, exc)
                    leftover.append({"source": plan.source, "src_root": src_root,
                                     "dst_root": dst_root,
                                     "target": plan.target, "error": str(exc)[:200]})
            if leftover and lease_alive(job_id):
                job_update(job_id, extra={**(_store().get(job_id).get("extra") or {}),
                                          "cleanup_pending": leftover})
                for rec in out.get("records") or []:
                    _archive_lease(job_id, rec["folder"])
                    saved = core.load_state(STATE).get(rec["folder"]) or {}
                    core.update_state(STATE, {rec["folder"]: {**saved, "cleanup_pending": leftover}})
        except Cancelled as exc:
            # 用户主动停止才落终态；失租（LossOfLease）绝不能写共享状态 ——
            # 接管者正在跑，旧线程把它写成 cancelled 等于替别人取消任务
            job_update(job_id, stage="cancelled", error="已停止")
            job_event(job_id, "cancelled", _first_line(str(exc)) or "已停止")
        except LossOfLease as exc:
            log.info("云添加任务 %s 失租，旧线程安静退出：%s", job_id, exc)
            job_event(job_id, "lease_lost", _first_line(str(exc)))
        except Exception as exc:                # noqa: BLE001 失败要落到任务状态上
            log.exception("云添加任务失败")
            if not lease_alive(job_id):
                return
            job_update(job_id, stage="failed", error=str(exc)[:300])
            job_event(job_id, "failed", _first_line(str(exc)))
            if retry.classify(str(exc)) == retry.QUOTA:
                until = core.next_quota_retry()
                _store().defer(job_id, until, str(exc)[:200], owner=execution_owner(job_id))
        finally:
            _UPLOAD_PROGRESS.location = None
            _UPLOAD_PROGRESS.location_work = None
            _UPLOAD_PROGRESS.location_backend = None
            lease_end(job_id)
        return

    async def main() -> dict:
        xl = xunlei_client()
        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
            job_update(job_id, stage="downloading", percent=0.0, speed=0)
            job_event(job_id, "stage", "downloading 投给迅雷")

            async def on_progress(percent, speed, status):
                require_lease(job_id)
                # 迅雷回传的是真实百分比；体积只用于显示
                total = int((status or {}).get("file_size") or 0)
                done = int(((status or {}).get("params") or {}).get("checked_size") or 0)
                job_update(job_id, percent=round(min(percent, 1.0) * 100, 1),
                           speed=speed or 0, downloaded=done, total=total)

            # 记下迅雷任务 id 与归属：停止时只删我们自己的任务
            def remember(task_id: str, meta: dict | None = None) -> None:
                job_update(job_id, xunlei_task=task_id, xunlei_ours=not (meta or {}).get("reused"))

            try:
                # 带上本任务上次投递的证据：重启后能续接自己的迅雷任务，
                # 而不是把自己的任务误判成「别人提交的」直接报错
                stored = _store().get(job_id)
                return await pipeline.download(
                    xl, client, source, download_dir, progress=on_progress,
                    poll=XUNLEI_POLL, timeout=get_setting("MAGNET_TIMEOUT"),
                    stall_timeout=get_setting("STALL_TIMEOUT"), settle=XUNLEI_SETTLE,
                    on_task=remember,
                    session={"task": str(stored.get("xunlei_task") or ""),
                             "infohash": str((stored.get("extra") or {}).get("infohash") or "")},
                )
            except Cancelled:
                task_id = (JOBS.get(job_id) or {}).get("xunlei_task")
                ours = (JOBS.get(job_id) or {}).get("xunlei_ours", True)
                if task_id and ours and lease_alive(job_id):
                    try:
                        await xl.remove(client, task_id)
                    except Exception as exc:      # 清不掉也别把停止流程搞崩
                        log.warning("停止时删除迅雷任务 %s 失败：%s", task_id, exc)
                raise

    started = time.monotonic()
    clock = {"download": 0.0, "organize": 0.0, "upload": 0.0}
    done_code, done_size = code, 0
    uploaded = False
    policy = retry.RetryPolicy(retries=get_setting("RETRIES"), base_delay=get_setting("RETRY_BASE_DELAY"))
    try:
        # ── 断点续跑：整理已完成、只剩上传时，直接复用工作区，不重下也不重拷。
        # 以前整理/上传中断一律重跑整条流程，等于把一个已下好的大文件再抄一遍。
        resumed = _resume_stage(_store().get(job_id), work, key)
        if resumed:
            folder, files, size = resumed
            done_code, done_size = folder, size
            require_lease(job_id, code)          # downloading→uploading 前
            job_update(job_id, stage="uploading", folder=folder, size=size, error="")
            job_event(job_id, "resumed", f"整理已完成，直接从上传续跑：{folder}")
            link = _upload_loop(job_id, work, folder, size, clock, policy)
            _finish(job_id, work, folder, files, size, link, source, clock, policy)
            uploaded = True
            return

        # ── 下载（可重试）：BT 断流、tracker 抖动都很常见，重试往往能续上。
        # 重试前不删目录：残片能续传，删了就得从零再来。
        result: dict = {}
        while True:
            phase = time.monotonic()
            try:
                result = asyncio.run(main()) or {}
                clock["download"] += time.monotonic() - phase
                break
            except Cancelled:
                raise
            except Exception as exc:
                clock["download"] += time.monotonic() - phase
                if not policy.can_retry(str(exc)):
                    raise
                policy.record(str(exc))
                wait = round(policy.delay(error=str(exc)), 1)
                kind = retry.classify(str(exc))
                log.warning("下载失败（%s），%s 秒后重试（第 %d 次）：%s",
                            kind, wait, policy.attempts, exc)
                job_update(job_id, stage="downloading", retries=policy.attempts,
                           error=f"{exc}｜{wait}s 后重试", percent=0.0, speed=0)
                job_event(job_id, "retry",
                          f"下载第 {policy.attempts} 次 · {wait}s 后重试 · {_first_line(exc)}")
                if not retry.backoff_sleep(policy, cancel_check=lambda: cancel_requested(job_id),
                                           error=str(exc)):
                    raise Cancelled("已停止")

        if cancel_requested(job_id):
            raise Cancelled("已停止")
        require_lease(job_id, code)              # downloading→organizing 前

        job_update(job_id, stage="organizing")
        job_event(job_id, "stage", "organizing 整理文件名与目录")
        phase = time.monotonic()
        # 整理在工作区做：下载原件保持不动，失败还能重来
        source_dir: Path = result.get("dir") or download_dir
        # 归属标记由下载器在确认落盘时补（迅雷重名改成 <键>-N 的情形）
        job_update(job_id, keep_dir=str(source_dir))
        require_lease(job_id, code)
        workspace.drop_workspace(work, key, "整理前清空")
        pipeline.stage_copy(source_dir, work, key)
        names = " ".join(p.name for p in work.glob("*"))
        folder, files = pipeline.organize(work, code or codes.detect(names) or "")
        size = pipeline.total_size(files)
        done_code, done_size = folder, size
        clock["organize"] = time.monotonic() - phase
        # 整理完成即落「从哪一步续跑」的证据：文件名清单 + 体积。
        # 光靠 stage=uploading 不可靠 —— 入队会把 stage 改回 queued，重启后就只知道「排队中」。
        job_update(job_id, resume_from="upload", folder=folder, size=size,
                   total=size, extra={**_store().get(job_id).get("extra", {}),
                                      "files": [f.name for f in files]})

        if get_setting("DAILY_UPLOAD_GB") and size > get_setting("DAILY_UPLOAD_GB") * 1024**3:
            raise RuntimeError(f"单任务 {core.human_size(size)} 超过 DAILY_UPLOAD_GB 限制")

        # ── 刮削并生成附件（失败不阻断）：NFO/JSON/封面/清单跟着视频一起上去
        scraped = scrape_attachments(work, folder, files, job_id)
        if scraped["written"]:
            job_update(job_id, extra={**(_store().get(job_id).get("extra") or {}),
                                      "files": [f.name for f in scraped["files"]]})

        # ── 上传（可重试）：网络中断、Drive 5xx 都可以再推一次
        require_lease(job_id, code)              # organizing→uploading 前
        link = _upload_loop(job_id, work, folder, size, clock, policy)
        _finish(job_id, work, folder, files, size, link, source, clock, policy)
        uploaded = True
    except Cancelled as exc:
        log.info("任务已停止：%s", job_id)
        # 主动停止是独立终态：用户自己停的，不该混进失败里（也不该计入失败率）
        job_update(job_id, stage="cancelled", error="已停止")
        job_event(job_id, "cancelled", _first_line(str(exc)) or "已停止")
    except LossOfLease as exc:
        # 失租只退出：接管者持租在跑，这里写 cancelled/failed 会覆盖它的状态，
        # 「清空已完成」还会把仍在跑的记录删掉。留一条事件供排查。
        log.info("任务 %s 失租，旧线程安静退出：%s", job_id, exc)
        job_event(job_id, "lease_lost", _first_line(str(exc)))
    except Exception as exc:
        log.exception("网页任务失败")
        if not lease_alive(job_id):
            return
        if retry.classify(str(exc)) == retry.QUOTA:
            # 配额不是失败，是等待：定到次日 00:10（本地时区）自动重跑
            until = core.next_quota_retry()
            note = f"额度用尽，{time.strftime('%m-%d %H:%M', time.localtime(until))} 自动重试"
            _store().defer(job_id, until, f"{note}：{str(exc)[:150]}", owner=execution_owner(job_id))
            job_update(job_id, stage="deferred", error=note)
            _event_deferred(job_id, until, note, str(exc))
            notify.job_deferred(code or job_id, until, str(exc)[:150])
        else:
            job_update(job_id, stage="failed", error=str(exc)[:300])
            job_event(job_id, "failed", _first_line(str(exc)))
            notify.job_failed(code or job_id, str(exc)[:300])
    finally:
        outcome = "ok" if uploaded else ("download_fail" if not clock["download"] else "upload_fail")
        record_timing(job_id, done_code, done_size, clock, outcome, started)
        with _jobs_lock:
            if lease_alive(job_id):
                CANCEL.pop(job_id, None)
        try:
            _store().clear_cancel(job_id, owner=execution_owner(job_id))       # 抹掉库里的取消标记，重试不会被卡住
        except Exception as exc:                # noqa: BLE001 收尾不抛
            log.warning("清取消标记失败：%s", exc)
        # 清理用迅雷上报的真实目录（重名时是 <键>-N），不是提交时预设的那个；
        # drop() 只删带本任务标记的目录，相邻的外部目录不受影响
        stored = _store().get(job_id)
        real_dir = Path(stored.get("keep_dir") or "") if stored.get("keep_dir") else None
        # 清理前必须确认目录仍归自己：失租时另一个执行者可能正在用同一目录
        if uploaded and lease_alive(job_id):
            # 只有全流程成功才删：失败留着原件，重试与人工排查都要用
            workspace.drop_workspace(work, key, "上传成功")
            if lease_alive(job_id):
                workspace.drop(download_dir, key, "上传成功")
            if real_dir and real_dir != download_dir and lease_alive(job_id):
                workspace.drop(real_dir, key, "上传成功")
        elif uploaded:
            log.warning("任务 %s 已失租，跳过目录清理：%s %s", job_id, work, download_dir)
        elif lease_alive(job_id):
            job_update(job_id, kept=str(real_dir or download_dir))
        _UPLOAD_PROGRESS.location = None
        _UPLOAD_PROGRESS.location_work = None
        _UPLOAD_PROGRESS.location_backend = None
        lease_end(job_id)


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
    """同步查磁力元信息（网页/文档预览用）。按 infohash 缓存，失败也记一笔。

    whatslink 一条要几秒；同一条磁力会被搜索、详情、提交前各看一遍，
    不缓存等于把同一份请求打三遍。
    """
    from . import whatslink

    cache = preview.PreviewCache(PREVIEW_DB)
    hit = cache.get(magnet)
    if hit is not None:
        return hit or None

    async def main():
        async with httpx.AsyncClient(follow_redirects=True) as client:
            return await whatslink.lookup(client, magnet)

    try:
        info = asyncio.run(main())
    except Exception as exc:
        log.warning("whatslink 预览失败：%s", exc)
        info = None
    cache.put(magnet, info)
    return info


# 等着被放行的任务：job_id -> (source, code)。放行后从队列移除。
WAITING: dict[str, tuple[str, str]] = {}
_waiting_lock = threading.Lock()
# 任务什么时候开始排队的，用于 DISK_QUEUE_TIMEOUT
_queued_at: dict[str, float] = {}

def _pending_jobs() -> list[dict]:
    """正在占用下载槽的任务快照，算已占用的空间额度用。

    以**任务表**为准而不是内存镜像：重启后内存是空的，以前这里会返回空表，
    结果一批恢复任务同时判定「盘还够」全部放行，把盘写满。
    """
    try:
        jobs = _store().unfinished()
    except Exception as exc:              # 库坏了也不能让调度线程直接死
        log.warning("读取任务表失败，退回内存快照：%s", exc)
        with _jobs_lock:
            return [dict(j) for j in JOBS.values()
                    if j.get("stage") in store.RUNNING]
    if jobs:
        return jobs
    with _jobs_lock:
        return [dict(j) for j in JOBS.values()
                if j.get("stage") in store.RUNNING]

def admit(job_id: str) -> bool:
    """现在放得下吗？放得下就从队列里拿掉、记上额度、启动线程。

    **所有起线程的路径都必须经过这里**，所以「现在允许开工吗」也在这里判一次：
    以前只有 pump_queue / _defer_loop / _subs_loop 自己查暂停，requeue() 直达
    admit()，恢复窗口里新提交/重新入队的任务照样开下载。闸门关着时任务留在
    WAITING 里等 pump_queue 放行，不丢、也不落成终态。
    """
    if not scheduling_open():
        return False
    with _waiting_lock:
        item = WAITING.get(job_id)
        if item is None:
            return False
        with _jobs_lock:
            job = dict(JOBS.get(job_id) or {})
        stored = _store().get(job_id) or {}
        # 体积以任务表为准：内存里可能还没补上（重启后直接入队的就是这种）
        need = int(stored.get("total") or job.get("total") or 0)
        if not need:
            need = _guess_size(stored.get("code") or job.get("code") or "")
        reserve = int(get_setting("DISK_RESERVE_GB") * diskspace.GB)
        free = diskspace.free_bytes(TMP_ROOT)
        # 已经许给「已在下载」的任务的额度要先扣掉，否则几个任务会同时
        # 判定「放得下」然后一起开下，合起来把盘吃穿。排队中的不算，
        # 否则队列一长额度就被还没开始的任务吃光，队头永远轮不到。
        already = diskspace.reserved_by(_pending_jobs())
        usable = None if free is None else free - max(0, already)
        if not diskspace.fits(need, usable, reserve):
            return False
        source, code = item
        token = f"{PROC_ID}:{job_id}:{uuid4().hex[:12]}"
        if not _store().claim_owner(job_id, token):
            job_event(job_id, "busy", "租约已被其他执行者持有，交还队列等下一轮")
            return False
        # Reserve disk budget under the same admission lock as the capacity check.
        _store().update_cas(job_id, token, stage="downloading", error="")
        WAITING.pop(job_id, None)
        _queued_at.pop(job_id, None)
        with _lease_lock:
            LEASES[job_id] = token
        with _jobs_lock:
            JOBS.setdefault(job_id, {}).update(
                {"stage": "downloading", "error": "", "queued_position": 0})
    job_event(job_id, "claimed", token)

    def work() -> None:
        _remember_thread(job_id)
        _EXECUTION.set((job_id, token, _store()))
        try:
            run_web_job(job_id, source, code)
        except Exception:
            # run_web_job 自己处理业务异常；走到这里的是它没兜住的那类（例如
            # 线程收尾阶段库已换、状态读不出来）。绝不能让异常逃出线程 ——
            # 那样只剩一条 PytestUnhandledThreadExceptionWarning，线上则是
            # 线程无声无息地死掉，任务停在中间状态却没人知道。
            log.exception("任务 %s 的后台线程异常退出", job_id)
            try:
                job_update(job_id, stage="failed", error="后台线程异常退出，详见服务日志")
                job_event(job_id, "failed", "后台线程异常退出")
            except Exception:
                log.exception("任务 %s 的失败状态也没能落库", job_id)
        finally:
            with _threads_lock:
                _threads.pop(job_id, None)

    threading.Thread(target=work, daemon=True, name=f"job-{job_id}").start()
    return True

def pump_queue() -> int:
    """把队列里放得下的任务依次放行，返回放行数量。

    按提交顺序处理：先来的先拿空间，避免大任务一直插队把小任务饿死。

    暂停或待对账时一律不放行：恢复后的库是旧快照，直接开工会按旧状态重下。
    """
    if not scheduling_open():
        return 0
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
    reserve_gb = get_setting("DISK_RESERVE_GB")
    for idx, job_id in enumerate(order, start=1):
        with _jobs_lock:
            job = dict(JOBS.get(job_id) or {})
        stored = _store().get(job_id) or {}
        # 体积以任务表为准：内存里可能还没补上（重启后直接入队的就是这种）
        need = int(stored.get("total") or job.get("total") or 0)
        if not need:
            need = _guess_size(stored.get("code") or job.get("code") or "")
        if free is None:
            hint = "等待磁盘空间确认"
        else:
            short = need + int(reserve_gb * diskspace.GB) - free
            hint = ("等待磁盘空间" if short <= 0
                    else f"等待磁盘空间（还差 {core.human_size(max(0, short))}）")
        # 排队位置是瞬时状态，只放内存镜像：每轮都写库既没必要，也会把库撑大
        if _store().update_cas(job_id, "", stage="queued", error=hint):
            with _jobs_lock:
                JOBS.setdefault(job_id, {}).update(
                    {"stage": "queued", "queued_position": idx, "error": hint})

_last_clean = 0.0

def requeue(job_id: str, source: str, code: str, stage: str = "queued") -> bool:
    """把已有任务重新放回磁盘闸门的队列（重启恢复、延期到期、手动重试都走这里）。

    以前这几条路径直接起线程，绕过了 admit() 的空间判断，恢复一批任务会把盘写满。
    """
    job = _store().get(job_id)
    if not job:
        return False
    if job.get("exec_owner") and float(job.get("exec_beat") or 0) >= time.time() - store.STALE_BEAT:
        return False
    if not int(job.get("total") or 0) and code:
        # 体积未知就按同系列历史中位数估，否则闸门把 need 当 0 直接放行
        guess = series_median(code, core.load_state(STATE))
        if guess:
            _store().update_cas(job_id, "", total=guess)
    if _store().update_cas(job_id, "", stage=stage, error=""):
        with _jobs_lock:
            JOBS.setdefault(job_id, {"id": job_id}).update(
                {"id": job_id, "stage": stage, "code": code, "source": source})
    with _waiting_lock:
        WAITING[job_id] = (source, code)
        _queued_at[job_id] = time.time()
    job_event(job_id, "queued", f"重新排队（{stage}）" + (f" · {code}" if code else ""))
    # 闸门关着（暂停/待对账）就只排队：admit 会拒绝起线程，任务留在 WAITING 等
    # pump_queue 放开后放行。不能落成终态，也不能丢，否则恢复确认后就永远没人接了。
    if admit(job_id):
        return True
    _refresh_queue_positions()
    if not scheduling_open():
        job_event(job_id, "held", "调度闸门关闭，任务留在队列等放行")
    return True


def stop_job(job_id: str) -> bool:
    """停掉还在排队的任务：从待执行队列摘掉并落 cancelled 终态。

    以前只对「有线程在跑」的任务有效：排队中的任务没有线程，点了停止什么也不发生，
    空间够了它照样开下 —— 用户以为已经取消了。

    先摘队列、再写终态：admit() 只认 WAITING 里的条目，摘掉之后它就拿不到这个
    任务了。反过来的顺序（先写 cancelled 再摘）中间那一瞬 admit 仍会把线程起来，
    形成「已取消却又在下载」的竞态。

    任务不在本进程（bot 在跑）时，改为写跨进程取消标记：执行者在下一个
    阶段边界消费它并正常收尾。以前这种点下来什么也不发生。
    """
    with _waiting_lock:
        item = WAITING.pop(job_id, None)
        _queued_at.pop(job_id, None)
    if item is None:
        # 已经不在队列：只有「确实有线程在跑」才算停止成功（已经落 cancelled 的
        # 重复停止仍返回 False）。否则就是点了个已经在跑的任务，让它自停。
        if job_id not in running_threads():
            stored = {}
            try:
                stored = _store().get(job_id) or {}
            except Exception as exc:            # noqa: BLE001 库不可用时走内存判断
                log.warning("查任务 %s 失败：%s", job_id, exc)
            if stored.get("stage") == "queued":
                # 库里排队、本进程却不在 WAITING（重启后丢失）：没人会再跑它，
                # 直接落终态，不能只记标记干等一个不存在的执行者
                if not _store().update_cas(job_id, "", stage="cancelled", error="已停止"):
                    return request_cancel(job_id)
                job_event(job_id, "cancelled", "排队中被用户停止")
                return True
            # 在跑（本进程或别的进程）：写取消标记让执行者自停
            return request_cancel(job_id)
        revoked = request_cancel(job_id)
        if revoked:
            job_event(job_id, "cancelled", "正在开工的任务被用户停止")
        return revoked
    if not _store().update_cas(job_id, "", stage="cancelled", error="已停止"):
        return request_cancel(job_id)
    with _jobs_lock:
        JOBS.setdefault(job_id, {"id": job_id}).update({"stage": "cancelled", "error": "已停止"})
    job_event(job_id, "cancelled", "排队中被用户停止")
    log.info("排队中的任务已停止：%s", job_id)
    return True


def _defer_loop() -> None:
    """定时把到点的延期任务重新入队。"""
    while True:
        try:
            if not scheduling_open():
                time.sleep(60)
                continue
            for job in _store().due():
                # 原子领取：谁先把它从 deferred 转成 queued，谁负责入队
                try:
                    if not _store().claim_due(job["id"]):
                        continue
                    log.info("延期任务 %s 到期，重新入队（%s）", job["id"], job["code"])
                    requeue(job["id"], job["source"], job["code"])
                except Exception as exc:      # 单条出错不能拖住其余延期任务
                    log.warning("延期任务 %s 入队失败：%s", job["id"], exc)
        except Exception as exc:
            log.warning("处理延期任务失败：%s", exc)
        time.sleep(60)


def requeue_unfinished(reason: str = "进程重启") -> dict:
    """把库里未完成的任务逐个重新入队，返回 {requeued, failed, held}。

    重启恢复与「对账确认后」共用这一份：后者以前只清标记、放开暂停就返回，
    而 _resume_loop 是启动时的一次性线程，之后再没有人 recover —— 恢复出来的
    任务永远停在 queued 里不跑（E3）。单条失败只记失败数，不拖住其余任务。

    held = 已入队但被闸门拦住（停在 WAITING 等 pump_queue），不算失败。
    """
    out = {"candidates": 0, "requeued": 0, "failed": 0, "held": 0, "errors": []}
    try:
        todo = recover_jobs()
    except Exception as exc:
        out["errors"].append(f"读取未完成任务失败：{str(exc)[:150]}")
        return out
    out["candidates"] = len(todo)
    for job in todo:
        try:
            # 整理/上传中断的也重跑整条流程：下载会因 infohash 复用直接命中
            job_event(job["id"], "resumed",
                      f"{reason}，重新入队（原阶段 {job.get('stage') or '未知'}）")
            requeue(job["id"], job["source"], job["code"])
        except Exception as exc:     # 单条坏掉不能让整批恢复中断
            out["failed"] += 1
            out["errors"].append(f"{job.get('id')}：{str(exc)[:120]}")
            log.warning("恢复任务 %s 入队失败：%s", job.get("id"), exc)
            continue
        out["requeued"] += 1
        if job["id"] in WAITING:
            out["held"] += 1
    return out


def _resume_loop() -> None:
    """重启后把未完成任务重新入队（保留原 job_id 与迅雷任务 id）。"""
    if reconcile_pending():
        # 恢复后先对账：库里可能是备份里的旧状态，未完成任务要人工确认后再放行
        log.warning("当前库处于待对账状态（reconcile_required），跳过重启恢复")
        return
    out = requeue_unfinished()
    if out["requeued"] or out["failed"] or out["errors"]:
        log.info("重启恢复：入队 %d 个（其中 %d 个排队等空间），失败 %d 个",
                 out["requeued"], out["held"], out["failed"])

def _panel_clean_tick() -> None:
    """每天一次清掉面板里过期的失败任务记录（PANEL_CLEAN_DAYS>0 才启用）。"""
    global _last_clean
    pcd = get_setting("PANEL_CLEAN_DAYS")
    if pcd <= 0 or time.time() - _last_clean < 86400:
        return
    _last_clean = time.time()
    try:
        out = clean_panel_errors(pcd, confirm=True)
        if out.get("deleted"):
            log.info("清理面板失败任务 %d 条", len(out["deleted"]))
    except Exception as exc:
        log.warning("清理面板失败任务出错：%s", exc)

# ─────────────────────────────────────────── 每日 tick
#
# 每天做四件以前只有人能手动触发的事：
#   · daily_digest()        今日汇总（完成/失败/额度等待/待办/待补刮）
#   · notify_long_pending()  长期等不到资源的番号（按 since 算天数，去重）
#   · Store.prune()         清理旧任务与事件行（任务表只增不减）
#   · backlog.prune()       清理待办里的老记录（pending 永不删）
#
# 「一天只跑一次」由两道保险保证：
#   1. 记住上次跑过的日期（state 表里的 daily_tick_day），跨天才触发；
#      状态写在库里 → 进程重启也不会把当天的汇总重跑一遍。
#   2. notify 自身还有一层按事件键的去重（notify._SENT），两层不冲突：
#      第一层管「今天这轮跑不跑」，第二层管「同一条消息发不发」。

DAILY_TICK_KEY = "daily_tick_day"


def _daily_tick(now: float | None = None) -> dict:
    """跑一次每日任务。返回本次做了什么，便于测试与排查。

    **一天只跑一次**：日期记在任务库的 state 表里，跨天才再跑。进程重启后
    读到的还是今天，于是不会把当天的汇总重发一遍（notify 那层去重只管 30 分钟
    窗口，重启后早就过期，靠不住）。
    """
    now = time.time() if now is None else now
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    try:
        last = _store().state_get(DAILY_TICK_KEY).get("day") or ""
    except Exception as exc:
        # 读不出来就不能确定今天是不是已经跑过 —— 此时宁可今天不跑，
        # 也不能一天发好几次汇总（发第二遍是骚扰，漏一次只是少一条消息）。
        log.warning("读每日 tick 状态失败，今天先不跑：%s", exc)
        return {"ok": False, "skipped": True, "reason": "读状态失败"}
    if last == day:
        return {"ok": True, "skipped": True, "day": day, "reason": "今天已跑过"}

    out: dict = {"ok": True, "skipped": False, "day": day}
    # 先发消息再记日期：发失败就当作没跑过，明天还会重试，不会静默漏掉。
    # 反过来先记日期再发，一旦发失败这一天就永远补不上了。
    try:
        digest = daily_digest(now)
        out["digest"] = digest
    except Exception as exc:
        log.warning("每日汇总出错：%s", exc)
        out["digest_error"] = str(exc)[:200]
    try:
        out["long_pending"] = len(notify_long_pending(now=now))
    except Exception as exc:
        log.warning("长期未达提醒出错：%s", exc)
        out["long_pending_error"] = str(exc)[:200]
    try:
        out["jobs_pruned"] = _store().prune(days=DAILY_PRUNE_DAYS)
    except Exception as exc:
        log.warning("清理旧任务出错：%s", exc)
        out["prune_error"] = str(exc)[:200]
    try:
        todo = backlog_load()
        removed = backlog.prune(todo, days=DAILY_BACKLOG_PRUNE_DAYS,
                                keep=DAILY_BACKLOG_KEEP)
        if removed:
            # 走 subs_save 那条路：待办存在任务库的 state 表里，要写回去
            subs_save(subs_load(), todo)
        out["backlog_pruned"] = removed
    except Exception as exc:
        log.warning("清理待办老记录出错：%s", exc)
        out["backlog_prune_error"] = str(exc)[:200]

    try:
        _store().state_put({DAILY_TICK_KEY: {"day": day,
                                            "at": time.strftime("%Y-%m-%d %H:%M:%S")}})
    except Exception as exc:
        # 记不住日期 = 明天会再跑一遍（重复一条汇总），但不会丢消息。
        # 这比记住了却没发出去好。
        log.warning("记录每日 tick 日期失败：%s", exc)
        out["state_error"] = str(exc)[:200]
    log.info("每日 tick %s：汇总已发，清理旧任务 %s 条、待办 %s 条",
             day, out.get("jobs_pruned"), out.get("backlog_pruned"))
    return out

def _queue_loop() -> None:
    """后台调度：定期重算空间，够一个就放一个。"""
    while True:
        try:
            _panel_clean_tick()
            try:
                _daily_tick()
            except Exception:
                # 每日 tick 是「顺便做」的事，绝不能因为它出错就跳过本轮调度
                log.exception("每日 tick 出错")
            pump_queue()
            dq = get_setting("DISK_QUEUE_TIMEOUT")
            if dq:
                now = time.time()
                with _waiting_lock:
                    expired = [j for j, t in _queued_at.items()
                               if now - t > dq]
                    for j in expired:
                        WAITING.pop(j, None)
                        _queued_at.pop(j, None)
                for job_id in expired:
                    job_update(job_id, stage="failed",
                               error=f"排队超过 {int(dq)}s 仍未等到磁盘空间")
                    job_event(job_id, "failed",
                              f"排队超过 {int(dq)}s 仍未等到磁盘空间")
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
    """登记任务并尝试放行。同番号已有未结束任务时返回空串（调用方据此回 409）。"""
    job_id = _new_job_id()
    if not _store().start(job_id, code, source[:300], owner="web"):
        return ""
    # 入队事件由 store.start 与登记一起落库（时间戳一致），这里不再重复记
    job_update(job_id, stage="queued", percent=0.0, speed=0)
    btih = xunlei.info_hash_of(source)
    if btih:
        # 记下 infohash 作为「这是本任务投的」的证据，重启后才能续接自己的任务
        job_update(job_id, extra={**_store().get(job_id).get("extra", {}), "infohash": btih})
    if info:
        # name / screenshots 只用于显示，落进 extra（业务字段表里没有它们，
        # 以前写进 name= 直接被过滤掉，界面上永远看不到标题）
        job_update(job_id, total=info.get("size") or 0,
                   extra={**_store().get(job_id).get("extra", {}),
                          "name": info.get("name") or "",
                          "screenshots": info.get("screenshots") or []})
    if not int((JOBS.get(job_id) or {}).get("total") or 0) and code:
        # 体积未知（whatslink 挂了/直链）就按同系列历史中位数估算，
        # 否则磁盘闸门会把 need 当 0 直接放行，一路下到把盘写满
        guess = series_median(code, core.load_state(STATE))
        if guess:
            job_update(job_id, total=guess, estimated=True)
    with _waiting_lock:
        WAITING[job_id] = (source, code)
        _queued_at[job_id] = time.time()
    # 立刻试一次：空间够就不用等下一个调度周期。
    # 闸门关着（暂停/待对账）时 admit 一定拒绝，任务就停在 WAITING 里等放行。
    if admit(job_id):
        return job_id
    _refresh_queue_positions()
    if not scheduling_open():
        job_event(job_id, "held", "调度闸门关闭，任务留在队列等放行")
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


def _migrate_keys(data: dict) -> bool:
    """老订阅键 `kind:id` → `source:kind:id`。返回是否有改动。"""
    changed = False
    for old_key in [k for k in data if k.count(":") == 1]:
        kind, _, sub_id = old_key.partition(":")
        if kind not in ("actor", "series") or not sub_id:
            continue
        record = data.pop(old_key)
        new_key = f"{record.get('source') or 'javdb'}:{kind}:{sub_id}"
        record["key"] = new_key
        # 新键已存在（迁移过一半）就以新键为准，旧记录不覆盖它
        data.setdefault(new_key, record)
        changed = True
    return changed


def subs_snapshot() -> list[dict]:
    out = []
    for key, sub in subs_load().items():
        item = dict(sub)
        item.setdefault("key", key)
        out.append(item)
    return sorted(out, key=lambda d: d.get("added_at") or "", reverse=True)


def add_sub(actor: str, max_per_run: int = 1, max_size_gb: float = 0,
            name: str = "", kind: str = "actor", source: str = "javbus") -> dict:
    """加一个订阅（演员或番号系列）。游标设成今天，只抓之后出的新片，不回填历史。

    source=javbus 时 ID 是站内 ID（演员如 `w5a`）；source=javdb 时是 /actors/xxx。
    两者 ID 空间不同，键带 kind 区分，避免把同一个 ID 当成两种对象。
    """
    if kind not in ("actor", "series"):
        kind = "actor"
    if source not in ("javbus", "javdb"):
        source = "javbus"
    # 链接主机本身能说明站点：和所选来源冲突时以链接为准并告知，
    # 不能默默把 javdb 的 ID 存成 javbus 的（查出来永远是空的）
    from_link = subscribe.source_of(actor)
    if from_link and from_link != source:
        switched = {"javbus": "JavBus", "javdb": "JavDB"}[from_link]
        log.info("订阅链接来自 %s，按链接来源处理（请求的是 %s）", switched, source)
        source = from_link
    sub_id = subscribe.sub_id_of(actor, kind)
    if not sub_id:
        label = "演员" if kind == "actor" else "系列"
        hint = "站内 ID" if source == "javbus" else "javdb 演员页/系列页 ID 或链接"
        return {"ok": False, "error": f"需要一个 {label}{hint}"}
    data = subs_load()
    # 演员与系列、javbus 与 javdb 各是一套 ID 空间，键必须带全，否则会互相覆盖
    key = f"{source}:{kind}:{sub_id}"
    if _migrate_keys(data):
        subs_save(data)                     # 老键一次性迁成新格式
    if key in data:
        return {"ok": False, "error": f"{sub_id} 已经订阅过了"}
    sub = subscribe.new_sub(sub_id, name or sub_id,
                            cursor=time.strftime("%Y-%m-%d"),
                            max_per_run=max_per_run, max_size_gb=max_size_gb,
                            kind=kind, source=source)
    sub["key"] = key
    data[key] = sub
    subs_save(data)
    return {"ok": True, "sub": sub}


def remove_sub(key: str) -> dict:
    data = subs_load()
    key = _resolve_key(data, key)
    if not key:
        return {"ok": False, "error": "订阅不存在"}
    data.pop(key)
    subs_save(data)
    return {"ok": True}


def toggle_sub(key: str, enabled: bool) -> dict:
    data = subs_load()
    key = _resolve_key(data, key)
    if not key:
        return {"ok": False, "error": "订阅不存在"}
    data[key]["enabled"] = bool(enabled)
    subs_save(data)
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


def backlog_view(key: str = "", limit: int = 200) -> dict:
    """待办中心：每条都解释「为什么还没下、什么时候再查、关联哪个任务」。

    以前待办只在订阅卡片上显示一个数字，用户看不出卡在哪、也动不了它。
    """
    todo = backlog_load()
    # 按 job_id 精确查任务，不要用 recent(200) 兜底：recent 取的是**最新 200 条**，
    # 待办里的任务一多（订阅跑久了很容易），老任务就查不到 → 显示成「进行中」，
    # 实际它可能早就失败了。展示层虽不造成重复下载，但会误导用户卡在哪。
    store_ = _store()
    archived = archived_codes()
    rows = []
    keys = [key] if key else list(todo)
    for sub_key in keys:
        for code, item in sorted((todo.get(sub_key) or {}).items()):
            state = item.get("state") or backlog.PENDING
            if state == backlog.QUEUED and code in archived:
                state = backlog.DONE           # 归档完成，展示上以归档为准
            job = store_.get(item.get("job_id") or "") or {}
            next_at = float(item.get("next_at") or 0)
            if state == backlog.PENDING:
                why = (f"{max(0, int((next_at - time.time()) / 60))} 分钟后再查"
                       if next_at > time.time() else "下一轮就会尝试")
            elif state == backlog.QUEUED:
                why = f"任务 {job.get('stage') or '进行中'}" + (
                    f"：{job['error']}" if job.get("error") else "")
            elif state == backlog.SKIPPED:
                why = "已按策略跳过"
            else:
                why = "已归档"
            rows.append({
                "sub": sub_key, "code": code, "state": state,
                "date": item.get("date") or "", "title": item.get("title") or "",
                "tries": int(item.get("tries") or 0), "next_at": next_at,
                "why": why, "note": item.get("note") or "",
                "job_id": item.get("job_id") or "", "job_stage": job.get("stage") or "",
                "tried": list(item.get("tried") or []),
            })
    rows.sort(key=lambda r: (r["state"] != backlog.PENDING, r["date"], r["code"]))
    return {"ok": True, "items": rows[:max(1, int(limit))],
            "counts": {s: sum(1 for r in rows if r["state"] == s)
                       for s in (backlog.PENDING, backlog.QUEUED,
                                 backlog.SKIPPED, backlog.DONE)}}


def retry_backlog(code: str, sub_key: str = "") -> dict:
    """用户手动「再试一次」：把条目拉回 pending 并立刻可查（清掉退避与试过的候选）。"""
    todo = backlog_load()
    target = sub_key or next((k for k, b in todo.items() if code.upper() in b), "")
    if not target:
        return {"ok": False, "error": "待办里没有这个番号"}
    item = (todo.get(target) or {}).get(code.upper())
    if not item:
        return {"ok": False, "error": "待办里没有这个番号"}
    item.update({"state": backlog.PENDING, "tries": 0, "next_at": 0, "note": "手动重试"})
    item.pop("job_id", None)
    subs_save(subs_load(), todo)
    return {"ok": True, "sub": target, "code": code.upper()}


def ignore_backlog(code: str, sub_key: str = "") -> dict:
    """用户主动放弃这一部：标 skipped，不再重试（不是失败，是决定不要）。"""
    todo = backlog_load()
    target = sub_key or next((k for k, b in todo.items() if code.upper() in b), "")
    if not target or not (todo.get(target) or {}).get(code.upper()):
        return {"ok": False, "error": "待办里没有这个番号"}
    backlog.mark(todo, target, code, backlog.SKIPPED, "手动忽略")
    (todo[target])[code.upper()].pop("job_id", None)
    subs_save(subs_load(), todo)
    return {"ok": True, "sub": target, "code": code.upper()}


def wishlist_add(codes_text: str, max_size_gb: float = 0) -> dict:
    """番号愿望单：只按番号，不走关键词搜索。

    与订阅共用「待办 + 调度 + 去重」，不另造一套流程：加进来就是一条待办，
    有磁力就下，没磁力就退避重查，直到归档或用户忽略。
    """
    # 局部变量不能叫 codes：会遮住模块级那个 codes 模块
    parsed = [codes.canonical(t) or t.strip().upper()
              for t in re.split(r"[\s,，;；]+", codes_text or "") if t.strip()]
    found = [c for c in dict.fromkeys(parsed) if c]
    if not found:
        return {"ok": False, "error": "需要至少一个番号"}
    todo = backlog_load()
    bucket = todo.setdefault(WISH_KEY, {})
    added = []
    for code in found:
        if code in bucket:
            continue
        bucket[code] = {"date": "", "title": "", "state": backlog.PENDING,
                        "tries": 0, "next_at": 0, "note": "愿望单",
                        "max_size_gb": max(0.0, float(max_size_gb))}
        added.append(code)
    subs_save(subs_load(), todo)
    return {"ok": True, "added": added, "existing": [c for c in found if c not in added]}


def subs_load() -> dict:
    """读订阅表：以任务库为准；首次升级时从旧 subs.json 导入一次。"""
    data = _store().state_get("subs")
    if data:
        return data
    legacy = subscribe.load_subs(SUBS)
    if legacy:
        _store().state_put({"subs": legacy})
        log.info("订阅表从 %s 导入任务库（%d 条）", SUBS, len(legacy))
    return legacy


def subs_save(data: dict, backlog_data: dict | None = None) -> None:
    """保存订阅；给了 backlog 就同一次事务写，避免「游标前移、待办没落」。"""
    updates = {"subs": data}
    if backlog_data is not None:
        updates["backlog"] = backlog_data
    _store().state_put(updates)


def backlog_load() -> dict:
    data = _store().state_get("backlog")
    if data:
        return data
    legacy = backlog.load(BACKLOG)
    if legacy:
        _store().state_put({"backlog": legacy})
        log.info("发现待办从 %s 导入任务库（%d 个订阅）", BACKLOG, len(legacy))
    return legacy


def run_subscriptions(only: str = "") -> dict:
    """查所有启用的订阅：先发现入待办，再从待办里取轮到的那几部提交下载。

    分两步是有原因的（都是踩过的坑）：
      · 只处理 1 部却把游标推到整批最新 → 中间的作品永久漏掉；
      · 某部一直没有磁力，每轮都卡在队头 → 后面有磁力的永远排不上。
    现在发现结果全部落进 backlog 待办表，提交按「本轮上限」取，没磁力的单独退避。

    落盘顺序：发现结果先进待办，再在**同一个事务**里推进游标 + 写待办。
    以前是两次独立写文件，中间崩一次就是「游标走了、待办没留下」的永久漏订。

    这是后台任务：单个订阅出问题只记在该条目上，不影响其它订阅，也不抛异常。
    """
    if not diskspace_room_ok():
        return {"ok": False, "error": "磁盘空间不足，本轮跳过", "results": []}

    data = subs_load()
    todo = backlog_load()
    today = time.strftime("%Y-%m-%d")
    results = []
    known = known_codes_now()
    only_key = _resolve_key(data, only) if only else ""
    for key, sub in list(data.items()):
        if only and key != only_key:
            continue
        if not sub.get("enabled", True):
            continue
        budget = max(1, int(sub.get("max_per_run") or 1))
        max_gb = float(sub.get("max_size_gb") or 0)
        started, skipped = [], []
        error = ""

        if (sub.get("source") or "javbus") == "javbus":
            # 网络请求放在事务外：抓取可能要十几秒，不能攥着写锁
            found = asyncio.run(subscribe.discover_javbus(
                sub.get("id") or key, sub.get("cursor") or "",
                kind=sub.get("kind") or "actor"))
            error = found["error"]
            if found["items"]:
                backlog.remember(todo, key, found["items"], today)
            data[key]["cursor"] = found["cursor"]
            # 只填了裸 ID 的订阅，补一个能看懂的名字（用户自己起过的不要覆盖）
            if found.get("name") and data[key].get("name") in ("", data[key].get("id")):
                data[key]["name"] = found["name"]
            # 发现结果 + 新游标一次提交：这一步之后崩也不会漏掉这一页
            subs_save(data, todo)

        # 从待办里取候选，凑够 budget 个真正提交的就收手；
        # 队头没磁力只是退避，不占用本轮名额
        for code in backlog.due(todo, key, budget):
            if len(started) >= budget:
                break
            if code in known:
                backlog.mark(todo, key, code, backlog.QUEUED, "已在归档或下载中")
                continue
            item, why = asyncio.run(subscribe.fetch_best(
                code, max_gb, tried=backlog.tried_of(todo, key, code)))
            if not item:
                backlog.mark(todo, key, code, backlog.PENDING, why)
                skipped.append({"code": code, "why": why})
                continue
            record = (todo.get(key) or {}).get(code) or {}
            job_id = start_job(item["magnet"], code,
                               {"name": record.get("title") or code,
                                "size": int(item.get("size_mb") or 0) * 1024 * 1024,
                                "screenshots": []})
            if not job_id:
                backlog.mark(todo, key, code, backlog.PENDING, "任务表里已有同番号任务")
                skipped.append({"code": code, "why": "同番号已在下载"})
                continue
            # 记下 job_id 与这条磁力：下载失败时靠它放回待办并换下一条候选
            backlog.mark(todo, key, code, backlog.QUEUED, f"job {job_id}",
                         job_id=job_id)
            backlog.note_tried(todo, key, code, item["magnet"])
            known.add(code)
            started.append({"code": code, "job": job_id, "size_mb": item["size_mb"]})

        data[key]["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        tally = backlog.counts(todo, key)
        if started:
            last = f"新片 {len(started)} 部，待办 {tally[backlog.PENDING]} 部"
        elif error:
            last = error
        elif skipped:
            last = f"跳过 {len(skipped)} 部（{'；'.join(s['why'] for s in skipped[:2])}）"
        elif tally[backlog.PENDING]:
            last = f"{tally[backlog.PENDING]} 部在等下轮（退避中）"
        else:
            last = "没有新片"
        data[key]["last_result"] = last
        results.append({"key": key, "kind": sub.get("kind") or "actor",
                        "id": sub.get("id") or key,
                        "name": sub.get("name") or key,
                        "started": started, "skipped": skipped, "pending": tally[backlog.PENDING],
                        "error": error})
    # 愿望单：同一张待办表里的独立分组，共用调度与去重，只是没有发现源
    wl_started = _run_wishlist(todo, known)
    results.extend(wl_started)
    reconcile_backlog(todo)
    subs_save(data, todo)
    return {"ok": True, "results": results}


def _run_wishlist(todo: dict, known: set[str], budget: int = 3) -> list[dict]:
    """跑愿望单里的番号。按体积上限逐条判断，没磁力就照常退避。"""
    bucket = todo.get(WISH_KEY) or {}
    if not bucket:
        return []
    started, skipped = [], []
    for code in backlog.due(todo, WISH_KEY, budget):
        if len(started) >= budget:
            break
        if code in known:
            backlog.mark(todo, WISH_KEY, code, backlog.QUEUED, "已在归档或下载中")
            continue
        max_gb = float((bucket.get(code) or {}).get("max_size_gb") or 0)
        item, why = asyncio.run(subscribe.fetch_best(
            code, max_gb, tried=backlog.tried_of(todo, WISH_KEY, code)))
        if not item:
            backlog.mark(todo, WISH_KEY, code, backlog.PENDING, why)
            skipped.append({"code": code, "why": why})
            continue
        job_id = start_job(item["magnet"], code,
                           {"name": code, "size": int(item.get("size_mb") or 0) * 1024 * 1024})
        if not job_id:
            backlog.mark(todo, WISH_KEY, code, backlog.PENDING, "任务表里已有同番号任务")
            skipped.append({"code": code, "why": "同番号已在下载"})
            continue
        backlog.mark(todo, WISH_KEY, code, backlog.QUEUED, f"job {job_id}", job_id=job_id)
        backlog.note_tried(todo, WISH_KEY, code, item["magnet"])
        known.add(code)
        started.append({"code": code, "job": job_id, "size_mb": item["size_mb"]})
    if not started and not skipped:
        return []
    return [{"key": WISH_KEY, "kind": "wish", "id": WISH_KEY, "name": "愿望单",
             "started": started, "skipped": skipped,
             "pending": backlog.counts(todo, WISH_KEY)[backlog.PENDING], "error": ""}]


def reconcile_backlog(todo: dict) -> list[dict]:
    """待办对账：已提交的那几条，按任务结果决定是归档、继续等，还是放回待办。

    backlog 标成 queued 之后就不参与筛选了；下载失败或记录消失时没人把它放回去，
    这部片就永久漏了 —— 这是订阅闭环最大的窟窿。三种结果分开处理：
      任务结束且归档成功 → 落地（不再动）
      任务失败/取消       → 放回 pending，带退避重试
      任务记录已不存在     → 也放回 pending（可能被 prune 或手工清掉）
    """
    # 按 job_id 精确查任务，不要用 recent(200) 兜底：recent 取的是**最新 200 条**，
    # 待办里的任务一多（订阅跑久了很容易），老任务就查不到 → job 为 None →
    # 走「任务记录已不存在」分支被放回 pending → 重复下载。
    archived = archived_codes()
    changed = []
    jobs_db = _store()                          # 局部名换成 jobs_db：store 是模块
    for sub_key, bucket in (todo or {}).items():
        for code, item in bucket.items():
            if item.get("state") != backlog.QUEUED:
                continue
            job_id = item.get("job_id") or ""
            if code in archived:
                item["state"] = backlog.DONE
                item["note"] = "已归档"
                changed.append({"code": code, "result": "archived"})
                continue
            job = jobs_db.get(job_id) if job_id else {}
            if job and job.get("stage") in store.RUNNING:
                continue                     # 还在跑，别动（cloud 也在跑，曾经漏过它）
            if job and job.get("stage") == "deferred":
                continue                     # 等额度，不算失败
            why = (job or {}).get("error") or ("任务记录已不存在" if not job else "未完成")
            backlog.mark(todo, sub_key, code, backlog.PENDING,
                         f"上次提交未成功：{str(why)[:120]}")
            (todo.get(sub_key) or {})[code]["job_id"] = ""
            changed.append({"code": code, "result": "requeued", "why": why})
    return changed


def diskspace_room_ok() -> bool:
    """有空间才跑自动下载；不够就整轮跳过，等下次。"""
    free = diskspace.free_bytes(TMP_ROOT)
    reserve = int(get_setting("DISK_RESERVE_GB") * diskspace.GB)
    return free is None or free > reserve


def _subs_loop() -> None:
    while True:
        try:
            if not scheduling_open():
                # 恢复后待对账：不自动提交新任务，免得旧状态与真实下载对不上
                time.sleep(SUBS_INTERVAL)
                continue
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
        keep_recent=get_setting("CLEANUP_KEEP_RECENT"),
        max_bytes=int(get_setting("CLEANUP_MAX_GB") * 1024**3),
        active_codes=active_codes(),
        older_than_days=get_setting("CLEANUP_OLDER_DAYS"),
        dry_run=dry_run,
    )
    result = plan.as_dict()
    if not dry_run:
        result["removed"] = purge_archives([i["code"] for i in plan.delete])
    return result


def _drop_share(backend, record: dict) -> None:
    """删归档时顺手删掉当初建的分享（OpenList）。取不到 id 就什么都不做。"""
    hook = getattr(backend, "drop_share", None)
    if hook is None:
        return
    sid = storage.openlist_link_id((record or {}).get("link") or "")
    if sid:
        hook(sid)


def purge_archives(codes_to_drop: list[str]) -> list[dict]:
    """真正删除：远端目录 + state 记录。任一失败就保留记录，便于重试。"""
    done = []
    try:
        backend = storage_backend()
    except Exception as exc:      # 后端没配好（缺 URL / 缺 grpcio）：一条都删不成，别抛 500
        log.warning("存储后端不可用，删除归档全部放弃：%s", exc)
        return done
    for code in codes_to_drop:
        if not code:
            continue
        state = core.load_state(STATE)
        record = state.get(code) or {}
        if not record:
            continue
        try:
            validate_archive_backend(backend, record)
            rel = archive_locator(record)
        except storage.StorageError as exc:
            log.warning("删除 %s 跳过：%s", code, exc)
            continue
        if backend.kind != "rclone":
            try:
                # 按记录定位（1.5）：云添加的归档在 CLOUD_DEST，不能拿全局根拼路径
                receipt = backend.purge(archive_locator(record))
            except storage.StorageError as exc:
                log.warning("删除 %s 失败：%s", code, exc)
                continue
            log.info("删除 %s：%s", code, receipt)
            _drop_share(backend, record)
        else:
            target = f"{RCLONE_REMOTE}:{rel}"
            rc, _, err = rclone("purge", target, timeout=1800.0)
            if rc != 0:
                log.warning("删除 %s 失败：%s", code, err.strip()[:200])
                continue
        if code in state:
            state.pop(code, None)
            core.save_state(STATE, state)
        done.append({"code": code, "ok": True})
    return done



def rename_archive(old: str, new: str) -> dict:
    """改已归档番号：Drive 目录搬到新名，state 同步改键与 code。

    顺序是「先搬 Drive，再改记录」：搬失败就原样留着，重试安全；
    反过来则会出现记录指向不存在的目录。
    """
    old = (old or "").strip()
    raw = (new or "").strip()
    # 名字会拼进 Drive 路径：含分隔符或 `..` 的直接拒绝，不要「清洗」成另一个名字
    if any(ch in raw for ch in "/\\") or ".." in raw:
        return {"ok": False, "error": "新名字不能包含路径分隔符"}
    # 空格、下划线归一成连字符，其余非法字符丢掉
    wanted = re.sub(r"[^0-9A-Za-z-]+", "-", raw).strip("-").upper()
    if not old:
        return {"ok": False, "error": "缺少原番号"}
    if not wanted or len(wanted) < 3:
        return {"ok": False, "error": "新名字太短或含非法字符"}
    if wanted == old:
        return {"ok": True, "code": old, "unchanged": True}

    state = core.load_state(STATE)
    if old not in state:
        return {"ok": False, "error": f"{old} 不在归档记录里"}
    if wanted in state:
        return {"ok": False, "error": f"{wanted} 已经存在，先处理掉同名归档"}

    try:
        backend = storage_backend()
    except Exception as exc:
        return {"ok": False, "error": f"存储后端不可用：{exc}"}
    try:
        validate_archive_backend(backend, state[old])
        base = archive_locator(state[old])
    except storage.StorageError as exc:
        return {"ok": False, "error": str(exc)}
    if backend.kind != "rclone":
        return _rename_archive_backend(backend, old, wanted, state)

    parent = base.rsplit("/", 1)[0] if "/" in base else ""
    src = f"{RCLONE_REMOTE}:{base}"
    dst_rel = f"{parent}/{wanted}".strip("/")
    dst = f"{RCLONE_REMOTE}:{dst_rel}"
    rc, out, err = rclone("lsf", f"{RCLONE_REMOTE}:{parent}", "--dirs-only", timeout=180.0)
    if rc == 0 and wanted in {line.strip().strip("/") for line in out.splitlines()}:
        return {"ok": False, "error": f"Drive 里已有 {DRIVE_ROOT}/{wanted}，先处理掉同名目录"}

    rc, _, err = rclone("move", src, dst, timeout=3600.0)
    if rc != 0:
        return {"ok": False, "error": f"移动 Drive 目录失败：{err.strip()[:200]}"}
    rclone("rmdir", src, timeout=180.0)          # 搬干净后空目录留着没用

    record = dict(state[old])
    record["code"] = wanted
    record["path"] = dst
    if record.get("rel"):
        record["rel"] = dst_rel
    rc, out, _ = rclone("link", dst, timeout=120.0)
    if rc == 0 and out.strip().startswith("http"):
        record["link"] = out.strip()
    core.rekey_state(STATE, old, wanted, record)
    log.info("归档改名 %s → %s", old, wanted)
    return {"ok": True, "from": old, "code": wanted, "link": record.get("link") or ""}


def _rename_archive_backend(backend, old: str, wanted: str, state: dict) -> dict:
    """OpenList / CloudDrive2 的改名：列一遍根目录查重，再让后端自己改名。

    顺序仍是「先动远端、再动记录」：远端失败就原样留着，重试安全。
    改的是**记录定位的那个目录**（1.5）：云添加的归档在 CLOUD_DEST，
    不能拿全局 STORAGE_ROOT 拼。
    """
    record_old = state[old]
    base = archive_locator(record_old)
    parent, _leaf = base.rsplit("/", 1) if "/" in base else ("", base)
    try:
        existing = set(backend.list_dirs(parent or storage.rel("")))
    except storage.StorageError as exc:
        return {"ok": False, "error": f"列出远端目录失败：{exc}"}
    if wanted in existing:
        return {"ok": False, "error": f"远端已有 {wanted}，先处理掉同名目录"}
    try:
        backend.rename_dir(base, wanted)
    except storage.StorageError as exc:
        return {"ok": False, "error": f"移动目录失败：{exc}"}

    record = dict(record_old)
    record["code"] = wanted
    record["path"] = f"{backend.kind}:{parent}/{wanted}"
    record["rel"] = f"{parent}/{wanted}"         # 1.5：定位跟着改名走
    try:
        link = backend.link(f"{parent}/{wanted}")
    except storage.StorageError:
        link = None
    if link:
        record["link"] = link
    core.rekey_state(STATE, old, wanted, record)
    log.info("归档改名 %s → %s（%s）", old, wanted, backend.kind)
    return {"ok": True, "from": old, "code": wanted, "link": record.get("link") or ""}


def wanted_name(key: str, record: dict) -> str:
    """规范名：`YYYY-MM-DD 番号 标题`（有元数据时），否则只有番号。

    只留文件名安全字符，长度封顶 —— 目录名过长的通配/同步工具会出错。
    """
    base = codes.canonical(key) or codes.canonical((record or {}).get("code") or "")
    if not base:
        return ""
    meta = (record or {}).get("meta") or {}
    date = (meta.get("release_date") or "").strip()[:10]
    title = re.sub(r"[^0-9A-Za-z\u3040-\u30ff\u4e00-\u9fff]+", "-", meta.get("title") or "")
    title = title.strip("-")[:100 - len(base) - len(date)]
    parts = [p for p in (date if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) else "", base, title) if p]
    return " ".join(parts)[:100]

def normalize_plan() -> dict:
    """扫一遍归档，按规范给出改名清单。

    只动「识别得出番号且与现名不同」的；识别不出的（`unclassified-…`）保持原样。
    目标名撞车的整组跳过 —— 自动改名不能把两条记录合成一条。
    """
    state = core.load_state(STATE)
    candidates, skipped = [], []
    for key, record in state.items():
        target = wanted_name(key, record or {})
        if not target or target == key:
            continue
        candidates.append({"code": key, "to": target})

    # 撞车检查：多个旧名指向同一新名，或新名本身已存在且不在改名清单里
    by_target: dict[str, list[str]] = {}
    for item in candidates:
        by_target.setdefault(item["to"], []).append(item["code"])
    renaming = {item["code"] for item in candidates}
    clean = []
    for item in candidates:
        peers = by_target[item["to"]]
        if len(peers) > 1 or (item["to"] in state and item["to"] not in renaming):
            skipped.append({**item, "reason": "目标名已被占用"})
            continue
        clean.append(item)
    return {"ok": True, "rename": clean, "skipped": skipped, "total": len(state)}

def adopt_plan(limit: int = 50) -> dict:
    """未分类归档的自愈方案：从文件名/来源里认番号，认出且能查到元数据才算数。

    `unclassified-<时间戳>` 是识别失败的兜底目录；事后用文件名再试一次，
    命中就按规范改名 —— 不猜：查不到元数据一律不动。
    """
    state = core.load_state(STATE)
    known = set(state)
    plan, skipped = [], []
    for key, record in list(state.items())[:10000]:
        if not key.lower().startswith("unclassified"):
            continue
        record = record or {}
        sources = [*(record.get("files") or []), record.get("source") or "", key]
        found = ""
        for text in sources:
            found = codes.canonical(text)
            if found:
                break
        if not found:
            skipped.append({"code": key, "reason": "文件名里认不出番号"})
            continue
        if found in known:
            skipped.append({"code": key, "reason": f"{found} 已在归档里"})
            continue
        plan.append({"code": key, "to": found})
        if len(plan) >= limit:
            break
    return {"ok": True, "adopt": plan, "skipped": skipped}

def series_median(code: str, state: dict) -> int:
    """同系列历史体积的中位数：体积未知时拿它兜底磁盘闸门，别一律按 0 放行。"""
    series = series_of(code).upper()
    sizes = sorted(
        int(v.get("size") or 0) for k, v in state.items()
        if series and series_of(k).upper() == series and int(v.get("size") or 0) > 0
    )
    if not sizes:
        return 0
    mid = len(sizes) // 2
    return sizes[mid] if len(sizes) % 2 else (sizes[mid - 1] + sizes[mid]) // 2

def _guess_size(code: str) -> int:
    """体积未知时的保守预留：同系列历史中位数，再没有就按 DEFAULT_SIZE 兜底。

    返回 0 就等于「这个任务不占盘」，闸门会无脑放行 —— 以前恢复的任务正是
    这样一批批冲进来的。宁可估大一点，让队头多等一会儿。
    """
    guess = series_median(code, core.load_state(STATE)) if code else 0
    return int(guess or get_setting("DEFAULT_SIZE_GB") * 1024**3)


def panel_errors(days: float) -> list[dict]:
    """面板里失败超过 days 天的任务记录（不含今天的）：僵尸清理由此驱动。"""
    cutoff = time.time() - max(0.0, days) * 86400
    out = []
    for item in panel_tasks().get("tasks") or []:
        if item.get("phase") != xunlei.ERROR:
            continue
        try:
            when = datetime.datetime.fromisoformat(item.get("updated") or "").timestamp()
        except (TypeError, ValueError):
            continue
        if when < cutoff:
            out.append(item)
    return out

def clean_panel_errors(days: float = 7.0, confirm: bool = False) -> dict:
    """清理面板里的失败任务记录。默认只出清单，confirm 才真删。"""
    victims = panel_errors(days)
    if not confirm:
        return {"ok": True, "days": days, "delete": victims,
                "hint": "需要 confirm=true 才会删除"}
    xl = xunlei_client()
    done, failed = [], []
    for item in victims:
        async def one(task_id=item["id"]):
            async with xl.client() as client:
                await xl.remove(client, task_id)
        try:
            asyncio.run(one())
            done.append(item["id"])
        except Exception as exc:
            failed.append({"id": item["id"], "error": str(exc)[:200]})
    return {"ok": True, "deleted": done, "failed": failed}

def subscribe_suggestions(limit: int = 8) -> dict:
    """从已归档元数据里统计常看的演员与系列，供一键订阅。

    ID 只读缓存（`data/ids.json`），不在这个接口里联网 —— 渲染不该等外网。
    没有 ID 的条目照样列出来，只是不能一键订阅。
    """
    state = core.load_state(STATE)
    actors: dict[str, int] = {}
    series: dict[str, int] = {}
    for record in state.values():
        info = (record or {}).get("meta") or {}
        for name in info.get("actors") or []:
            name = (name or "").strip()
            if name:
                actors[name] = actors.get(name, 0) + 1
        name = (info.get("series") or "").strip()
        if name:
            series[name] = series.get(name, 0) + 1

    def top(bucket: dict[str, int], kind: str) -> list[dict]:
        rows = sorted(bucket.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
        return [{"name": name, "count": count, "kind": kind,
                 "id": meta.cached_id(name, kind) or ""} for name, count in rows]

    return {"ok": True, "actors": top(actors, "actor"), "series": top(series, "series"),
            "archived": len(state)}

def subscribe_from_suggestion(kind: str, name: str, max_per_run: int = 1,
                              max_size_gb: float = 0, sub_id: str = "") -> dict:
    """把建议落成订阅：显式给的 ID 优先，其次缓存，最后联网解析。

    手填的 ID 会写进缓存 —— 同一个名字下次就不用再问。
    """
    kind = "series" if kind == "series" else "actor"
    name = (name or "").strip()
    if not name and not sub_id:
        return {"ok": False, "error": "缺少名字或 ID"}
    given = (sub_id or "").strip()
    if given:
        parsed = subscribe.sub_id_of(given, kind)
        if not parsed:
            label = "演员页" if kind == "actor" else "系列页"
            return {"ok": False, "error": f"看不懂这个 {label} ID 或链接：{given[:60]}"}
        if name:
            meta._save_id(name, kind, parsed)
        return add_sub(parsed, max_per_run=max_per_run, max_size_gb=max_size_gb,
                       name=name or parsed, kind=kind)
    sub_id = meta.cached_id(name, kind)
    if not sub_id:
        try:
            sub_id = asyncio.run(meta.resolve_id(name, kind))
        except Exception as exc:
            return {"ok": False, "error": f"解析 {name} 的 javdb ID 失败：{str(exc)[:120]}"}
    if not sub_id:
        return {"ok": False, "error": f"站内搜不到「{name}」的 javdb ID，请手动填链接"}
    return add_sub(sub_id, max_per_run=max_per_run, max_size_gb=max_size_gb,
                   name=name, kind=kind)

def magnet_search(code: str, limit: int = 8) -> dict:
    """按番号搜磁力（只读，不提交），同时附上这个番号的元信息。

    只看体积与「字幕/高清」两个标签，不足以判断选哪条；把封面、标题、日期、
    演员、时长一起给出来，用户才知道自己选的是什么。
    """
    code = codes.canonical(code) or (code or "").strip().upper()
    if not code:
        return {"ok": False, "error": "缺少番号"}

    async def main():
        async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
            items = await sources.search(code, client=client, limit=limit)
            info = None
            try:
                info = await meta.lookup(code, client=client)
            except Exception as exc:      # 元数据是锦上添花，查不到也照常给磁力
                log.info("搜 %s 时取元数据失败：%s", code, exc)
            return items, info

    try:
        items, info = asyncio.run(main())
    except Exception as exc:
        return {"ok": False, "code": code, "error": str(exc)[:200]}
    return {"ok": True, "code": code, "items": items,
            "info": info or meta.cached(code) or {}}

def magnet_preview(magnet: str) -> dict:
    """给「已选中的那一条磁力」取预览。搜索时不批量查，避免一次打十次外部请求。"""
    if not (magnet or "").lower().startswith("magnet:"):
        return {"ok": False, "error": "需要一个磁力链接"}
    info = peek(magnet)
    return {"ok": True, "magnet": magnet[:120],
            "info": info or {}, "found": bool(info)}


def panel_tasks() -> dict:
    """迅雷面板里的任务（含本程序之外的），供看板展示，免得再开一个 2345 页面看队列。"""
    xl = xunlei_client()

    async def main() -> dict:
        async with xl.client() as client:
            tasks = await xl.tasks(client)
        return {
            "tasks": [
                {
                    "id": t.get("id"),
                    "name": t.get("name") or "",
                    "phase": t.get("phase") or "",
                    "percent": float(t.get("progress") or 0),
                    "speed": int((t.get("params") or {}).get("speed") or 0),
                    "size": int(t.get("file_size") or 0),
                    "error": (t.get("params") or {}).get("error") or "",
                    "path": (t.get("params") or {}).get("real_path") or "",
                    "updated": t.get("updated_time") or "",
                }
                for t in tasks
                # 只留还没结束的：历史完成记录几十条，塞满侧栏没有意义
                if (t.get("phase") or "") != xunlei.DONE
            ]
        }

    try:
        return asyncio.run(main())
    except Exception as exc:
        return {"error": str(exc)[:200], "tasks": []}

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
                "cleanup_pending": item.get("cleanup_pending") or [],
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

    def _payload(self, limit: int = 8192) -> dict:
        """读 JSON 请求体，坏数据当空表 —— 二十几处路由都在重复这段。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(min(length, limit)) if length > 0 else b""
            data = json.loads(body or b"{}")
        except (ValueError, OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

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
            if not any(ids):
                self._json({"ok": False, "code": code, "duplicate": "active",
                            "error": f"{code} 正在下载，等它跑完再提交"}, 409)
                return
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
            # 延期中（没有运行中的线程）也要能停：不抹掉唤醒时间就会被自动复活。
            # stop_job 自己会处理：排队中、本进程运行中、以及跨进程（bot 在跑）
            # 三种情形 —— 最后一种写取消标记，执行者在阶段边界消费。
            stopped = stop_job(job_id) if job_id else False
            defer_cancelled = _store().cancel_defer(job_id) if job_id else False
            if not (stopped or defer_cancelled):
                self._json({"ok": False, "error": "任务不存在"}, 404)
                return
            self._json({"ok": True, "deferred": defer_cancelled,
                        "queued": stopped})
            return
        if path == "/api/archive/adopt":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(min(length, 4096)) or b"{}") or {}
            except (ValueError, json.JSONDecodeError):
                payload = {}
            plan = adopt_plan()
            if not payload.get("confirm"):
                self._json({**plan, "hint": "需要 confirm=true 才会改名"})
                return
            done, failed = [], []
            for item in plan["adopt"]:
                try:
                    info = asyncio.run(meta.lookup(item["to"]))
                except Exception as exc:      # 查不到就不动，宁可留着未分类
                    info = None
                state = core.load_state(STATE)
                record = state.get(item["code"]) or {}
                merged = {**record, "code": item["to"], "meta": info or record.get("meta") or {}}
                core.update_state(STATE, {item["code"]: merged})
                target = wanted_name(item["code"], merged)
                result = rename_archive(item["code"], target or item["to"])
                (done if result.get("ok") else failed).append(
                    {"from": item["code"], "to": target or item["to"],
                     **({} if result.get("ok") else {"error": result.get("error")})})
            self._json({"ok": True, "renamed": done, "failed": failed,
                        "skipped": plan["skipped"]})
            return
        if path == "/api/archive/normalize":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(min(length, 4096)) or b"{}") or {}
            except (ValueError, json.JSONDecodeError):
                payload = {}
            plan = normalize_plan()
            # 没显式 confirm 一律只出清单，不搬 Drive
            if not payload.get("confirm"):
                self._json({**plan, "hint": "需要 confirm=true 才会真正改名"})
                return
            done, failed = [], []
            for item in plan["rename"]:
                result = rename_archive(item["code"], item["to"])
                (done if result.get("ok") else failed).append(
                    {"from": item["code"], "to": item["to"], **({"error": result.get("error")} if not result.get("ok") else {})})
            self._json({"ok": True, "renamed": done, "failed": failed,
                        "skipped": plan["skipped"]})
            return
        if path == "/api/archive/rename":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(min(length, 4096)) or b"{}") or {}
            except (ValueError, json.JSONDecodeError):
                payload = {}
            result = rename_archive(payload.get("code") or "", payload.get("to") or "")
            self._json(result, 200 if result.get("ok") else 409)
            return
        if path == "/api/xunlei/cleanup":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(min(length, 4096)) or b"{}") or {}
            except (ValueError, json.JSONDecodeError):
                payload = {}
            # days=0 表示「连今天的也算」：以前 `or` 会把 0 当成没填而退回 7 天，
            # 于是刚失败的线段永远清不掉（面板上全是失败任务却一条也清不了）
            raw_days = payload.get("days")
            days = float(raw_days if raw_days is not None
                         else (get_setting("PANEL_CLEAN_DAYS") or 7))
            self._json(clean_panel_errors(days, bool(payload.get("confirm"))))
            return
        if path == "/api/xunlei/control":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(min(length, 4096)) or b"{}") or {}
            except (ValueError, json.JSONDecodeError):
                payload = {}
            task_id = (payload.get("id") or "").strip()
            action = (payload.get("action") or "").strip()
            if not task_id or action not in ("pause", "resume", "delete"):
                self._json({"ok": False, "error": "需要 id 与 action（pause/resume/delete）"}, 400)
                return

            async def control() -> None:
                xl = xunlei_client()
                async with xl.client() as client:
                    if action == "delete":
                        await xl.remove(client, task_id)
                    elif action == "pause":
                        await xl.pause(client, task_id)
                    else:
                        await xl.control(client, task_id, "running")

            try:
                asyncio.run(control())
                self._json({"ok": True, "id": task_id, "action": action})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)[:200]}, 502)
            return
        if path == "/api/jobs/clear":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(min(length, 4096)) or b"{}") or {}
            except (ValueError, json.JSONDecodeError):
                payload = {}
            self._json(clear_jobs(payload.get("stage") or ""))
            return
        if path == "/api/job/delete":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(min(length, 4096)) or b"{}") or {}
            except (ValueError, json.JSONDecodeError):
                payload = {}
            self._json(delete_job(str(payload.get("id") or ""), bool(payload.get("confirm"))))
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
        if path == "/api/subs/suggest/apply":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(min(length, 4096)) or b"{}") or {}
            except (ValueError, json.JSONDecodeError):
                payload = {}
            result = subscribe_from_suggestion(
                payload.get("kind") or "actor", payload.get("name") or "",
                int(payload.get("max_per_run") or 1),
                float(payload.get("max_size_gb") or 0),
                payload.get("id") or "",
            )
            self._json(result, 200 if result.get("ok") else 400)
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
        if path == "/api/backup":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            self._json(backup_now(self._payload().get("path") or ""))
            return
        if path == "/api/restore/check":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            self._json(restore_check(self._payload().get("source") or ""))
            return
        if path == "/api/restore/apply":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            p = self._payload()
            self._json(restore_apply(p.get("source") or "", bool(p.get("confirm"))))
            return
        if path == "/api/restore/confirm":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            p = self._payload()
            self._json(restore_confirm(bool(p.get("confirm")), bool(p.get("force"))))
            return
        if path == "/api/restore/status":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            self._drain()
            self._json(restore_status())
            return
        if path == "/api/digest":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            self._drain()
            self._json(daily_digest())
            return
        if path == "/api/archive/verify":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            p = self._payload()
            code = p.get("code") or ""
            self._json(verify_archive(code, bool(p.get("confirm"))) if code
                       else verify_all(int(p.get("limit") or 50), int(p.get("offset") or 0)))
            return
        if path == "/api/archive/dups":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            self._json(dup_check(self._payload().get("code") or ""))
            return
        if path == "/api/scrape/status":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            self._drain()
            self._json({"ok": True, **scrape_status()})
            return
        if path == "/api/scrape/preview":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            self._json(scrape_preview((self._payload().get("code") or "")))
            return
        if path == "/api/scrape/sync":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            p = self._payload()
            self._json(scrape_sync(p.get("code") or "", p.get("fields") or None,
                                   p.get("lock") or None, bool(p.get("resync")),
                                   bool(p.get("confirm")), p.get("unlock") or None,
                                   p.get("only") or None))
            return
        if path == "/api/settings":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            payload = self._payload()
            updates = payload.get("updates") or payload.get("settings") or {}
            self._json(set_settings(updates))
            return
        if path == "/api/xlp/credentials":
            # 写凭据要 POST（带请求体）。之前误挂在 do_GET 上：前端 POST 过来
            # 落到 GET 分支找不到路由 → 404 text/plain → 浏览器 r.json() 抛
            # 「JSON.parse: unexpected keyword」，看不出真正原因。
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            payload = self._payload(limit=65536)
            self._json(xlp_apply_credentials(str(payload.get("text") or ""),
                                           kind=str(payload.get("kind") or "")))
            return
        if path == "/api/images/clear":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(min(length, 4096)) or b"{}") or {}
            except (ValueError, json.JSONDecodeError):
                payload = {}
            self._json(image_cache_clear(bool(payload.get("confirm"))))
            return
        if path == "/api/storage/test":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            self._drain()
            self._json(storage_test())
            return
        if path == "/api/backlog/retry":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            payload = self._payload()
            self._json(retry_backlog(payload.get("code") or "", payload.get("sub") or ""))
            return
        if path == "/api/backlog/ignore":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            payload = self._payload()
            self._json(ignore_backlog(payload.get("code") or "", payload.get("sub") or ""))
            return
        if path == "/api/wish/add":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            payload = self._payload()
            self._json(wishlist_add(payload.get("codes") or "",
                                    float(payload.get("max_size_gb") or 0)))
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
            target = (f"{RCLONE_REMOTE}:{DRIVE_ROOT}/{code}" if storage.kind() == "rclone"
                      else f"{storage.kind()}:{storage.rel(code)}")
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
        if path == "/api/logout":
            self._drain()
            self._json(
                {"ok": True},
                extra={"Set-Cookie": f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"},
            )
            return
        self._drain()
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
            self._json({"ok": True, "subs": subs_snapshot(),
                        "suggest": subscribe_suggestions(),
                        "backlog": backlog_view()})
            return
        if path == "/api/local/disk":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            self._json(local_disk_inventory())
            return
        if path == "/api/settings":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            self._json({"ok": True, "items": settings_snapshot()})
            return
        if path == "/api/storage":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            self._drain()
            self._json(storage_view())
            return
        if path == "/api/cloud/capability":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            self._json(cloud_capability())
            return
        if path == "/api/cloud/directlink":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            qs = parse_qs(parts.query)
            want_path = (qs.get("path", [""]) or [""])[0]
            want_code = (qs.get("code", [""]) or [""])[0]
            self._json(cloud_direct_link(path=want_path, code=want_code))
            return
        if path == "/api/xlp/auth":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            qs = parse_qs(parts.query)
            want_refresh = (qs.get("refresh", ["0"]) or ["0"])[0] in ("1", "true", "yes")
            self._json(xlp_auth_status(refresh=want_refresh))
            return
        if path == "/api/meta/backfill":
            # 给老归档补元数据：会联网抓，所以显式触发而不是渲染时自动跑
            limit = int((parse_qs(parts.query).get("limit", ["50"]) or ["50"])[0] or 50)
            self._json(backfill_meta(limit=limit))
            return
        if path == "/api/cleanup/preview":
            self._json({"ok": True, **build_cleanup_plan(dry_run=True)})
            return
        if path == "/api/find":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            query = parse_qs(urlsplit(self.path).query)
            keyword = (query.get("q") or [""])[0]
            self._json(find_cards(keyword))
            return
        if path == "/api/images":
            if not self._authorized():
                self._drain()
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            self._drain()
            self._json(image_cache_view())
            return
        if path == "/api/jobs":
            self._json({"jobs": jobs_snapshot()})
            return
        if path == "/api/magnet/preview":
            self._json(magnet_preview((parse_qs(parts.query).get("magnet") or [""])[0]))
            return
        if path == "/api/magnets":
            query = parse_qs(urlsplit(self.path).query)
            self._json(magnet_search((query.get("code") or [""])[0]))
            return
        if path == "/api/xunlei":
            self._json(panel_tasks())
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def _proxy_image(self, query: dict) -> None:
        """同源转发图片（截图 / 封面），避免浏览器直连第三方域名。

        加磁盘缓存是被图床逼出来的：`www.javbus.com` 在本机实测握手很不稳
        （同一张图连打 5 次：200/200/200/超时/超时），媒体墙一次要 12 张，
        不缓存就是一半「无封面」、还每张白等 8 秒。缓存后一张图只需成功一次。
        """
        url = (query.get("u") or [""])[0].strip()
        host = (urlsplit(url).hostname or "").lower()
        # 白名单只放已知图床（javbus / jdbstatic / dmm / whatslink），别的一律 400；
        # 代理永远不做任意 URL 转发。
        if not url or not any(host == h or host.endswith("." + h) for h in IMG_HOSTS):
            self._send(400, b"bad proxy url", "text/plain; charset=utf-8")
            return
        cached = imgcache.get(url)
        if cached:
            data, ctype, _path = cached
            self._send(200, data, ctype, {"Cache-Control": "public, max-age=86400"})
            return
        referer = ("https://whatslink.info/" if "whatslink" in host
                   else "https://www.javbus.com/" if "javbus" in host
                   else "https://javdb.com/")
        req_headers = {"User-Agent": "Mozilla/5.0", "Referer": referer}
        # 4xx 立刻放弃（403/404 再试也是同一个结果）；传输错误多给几次机会 ——
        # 图床是「偶发握手超时」，同一张图第二、三次就成，这不是白等。
        last = None
        for attempt in range(IMG_ATTEMPTS):
            try:
                req = urllib.request.Request(url, headers=req_headers)
                with urllib.request.urlopen(req, timeout=IMG_TIMEOUT) as resp:
                    data = resp.read(4 * 1024 * 1024)
                    ctype = resp.headers.get("Content-Type") or "image/jpeg"
                imgcache.put(url, data, ctype)
                # 存完顺手修剪一次上限（删不掉也无所谓，取图不能被维护动作搞挂）
                limit = img_cache_max_bytes()
                if limit:
                    imgcache.trim(limit)
                self._send(200, data, ctype, {"Cache-Control": "public, max-age=86400"})
                return
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code < 500:
                    break
            except Exception as exc:
                last = exc
            if attempt + 1 < IMG_ATTEMPTS:
                time.sleep(0.3 * (attempt + 1))
        self._send(502, f"proxy failed: {last}".encode(), "text/plain; charset=utf-8")

    def _json(self, payload: dict, code: int = 200, extra: dict | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self._send(code, body, "application/json; charset=utf-8", extra)

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s %s", self.address_string(), fmt % args)


def serve(host: str = BIND, port: int = PORT) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), Handler)
    log.info("展示页 http://%s:%s  state=%s auth=%s", host, httpd.server_address[1], STATE, bool(AUTH))
    return httpd


# ─────────────────────────────────────────── 刮削补全（上传后的元数据维护）

def scrape_status() -> dict:
    """当前刮削配置的体检结果：通不通、启用哪些源、哪些归档待补刮。

    不返回 token，也不拼到任何 URL 里。
    """
    info = {"configured": metatube.configured(), "url": metatube.base(),
            "scrape": get_setting("SCRAPE"), "nfo": get_setting("SCRAPE_NFO"), "poster": get_setting("SCRAPE_POSTER"),
            "ok": False, "version": "", "providers": [], "error": "",
            "pending": _scrape_pending()}
    if not metatube.configured():
        info["error"] = "未配置 METATUBE_URL"
        return info

    async def probe():
        async with metatube._client() as client:
            root = await client.get(f"{metatube.base()}/")
            version = ((root.json() or {}).get("data") or {}).get("version", "")
            provs = await metatube.providers(client)
            return version, list((provs.get("movie_providers") or {}))

    try:
        info["version"], info["providers"] = asyncio.run(probe())
        info["ok"] = True
    except Exception as exc:
        info["error"] = str(exc)[:200]
    return info


def _scrape_pending() -> list[str]:
    """归档里元数据不全的番号：标题都没有，或上次生成附件失败过。"""
    pending = []
    for code, record in (core.load_state(STATE) or {}).items():
        meta_rec = (record or {}).get("meta") or {}
        if not meta_rec.get("title") or (record or {}).get("scrape_pending"):
            pending.append(code)
    return sorted(pending)


def scrape_preview(code: str) -> dict:
    """差异预览：重新刮一次，只列出会变的字段，不动任何文件。"""
    code = codes.canonical(code) or (code or "").strip().upper()
    state = core.load_state(STATE)
    record = state.get(code) or {}
    old = (record.get("meta") or {})
    locks = set(record.get("locked") or [])
    fresh = {}
    if metatube.configured():
        try:
            fresh = asyncio.run(metatube.scrape(code))
        except Exception as exc:
            return {"ok": False, "code": code, "error": str(exc)[:200]}
    if fresh and not metatube.matches(code, fresh):
        return {"ok": False, "code": code,
                "error": f"服务返回的是 {fresh.get('code')}，不是 {code}"}
    changes = {}
    # 预览必须走**和真正写入完全一样**的判定，否则会出现「预览说不会覆盖、实际却覆盖了」。
    # 以前这里自己写了一套跳过逻辑（无条件跳过锁字段），而 scrape_sync 走
    # metadata.merge —— 两处判据一旦分叉，用户看到的就是假话。
    merged = metadata.merge(old, fresh, locks=locks) if fresh else dict(old)
    for key in metadata.LOCKABLE:
        before, after = old.get(key), merged.get(key)
        if before == after:
            continue
        changes[key] = {"from": before, "to": after}
    return {"ok": True, "code": code, "locked": sorted(locks), "changes": changes,
            "found": bool(fresh), "provider": fresh.get("provider") or ""}


def scrape_sync(code: str, fields: dict | None = None, lock: list[str] | None = None,
                resync: bool = False, confirm: bool = False,
                unlock: list[str] | None = None, only: list[str] | None = None) -> dict:
    """补刮 / 手工纠正 / 同步附件到 Drive。

    · fields   手工改的字段，会被自动锁定（改过就别让自动刮削再覆盖）
    · lock     要锁定的字段（lock=["title"]）
    · unlock   要解锁的字段（unlock=["title"]），只认 LOCKABLE 里的名字
    · resync   重新刮一次，只补缺失、尊重锁定
    · confirm  要真去动 Drive 才需要
    · only     只重推名单里的附件（单文件重试），省略就是全部重推
    """
    code = codes.canonical(code) or (code or "").strip().upper()
    if not code:
        return {"ok": False, "error": "缺少番号"}
    lock = [k for k in (lock or ()) if k in metadata.LOCKABLE]
    unlock = [k for k in (unlock or ()) if k in metadata.LOCKABLE]
    # 同一字段既锁又解说不清要什么结果，与其静默选一个，不如让调用方写清楚
    both = sorted(set(lock) & set(unlock))
    if both:
        return {"ok": False, "code": code, "error": "同一字段不能同时锁定和解锁",
                "fields": both}
    state = core.load_state(STATE)
    record = dict(state.get(code) or {})
    if not record:
        return {"ok": False, "error": f"{code} 没有归档记录"}
    old = dict(record.get("meta") or {})
    locks = set(record.get("locked") or [])
    locks |= set(lock)
    locks -= set(unlock)
    merged = dict(old)
    if resync and metatube.configured():
        try:
            fresh = asyncio.run(metatube.scrape(code))
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200]}
        if fresh and not metatube.matches(code, fresh):
            return {"ok": False, "error": f"服务返回的是 {fresh.get('code')}，不是 {code}"}
        merged = metadata.merge(old, fresh, locks=locks)
    if fields:
        for key, value in fields.items():
            if key in metadata.LOCKABLE:
                merged[key] = value
                locks.add(key)
    changed = {k: v for k, v in merged.items() if old.get(k) != v}
    record.update({"meta": merged, "locked": sorted(locks)})
    out = {"ok": True, "code": code, "changed": changed, "locked": sorted(locks)}
    if unlock:
        out["unlocked"] = sorted(set(unlock))
    if not confirm:
        # 只改站内记录时按「有没有标题」判定待补刮；真要推附件时由 sync_attachments
        # 按**实际推送结果**决定，不能在这里先清掉（曾经因此把失败也报成已补全）。
        record["scrape_pending"] = not merged.get("title")
        core.update_state(STATE, {code: record})
        out["pending"] = record["scrape_pending"]
        out["hint"] = "只更新了站内记录；要同步到 Drive 需 confirm=true"
        return out
    synced = sync_attachments(code, record, only=only)
    # 落盘的是 sync_attachments 回写后的记录：清单按实际字节、pending 按推送结果
    final = synced.pop("record", None) or record
    core.update_state(STATE, {code: final})
    out["pending"] = bool(final.get("scrape_pending"))
    out.update(synced)
    if synced.get("sync_failed") or synced.get("generate_failed"):
        out["partial"] = True
    return out


def sync_attachments(code: str, record: dict, only: list[str] | None = None) -> dict:
    """只把附件推到 Drive，不重传视频。

    在一个临时目录里重新生成附件，再用 rclone 逐文件 copyto —— 视频已经在云端，
    重新整目录 copy 会把它当成新文件再传一遍。

    only 是「只重推这几个附件」的白名单（失败重试用）：只认固定的附件文件名，
    别的名字一律忽略，免得把任意名字拼进远端路径。只给了非法名字就一个都不推 ——
    不能因为筛选后为空就退化成「全部重推」，那等于把用户点名之外的附件也动了。

    清单的两条纪律（曾经两条都错）：
      1. 新清单以**旧完整清单**为基底，只替换本次真正重写的附件项；没有重写的条目
         （媒体、以及本次没生成的海报）原样保留。以前把所有附件项都剔出 keep，
         于是补一次刮削就把 poster.jpg 从云端清单里抹掉了。
      2. manifest.json **最后**推：先让内容上线再发布清单，中途失败也不会留下
         一份描述着不存在文件的清单。
    """
    named = only is not None and len(only) > 0
    wanted = {str(n) for n in (only or ()) if str(n) in ATTACHMENT_NAMES}
    work = Path(tempfile.mkdtemp(prefix="xd-attach-"))
    try:
        rec = (record or {}).get("meta") or {}
        locks = set((record or {}).get("locked") or [])
        sidecar = {metadata.JSON_NAME, metadata.NFO_NAME, metadata.POSTER_NAME,
                   metadata.MANIFEST_NAME}
        # 旧清单里的**全部**条目都先当基底带着，稍后用本次实际写出的文件覆盖同名项。
        # 视频等媒体不在工作区、本次也不会重写，必须原样保留；海报若本次没生成
        # （开关关着或取图失败），也要保留旧条目，否则云端清单与实物对不上。
        kept = []
        for entry in ((record or {}).get("manifest") or {}).get("files") or []:
            name = str((entry or {}).get("name") or "")
            if name and name != metadata.MANIFEST_NAME:
                kept.append({"name": name, "size": int((entry or {}).get("size") or 0),
                             "ext": Path(name).suffix.lower()})
        if not kept:                              # 没有清单就退回记录里的文件清单
            kept = [{"name": str(n), "size": 0, "ext": Path(str(n)).suffix.lower()}
                    for n in ((record or {}).get("files") or []) if n
                    and str(n) != metadata.MANIFEST_NAME]

        # 海报：开关开着且记录里有封面地址就重建一次；取不到就沿用旧条目（不删）
        poster = b""
        if get_setting("SCRAPE_POSTER") and rec.get("cover_url"):
            poster = metadata.fetch_image(rec["cover_url"]) or None

        out = metadata.write_sidecar(work, rec, [], code=code, locks=locks,
                                     poster=poster, want_nfo=get_setting("SCRAPE_NFO"), keep=kept)
        generated = [n for n in out["written"] if n != metadata.MANIFEST_NAME]
        target = f"{RCLONE_REMOTE}:{DRIVE_ROOT}/{code}"
        try:
            backend = storage_backend()
        except Exception as exc:
            raise RuntimeError(f"存储后端不可用：{exc}") from exc
        validate_archive_backend(backend, record)
        rel = archive_locator(record)            # 1.5：按记录定位，云归档在 CLOUD_ROOT
        target = f"{RCLONE_REMOTE}:{rel}"

        def push(name: str) -> tuple[bool, str]:
            """推一个附件；返回 (是否成功, 失败原因)。"""
            if backend.kind != "rclone":
                try:
                    backend.upload_file(work / name, f"{rel}/{name}")
                    return True, ""
                except storage.StorageError as exc:
                    return False, str(exc)[:200]
            rc, _, err = rclone("copyto", str(work / name), f"{target}/{name}",
                                timeout=300.0)
            return (True, "") if rc == 0 else (False, err.strip()[:200])

        pushed, failed, skipped = [], [], []
        manifest_first = metadata.MANIFEST_NAME in out["written"]
        for name in sorted(generated, key=lambda n: n == metadata.MANIFEST_NAME):
            if named and name not in wanted:
                skipped.append(name)
                continue
            ok, why = push(name)
            (pushed if ok else failed).append(name if ok else {"name": name, "error": why})

        res: dict = {"synced": pushed, "sync_failed": failed}
        # 生成阶段就失败的（磁盘写不进等）以前被整个丢掉，面板只看到「已同步」
        if out["failed"]:
            res["generate_failed"] = out["failed"]
        if named:
            res["skipped"] = skipped
            # 点名要推却没生成、也没推送的：必须明说，不能返回一个看起来成功的空结果
            missing = sorted(wanted - set(pushed) - {f["name"] for f in failed})
            if missing:
                res["missing"] = missing
                res["error"] = ("以下附件没有生成，无法重推：" + "、".join(missing)
                                + ("（海报开关关闭或取图失败）" if metadata.POSTER_NAME in missing
                                   else ""))
        # 清单最后推：前提是本次确实写了新清单，且（全量模式或点名要它）
        if manifest_first and (not named or metadata.MANIFEST_NAME in wanted):
            ok, why = push(metadata.MANIFEST_NAME)
            if ok:
                pushed.append(metadata.MANIFEST_NAME)
            else:
                failed.append({"name": metadata.MANIFEST_NAME, "error": why})
        elif manifest_first:
            skipped.append(metadata.MANIFEST_NAME)

        res["record"] = _record_after_sync(record, work, pushed, failed)
        return res
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _record_after_sync(record: dict, work: Path, pushed: list, failed: list) -> dict:
    """按**实际推送成功**的附件回写站内清单与待补刮标记。

    以前同步完只上传、不回写：站内 manifest 里还是旧大小，健康校验据此误报；
    而 scrape_pending 在同步**之前**就按「有没有标题」清掉了，于是三个附件全
    推失败也照样退出待补刮清单 —— 面板上一切正常，云端却缺文件。
    """
    old = dict((record or {}).get("manifest") or {})
    files = [{"name": str(e.get("name")), "size": int(e.get("size") or 0),
              "ext": str(e.get("ext") or Path(str(e.get("name"))).suffix.lower())}
             for e in (old.get("files") or []) if e.get("name")]
    by = {e["name"]: e for e in files}
    for name in pushed:
        path = work / name
        if path.is_file():                       # 按实际落盘字节更新，不猜
            by[name] = {"name": name, "size": path.stat().st_size,
                        "ext": path.suffix.lower()}
    merged = sorted(by.values(), key=lambda e: e["name"])
    out = dict(record or {})
    out["manifest"] = {**old, "files": merged,
                       "total_size": sum(e["size"] for e in merged)}
    ok_names = {e for e in pushed}
    # 只有「该推的都推上去了」才算不再待补刮；生成失败或传输失败都继续挂着
    out["scrape_pending"] = bool(failed) or (record or {}).get("scrape_pending") is True
    if ok_names and not failed:
        out["scrape_pending"] = False
    if failed:
        out["sync_failed"] = [f if isinstance(f, str) else f.get("name") for f in failed]
    else:
        out.pop("sync_failed", None)
    return out


# ─────────────────────────────────────────── 归档完整性校验（第四批）

def validate_archive_backend(backend, record: dict) -> None:
    """Never apply an old record to a newly configured, different backend."""
    try:
        metadata.validate_archive_backend(record, backend.kind, RCLONE_REMOTE)
    except ValueError as exc:
        raise storage.StorageError(str(exc)) from exc


def _rel_from_path(path: str, code: str) -> str:
    """从记录里的 `后端:远端/路径` 派生后端内相对路径。

    只认**末段等于番号**的情况：路径写错、番号对不上时宁可返回空串，
    让调用方退回全局根，而不是拿着一条可能指向别的目录的路径去删文件。
    """
    text = str(path or "").strip()
    if not text or ":" not in text:
        return ""
    _kind, _, tail = text.partition(":")
    rel = tail.strip("/")
    if not rel or any(bit in (".", "..") for bit in rel.split("/")) or "\\" in rel:
        return ""
    if code and rel.rsplit("/", 1)[-1] != code:
        return ""
    return rel


def archive_locator(record: dict) -> str:
    """按归档记录定位远端目录，返回后端内相对路径。

    记录里有 1.5 写入的定位三元组（backend/root/rel）就直接用 —— 云添加的
    归档在 CLOUD_DEST，而全局 STORAGE_ROOT 可能是另一个根（或者后来被改过），
    曾经就是「记录写着 cloud:X-Drive/DEMO，核验却去 legacy-root/DEMO 找」。
    旧记录没有三元组，退回 storage.rel(code) 的老算法（行为与升级前一致）。
    """
    record = record or {}
    rel = str(record.get("rel") or "").strip("/")
    if rel:
        if any(bit in (".", "..") for bit in rel.split("/")) or "\\" in rel:
            raise storage.StorageError("归档路径不合法")
        return rel
    code = str(record.get("code") or "").strip("/")
    if ".." in code or "/" in code or "\\" in code:
        raise storage.StorageError(f"归档番号不合法：{code!r}")
    # 旧记录没有三元组，但 path 记着**归档当时**的真实远端位置
    # （clouddrive2:迅雷云盘/X-Drive/MIMK-267）。拿它派生 rel 比退回全局根准：
    # 归档是在当时的根下写的，后来改了 STORAGE_ROOT 也会照旧找对地方
    # （实测 MIMK-267 正是这样：全局根已是 Downloads/downloads/test，
    #  退回全局根只会报「路径不存在」，而实际文件在 迅雷云盘/X-Drive 下）。
    derived = _rel_from_path(record.get("path"), code)
    if derived:
        return derived
    return storage.rel(code)


def remote_ls(folder: str, timeout: float = 180.0, rel_path: str = "") -> list[dict]:
    """列远端目录里的文件（名字 + 大小）。校核与补刮都靠它，两条链路口径要一致。

    rel_path 给出时直接按它列（1.5：归档记录自带定位）；不给就按全局根拼
    （老调用点、以及还没有定位信息的旧记录）。
    """
    try:
        backend = storage_backend()
    except Exception as exc:
        raise RuntimeError(f"列目录失败：{exc}") from exc
    rel = (rel_path or "").strip("/") or storage.rel(folder)
    if backend.kind != "rclone":
        try:
            entries = backend.list_files(rel)
        except storage.StorageError as exc:
            raise RuntimeError(f"列目录失败：{exc}") from exc
        return [{"name": e.name, "size": e.size} for e in entries]
    rc, out, err = rclone("lsjson", f"{RCLONE_REMOTE}:{rel}",
                          "--files-only", timeout=timeout)
    if rc != 0:
        raise RuntimeError(f"列目录失败：{err.strip()[:200]}")
    try:
        items = json.loads(out or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"列目录返回不可解析：{exc}") from exc
    return [{"name": str(i.get("Name") or ""), "size": int(i.get("Size") or 0)}
            for i in items if i.get("Name")]


def verify_archive(code: str, confirm: bool = False) -> dict:
    """核对归档：本地清单 vs 远端实际文件。

    默认**只报告**：远端缺文件可能是历史上传未完成或有人手删，程序不该自作主张
    补传或删除。要看差异就让它列出来，动手得显式 confirm。
    """
    code = codes.canonical(code) or (code or "").strip().upper()
    state = core.load_state(STATE)
    record = state.get(code) or {}
    if not record:
        return {"ok": False, "code": code, "error": "没有归档记录"}
    local = {str(n): 0 for n in (record.get("files") or [])}
    # 有清单就按清单比：后加的附件（nfo/json/封面）也在里面
    manifest_files = ((record.get("manifest") or {}).get("files") or [])
    if manifest_files:
        local = {str(f.get("name")): int(f.get("size") or 0) for f in manifest_files if f.get("name")}
    try:
        # 按记录定位：云添加的归档在 CLOUD_DEST，不跟全局 STORAGE_ROOT 走
        validate_archive_backend(storage_backend(), record)
        remote = {i["name"]: i["size"] for i in remote_ls(code, rel_path=archive_locator(record))}
    except Exception as exc:
        return {"ok": False, "code": code, "error": str(exc)[:200], "state": "检查失败"}

    missing = sorted(n for n in local if n not in remote)
    extra = sorted(n for n in remote if local and n not in local)
    # 本地记了大小就跟远端比：远端 0 字节（上传残缺）也算大小异常。
    # 本地大小不明（0 或没记）就不做大小断言，但要标出来 —— 不能当成通过。
    sized = sorted(n for n in local if n in remote and local[n] and local[n] != remote[n])
    unknown = sorted(n for n in local if n in remote and not local[n])
    if missing:
        verdict = "缺文件"
    elif sized:
        verdict = "大小异常"
    elif not local:
        verdict = "无清单"
    elif unknown:
        verdict = "大小未知"
    else:
        verdict = "校验通过"
    out = {"ok": True, "code": code, "state": verdict, "missing": missing,
           "extra": extra, "size_mismatch": [
               {"name": n, "local": local[n], "remote": remote[n]} for n in sized],
           "size_unknown": unknown,
           "local_count": len(local), "remote_count": len(remote),
           "confirmed": bool(confirm)}
    if unknown:
        out["note"] = f"以下文件本地大小未知，未做大小校验：{'、'.join(unknown)}"
    if confirm:
        out["hint"] = "确认只记录结论，不会自动补传或删除远端文件"
    return out


def verify_all(limit: int = 50, offset: int = 0) -> dict:
    """批量核对：只看状态，不逐条拉远端详情。

    分页：一共 total 条，本次只看 [offset, offset+limit)。next_offset 为 None
    表示已经到末尾 —— 前端据此翻页，不靠自己猜有没有下一批。
    summary 只统计本次这一页，另外给 scanned 说清查了几条。
    """
    state = core.load_state(STATE)
    keys = list(state)
    total = len(keys)
    start = max(0, int(offset))
    size = max(1, int(limit))
    page = keys[start:start + size]
    rows = [verify_archive(code) for code in page]
    summary = {}
    for row in rows:
        summary[row.get("state", "检查失败")] = summary.get(row.get("state", "检查失败"), 0) + 1
    end = start + len(page)
    return {"ok": True, "summary": summary, "items": rows, "offset": start,
            "limit": size, "total": total, "scanned": len(rows),
            "next_offset": end if end < total else None}


# ─────────────────────────────────────────── 重复归档分层检查

def dup_check(code: str = "") -> dict:
    """查重复归档，分三层：

      · 同番号多条归档记录（改名/复工留下的）
      · 不同番号但 infohash 相同（同资源换名重下）
      · 同番号不同 infohash（不同版本，多半正常，只提示）

    只报告，不删。删除涉及远端数据，必须人工判断。
    """
    state = core.load_state(STATE)
    codes = [code] if code else list(state)
    by_hash: dict[str, list[str]] = {}
    rows = []
    for key in codes:
        record = state.get(key) or {}
        folder = (record.get("folder") or key)
        if folder != key:
            rows.append({"kind": "同番号多条记录", "code": key, "detail": f"记录 {key} → 目录 {folder}"})
        btih = str((record.get("infohash") or "")).lower()
        if btih:
            by_hash.setdefault(btih, []).append(key)
    for btih, keys in by_hash.items():
        if len(set(keys)) > 1:
            rows.append({"kind": "同 infohash 多份归档", "code": sorted(keys)[0],
                         "detail": f"{'、'.join(sorted(set(keys)))} 共用 {btih[:12]}…"})
    return {"ok": True, "duplicates": rows,
            "hint": "只报告，不删除；远端数据由你判断"}


# ─────────────────────────────────────────── 通知调度（第四批）

def daily_digest(now: float | None = None) -> dict:
    """每日汇总：今天归档几部、失败几部、待办几个、还差多少没刮。一天只发一次。"""
    now = time.time() if now is None else now
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    jobs = _store().recent(200)
    today = [j for j in jobs
             if time.strftime("%Y-%m-%d", time.localtime(j.get("created_at") or 0)) == day]
    done = [j for j in today if j.get("stage") == "done"]
    failed = [j for j in today if j.get("stage") == "failed"]
    waiting = [j for j in jobs if j.get("stage") == "deferred"]
    todo = backlog_load()
    pending = sum(backlog.counts(todo, k)[backlog.PENDING] for k in todo)
    total = sum(int(j.get("total") or 0) for j in done)
    body = (f"{day}\n完成 {len(done)} 部（{core.human_size(total)}）\n"
            f"失败 {len(failed)} 部\n额度等待 {len(waiting)} 个\n"
            f"订阅待办 {pending} 部\n待补刮 {len(_scrape_pending())} 部")
    sent = notify.daily_summary(body, day)
    return {"ok": True, "day": day, "sent": sent, "text": body}


def notify_long_pending(days: float = 14.0, now: float | None = None) -> list[dict]:
    """长期等不到资源的番号：提醒一次，别让它无声无息挂着。

    只针对 pending 且进待办超过 days 天的，按 (番号) 去重 —— 每天提醒同一个番号
    就是刷屏，用户要的是「这几部一直没资源」这一个信息。
    """
    now = time.time() if now is None else now
    todo = backlog_load()
    out = []
    for sub_key, bucket in todo.items():
        for code, item in bucket.items():
            if item.get("state") != backlog.PENDING:
                continue
            since = float(item.get("since") or item.get("first_seen") or 0)
            if not since:
                continue
            age = (now - since) / 86400
            if age < days:
                continue
            sent = notify.long_pending(code, age, item.get("note") or "")
            out.append({"code": code, "days": round(age, 1), "sent": sent})
    return out


# ─────────────────────────────────────────── 备份与恢复（第五批）

def backup_now(target: str = "") -> dict:
    """导出任务库（含订阅与待办）快照。默认写到 /data/backup-<时间>.db。

    只导数据库：凭据都在环境变量里，不在库里，所以备份包天然不含 token。
    """
    target = target or str(Path(os.getenv("JOBS_DB", "/data/jobs.db")).parent
                           / f"backup-{time.strftime('%Y%m%d-%H%M%S')}.db")
    try:
        _store().backup(target)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}
    return {"ok": True, "path": target, "size": os.path.getsize(target),
            "stats": _store().stats()}


# ─────────────────────────────────────────── 恢复：只读检查 → 拒绝活任务 → 覆盖 → 待对账
#
# 纪律（与备份一致）：
#   · 检查只读：用 mode=ro 打开备份，不迁移、不写一个字节；
#   · 有活任务拒绝恢复：不暂停后台循环、不清内存就直接覆盖，旧 worker 会往新库写；
#   · 恢复后**不自动开工**：清空内存状态并写下 reconcile_required，由人确认对账后再放行；
#   · 不删任何用户数据：当前库先备份到独立路径，路径与信息都回报给用户。

# 恢复后「待对账」标记放 state 表：进程重启也还在，不会一重启就自己跑起来。
# 但覆盖后的库本身可能写不进去（SQLITE_BUSY/磁盘满/权限），所以另有一份旁路
# 文件标记（store.RECONCILE_FILE，与任务库同目录）—— 库写不动时也拦得住。
# 「库不可信」因此不是内存状态：进程重启、bot 另起一个进程，都能读到同一个判据。
RECONCILE_KEY = store.RECONCILE_KEY
# 内存里「暂停调度」的开关：为 True 时 pump_queue / _resume_loop / _subs_loop 都不干活
_PAUSED = threading.Event()
# 在途执行线程（job_id → Thread）：恢复前必须确认它们已经结束
_threads: dict[str, threading.Thread] = {}
_threads_lock = threading.Lock()
# 恢复流程互斥：两个恢复请求不能同时动库
_restore_lock = threading.Lock()


def _remember_thread(job_id: str) -> None:
    """记下这个任务刚起的执行线程，恢复前据此判断「有没有在途工作」。"""
    with _threads_lock:
        _threads[job_id] = threading.current_thread()


def running_threads() -> list[str]:
    """还没结束的执行线程（已退出的顺手清掉，不让表一直涨）。"""
    with _threads_lock:
        dead = [k for k, t in _threads.items() if not t.is_alive()]
        for k in dead:
            _threads.pop(k, None)
        return sorted(_threads)


def _db_path() -> str:
    """当前任务库路径：优先用已打开的 STORE，否则按环境变量推导（不碰库文件）。"""
    if STORE is not None:
        return STORE.path
    return jobs_db_path()


def reconcile_mark() -> dict:
    """读「待对账」标记：库内标记与旁路文件合并，任一为真即待对账。

    交给 store 的纯函数读：它**不抛异常**，库读不出来也按待对账处理 ——
    以前这里读失败返回 False，等于「库坏了就照常开工」，正好反过来。
    """
    return store.read_reconcile_mark(_db_path())


def reconcile_pending() -> bool:
    """当前库是否处于「恢复后待对账」状态（库内标记或旁路文件，二者任一为真）。"""
    return bool(reconcile_mark().get("required"))


def paused() -> bool:
    return _PAUSED.is_set()


def set_paused(value: bool) -> None:
    if value:
        _PAUSED.set()
    else:
        _PAUSED.clear()


def scheduling_open() -> bool:
    """现在允许开工吗？—— **唯一的闸门判据**，所有起线程/自动提交的路径都问它。

    暂停是内存 Event（进程重启即丢），待对账标记在库里+旁路文件（重启仍在），
    两者任一为真都不开工。以前泵线程、延期、订阅各自查、requeue 直达 admit()
    不查，恢复窗口里新任务照样开下载。
    """
    return not paused() and not reconcile_pending()


def _ro_stats(conn) -> dict:
    """只读地数一下备份库里有什么。表缺失就记 0，不让检查整个失败。"""
    out = {"jobs": 0, "active": 0, "has_subs": False, "has_backlog": False, "has_events": False}

    def has(table: str) -> bool:
        return bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())

    if has("jobs"):
        out["jobs"] = int(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
        marks = ",".join("?" * len(store.ACTIVE))
        out["active"] = int(conn.execute(
            f"SELECT COUNT(*) FROM jobs WHERE stage IN ({marks})", store.ACTIVE).fetchone()[0])
    if has("state"):
        out["has_subs"] = bool(conn.execute(
            "SELECT 1 FROM state WHERE key='subs'").fetchone())
        out["has_backlog"] = bool(conn.execute(
            "SELECT 1 FROM state WHERE key='backlog'").fetchone())
    out["has_events"] = has("job_events")
    return out


def _ro_active_jobs(conn) -> list[dict]:
    """备份里未完成的任务（只读）。stage 全空/未知时只返回表里能确认的字段。"""
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                            " AND name='jobs'").fetchone():
            return []
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        want = [c for c in ("id", "code", "stage") if c in cols]
        if not want:
            return []
        marks = ",".join("?" * len(store.ACTIVE))
        rows = conn.execute(
            f"SELECT {','.join(want)} FROM jobs WHERE stage IN ({marks})"
            " ORDER BY created_at" if "created_at" in cols else
            f"SELECT {','.join(want)} FROM jobs WHERE stage IN ({marks})",
            store.ACTIVE).fetchall()
        return [{c: row[i] for i, c in enumerate(want)} for row in rows]
    except Exception as exc:            # 结构诡异的老库：报不出清单也不该让检查失败
        log.warning("读取备份库未完成任务失败：%s", exc)
        return []


def _open_readonly(path: str) -> sqlite3.Connection:
    """以**绝对只读**方式打开一个库文件，不写一个字节。

    只写 mode=ro 还不够：sqlite 会为目标文件建空的 -wal/-shm（mtime 也变），
    所以再加上 immutable=1 —— 它告诉 sqlite「这个文件不会被别人改」，
    于是连共享内存都不建。我们的备份是 backup API 写出来的、关闭时已 checkpoint，
    不存在「数据还压在 WAL 里」的情况，immutable 读到的就是完整内容。
    """
    return sqlite3.connect(f"file:{quote(path)}?mode=ro&immutable=1", uri=True,
                           timeout=10.0)


def _fmt_at(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "未知"


def restore_check(source: str) -> dict:
    """恢复前检查（**严格只读**）：报告备份里有什么、版本对不对、能不能直接恢复。

    以前这里用 store.Store(source) 打开备份 —— 那个构造函数会建表并迁移，
    一次「检查」就把备份文件的 user_version 从 2 改成 4，哈希与 mtime 全变。
    现在用 mode=ro 打开：不建表、不迁移、不写任何字节，检查前后文件逐字节一致。

    版本不匹配时只**标注**（needs_migration），不擅自升级备份库 —— 要不要升、
    升到哪版由恢复动作在真正覆盖后处理。
    """
    if not source or not os.path.isfile(source):
        return {"ok": False, "error": "备份文件不存在"}
    try:
        conn = _open_readonly(source)
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"备份库打不开：{str(exc)[:150]}"}
    try:
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            tables = sorted(r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall())
            stats = _ro_stats(conn)
            active = _ro_active_jobs(conn)
        except sqlite3.DatabaseError as exc:
            return {"ok": False, "error": f"备份库打不开：{str(exc)[:150]}"}
    finally:
        conn.close()
    stats["schema"] = version
    current = _store().stats()
    blocked = _restore_blockers()
    out = {
        "ok": True, "source": source, "backup": stats, "current": current,
        "schema_backup": version, "schema_current": current.get("schema"),
        "needs_migration": version != current.get("schema"),
        "tables": tables,
        "active_in_backup": active,
        "can_restore": not blocked,
        "blockers": blocked,
        "hint": ("恢复会覆盖当前任务库；恢复后进入待对账状态，确认对账前不会自动开始调度"
                 if not blocked else
                 "恢复会覆盖当前任务库；恢复后进入待对账状态，确认对账前不会自动开始调度"
                 "（当前还有活跃任务，需先排空或进入维护模式才能恢复）"),
    }
    if version <= 0 or "jobs" not in tables:
        out.update({"ok": False, "error": "这个文件不像任务库（没有 jobs 表）"})
    return out


def _restore_blockers() -> list[str]:
    """恢复的前置条件：任何一项不满足都不能动库。"""
    out = []
    try:
        active = _store().unfinished_any()
    except Exception as exc:
        out.append(f"读不出任务表（{str(exc)[:80]}），无法确认有没有活跃任务")
        active = []
    if active:
        marks = "、".join(f"{j.get('code') or j.get('id')}（{j.get('stage')}）"
                         for j in active[:5])
        out.append(f"任务表里还有 {len(active)} 个未完成任务：{marks}")
    with _waiting_lock:
        waiting = list(WAITING.keys())
    if waiting:
        out.append(f"内存队列里还有 {len(waiting)} 个排队任务")
    live = running_threads()
    if live:
        out.append(f"还有 {len(live)} 个任务线程在跑：{'、'.join(live[:5])}")
    return out


def _safety_path(source: str = "") -> str:
    """当前库的备份路径：**独立于 JOBS_DB 与备份源**，绝不写回同一个文件。"""
    db = os.path.abspath(jobs_db_path())
    folder = os.path.dirname(db) or "."
    base = Path(folder) / f"before-restore-{time.strftime('%Y%m%d-%H%M%S')}.db"
    target = str(base)
    n = 1
    taken = {db, os.path.abspath(source)} if source else {db}
    while os.path.exists(target) or os.path.abspath(target) in taken:
        n += 1
        target = f"{base.with_suffix('')}-{n}.db"
    return target


def jobs_db_path() -> str:
    """当前任务库路径（与 _store() 用同一个默认值）。"""
    return os.getenv("JOBS_DB", str(Path(STATE).parent / "jobs.db"))


def _clear_memory_state() -> None:
    """覆盖数据库后清空所有内存镜像：旧队列/取消表/租约再往新库写就全是脏数据。"""
    with _jobs_lock:
        for event in CANCEL.values():
            event.set()                  # 让在途下载尽快自停，再丢掉引用
        CANCEL.clear()
        JOBS.clear()
    with _waiting_lock:
        WAITING.clear()
        _queued_at.clear()
    with _lease_lock:
        LEASES.clear()
        _lease_live.clear()
    with _threads_lock:
        _threads.clear()


def restore_apply(source: str, confirm: bool = False) -> dict:
    """用备份覆盖当前任务库，然后**停下来等对账**。

    顺序（任一步失败都不动当前库）：
      1. 必须 confirm；
      2. 只读检查备份，确认是任务库；
      3. 拒绝：有活跃任务 / 内存队列非空 / 有在途执行线程 —— 此时什么都不写；
      4. 暂停调度 → 当前库备份到**独立路径** → 用备份覆盖；
      5. 清空内存状态，写 reconcile_required，保持暂停，不自动开工。
    """
    if not confirm:
        return {"ok": False, "error": "需要 confirm=true"}
    with _restore_lock:
        pre = restore_check(source)
        if not pre.get("ok"):
            return pre
        blocked = _restore_blockers()
        if blocked:
            # 拒绝时一个字节都不写：不建半成品备份、不动当前库
            return {"ok": False, "refused": True, "error": "；".join(blocked),
                    "blockers": blocked,
                    "hint": "请先排空（等活跃任务结束）或进入维护模式，再执行恢复",
                    "current": pre.get("current")}

        was_paused = paused()
        set_paused(True)                     # 先停调度，避免备份与覆盖之间又有任务进来
        safety = _safety_path(source)
        try:
            _store().backup(safety)
        except Exception as exc:
            set_paused(was_paused)
            return {"ok": False, "error": f"恢复前无法备份当前库：{str(exc)[:200]}"}

        global STORE
        try:
            src = _open_readonly(source)
            try:
                with sqlite3.connect(jobs_db_path()) as dst:
                    src.backup(dst)          # 用 sqlite 的 backup API：WAL 下拷文件会缺页
            finally:
                src.close()
        except Exception as exc:
            STORE = None                    # 覆盖了一半也不能继续用旧连接
            set_paused(True)                # 停在暂停态，等人确认
            return {"ok": False, "error": f"恢复失败：{str(exc)[:200]}", "safety": safety}

        _clear_memory_state()
        STORE = None                        # 丢掉缓存的连接，下次按新库重新打开
        mark = {"required": True, "source": source, "safety": safety,
                "schema_backup": pre.get("schema_backup"),
                "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        # 两处都写：state 表跟着库走（备份/覆盖也会带上），旁路文件在库写不动时兜底。
        # 任一处成功都算「标记成立」；两处全败才算硬失败（E2：以前写库失败只 warn，
        # 仍返回 ok=True + reconcile_required=False，重启后 _resume_loop 立刻 recover）。
        marked_in, mark_errors = [], []
        try:
            _store().state_put({RECONCILE_KEY: dict(mark)})
            marked_in.append("db")
        except Exception as exc:
            mark_errors.append(f"库内标记写入失败：{str(exc)[:150]}")
            log.error("写入对账标记失败：%s", exc)
        try:
            store.write_reconcile_mark(mark, _db_path())
            marked_in.append("file")
        except Exception as exc:
            mark_errors.append(f"旁路标记写入失败：{str(exc)[:150]}")
            log.error("写入旁路对账标记失败：%s", exc)
        if mark_errors:
            # 库刚被覆盖、标记却写不进去：这份库不可信。保持暂停（进程内），
            # 并且把「需要重新恢复」写进旁路文件 —— 只要写成了，重启后 _resume_loop
            # 仍被拦住；连旁路也写不进去时只能靠内存暂停，必须如实告诉调用方。
            set_paused(True)
            log.error("已用 %s 覆盖任务库，但对账标记没能完全写入：%s",
                      source, "；".join(mark_errors))
            if "file" in marked_in:
                error = ("任务库已被备份覆盖，但「待对账」标记没能完整写入："
                         + "；".join(mark_errors)
                         + "。当前库状态不可信，已暂停调度；旁路标记已落盘，重启后也不会自动开工")
            else:
                error = ("任务库已被备份覆盖，且「待对账」标记处处写不进去："
                         + "；".join(mark_errors)
                         + "。当前库状态不可信，已暂停调度；重启后无法阻止自动开工"
                         " —— 请人工核对后再开机")
            return {"ok": False, "restored_from": source, "safety": safety,
                    "reconcile_required": True, "paused": True,
                    "mark_written": marked_in, "errors": mark_errors,
                    "trusted": False, "error": error,
                    "hint": "先确认服务已停止调度，再核对任务表与迅雷面板；"
                            "修好磁盘/权限后重新执行恢复"}
        log.warning("已用 %s 覆盖任务库，进入待对账；确认前不自动调度", source)
        return {"ok": True, "restored_from": source, "safety": safety,
                "stats": _store().stats(), "trusted": True,
                "reconcile_required": True, "paused": paused(),
                "mark_written": marked_in,
                "needs_migration": pre.get("needs_migration"),
                "hint": "已暂停调度。先对账（任务列表与迅雷面板对照），"
                        "确认后调用 /api/restore/confirm 才会继续调度"}


def restore_confirm(confirm: bool = False, force: bool = False) -> dict:
    """对账完成：清掉 reconcile_required、放开调度，并**立刻**把未完成任务入队。

    默认要求库里没有未完成任务（恢复出来的「未完成」多半是旧状态，直接放行会重复下载）；
    确实要带着它们继续，就显式 force=true。
    """
    pending = _store()
    marks = pending.state_get(RECONCILE_KEY)
    left = pending.unfinished_any()
    unfinished = [{"id": j.get("id"), "code": j.get("code"), "stage": j.get("stage")}
                  for j in left]
    if not confirm:
        return {"ok": False, "reconcile_required": bool(marks.get("required")),
                "unfinished": unfinished, "error": "需要 confirm=true"}
    if left and not force:
        return {"ok": False, "reconcile_required": bool(marks.get("required")),
                "unfinished": unfinished,
                "error": f"库里还有 {len(left)} 个未完成任务，先处理掉或确认无碍后 force=true"}
    # 两处标记都要清：库内清了而旁路文件还在，闸门会一直关着（反过来也一样）
    clear_errors = []
    try:
        pending.state_put({RECONCILE_KEY: {}})
    except Exception as exc:
        clear_errors.append(f"清除库内标记失败：{str(exc)[:150]}")
    try:
        store.clear_reconcile_mark(_db_path())
    except Exception as exc:
        clear_errors.append(f"清除旁路标记失败：{str(exc)[:150]}")
    if clear_errors:
        # 留着标记就不算确认成功：宁可多停一会儿，也不能带着旧状态开工
        return {"ok": False, "reconcile_required": True,
                "error": "；".join(clear_errors)}
    set_paused(False)
    # 入队走 requeue → admit（同一道闸门）。以前这里只放开暂停就返回，而
    # _resume_loop 是启动时的一次性线程，恢复出来的任务再也没人接（E3）。
    out = requeue_unfinished("对账确认后")
    log.info("对账完成，恢复调度：入队 %d 个（排队等空间 %d），失败 %d 个",
             out["requeued"], out["held"], out["failed"])
    return {"ok": True, "reconcile_required": False, "paused": paused(),
            "unfinished": len(left), "requeued": out["requeued"],
            "held": out["held"], "requeue_failed": out["failed"],
            "errors": out["errors"],
            "hint": (f"调度已放开；{out['requeued']} 个未完成任务已重新入队"
                     f"（其中 {out['held']} 个在排队等磁盘空间）"
                     + (f"，{out['failed']} 个入队失败，见 errors" if out["failed"] else "")
                     if out["requeued"] or out["failed"] else "调度已放开；没有未完成任务需要入队")}


def restore_status() -> dict:
    """恢复面板用：当前是否待对账、是否暂停、库版本。"""
    return {"ok": True, "reconcile_required": reconcile_pending(), "paused": paused(),
            "blockers": _restore_blockers(), "stats": _store().stats(),
            "mark": _store().state_get(RECONCILE_KEY), "mark_detail": reconcile_mark()}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    httpd = serve()
    # 磁盘空间调度线程：排队任务要能被自动放行，不能只靠提交时那一次判断
    threading.Thread(target=_queue_loop, daemon=True, name="disk-queue").start()
    # 重启对账：上次没跑完的任务重新入队，不从头再下
    threading.Thread(target=_resume_loop, daemon=True, name="resume").start()
    threading.Thread(target=_defer_loop, daemon=True, name="defer").start()
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
