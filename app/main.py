"""TG bot：收到磁力/直链 → 识别番号 → 迅雷下载 → 整理 → rclone 存 Google Drive。

用法：
  python -m app.main            # 启动长轮询
  python -m app.main --check    # 自检 Telegram / 迅雷容器 / rclone remote
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from telegram import Message, Update
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from . import (codes, core, diskspace, fetcher, meta, metadata, metrics, metatube, storage,
               pipeline, retry, settings as settings_mod, sources, store, whatslink,
               workspace, xunlei)

log = logging.getLogger("x-drive")


@dataclass(frozen=True)
class Config:
    token: str
    allowed: set[int]
    remote: str
    drive_root: str
    max_mb: int
    concurrency: int
    state_path: str
    tmp_root: str
    rclone_config: str
    pending_dir: str = "/data/pending"
    jobs_db: str = "/data/jobs.db"
    metrics_path: str = "/data/metrics.json"
    state_dir: str = "/work"
    xunlei_url: str = "http://127.0.0.1:2345"
    xunlei_parent: str = ""
    xunlei_token: str = ""
    xunlei_poll: float = 10.0
    xunlei_settle: float = 30.0
    # 可热更新参数（daily_upload_gb / magnet_timeout / stall_timeout /
    # upload_transfers / upload_chunk / retries / retry_base_delay /
    # scrape / scrape_nfo / scrape_poster）不再是 dataclass 字段，
    # 改成下面的 property 每次从 state 表读，网页改了立即生效、不必重启。

    @property
    def max_bytes(self) -> int:
        return self.max_mb * 1024 * 1024

    # ── 可热更新参数：改成 property，每次访问从 state 表读（网页改了立即生效，
    #    不必重启）。这些字段不再进 dataclass 的 __init__，由 from_env 之后动态读。
    #    其余（token / 端口 / 挂载路径等启动参数）仍是 dataclass 字段，保持不变。
    def _settings(self) -> "settings_mod.Settings":
        inst = settings_mod.Settings(task_store(self))
        # 绑定给 scrapers（METATUBE_* / JAVBUS_* / META_USE_JAVBUS 的热更新桥）
        settings_mod.bind(inst)
        return inst

    def _get(self, key: str):
        try:
            return self._settings().get(key)
        except Exception:
            return settings_mod.SETTINGS_SPEC.get(key, {}).get("default")

    @property
    def daily_upload_gb(self) -> int:
        return self._get("DAILY_UPLOAD_GB")

    @property
    def magnet_timeout(self) -> float:
        return self._get("MAGNET_TIMEOUT")

    @property
    def stall_timeout(self) -> float:
        return self._get("STALL_TIMEOUT")

    @property
    def upload_transfers(self) -> int:
        return self._get("UPLOAD_TRANSFERS")

    @property
    def upload_chunk(self) -> str:
        return self._get("UPLOAD_CHUNK")

    @property
    def retries(self) -> int:
        return self._get("RETRIES")

    @property
    def retry_base_delay(self) -> float:
        return self._get("RETRY_BASE_DELAY")

    @property
    def scrape(self) -> bool:
        return self._get("SCRAPE")

    @property
    def scrape_nfo(self) -> bool:
        return self._get("SCRAPE_NFO")

    @property
    def scrape_poster(self) -> bool:
        return self._get("SCRAPE_POSTER")

    @classmethod
    def from_env(cls) -> "Config":
        ids = {
            int(x) for x in re.split(r"[,\s]+", os.getenv("ALLOWED_USER_IDS", "")) if x.strip().isdigit()
        }
        return cls(
            token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            allowed=ids,
            remote=os.getenv("RCLONE_REMOTE", "gdrive").strip(),
            drive_root=os.getenv("DRIVE_ROOT", "X-Drive").strip().strip("/"),
            max_mb=int(os.getenv("MAX_FILE_MB", "500")),
            concurrency=max(1, int(os.getenv("CONCURRENCY", "2"))),
            state_path=os.getenv("STATE_PATH", "/data/state.json"),
            tmp_root=os.getenv("TMP_ROOT", "/downloads"),
            rclone_config=os.getenv("RCLONE_CONFIG", "/config/rclone/rclone.conf"),
            state_dir=os.getenv("STATE_DIR", "/work"),
            pending_dir=os.getenv("PENDING_DIR", "/data/pending"),
            jobs_db=os.getenv("JOBS_DB", "/data/jobs.db"),
            metrics_path=os.getenv("METRICS_PATH", "/data/metrics.json"),
            xunlei_url=os.getenv("XUNLEI_URL", "http://127.0.0.1:2345").strip(),
            xunlei_parent=os.getenv("XUNLEI_PARENT_ID", "").strip(),
            xunlei_token=os.getenv("XUNLEI_PAN_AUTH", "").strip(),
            xunlei_poll=float(os.getenv("XUNLEI_POLL", "10") or 10),
            xunlei_settle=float(os.getenv("XUNLEI_SETTLE", "30") or 0),
        )


# ---------------------------------------------------------------- 外部命令


class LeaseLost(RuntimeError):
    """租约被别的执行者接走：本线程必须安静退出。

    与普通失败分开：接管者正持租在跑，旧线程若走失败收口会把共享任务写成
    failed、还会把自己「曾经创建」的迅雷任务删掉 —— 那个任务现在可能正被
    新执行者续接着。失租只退出，不写共享终态、不动外部任务。
    """


class JobCancelled(RuntimeError):
    """用户请求取消（跨进程）：执行者在阶段边界消费后正常收尾成 cancelled。"""


def cancel_requested(cfg: "Config", job_id: str) -> bool:
    """任务是否被请求取消。取消标记在任务表里，web 与 bot 都能看到。"""
    if not job_id:
        return False
    try:
        return bool(task_store(cfg).cancel_marked(job_id))
    except Exception as exc:                   # 库暂时不可用时不误杀
        log.warning("读取消标记失败：%s", exc)
        return False


def _flag(name: str, default: str = "1") -> bool:
    """环境变量开关：0/false/False 视为关。与网页侧同一套写法。

    保留给可能的其它环境变量开关用；刮削三项已迁到 settings 的 property，
    不再经这里。
    """
    return os.getenv(name, default) not in ("0", "false", "False")


async def rclone(cfg: Config, *args: str, timeout: float = 180.0) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "rclone",
        "--config",
        cfg.rclone_config,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return 124, "", f"rclone {' '.join(args[:2])} 超时"
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


def usage_file(cfg: Config) -> Path:
    return Path(cfg.pending_dir).parent / "usage.json"


def today_bytes(cfg: Config) -> int:
    try:
        data = json.loads(usage_file(cfg).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if data.get("date") != datetime.now().strftime("%Y-%m-%d"):
        return 0
    return int(data.get("bytes") or 0)


def add_usage(cfg: Config, size: int) -> None:
    path = usage_file(cfg)
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("date") != today:
            data = {"date": today, "bytes": 0}
    except (OSError, json.JSONDecodeError):
        data = {"date": today, "bytes": 0}
    data["bytes"] = int(data.get("bytes") or 0) + max(0, size)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def quota_exceeded(cfg: Config, incoming: int) -> bool:
    """按 DAILY_UPLOAD_GB 预判；0 表示不限制。"""
    if cfg.daily_upload_gb <= 0:
        return False
    limit = cfg.daily_upload_gb * 1024**3
    return today_bytes(cfg) + incoming > limit


async def folder_link(cfg: Config, target: str) -> str | None:
    rc, out, _ = await rclone(cfg, "link", target, timeout=120.0)
    if rc == 0 and out.strip().startswith("http"):
        return out.strip()
    parent, _, name = target.rpartition("/")
    if not parent:
        return None
    rc, out, _ = await rclone(cfg, "lsjson", "--dirs-only", parent, timeout=120.0)
    if rc != 0:
        return None
    try:
        for item in json.loads(out):
            if item.get("Name") == name and item.get("ID"):
                return f"https://drive.google.com/drive/folders/{item['ID']}"
    except json.JSONDecodeError:
        pass
    return None


# ---------------------------------------------------------------- 流程


def cache_key(source: str) -> str:
    """按来源 URL 算一个稳定的短键（曾经的推文 ID 已随数据源改版移除）。"""
    return "h" + hashlib.sha1(source.encode()).hexdigest()[:16]


def safe_dir_name(key: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", key) or hashlib.sha1(key.encode()).hexdigest()[:16]


_STORE: "store.Store | None" = None


def task_store(cfg: Config) -> "store.Store":
    """bot 与 web 共用同一张任务表：同番号在两边只能有一个执行者。"""
    global _STORE
    if _STORE is None:
        _STORE = store.Store(cfg.jobs_db)
    return _STORE


def task_event(cfg: Config, job_id: str, kind: str, detail: str = "") -> None:
    """记一条任务事件（与网页端同一张表）。**绝不抛异常**：观测不能拖累任务。"""
    if not job_id or not kind:
        return
    try:
        owner = execution_owner(cfg, job_id)
        task_store(cfg).event(job_id, kind, detail, owner=owner if owner and kind != "lease_lost" else None)
    except Exception as exc:
        log.warning("记录任务 %s 的事件 %s 失败：%s", job_id, kind, exc)


def scheduling_gate(cfg: Config) -> dict:
    """bot 侧的调度闸门：与网页端共用同一判据（库内标记 ∪ 同目录旁路文件）。

    bot 与 web 是两个独立进程，内存里的暂停开关互相看不见，所以这里只认
    store.read_reconcile_mark —— 它在读不出来时按「待对账」回报，绝不抛异常。
    """
    try:
        mark = store.read_reconcile_mark(cfg.jobs_db)
    except Exception as exc:            # read_reconcile_mark 自称不抛；保险起见
        return {"open": False, "reason": f"读取对账标记异常：{str(exc)[:120]}", "mark": {}}
    if mark.get("required"):
        return {"open": False, "reason": "库处于待对账状态（恢复后未确认）", "mark": mark}
    return {"open": True, "reason": "", "mark": mark}


def first_line(text, limit: int = 120) -> str:
    """错误原文取首行，多行堆栈只留有用那行。"""
    lines = str(text or "").strip().splitlines()
    return (lines[0] if lines else "")[:limit]


def claim_task(cfg: Config, code: str, source: str, chat_id: int | None) -> str:
    """登记一个任务。同番号已有未结束任务时返回空串（与网页侧 409 同义）。"""
    job_id = "b" + hashlib.sha1(
        f"{time.time():.6f}:{code}:{source}".encode()).hexdigest()[:12]
    if not task_store(cfg).start(job_id, code, source[:300], owner="bot"):
        log.info("番号 %s 已在任务表里未结束，跳过重复提交", code)
        return ""
    task_store(cfg).update_cas(job_id, "", stage="downloading")
    # 入队事件由 store.start 与登记同一次提交；这里补上阶段与真正领取
    task_event(cfg, job_id, "stage", "downloading 投给迅雷")
    extra: dict = {}
    if chat_id is not None:
        extra["chat_id"] = chat_id
    btih = xunlei.info_hash_of(source)
    if btih:
        extra["infohash"] = btih        # 续接证据：重启后据此认回自己的迅雷任务
    if extra:
        task_store(cfg).update_cas(job_id, "", extra=extra)
    return job_id


_EXECUTION = ContextVar("bot_execution", default=None)


def execution_owner(cfg: Config, job_id: str) -> str:
    current = _EXECUTION.get()
    return current[1] if current and current[0] == job_id and current[2] is task_store(cfg) else ""


def task_update(cfg: Config, job_id: str, **fields) -> bool:
    return task_store(cfg).update_cas(job_id, execution_owner(cfg, job_id), **fields)


def finish_task(cfg: Config, job_id: str, stage: str, error: str = "") -> bool:
    """把任务落到终态，返回「是否真的写进去了」。**绝不抛异常**：这是失败收口路径本身。

    原先它直接抛：一旦任务表写不进去，except 分支里的 finish_task("failed")
    会二次抛出，从 run_task 逸出到 deliver（deliver 也不兜），整个 bot handler
    崩掉 —— 恰恰是最需要报告失败的时刻。与 task_event 同样按旁路处理。

    但"不抛"不等于"可以当成功"：返回 False 时调用方必须如实报告，否则面板里
    任务卡在旧状态、用户却看到 ✅，比崩溃更难查。
    """
    if not job_id:
        return True
    try:
        return task_store(cfg).finish_cas(job_id, execution_owner(cfg, job_id), stage, error)
    except Exception as exc:
        log.warning("任务 %s 终态 %s 落库失败：%s", job_id, stage, exc)
        return False


# ── 执行租约：与网页端同一套（store.claim_owner / beat / release），
# 心跳间隔同样取过期阈值的三分之一，漏两次也不会被误判失联。
PROC_ID = f"bot-{os.getpid()}-{int(time.time())}"
HEARTBEAT_INTERVAL = store.STALE_BEAT / 3
LEASES: dict[str, str] = {}
_lease_live: dict[str, bool] = {}
# 心跳线程的唤醒信号：让它不必等满一个 interval 就能退出（lease_end / 换代 / 测试复位）
_lease_wake: dict[str, threading.Event] = {}
_lease_lock = threading.Lock()


def lease_owner(job_id: str) -> str:
    """本次执行对这个任务的租约名（不存在就创建）。token 每次执行都不同。"""
    with _lease_lock:
        return _lease_owner_locked(job_id)


def _lease_owner_locked(job_id: str) -> str:
    token = LEASES.get(job_id)
    if not token:
        token = f"{PROC_ID}:{job_id}:{uuid4().hex[:12]}"
        LEASES[job_id] = token
    return token


def lease_begin(cfg: Config, job_id: str) -> bool:
    """开始执行：铸新 token → 领取 → 起心跳；领不到租约返回 False。

    顺序不能反：领取用的 owner 与心跳用的 owner 必须是同一个字符串，
    否则第一次 beat 就落空、表现为「刚拿到就被判失租」。
    """
    store_ = task_store(cfg)
    token = f"{PROC_ID}:{job_id}:{uuid4().hex[:12]}"
    if not store_.claim_owner(job_id, token):
        return False
    with _lease_lock:
        LEASES[job_id] = token
    _EXECUTION.set((job_id, token, store_))
    # 占位：心跳线程每 HEARTBEAT_INTERVAL 才跳一次，起跳前也要算「在场」，
    # 否则紧随其后的 require_lease 会把它当成没起心跳而过不去
    _lease_live[job_id] = True
    stop = threading.Event()

    def pulse() -> None:
        # 代际校验：同一 job 重新开始执行会换新 token 覆盖同一格，旧线程若只凭标志位
        # 判断，就会在换代后继续跑、失租时把**新的**标志位一起置 False。
        # 所以每轮先确认「这格 token 还是我铸的那个」，不是就安静退场。
        try:
            while _lease_live.get(job_id, False):
                if stop.wait(HEARTBEAT_INTERVAL):
                    break            # 被 lease_end / 换代 / 测试复位唤醒，立刻收工
                if not _lease_live.get(job_id, False):
                    break
                with _lease_lock:
                    if LEASES.get(job_id) != token:
                        break        # 已被新一次执行取代，本线程退场
                try:
                    ok = store_.beat(job_id, token)
                except Exception as exc:
                    log.warning("任务 %s 续租出错：%s", job_id, exc)
                    continue
                if not ok:
                    with _lease_lock:
                        if LEASES.get(job_id) == token:
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
    threading.Thread(target=pulse, daemon=True, name=f"bot-lease-{job_id}").start()
    return True


def lease_alive(cfg: Config, job_id: str) -> bool:
    """本次执行的租约是否仍有效；心跳失败过就直接判定失租。"""
    if not _lease_live.get(job_id, True):
        return False
    owner = execution_owner(cfg, job_id)
    if not owner:
        return False
    try:
        return bool(task_store(cfg).beat(job_id, owner))
    except Exception as exc:      # 库暂时不可用不等于失租
        log.warning("检查任务 %s 的租约失败：%s", job_id, exc)
        return False


def require_lease(cfg: Config, job_id: str) -> None:
    """关键节点自查：失租/取消就停手，绝不和另一个执行者抢同一目录。

    抛 LeaseLost（不是普通 RuntimeError）：外层据此区分「自己的任务失败了」
    和「这个任务已经归别人管了」—— 后者不能写终态、不能删外部下载任务。
    """
    if job_id and not lease_alive(cfg, job_id):
        raise LeaseLost(f"任务 {job_id} 已由另一个执行者接手，本端停止")
    if cancel_requested(cfg, job_id):
        raise JobCancelled("已停止")


def lease_end(cfg: Config, job_id: str) -> None:
    owner = execution_owner(cfg, job_id)
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
            task_store(cfg).release(job_id, owner)
        except Exception as exc:
            log.warning("释放任务 %s 的租约失败：%s", job_id, exc)


def defer_task(cfg: Config, job_id: str, reason: str = "") -> float:
    """配额类：写明确的唤醒时间。与网页端同一个 Store.defer()，两边语义一致。

    以前 bot 只把状态改成 deferred、不写 defer_until，调度器永远选不中它，
    表现就是「说明天自动补跑，其实再也不会跑」。
    """
    until = core.next_quota_retry()
    if job_id:
        task_store(cfg).defer(job_id, until, reason, owner=execution_owner(cfg, job_id))
        when = time.strftime("%m-%d %H:%M", time.localtime(until)) if until else "未知"
        task_event(cfg, job_id, "deferred",
                   f"{when} 唤醒 · {reason or '额度用尽，稍后重试'}")
    return until


async def deliver(ctx: ContextTypes.DEFAULT_TYPE, url: str, status: Message, force: bool = False) -> str:
    cfg: Config = ctx.application.bot_data["cfg"]
    client: httpx.AsyncClient = ctx.application.bot_data["http"]
    state: dict = ctx.application.bot_data["state"]

    async def progress(text: str) -> None:
        await edit(status, text)

    text = await run_task(
        cfg, client, state, url, progress, force=force, chat_id=status.chat_id
    )
    metrics.bump(cfg.metrics_path, metrics.classify(text))
    return text


async def scrape_bot_attachments(cfg: Config, work: Path, folder: str,
                                 files: list[Path], job_id: str = "") -> dict:
    """bot 侧的刮削附件：核心逻辑与网页共用 metadata.build_attachments。

    这里只做 bot 独有的事：读 cfg 开关、按任务 extra 的 locked 保住手改字段、
    把结果登记回任务 extra。刮削失败只记待补刮，绝不阻断上传。
    """
    if not cfg.scrape:
        return {"record": {}, "files": files, "written": [], "failed": [], "pending": False}

    code = codes.canonical(folder) or folder
    store_ = task_store(cfg)
    extra = (store_.get(job_id) or {}).get("extra") or {} if job_id else {}
    locks = set(extra.get("locked") or [])

    # 这里在 bot 的事件循环里：必须 await，不能在协程里 asyncio.run
    # （那样会抛「cannot be called from a running event loop」，刮削永远失败）。
    found: dict = {}
    try:
        found = await metatube.scrape(code) if metatube.configured() else {}
    except Exception as exc:                 # 刮削是可选环节
        log.info("刮 %s 失败（不影响上传）：%s", code, exc)
        found = {}
    # 番号对不上就不用：宁可留待补刮，也不把别人的元数据塞进归档
    if found and not metatube.matches(code, found):
        log.warning("MetaTube 返回 %s 与 %s 不符，丢弃", found.get("code"), code)
        found = {}

    out = metadata.build_attachments(work, code, files, lambda _c: found, locks=locks,
                                     old=extra.get("scraped") or {},
                                     want_nfo=cfg.scrape_nfo,
                                     want_poster=cfg.scrape_poster)
    if job_id:
        task_update(cfg, job_id, extra={**(store_.get(job_id).get("extra") or {}),
                                     "scraped": out["record"],
                                     "scrape_pending": out["pending"],
                                     "scrape_failed": out["failed"]})
    return out


async def next_candidate_magnet(code: str, tried_btih: set[str],
                                client: httpx.AsyncClient | None = None) -> str:
    """死种换源：搜候选，跳过已试过的 infohash，返回下一条候选磁力。

    返回空串表示没有可换的候选。按 **infohash** 判同源，而不是磁力字符串 ——
    同一种子换不同 tracker 参数不该被当成「新候选」反复撞同一条死种。
    排序口径沿用 sources.rank（字幕 → 高清 → 做种 → 体积），挑到的第一条
    没试过的就是下一条。
    """
    if not code:
        return ""
    try:
        items = await sources.search(code, client=client, limit=30)
    except Exception as exc:
        log.warning("换源搜候选失败 %s：%s", code, exc)
        return ""
    for item in items:
        magnet = item.get("magnet") or ""
        btih = xunlei.info_hash_of(magnet)
        if btih and btih not in tried_btih:
            return magnet
    return ""


async def run_task(
    cfg: Config,
    client: httpx.AsyncClient,
    state: dict,
    text: str,
    progress,
    force: bool = False,
    chat_id: int | None = None,
    resume_id: str = "",
) -> str:
    """番号 → 下载（磁力/直链都投给迅雷）→ 整理 → 按番号上传 Drive。

    resume_id：恢复已有任务时沿用原 job_id（不新建），重启续跑同一条记录。
    """
    xl = xunlei.Xunlei(cfg.xunlei_url, cfg.xunlei_parent, cfg.tmp_root, cfg.xunlei_token)
    sources = fetcher.sources(text)
    if not sources:
        return (
            "❌ 没找到可以下载的链接。\n发磁力或直链给我，例如：\n"
            "`magnet:?xt=urn:btih:...`\n`https://example.com/SSIS-123.mp4`"
        )

    # 番号与链接配对：`SSIS-123 magnet:a ABP-456 magnet:b` 里每个番号各归其后那个链接。
    # 不能对整条消息只 detect 一次 —— 那样只会拿到第一个番号，
    # 后面所有链接都会被归到同一个番号下（表现为「已存在」而不再下载）。
    codes_by_source = codes.pair(text)
    results: list[str] = []
    failures: list[str] = []

    for index, source in enumerate(sources, start=1):
        code = codes_by_source.get(source) or ""
        key = code or f"job{index}"

        known = state.get(code) if code else None
        if known and known.get("link") and not force:
            results.append(f"♻️ {code} 已存在：{known['link']}")
            continue

        # 2.6 统一准入：闸门关闭（恢复后待对账）时新消息/重试一律不开工，
        # 任务留在任务表等网页端放行。以前只有启动补跑查这里，
        # 用户在待对账期间发的消息照样会按旧快照开工。
        gate = scheduling_gate(cfg)
        if not gate["open"]:
            failures.append(f"{key} 调度闸门关闭（{gate['reason']}），本轮不执行")
            continue

        if resume_id and index == 1:
            job_id = resume_id              # 2.5 沿用原任务记录，不新建
            task_event(cfg, job_id, "resumed", f"重启续跑 {key}")
        else:
            # 与网页端共用任务表：同番号在两边只会有一个执行者
            job_id = claim_task(cfg, key, source, chat_id)
            if not job_id:
                failures.append(f"{key} 正在下载（任务表里已有未结束任务），跳过重复提交")
                continue

        info = await pipeline.probe(client, source) if fetcher.is_magnet(source) else None
        hint = ""
        if info:
            hint = "\n" + whatslink.describe(info)
            if not code:
                guessed = whatslink.guess_code(info)
                if guessed:
                    code = key = guessed
                    hint += f"\n（从文件名识别到番号 {guessed}）"
            if info.get("size") and quota_exceeded(cfg, int(info["size"])):
                until = defer_task(
                    cfg, job_id, f"体积超过今日剩余配额：{core.human_size(info['size'])}")
                failures.append(
                    f"{source[:40]}…: 体积超过今日剩余配额，"
                    f"{time.strftime('%m-%d %H:%M', time.localtime(until))} 自动重试")
                continue

        # 与网页端同一套执行租约：领不到就说明已有执行者在跑，绝不进同一目录
        if not lease_begin(cfg, job_id):
            failures.append(f"{key} 已由另一个执行者接手，跳过")
            continue
        task_event(cfg, job_id, "claimed", LEASES.get(job_id) or "")

        # 下载目录（迅雷建）与工作区（我们整理）分开，失败不动原件
        # 投递时用的键：下载目录按它建（迅雷已按它落盘），失败路径的日志也靠它
        key_safe = workspace.key_of(key)
        download_dir = workspace.staging(cfg.tmp_root, key_safe)
        work = workspace.workspace(cfg.state_dir, key_safe)
        # 清理用的键：以最终归档目录名为准。整理后 folder 可能与投递时的 key 不同
        # （投递时没给番号、从文件名或磁力信息里才认出来），那时仍按旧键清理，
        # 工作区名对不上、下载目录标记也对不上，原件会永远留在盘上。
        clean_key = key_safe
        uploaded = False          # 上传是否真的完成（只在这里置真，不再被文件清单覆盖）
        archived = False          # 归档记录是否已成功落盘；与 uploaded 同时成立才允许清原件
        outcome_ok = False        # 整条链路是否无异常走完；只决定 metrics 口径，不参与清理
        clock = {"download": 0.0, "organize": 0.0, "upload": 0.0}
        retried = {"download": 0, "upload": 0}
        job_size = 0
        job_started = time.monotonic()
        # 迅雷任务 id：失败时要在面板里一并清掉，否则僵尸任务越积越多
        handle: dict[str, str] = {}
        meta_info: dict = {}
        try:
            async def on_progress(percent, speed, status, _p=progress, _i=index):
                require_lease(cfg, job_id)
                mb = (speed or 0) / 1048576
                await _p(f"⏳ 下载 {_i}/{len(sources)} · {percent * 100:.1f}% · {mb:.1f}MB/s")

            await progress(f"⏳ 下载 {index}/{len(sources)}…{hint or '（磁力可能较慢）'}")

            # 换源重试的状态：cur["magnet"] 是当前要下的磁力（死种时会换成候选池里的
            # 下一条），tried_btih 记下已试过并失败的**infohash**（换源时跳过同一种子，
            # 避免 infohash 相同、tracker 参数不同被当成「新候选」反复撞同一条死种）。
            cur = {"magnet": source}
            tried_btih = {h for h in (xunlei.info_hash_of(source),) if h}

            async def do_download():
                # 同上：带本任务上次投递的证据，重试时识别自己的任务而不是拒绝
                prev = task_store(cfg).get(job_id) or {}
                got = await pipeline.download(
                    xl, client, cur["magnet"], download_dir, progress=on_progress,
                    poll=cfg.xunlei_poll, timeout=cfg.magnet_timeout,
                    stall_timeout=cfg.stall_timeout, settle=cfg.xunlei_settle,
                    on_task=lambda task_id, meta=None: handle.update(
                        id=task_id, ours=not (meta or {}).get("reused")),
                    session={"task": handle.get("id") or prev.get("xunlei_task") or "",
                             "infohash": str((prev.get("extra") or {}).get("infohash") or "")},
                )
                handle["dir"] = str(got.get("dir") or download_dir)
                return got

            async def on_dl_retry(attempt, error, wait):
                kind = retry.classify(error)
                label = {"transient": "网络", "dead": "疑似死种"}.get(kind, kind)
                await progress(f"↻ 下载失败（{label}），{round(wait)} 秒后重试（第 {attempt} 次）\n{error[:120]}")
                task_event(cfg, job_id, "retry",
                           f"下载第 {attempt} 次 · {round(wait)}s 后重试 · {first_line(error)}")

            policy = retry.RetryPolicy(retries=cfg.retries, base_delay=cfg.retry_base_delay)
            phase = time.monotonic()
            # 下载 + 死种换源：先按 policy 重试当前磁力；若是死种（不是网络抖动），
            # 反复撞同一条没有意义，去候选池换一条全新的重下，直到候选耗尽才放弃。
            while True:
                try:
                    await retry.run_with_retry(do_download, policy, on_retry=on_dl_retry)
                    break                      # 下载成功
                except Exception as exc:
                    if retry.classify(str(exc)) != retry.DEAD or not code:
                        raise
                    new_magnet = await next_candidate_magnet(code, tried_btih, client)
                    if not new_magnet:
                        raise                      # 没有可换的候选了，按死种收尾
                    new_btih = xunlei.info_hash_of(new_magnet)
                    if new_btih:
                        tried_btih.add(new_btih)
                    cur["magnet"] = new_magnet
                    policy.reset()               # 换源后重试计数重新开始
                    task_event(cfg, job_id, "stage",
                               f"死种换源：换一条候选重下（已试 {len(tried_btih)} 个种子）")
                    await progress(f"↻ {code} 疑似死种，已换一条候选重下（第 {len(tried_btih)} 个）")
            clock["download"] = time.monotonic() - phase
            retried["download"] = policy.attempts

            phase = time.monotonic()
            require_lease(cfg, job_id)          # downloading→organizing 前
            task_event(cfg, job_id, "stage", "organizing 整理文件名与目录")
            require_lease(cfg, job_id)
            workspace.drop_workspace(work, key_safe, "整理前清空")
            pipeline.stage_copy(Path(handle.get("dir") or download_dir), work, key_safe)
            found = fetcher.media_files(work) or fetcher.media_files(work, types=fetcher.MEDIA_EXT)
            final_code = code or codes.detect(found[0].name if found else "")
            folder, files = pipeline.organize(work, final_code or "")
            total = pipeline.total_size(files)
            # 清理按最终归档目录名走；下载目录仍是投递时那个（迅雷已按它建好）
            clean_key = workspace.key_of(folder)
            clock["organize"] = time.monotonic() - phase
            job_size = total

            if quota_exceeded(cfg, total):
                raise RuntimeError(
                    f"今日上传量已达 {cfg.daily_upload_gb}GB 上限，文件留在 /downloads 可明天重试"
                )

            # ── 刮削并生成附件（失败不阻断）：NFO/JSON/封面/清单跟视频一起传
            scraped = await scrape_bot_attachments(cfg, work, folder, files, job_id)
            if scraped["written"]:
                task_update(cfg, job_id, extra={
                    **(task_store(cfg).get(job_id).get("extra") or {}),
                    "files": [f.name for f in scraped["files"]]})

            require_lease(cfg, job_id)          # organizing→uploading 前
            task_event(cfg, job_id, "stage", f"uploading {folder}")
            backend = storage.get()
            target = (f"{cfg.remote}:{cfg.drive_root.strip('/')}/{folder}"
                      if backend.kind == "rclone" else f"{backend.kind}:{storage.rel(folder)}")
            await progress(f"⏳ 上传 {folder}（{core.human_size(total)}）…（含附件）"
                           if scraped["files"] != files else
                           f"⏳ 上传 {folder}（{core.human_size(total)}）…")

            upload_rel = target.split(":", 1)[1]

            async def do_upload():
                require_lease(cfg, job_id)
                if backend.kind != "rclone":
                    # 适配器是同步实现（网页在后台线程用），这里丢到线程里跑
                    rel = upload_rel
                    await asyncio.to_thread(backend.ensure_dir, rel)
                    await asyncio.to_thread(
                        lambda: backend.upload_dir(work, rel,
                                                   transfers=cfg.upload_transfers,
                                                   chunk=cfg.upload_chunk))
                    return
                await rclone(cfg, "mkdir", target, timeout=120.0)
                rc, _, err = await rclone(
                    cfg, "copy", str(work), target,
                    "--transfers", str(cfg.upload_transfers),
                    "--drive-chunk-size", cfg.upload_chunk,
                    "--retries", "2", "--timeout", "30m",
                    "--drive-stop-on-upload-limit",
                    timeout=7200.0,
                )
                if rc != 0:
                    if "uploadLimitExceeded" in err or "quota" in err.lower():
                        raise RuntimeError("Drive 上传配额已用完，明日再试")
                    raise RuntimeError(f"rclone 上传失败：{err.strip()[:300]}")

            async def on_up_retry(attempt, error, wait):
                await progress(f"↻ 上传失败，{round(wait)} 秒后重试（第 {attempt} 次）\n{error[:120]}")
                task_event(cfg, job_id, "retry",
                           f"上传第 {attempt} 次 · {round(wait)}s 后重试 · {first_line(error)}")

            up_policy = retry.RetryPolicy(retries=cfg.retries, base_delay=cfg.retry_base_delay)
            phase = time.monotonic()
            await retry.run_with_retry(do_upload, up_policy, on_retry=on_up_retry)
            clock["upload"] = time.monotonic() - phase
            retried["upload"] = up_policy.attempts
            require_lease(cfg, job_id)
            add_usage(cfg, total)

            if backend.kind != "rclone":
                # CD2 会归一化目录名（全角替换、削尾部空白/点号），记账得按真实名
                probe = getattr(backend, "real_folder", None)
                actual = await asyncio.to_thread(probe, upload_rel) if probe else folder
                if actual and actual != folder:
                    log.info("远端目录名被归一化：%s → %s", folder, actual)
                    folder = actual
                    upload_rel = f"{upload_rel.rsplit('/', 1)[0]}/{folder}"
                    target = f"{backend.kind}:{upload_rel}"
                try:
                    link = await asyncio.to_thread(backend.link, upload_rel)
                except Exception as exc:
                    log.warning("取 %s 的分享链接失败：%s", folder, exc)
                    link = None
            else:
                link = await folder_link(cfg, target)
            # 兜底元数据（与网页 _finish 的 meta.lookup 同语义）：查不到/出异常都不影响记账
            try:
                meta_info = await meta.lookup(folder)
            except Exception as exc:
                log.warning("查 %s 的元数据失败：%s", folder, exc)
            require_lease(cfg, job_id)          # 写归档前
            # 与网页 _finish 同一份字段契约（metadata.archive_record）：
            # 以前 bot 写出的记录没有 meta/manifest/infohash/locked/scrape_pending，
            # 之后的补刮、去重、校验对着它就无据可依。
            # 两个语义分开的变量：uploaded_paths 只是「本次上传清单」（只读，给 names 用，
            # 绝不拿它当成功标志：以前 uploaded 被这份清单覆盖成真值，记账一步抛异常也会
            # 走到清理分支，把失败任务的原件删掉）。
            uploaded_paths = scraped["files"]
            task_extra = task_store(cfg).get(job_id).get("extra") or {} \
                if job_id else {}
            names = [f.name for f in uploaded_paths]
            state[folder] = metadata.archive_record(
                folder, target, link, names, total, source, work,
                scraped=meta_info or scraped["record"] or task_extra.get("scraped") or {},
                manifest_files=names,
                infohash=str(task_extra.get("infohash") or ""),
                locks=task_extra.get("locked") or (),
                pending=bool(scraped["pending"] or task_extra.get("scrape_pending")),
                at=datetime.now().isoformat(timespec="seconds"),
                backend=backend.kind,
                root=target.split(":", 1)[1].rsplit("/", 1)[0],
                rel=target.split(":", 1)[1],
                remote=target.split(":", 1)[0] if backend.kind == "rclone" else "",
            )
            require_lease(cfg, job_id)
            core.update_state(cfg.state_path, {folder: state[folder]})
            # 「归档记录已落盘」：必须放在所有落盘动作（写状态文件）之后。
            # finish_task 在库坏了时会抛，那时记录已经写进 state.json，原件清掉是对的；
            # 但 update_state 抛异常时这份记录没落盘，archived 保持 False → 原件留下可续传。
            archived = True
            uploaded = True        # 上传确实成功了：到这里才置真（记账已落盘，顺序固定）
            # 任务表记账失败不影响归档结果（记录已落盘），但它返回 False 说明
            # 面板里这条任务会停在旧状态；所以要如实告诉用户，不能只报 ✅。
            task_done = finish_task(cfg, job_id, "done")
            if not task_done and not lease_alive(cfg, job_id):
                raise LeaseLost("归档终态写入前已被其他执行者接手")
            task_event(cfg, job_id, "done",
                       " · ".join(x for x in (folder, core.human_size(total), link or "") if x))
            line = f"✅ {folder} · {core.human_size(total)}" + (
                f"\n📁 {link}" if link else "")
            if not task_done:
                line += "\n⚠️ 已归档，但任务表没写进去（面板里可能仍显示进行中）"
            results.append(line)
            # 统计口径与清理口径**分开**：archived 决定「记录已落盘、可以清原件」，
            # outcome_ok 决定「这条链路是否完整走完」。任务表没落库时统计记 fail，
            # 与给用户的 ⚠️ 保持一致 —— 同一任务不能一边说失败一边记 ok。
            outcome_ok = task_done
        except JobCancelled as exc:
            # 用户在 Web 上点了停止（跨进程取消标记）：正常收尾成 cancelled，
            # 不是失败。只停我们自己的迅雷任务，留给重试的证据不动。
            log.info("任务 %s 被用户取消：%s", job_id, exc)
            finish_task(cfg, job_id, "cancelled", "已停止")
            task_event(cfg, job_id, "cancelled", first_line(str(exc)) or "已停止")
            if lease_alive(cfg, job_id) and handle.get("id") and handle.get("ours", True):
                try:
                    await xl.remove(client, handle["id"])
                except Exception as exc2:      # noqa: BLE001 停止流程不崩
                    log.warning("取消时删除迅雷任务 %s 失败：%s", handle.get("id"), exc2)
        except LeaseLost as exc:
            # 失租只退出：任务已归新执行者。不能写 failed（会覆盖接管者的状态），
            # 也不能删迅雷任务 —— handle 里那个 id 现在可能正被接管者续接。
            log.info("任务 %s 失租，旧线程安静退出：%s", job_id, exc)
            task_event(cfg, job_id, "lease_lost", first_line(str(exc)))
        except Exception as exc:
            log.exception("下载失败 %s", source[:80])
            if not lease_alive(cfg, job_id):
                continue
            if retry.classify(str(exc)) == retry.QUOTA:
                # 配额类不是失败，是「今天没额度」：留待办，明天开机自动补跑
                until = defer_task(cfg, job_id, str(exc)[:200])
                failures.append(
                    f"{source[:40]}…: {str(exc)[:120]}"
                    f"（今日额度用尽，{time.strftime('%m-%d %H:%M', time.localtime(until))} 自动重试）")
            else:
                failures.append(f"{source[:40]}…: {str(exc)[:160]}")
                finish_task(cfg, job_id, "failed", str(exc)[:200])
                task_event(cfg, job_id, "failed", first_line(str(exc)))
            if lease_alive(cfg, job_id) and handle.get("id") and handle.get("ours", True):
                await xl.remove(client, handle["id"])
        finally:
            try:
                task_store(cfg).clear_cancel(job_id, owner=execution_owner(cfg, job_id))   # 抹掉取消标记，重试不会被卡住
            except Exception as exc:            # noqa: BLE001 收尾不抛
                log.warning("清取消标记失败：%s", exc)
            metrics.record_timing(cfg.metrics_path, {
                "code": key,
                "size": job_size,
                # 只有真正成功（上传完成、记录落盘、且没有走到 except）才算 ok
                "outcome": "ok" if outcome_ok else "fail",
                "retries": retried["download"] + retried["upload"],
                "download": clock["download"],
                "organize": clock["organize"],
                "upload": clock["upload"],
                "total": time.monotonic() - job_started,
            })
            # 只看「这一个任务」是否成功：之前用累计的 failures 判断，
            # 前面失败过一次，后面成功任务的目录就再也不会被清，磁盘一路涨。
            # 失败时保留目录，方便 /retry 续传，但整体有容量上限。
            # 清理前必须确认目录仍归自己：失租时另一个执行者可能正在用同一目录。
            if uploaded and archived and outcome_ok and lease_alive(cfg, job_id):
                workspace.drop_workspace(work, clean_key, "上传成功")
                # 下载目录的归属标记写的是投递时的键，必须按 key_safe 校验
                if lease_alive(cfg, job_id):
                    workspace.drop(download_dir, key_safe, "上传成功")
                # 迅雷重名时真实目录是 <键>-N，预设的 dest 删了也不代表原件清掉了
                real_dir = Path(handle.get("dir") or "")
                if real_dir and real_dir != download_dir and lease_alive(cfg, job_id):
                    workspace.drop(real_dir, key_safe, "上传成功")
            elif uploaded and archived:
                log.warning("任务 %s 已失租，跳过目录清理：%s %s", job_id, work, download_dir)
            elif uploaded:
                # 传上去了但归档没落盘（记账抛异常）：任务记为失败，原件留着续传排错
                log.warning("任务 %s 已上传但归档记录未落盘，保留原件：%s %s",
                            job_id, handle.get("dir") or download_dir, work)
            else:
                # 失败两份都留着：原件可续传，工作区便于排查
                log.info("失败任务保留在原地：%s", handle.get("dir") or download_dir)
            lease_end(cfg, job_id)

    parts = results + [f"⚠️ {len(failures)} 项失败：" + "；".join(failures)] if failures else results
    return "\n".join(parts) or "❌ 没有任何结果"


async def edit(message: Message, text: str) -> None:
    try:
        await message.edit_text(text)
    except TelegramError:
        pass


def pick_url(args: list[str], text: str) -> str | None:
    """命令参数里找链接；没有就用回复的那条消息。"""
    for token in args:
        if token.startswith("http"):
            return token
    found = core.extract_url(text or "")
    return found


def mentioned_key(args: list[str]) -> str | None:
    for token in args:
        digits = re.sub(r"\D", "", token)
        if len(digits) >= 10:
            return digits
    return None


async def on_retry(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """补抓上次没入库的媒体；带链接时对该推文重跑，否则用最后一条待办。"""
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    cfg: Config = ctx.application.bot_data["cfg"]
    if user.id not in cfg.allowed:
        return
    state: dict = ctx.application.bot_data["state"]
    pending: dict = ctx.application.bot_data["pending"]

    text = " ".join(ctx.args or "")
    if not fetcher.sources(text) and message.reply_to_message:
        text = message.reply_to_message.text or message.reply_to_message.caption or ""
    if not fetcher.sources(text):
        latest = sorted(pending.values(), key=lambda p: p.get("ts", 0), reverse=True)
        if not latest:
            await message.reply_text("没有待重试的任务。用法：`/retry <磁力或直链>`，或回复一条含链接的消息")
            return
        text = latest[0].get("url") or ""
    if not fetcher.sources(text):
        await message.reply_text("⛔ 需要磁力链接或直链")
        return

    status = await message.reply_text("⏳ 重试中…")
    sem: asyncio.Semaphore = ctx.application.bot_data["sem"]
    async with sem:
        try:
            reply = await deliver(ctx, text, status, force=True)
        except Exception as exc:
            log.exception("重试失败")
            reply = f"❌ 重试失败：{str(exc)[:400]}（可用 /retry 再试）"
    await edit(status, reply)


async def on_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """列出已归档的番号与 Drive 链接。"""
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    cfg: Config = ctx.application.bot_data["cfg"]
    if user.id not in cfg.allowed:
        return
    state: dict = ctx.application.bot_data["state"]
    if not state:
        await message.reply_text("还没有归档记录。")
        return
    rows = sorted(state.values(), key=lambda r: r.get("at", ""), reverse=True)[:20]
    lines = [f"📚 已归档 {len(state)} 个番号（最近 20 个）"]
    for r in rows:
        size = r.get("size") or 0
        lines.append(
            f"· {r.get('code') or '?'}　{size / 1073741824:.2f}GB" if size
            else f"· {r.get('code') or '?'}"
        )
        if r.get("link"):
            lines.append(f"  {r['link']}")
    text = "\n".join(lines)
    await message.reply_text(text[:3500])


async def on_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """删除一个番号的整目录：/del <番号>，确认执行加 --yes。

    state 的键就是归一的番号（见 run_task 里用 folder 作键），所以这里按番号查。
    """
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    cfg: Config = ctx.application.bot_data["cfg"]
    if user.id not in cfg.allowed:
        return
    args = ctx.args or []
    target_code = next((a for a in args if not a.startswith("-")), "")
    if not target_code:
        await message.reply_text("用法：`/del <番号>`，确认执行加 `--yes`")
        return
    state: dict = ctx.application.bot_data["state"]
    key = codes.safe_folder(codes.normalize(target_code))
    record = state.get(key)
    if not record:
        candidates = [k for k in state if k.upper() == key.upper()]
        if not candidates:
            await message.reply_text(f"未找到 {key} 的记录（`/list` 可查看已归档番号）")
            return
        key = candidates[0]
        record = state[key]
    if "--yes" not in args:
        await message.reply_text(
            f"将删除 {key}：\n{record.get('path')}\n\n确认请发送 `/del {key} --yes`"
        )
        return

    target = record.get("path") or ""
    backend = storage.get()
    try:
        metadata.validate_archive_backend(record, backend.kind, cfg.remote)
    except ValueError as exc:
        await message.reply_text(f"❌ 删除中止：{str(exc)[:300]}")
        return
    if backend.kind != "rclone":
        code = str(record.get("code") or key)
        if ".." in code or "/" in code:
            await message.reply_text("⛔ 路径校验未通过，已拒绝删除")
            return
        try:
            # 按记录定位（与网页同口径）：云添加的归档在 CLOUD_DEST，
            # 不跟全局 STORAGE_ROOT 走
            rel = str(record.get("rel") or storage.rel(code)).strip("/")
            if any(bit in (".", "..") for bit in rel.split("/")) or "\\" in rel:
                raise storage.StorageError("归档路径不合法")
            receipt = await asyncio.to_thread(backend.purge, rel)
        except storage.StorageError as exc:
            await message.reply_text(f"❌ 删除失败：{str(exc)[:300]}")
            return
        state.pop(key, None)
        core.save_state(cfg.state_path, state)
        await message.reply_text(f"🗑 已删除 {key}（{backend.kind}：{receipt}）")
        return
    root = f"{cfg.remote}:{cfg.drive_root.strip('/')}" if cfg.drive_root else f"{cfg.remote}:"
    if record.get("backend") == "rclone" and record.get("rel"):
        rel = str(record["rel"]).strip("/")
        if any(bit in (".", "..") for bit in rel.split("/")) or "\\" in rel:
            await message.reply_text("⛔ 归档路径校验未通过")
            return
        target = f"{cfg.remote}:{rel}"
        root = f"{cfg.remote}:"
    if not (target.startswith(root + "/") or root.endswith(":") and target.startswith(root)) or ".." in target:
        await message.reply_text("⛔ 路径校验未通过，已拒绝删除")
        return
    rc, _, err = await rclone(cfg, "purge", target, timeout=600.0)
    if rc != 0:
        await message.reply_text(f"❌ 删除失败：{err.strip()[:300]}")
        return
    state.pop(key, None)
    core.save_state(cfg.state_path, state)
    await message.reply_text(f"🗑 已删除 {key}（文件在 Drive 回收站，可恢复）")


async def on_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """`/status`：只读汇总运行状态，纯本地读取、零副作用。

    用户在手机上想看一眼「现在什么情况」，不能开电脑看网页面板。这里只读
    bot 自己那几份本地状态（任务表 / metrics / 用量 / 磁盘 / 待补跑 / 闸门），
    **不 import app.web**：web 是另一个进程，有自己的 STORE/STATE 路径。
    """
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    cfg: Config = ctx.application.bot_data["cfg"]
    if user.id not in cfg.allowed:
        return

    # 任务表：unfinished_any 含 deferred（等额度），比 unfinished 更如实反映「还要跑」
    try:
        active = task_store(cfg).unfinished_any()
    except Exception as exc:
        log.warning("/status 读任务表失败：%s", exc)
        active = []
    # 闸门：恢复后待对账时调度是关的，运维最该知道这件事
    gate = scheduling_gate(cfg)
    # 今日完成/失败：metrics.snapshot 里 total 是 {ok, partial, error, ...}
    snap = metrics.snapshot(cfg.metrics_path)
    total = snap.get("total") or {}
    done = int(total.get("ok", 0)) + int(total.get("partial", 0))
    failed = sum(int(v) for k, v in total.items() if k not in ("ok", "partial"))
    used = today_bytes(cfg)
    free = diskspace.free_bytes(cfg.tmp_root)

    lines = ["📊 状态"]
    lines.append(f"· 进行中 {len(active)} 个"
                 + ("（含等额度的）" if any(j.get("stage") == "deferred" for j in active) else ""))
    lines.append(f"· 今日完成 {done} · 失败 {failed}")
    if cfg.daily_upload_gb:
        cap = cfg.daily_upload_gb * 1024 ** 3
        lines.append(f"· 今日上传 {core.human_size(used)} / {cfg.daily_upload_gb}GB")
    else:
        lines.append(f"· 今日上传 {core.human_size(used)}（不限额）")
    lines.append("· 磁盘剩余 " + (core.human_size(free) if free is not None else "无法探测"))
    lines.append(f"· 待补跑 {len(load_pending(cfg))} 条")
    if not gate["open"]:
        lines.append(f"⛔ 调度闸门关闭：{gate['reason']}")
    await message.reply_text("\n".join(lines))


async def on_subs(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """`/subs`：只读列出订阅，与网页面板共用同一张任务库里的 state 表（键 "subs"）。

    订阅表是 web 侧写的，bot 进程读它靠 `store.Store(cfg.jobs_db).state_get("subs")`。
    docker-compose 里 bot 与 web 挂同一个 ./data，所以 jobs_db 是同一个库文件。
    读不到（库不存在 / 没有 subs 键）就回一句人话，不能崩溃。
    """
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    cfg: Config = ctx.application.bot_data["cfg"]
    if user.id not in cfg.allowed:
        return

    try:
        subs = store.Store(cfg.jobs_db).state_get("subs")
    except Exception as exc:
        log.warning("/subs 读订阅表失败：%s", exc)
        await message.reply_text("读订阅表失败，稍后再试。")
        return
    if not subs:
        await message.reply_text("还没有订阅。")
        return

    rows = []
    for key, sub in sorted(subs.items()):
        name = sub.get("name") or sub.get("id") or key
        kind = "演员" if sub.get("kind") == "actor" else "系列"
        enabled = "" if sub.get("enabled", True) else "（停用）"
        last_run = sub.get("last_run") or "从未"
        last_result = sub.get("last_result") or ""
        rows.append(f"· {name}{enabled} · {kind}\n  上次 {last_run}"
                    + (f" · {last_result}" if last_result else ""))
    head = f"📡 订阅 {len(subs)} 个"
    await message.reply_text((head + "\n" + "\n".join(rows))[:3500])


async def on_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message:
        await message.reply_text(
            "用法：\n"
            "• 发磁力或直链（可带番号，如 `SSIS-123 magnet:?xt=...`）→ 下载后按番号存 Drive\n"
            "• 只发番号（如 `SSIS-123`）→ 自动搜磁力，挑字幕/高清/最大的那条下载\n"
            "• `/find SSIS-123` → 只列候选磁力，不下载\n"
            "• 没写番号时从文件名里识别，识别不到归到「未分类-时间」\n"
            "• `/retry` 或 `/retry <磁力>` → 重试失败的任务\n"
            "• `/list` → 查看已归档的番号\n"
            "• `/status` → 查看运行状态（进行中 / 今日完成 / 磁盘 / 闸门）\n"
            "• `/subs` → 查看订阅列表\n"
            "• `/del <番号>` → 查看将删除的目录，加 `--yes` 执行\n"
            "• `/help` → 本说明"
        )


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    text = (message.text or message.caption or "") if message else ""
    if not message or not text or not user:
        return
    cfg: Config = ctx.application.bot_data["cfg"]
    if not fresh_update(update):
        log.info("跳过过期消息 %s", message.message_id)
        return
    if user.id not in cfg.allowed:
        await message.reply_text(
            f"⛔ 未授权。你的 user id：{user.id}，把它加进 ALLOWED_USER_IDS 后重启。"
        )
        return
    if not fetcher.sources(text):
        # 只发番号也认：自动按番号搜磁力，挑一条最像样的再走原流程
        code = codes.detect(text)
        found = await auto_magnet(code) if code else None
        if not found:
            return
        text = f"{code} {found['magnet']}"
        hint = (f"🔍 自动搜到 {code} 的磁力"
                f"（{found.get('size_text') or '体积未知'}"
                f"{'，有字幕' if found.get('has_subtitle') else ''}"
                f"{'，高清' if found.get('is_hd') else ''}）")
    else:
        hint = ""

    code = codes.detect(text)
    status = await message.reply_text(
        f"{hint + chr(10) if hint else ''}⏳ 已收到{'　番号 ' + code if code else '下载任务'}…"
    )
    sem: asyncio.Semaphore = ctx.application.bot_data["sem"]
    try:
        async with sem:
            try:
                reply = await deliver(ctx, text, status)
            except Exception as exc:
                log.exception("下载失败")
                reply = f"❌ {str(exc)[:400]}"
    except asyncio.CancelledError:
        await edit(status, "⚠️ 服务重启，任务已记录，重启后自动补跑")
        raise
    await edit(status, reply)

async def auto_magnet(code: str) -> dict | None:
    """番号 → 最优磁力。搜不到就返回 None（消息多半只是聊天，忽略即可）。"""
    if not code:
        return None
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
            items = await sources.search(code, client=client, limit=5)
    except Exception as exc:
        log.warning("搜 %s 的磁力失败：%s", code, exc)
        return None
    if not items:
        log.info("所有磁力源都没有 %s 的结果", code)
        return None
    log.info("自动搜到 %s 的磁力 %d 条，取第一条", code, len(items))
    return items[0]

async def on_find(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """`/find SSIS-123`：只列候选磁力，不下载。"""
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    cfg: Config = ctx.application.bot_data["cfg"]
    if user.id not in cfg.allowed:
        return
    code = codes.detect(" ".join(ctx.args or []))
    if not code:
        await message.reply_text("用法：/find SSIS-123")
        return
    items = []
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
            items = await sources.search(code, client=client, limit=5)
    except Exception as exc:
        await message.reply_text(f"❌ 搜索失败：{str(exc)[:200]}")
        return
    if not items:
        await message.reply_text(f"🔍 {code} 没搜到磁力")
        return
    # 附上资源信息：只看体积与标签，判断不了这是不是你要的那部
    info = None
    try:
        info = await meta.lookup(code)
    except Exception as exc:
        log.info("取 %s 元数据失败：%s", code, exc)
    head = f"🔍 {code} 共 {len(items)} 条候选（点链接直接转发给我即可下载）"
    if info:
        bits = [info.get("date"), info.get("duration"), info.get("maker")]
        actors = (info.get("actors") or [])[:3]
        if actors:
            bits.append(" / ".join(actors))
        head = (f"🎬 {info.get('title') or code}\n"
                + (f"{' · '.join(b for b in bits if b)}\n" if any(bits) else "")
                + head)
    lines = [head]
    for i, item in enumerate(items, start=1):
        tags = "·".join(t for t in (
            "字幕" if item.get("has_subtitle") else "",
            "高清" if item.get("is_hd") else "",
            f"{item['seeders']}种" if item.get("seeders") else "",
            item.get("source") or "",
        ) if t)
        lines.append(f"{i}. {item.get('size_text') or '体积未知'} {tags}\n`{item['magnet']}`")
    await message.reply_text("\n".join(lines), parse_mode="Markdown")

# ---------------------------------------------------------------- 待办补跑


def load_pending(cfg: Config) -> dict:
    pending: dict = {}
    directory = Path(cfg.pending_dir)
    if not directory.is_dir():
        return pending
    for path in directory.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        key = record.get("key") or path.stem
        pending[key] = record
    return pending


def fresh_update(update, max_age: int = 900) -> bool:
    """忽略积压太久的消息，避免停机后一次性补刷。"""
    message = update.effective_message
    if message is None or message.date is None:
        return False
    age = time.time() - message.date.timestamp()
    return age <= max_age


async def bootstrap(application: Application) -> None:
    """post_init 里只做注册，补跑放后台，避免拖慢启动。"""
    application.create_task(resume_pending(application))


async def resume_pending(application: Application) -> None:
    """启动时补跑上次没完成的下载任务，并把结果发回原会话。

    2.5：恢复的主数据源是**共享任务表**（jobs 表）。以前只读 pending/*.json，
    而新任务只写 jobs 表 —— bot 单独重启后中断的任务没人接，番号还被它占着，
    /retry 也撞唯一索引。旧 pending 文件仍兼容（历史遗留），一条任务以
    jobs 表为准，避免两边重复补跑。
    """
    cfg: Config = application.bot_data["cfg"]
    pending: dict = application.bot_data["pending"]
    # 与网页端同一份调度闸门：恢复后「待对账」期间一律不补跑。
    # bot 与 web 是两条独立进程，靠库内标记 + 同目录旁路文件互相看见，不靠内存开关。
    gate = scheduling_gate(cfg)
    if not gate["open"]:
        log.warning("调度闸门关闭（%s），本次启动不补跑；对账后由网页端放行", gate["reason"])
        return

    # ── 主路径：共享任务表里「还要跑」的任务（不含 deferred：它等额度到期唤醒）
    try:
        todo = [j for j in task_store(cfg).unfinished_any() if j.get("stage") != "deferred"]
    except Exception as exc:                    # noqa: BLE001 读不到就退回旧路径
        log.warning("读取共享任务表失败，退回 pending 文件：%s", exc)
        todo = []
    for job in todo:
        job_id = job["id"]
        source = job.get("source") or ""
        code = job.get("code") or ""
        if not source:
            continue
        chat_id = (job.get("extra") or {}).get("chat_id")
        sent = None
        if chat_id:
            try:
                sent = await application.bot.send_message(chat_id, "⏳ 重启后续跑：解析媒体…")
            except TelegramError as exc:
                log.warning("续跑时无法通知会话 %s：%s", chat_id, exc)

        async def progress(text, _sent=sent):
            if _sent is not None:
                await edit(_sent, text)

        try:
            text = await run_task(
                cfg, application.bot_data["http"], application.bot_data["state"],
                source, progress, force=True, chat_id=chat_id, resume_id=job_id,
            )
            if sent is not None:
                await edit(sent, text)
        except Exception as exc:                # noqa: BLE001 单条失败不拖住其余
            log.exception("续跑任务 %s 失败", job_id)
            finish_task(cfg, job_id, "failed", str(exc)[:200])
            if sent is not None:
                try:
                    await edit(sent, f"❌ 续跑失败：{first_line(str(exc))}")
                except TelegramError:
                    pass
    if todo:
        log.info("从共享任务表续跑 %d 条", len(todo))

    if not pending:
        return
    log.info("发现 %d 条待补跑记录（旧 pending 文件）", len(pending))
    for key, record in list(pending.items()):
        url = record.get("url") or ""
        chat_id = record.get("chat_id")
        if not url:
            pending.pop(key, None)
            continue
        sent = None
        if chat_id:
            try:
                sent = await application.bot.send_message(chat_id, "⏳ 重启后补跑：解析媒体…")
            except TelegramError as exc:
                log.warning("补跑时无法通知会话 %s：%s", chat_id, exc)

        async def progress(text: str, _sent=sent) -> None:
            if _sent is not None:
                await edit(_sent, text)

        try:
            text = await run_task(
                cfg, application.bot_data["http"], application.bot_data["state"],
                url, progress, force=True, chat_id=chat_id,
            )
            application.bot_data["pending"].pop(key, None)
            if sent is not None:
                await edit(sent, "🔁 补跑完成\n" + text)
        except Exception as exc:  # 单条失败不影响其他补跑
            log.exception("补跑失败 %s：%s", key, exc)
            if sent is not None:
                await edit(sent, f"❌ 补跑失败：{str(exc)[:300]}（可用 /retry 再试）")


# ---------------------------------------------------------------- 启动


async def self_check(cfg: Config) -> bool:
    ok = True
    async with httpx.AsyncClient() as client:
        try:
            me = await client.get(f"https://api.telegram.org/bot{cfg.token}/getMe", timeout=15.0)
            info = me.json()
            good = bool(info.get("ok"))
            print(f"telegram token: {'OK @' + info['result']['username'] if good else info.get('description','无效')}")
            ok &= good
        except Exception as exc:
            print(f"telegram token: 校验失败 {exc}")
            ok = False

    xl = xunlei.Xunlei(cfg.xunlei_url, cfg.xunlei_parent, cfg.tmp_root, cfg.xunlei_token)
    try:
        async with xl.client() as client:
            await xl.alive(client)
        print(f"迅雷: OK {cfg.xunlei_url} parent={cfg.xunlei_parent} tmp={cfg.tmp_root}")
    except Exception as exc:
        print(f"迅雷: 不可用 {cfg.xunlei_url} ({exc})")
        ok = False

    # 容器里最容易出的两类错：面板地址写成 127.0.0.1（连的是自己），
    # 以及迅雷下载目录没挂进容器（任务成功但看不见文件）。这两项必须在容器内自测。
    if not cfg.tmp_root or not Path(cfg.tmp_root).is_dir():
        print(f"下载目录: 不存在 {cfg.tmp_root}（检查 XUNLEI_DOWNLOADS_HOST 挂载）")
        ok = False
    elif not os.access(cfg.tmp_root, os.W_OK | os.X_OK):
        print(f"下载目录: 没有写权限 {cfg.tmp_root}")
        ok = False
    else:
        print(f"下载目录: OK {cfg.tmp_root}")
    if not Path(cfg.state_dir).is_dir() or not os.access(cfg.state_dir, os.W_OK):
        print(f"工作区: 不可写 {cfg.state_dir}（检查 ./work:/work 挂载与权限）")
        ok = False
    else:
        print(f"工作区: OK {cfg.state_dir}")

    try:
        backend = storage.get()
    except Exception as exc:                 # 后端没配好（缺 URL / 缺 grpcio）也要说得清楚
        print(f"存储后端: {exc}")
        print("自检 未通过")
        return False
    if backend.kind == "rclone":
        for binary in ("rclone",):
            found = shutil.which(binary)
            print(f"{binary}: {found or '未安装'}")
            ok &= bool(found)

        root = f"{cfg.remote}:{cfg.drive_root.strip('/')}" if cfg.drive_root else f"{cfg.remote}:"
        rc, _, err = await rclone(cfg, "lsd", root, timeout=90.0)
        if rc != 0:
            rc, _, err = await rclone(cfg, "lsd", f"{cfg.remote}:", timeout=90.0)
        print(f"rclone {cfg.remote}: {'OK' if rc == 0 else err.strip()[:300]}")
        ok &= rc == 0
        if rc == 0:
            rc2, _, err2 = await rclone(cfg, "mkdir", f"{root}/_healthcheck", timeout=90.0)
            print(f"Drive 写权限: {'OK' if rc2 == 0 else err2.strip()[:200]}")
            if rc2 == 0:
                await rclone(cfg, "rmdir", f"{root}/_healthcheck", timeout=90.0)
            ok &= rc2 == 0
    else:
        # 另两个后端（OpenList / CloudDrive2）自带 check()：连通性 + 可写 + 配额
        result = await asyncio.to_thread(storage.check)
        print(f"存储后端 {result.get('kind')}: {result.get('detail') or result.get('error')}")
        ok &= bool(result.get("ok"))

    print("自检", "通过" if ok else "未通过")
    return ok


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    cfg = Config.from_env()

    if "--check" in sys.argv:
        sys.exit(0 if asyncio.run(self_check(cfg)) else 1)

    if not cfg.token:
        sys.exit("缺少 TELEGRAM_BOT_TOKEN")
    if not cfg.allowed:
        log.warning("ALLOWED_USER_IDS 为空：所有消息都会被拒绝")
    # 启动时打印一次闸门判定：运维据此确认 bot 与 web 读的是同一个判据
    gate = scheduling_gate(cfg)
    if gate["open"]:
        log.info("调度闸门：开放（%s）", cfg.jobs_db)
    else:
        log.warning("调度闸门：关闭 · %s（%s）", gate["reason"], cfg.jobs_db)
    Path(cfg.tmp_root).mkdir(parents=True, exist_ok=True)

    async def shutdown(application: Application) -> None:
        await application.bot_data["http"].aclose()

    app = (
        Application.builder()
        .token(cfg.token)
        .read_timeout(30)
        .connect_timeout(20)
        .post_shutdown(shutdown)
        .post_init(bootstrap)
        .build()
    )
    Path(cfg.pending_dir).mkdir(parents=True, exist_ok=True)
    app.bot_data.update(
        cfg=cfg,
        state=core.load_state(cfg.state_path),
        pending=load_pending(cfg),
        sem=asyncio.Semaphore(cfg.concurrency),
        http=httpx.AsyncClient(
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; x-drive/1.0)",
                "Referer": "https://x.com/",
            },
        ),
    )
    app.add_handler(
        MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, on_message)
    )
    app.add_handler(CommandHandler("find", on_find))
    app.add_handler(CommandHandler("retry", on_retry))
    app.add_handler(CommandHandler("list", on_list))
    app.add_handler(CommandHandler("status", on_status))
    app.add_handler(CommandHandler("subs", on_subs))
    app.add_handler(CommandHandler("del", on_delete))
    app.add_handler(CommandHandler(["help", "start"], on_help))
    log.info(
        "启动：remote=%s root=%s 并发=%d 待补跑=%d",
        cfg.remote, cfg.drive_root, cfg.concurrency, len(app.bot_data["pending"]),
    )
    # drop_pending_updates=False：停机期间用户发的链接不再被 Telegram 侧丢弃；
    # 旧的积压由 processed_update 里的时间窗过滤，避免重启后一次性补刷历史消息。
    app.run_polling(drop_pending_updates=False, allowed_updates=["message"])


if __name__ == "__main__":
    main()
