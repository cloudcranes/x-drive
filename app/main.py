"""TG bot：收到磁力/直链 → 识别番号 → 迅雷下载 → 整理 → rclone 存 Google Drive。

用法：
  python -m app.main            # 启动长轮询
  python -m app.main --check    # 自检 Telegram / 迅雷容器 / rclone remote
"""
from __future__ import annotations

import asyncio
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

from . import (codes, core, fetcher, meta, metadata, metrics, metatube, pipeline, retry,
               sources, store, whatslink, workspace, xunlei)

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
    daily_upload_gb: int = 0
    magnet_timeout: float = 7200.0
    stall_timeout: float = 900.0
    xunlei_url: str = "http://127.0.0.1:2345"
    xunlei_parent: str = ""
    xunlei_token: str = ""
    xunlei_poll: float = 10.0
    xunlei_settle: float = 30.0
    upload_transfers: int = 4
    upload_chunk: str = "64M"
    retries: int = 2
    retry_base_delay: float = 3.0
    # 刮削附件：默认全开，与网页侧同名环境变量同语义；关掉就退回「只有视频」
    scrape: bool = True
    scrape_nfo: bool = True
    scrape_poster: bool = True

    @property
    def max_bytes(self) -> int:
        return self.max_mb * 1024 * 1024

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
            daily_upload_gb=int(os.getenv("DAILY_UPLOAD_GB", "0") or 0),
            magnet_timeout=float(os.getenv("MAGNET_TIMEOUT", "7200") or 7200),
            stall_timeout=float(os.getenv("STALL_TIMEOUT", "900") or 0),
            xunlei_url=os.getenv("XUNLEI_URL", "http://127.0.0.1:2345").strip(),
            xunlei_parent=os.getenv("XUNLEI_PARENT_ID", "").strip(),
            xunlei_token=os.getenv("XUNLEI_PAN_AUTH", "").strip(),
            xunlei_poll=float(os.getenv("XUNLEI_POLL", "10") or 10),
            xunlei_settle=float(os.getenv("XUNLEI_SETTLE", "30") or 0),
            upload_transfers=max(1, int(os.getenv("UPLOAD_TRANSFERS", "4") or 4)),
            upload_chunk=os.getenv("UPLOAD_CHUNK", "64M").strip() or "64M",
            retries=max(0, int(os.getenv("RETRIES", "2") or 2)),
            retry_base_delay=float(os.getenv("RETRY_BASE_DELAY", "3") or 3),
            scrape=_flag("SCRAPE", "1"),
            scrape_nfo=_flag("SCRAPE_NFO", "1"),
            scrape_poster=_flag("SCRAPE_POSTER", "1"),
        )


# ---------------------------------------------------------------- 外部命令


def _flag(name: str, default: str = "1") -> bool:
    """环境变量开关：0/false/False 视为关。与网页侧同一套写法。"""
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


def claim_task(cfg: Config, code: str, source: str, chat_id: int | None) -> str:
    """登记一个任务。同番号已有未结束任务时返回空串（与网页侧 409 同义）。"""
    job_id = "b" + hashlib.sha1(
        f"{time.time():.6f}:{code}:{source}".encode()).hexdigest()[:12]
    if not task_store(cfg).start(job_id, code, source[:300], owner="bot"):
        log.info("番号 %s 已在任务表里未结束，跳过重复提交", code)
        return ""
    task_store(cfg).update(job_id, stage="downloading")
    extra: dict = {}
    if chat_id is not None:
        extra["chat_id"] = chat_id
    btih = xunlei.info_hash_of(source)
    if btih:
        extra["infohash"] = btih        # 续接证据：重启后据此认回自己的迅雷任务
    if extra:
        task_store(cfg).update(job_id, extra=extra)
    return job_id


def finish_task(cfg: Config, job_id: str, stage: str, error: str = "") -> None:
    if job_id:
        task_store(cfg).finish(job_id, stage, error)


# ── 执行租约：与网页端同一套（store.claim_owner / beat / release），
# 心跳间隔同样取过期阈值的三分之一，漏两次也不会被误判失联。
PROC_ID = f"bot-{os.getpid()}-{int(time.time())}"
HEARTBEAT_INTERVAL = store.STALE_BEAT / 3
LEASES: dict[str, str] = {}
_lease_live: dict[str, bool] = {}


def lease_owner(job_id: str) -> str:
    """本次执行对这个任务的租约名（不存在就创建）。token 每次执行都不同。"""
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
    LEASES.pop(job_id, None)
    token = lease_owner(job_id)
    if not store_.claim_owner(job_id, token):
        LEASES.pop(job_id, None)
        return False
    # 占位：心跳线程每 HEARTBEAT_INTERVAL 才跳一次，起跳前也要算「在场」，
    # 否则紧随其后的 require_lease 会把它当成没起心跳而过不去
    _lease_live[job_id] = True

    def pulse() -> None:
        while _lease_live.get(job_id, False):
            time.sleep(HEARTBEAT_INTERVAL)
            if not _lease_live.get(job_id, False):
                break
            try:
                ok = store_.beat(job_id, token)
            except Exception as exc:
                log.warning("任务 %s 续租出错：%s", job_id, exc)
                continue
            if not ok:
                _lease_live[job_id] = False
                log.warning("任务 %s 的租约已被他人持有，停止续租", job_id)
                break

    threading.Thread(target=pulse, daemon=True, name=f"bot-lease-{job_id}").start()
    return True


def lease_alive(cfg: Config, job_id: str) -> bool:
    """本次执行的租约是否仍有效；心跳失败过就直接判定失租。"""
    if not _lease_live.get(job_id, True):
        return False
    owner = LEASES.get(job_id)
    if not owner:
        return False
    try:
        return bool(task_store(cfg).beat(job_id, owner))
    except Exception as exc:      # 库暂时不可用不等于失租
        log.warning("检查任务 %s 的租约失败：%s", job_id, exc)
        return True


def require_lease(cfg: Config, job_id: str) -> None:
    """关键节点自查：失租就停手，绝不和另一个执行者抢同一目录。"""
    if job_id and not lease_alive(cfg, job_id):
        raise RuntimeError(f"任务 {job_id} 已由另一个执行者接手，本端停止")


def lease_end(cfg: Config, job_id: str) -> None:
    """结束执行：停心跳并让出租约。"""
    _lease_live.pop(job_id, None)
    owner = LEASES.pop(job_id, None)
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
        task_store(cfg).defer(job_id, until, reason)
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
        store_.update(job_id, extra={**(store_.get(job_id).get("extra") or {}),
                                     "scraped": out["record"],
                                     "scrape_pending": out["pending"],
                                     "scrape_failed": out["failed"]})
    return out


async def run_task(
    cfg: Config,
    client: httpx.AsyncClient,
    state: dict,
    text: str,
    progress,
    force: bool = False,
    chat_id: int | None = None,
) -> str:
    """番号 → 下载（磁力/直链都投给迅雷）→ 整理 → 按番号上传 Drive。"""
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

        # 下载目录（迅雷建）与工作区（我们整理）分开，失败不动原件
        key_safe = workspace.key_of(key)
        download_dir = workspace.staging(cfg.tmp_root, key_safe)
        work = workspace.workspace(cfg.state_dir, key_safe)
        uploaded = False
        clock = {"download": 0.0, "organize": 0.0, "upload": 0.0}
        retried = {"download": 0, "upload": 0}
        job_size = 0
        job_started = time.monotonic()
        # 迅雷任务 id：失败时要在面板里一并清掉，否则僵尸任务越积越多
        handle: dict[str, str] = {}
        meta_info: dict = {}
        try:
            async def on_progress(percent, speed, status, _p=progress, _i=index):
                mb = (speed or 0) / 1048576
                await _p(f"⏳ 下载 {_i}/{len(sources)} · {percent * 100:.1f}% · {mb:.1f}MB/s")

            await progress(f"⏳ 下载 {index}/{len(sources)}…{hint or '（磁力可能较慢）'}")

            async def do_download():
                # 同上：带本任务上次投递的证据，重试时识别自己的任务而不是拒绝
                prev = task_store(cfg).get(job_id) or {}
                got = await pipeline.download(
                    xl, client, source, download_dir, progress=on_progress,
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

            policy = retry.RetryPolicy(retries=cfg.retries, base_delay=cfg.retry_base_delay)
            phase = time.monotonic()
            await retry.run_with_retry(do_download, policy, on_retry=on_dl_retry)
            clock["download"] = time.monotonic() - phase
            retried["download"] = policy.attempts

            phase = time.monotonic()
            require_lease(cfg, job_id)          # downloading→organizing 前
            workspace.drop_workspace(work, key_safe, "整理前清空")
            pipeline.stage_copy(Path(handle.get("dir") or download_dir), work, key_safe)
            found = fetcher.media_files(work) or fetcher.media_files(work, types=fetcher.MEDIA_EXT)
            final_code = code or codes.detect(found[0].name if found else "")
            folder, files = pipeline.organize(work, final_code or "")
            total = pipeline.total_size(files)
            clock["organize"] = time.monotonic() - phase
            job_size = total

            if quota_exceeded(cfg, total):
                raise RuntimeError(
                    f"今日上传量已达 {cfg.daily_upload_gb}GB 上限，文件留在 /downloads 可明天重试"
                )

            # ── 刮削并生成附件（失败不阻断）：NFO/JSON/封面/清单跟视频一起传
            scraped = await scrape_bot_attachments(cfg, work, folder, files, job_id)
            if scraped["written"]:
                task_store(cfg).update(job_id, extra={
                    **(task_store(cfg).get(job_id).get("extra") or {}),
                    "files": [f.name for f in scraped["files"]]})

            require_lease(cfg, job_id)          # organizing→uploading 前
            target = f"{cfg.remote}:{cfg.drive_root.strip('/')}/{folder}"
            await progress(f"⏳ 上传 {folder}（{core.human_size(total)}）…（含附件）"
                           if scraped["files"] != files else
                           f"⏳ 上传 {folder}（{core.human_size(total)}）…")

            async def do_upload():
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

            up_policy = retry.RetryPolicy(retries=cfg.retries, base_delay=cfg.retry_base_delay)
            phase = time.monotonic()
            await retry.run_with_retry(do_upload, up_policy, on_retry=on_up_retry)
            clock["upload"] = time.monotonic() - phase
            retried["upload"] = up_policy.attempts
            add_usage(cfg, total)

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
            uploaded = scraped["files"]
            task_extra = task_store(cfg).get(job_id).get("extra") or {} \
                if job_id else {}
            names = [f.name for f in uploaded]
            state[folder] = metadata.archive_record(
                folder, target, link, names, total, source, work,
                scraped=meta_info or scraped["record"] or task_extra.get("scraped") or {},
                manifest_files=names,
                infohash=str(task_extra.get("infohash") or ""),
                locks=task_extra.get("locked") or (),
                pending=bool(scraped["pending"] or task_extra.get("scrape_pending")),
                at=datetime.now().isoformat(timespec="seconds"),
            )
            core.update_state(cfg.state_path, {folder: state[folder]})
            uploaded = True
            finish_task(cfg, job_id, "done")
            results.append(
                f"✅ {folder} · {core.human_size(total)}" + (f"\n📁 {link}" if link else "")
            )
        except Exception as exc:
            log.exception("下载失败 %s", source[:80])
            if retry.classify(str(exc)) == retry.QUOTA:
                # 配额类不是失败，是「今天没额度」：留待办，明天开机自动补跑
                until = defer_task(cfg, job_id, str(exc)[:200])
                failures.append(
                    f"{source[:40]}…: {str(exc)[:120]}"
                    f"（今日额度用尽，{time.strftime('%m-%d %H:%M', time.localtime(until))} 自动重试）")
            else:
                failures.append(f"{source[:40]}…: {str(exc)[:160]}")
                finish_task(cfg, job_id, "failed", str(exc)[:200])
            if handle.get("id") and handle.get("ours", True):
                await xl.remove(client, handle["id"])
        finally:
            metrics.record_timing(cfg.metrics_path, {
                "code": key,
                "size": job_size,
                "outcome": "ok" if uploaded else "fail",
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
            if uploaded and lease_alive(cfg, job_id):
                workspace.drop_workspace(work, key_safe, "上传成功")
                workspace.drop(download_dir, key_safe, "上传成功")
                # 迅雷重名时真实目录是 <键>-N，预设的 dest 删了也不代表原件清掉了
                real_dir = Path(handle.get("dir") or "")
                if real_dir and real_dir != download_dir:
                    workspace.drop(real_dir, key_safe, "上传成功")
            elif uploaded:
                log.warning("任务 %s 已失租，跳过目录清理：%s %s", job_id, work, download_dir)
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
    root = f"{cfg.remote}:{cfg.drive_root.strip('/')}" if cfg.drive_root else f"{cfg.remote}:"
    if not target.startswith(root + "/") or ".." in target:
        await message.reply_text("⛔ 路径校验未通过，已拒绝删除")
        return
    rc, _, err = await rclone(cfg, "purge", target, timeout=600.0)
    if rc != 0:
        await message.reply_text(f"❌ 删除失败：{err.strip()[:300]}")
        return
    state.pop(key, None)
    core.save_state(cfg.state_path, state)
    await message.reply_text(f"🗑 已删除 {key}（文件在 Drive 回收站，可恢复）")


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
    """启动时补跑上次没完成/失败的下载任务，并把结果发回原会话。"""
    cfg: Config = application.bot_data["cfg"]
    pending: dict = application.bot_data["pending"]
    if not pending:
        return
    log.info("发现 %d 条待补跑记录", len(pending))
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
