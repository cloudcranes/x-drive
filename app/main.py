"""TG bot：收到 x.com 链接 → 调 parse-video-py 解析 → 下载 → rclone 存 Google Drive。

用法：
  python -m app.main            # 启动长轮询
  python -m app.main --check    # 自检 parse 服务与 rclone remote
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
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from telegram import Message, Update
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from . import codes, core, fetcher, metrics

log = logging.getLogger("x-drive")

DEFAULT_DOMAINS = "x.com,twitter.com,t.co"
DEFAULT_DOWNLOAD_TIMEOUT = httpx.Timeout(120.0, connect=15.0, write=30.0)


@dataclass(frozen=True)
class Config:
    token: str
    allowed: set[int]
    parse_base: str
    domains: list[str]
    remote: str
    drive_root: str
    max_mb: int
    concurrency: int
    state_path: str
    tmp_root: str
    rclone_config: str
    pending_dir: str = "/data/pending"
    metrics_path: str = "/data/metrics.json"
    daily_upload_gb: int = 0
    magnet_timeout: float = 7200.0

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
            parse_base=os.getenv("PARSE_BASE_URL", "http://parse:8000").rstrip("/"),
            domains=[
                d.strip().lower()
                for d in os.getenv("ALLOWED_DOMAINS", DEFAULT_DOMAINS).split(",")
                if d.strip()
            ],
            remote=os.getenv("RCLONE_REMOTE", "gdrive").strip(),
            drive_root=os.getenv("DRIVE_ROOT", "X-Drive").strip().strip("/"),
            max_mb=int(os.getenv("MAX_FILE_MB", "500")),
            concurrency=max(1, int(os.getenv("CONCURRENCY", "2"))),
            state_path=os.getenv("STATE_PATH", "/data/state.json"),
            tmp_root=os.getenv("TMP_ROOT", "/downloads"),
            rclone_config=os.getenv("RCLONE_CONFIG", "/config/rclone/rclone.conf"),
            pending_dir=os.getenv("PENDING_DIR", "/data/pending"),
            metrics_path=os.getenv("METRICS_PATH", "/data/metrics.json"),
            daily_upload_gb=int(os.getenv("DAILY_UPLOAD_GB", "0") or 0),
            magnet_timeout=float(os.getenv("MAGNET_TIMEOUT", "7200") or 7200),
        )


# ---------------------------------------------------------------- 外部命令


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


async def parse_link(client: httpx.AsyncClient, cfg: Config, url: str) -> dict:
    resp = await client.get(
        f"{cfg.parse_base}/video/share/url/parse", params={"url": url}, timeout=90.0
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("code") != 200:
        raise RuntimeError(payload.get("msg") or "解析失败")
    return payload.get("data") or {}


async def expand_short(client: httpx.AsyncClient, url: str) -> str:
    if (urlsplit(url).hostname or "").lower().removeprefix("www.") != "t.co":
        return url
    try:
        resp = await client.get(url, follow_redirects=False, timeout=30.0)
        location = resp.headers.get("location")
        if location:
            return location
    except httpx.HTTPError as exc:
        log.warning("t.co 展开失败: %s", exc)
    return url


async def download(client: httpx.AsyncClient, url: str, dest: Path, max_bytes: int) -> int:
    async with client.stream("GET", url, timeout=DEFAULT_DOWNLOAD_TIMEOUT) as resp:
        resp.raise_for_status()
        declared = int(resp.headers.get("content-length") or 0)
        if declared and declared > max_bytes:
            raise RuntimeError(f"{declared // 1048576}MB 超过上限 {max_bytes // 1048576}MB")
        total = 0
        with open(dest, "wb") as fh:
            async for chunk in resp.aiter_bytes(65536):
                total += len(chunk)
                if total > max_bytes:
                    raise RuntimeError(f"超过上限 {max_bytes // 1048576}MB")
                fh.write(chunk)
    if total == 0:
        raise RuntimeError("空文件")
    return total


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


def cache_key(expanded: str) -> str:
    tid = core.tweet_id_from_url(expanded)
    if tid:
        return tid
    return "h" + hashlib.sha1(expanded.encode()).hexdigest()[:16]


def safe_dir_name(key: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", key) or hashlib.sha1(key.encode()).hexdigest()[:16]


def pending_file(cfg: Config, key: str) -> Path:
    return Path(cfg.pending_dir) / f"{safe_dir_name(key)}.json"


def mark_pending(cfg: Config, key: str, **fields) -> None:
    """记录未完成任务，进程重启后据此补跑；成功与主动放弃时由 clear_pending 删除。"""
    path = pending_file(cfg, key)
    record: dict = {}
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    record.update(fields)
    record.setdefault("key", key)
    record["ts"] = time.time()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as exc:
        log.warning("待办落盘失败 %s：%s", path, exc)


def clear_pending(cfg: Config, key: str) -> None:
    try:
        pending_file(cfg, key).unlink(missing_ok=True)
    except OSError:
        pass


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


async def run_task(
    cfg: Config,
    client: httpx.AsyncClient,
    state: dict,
    text: str,
    progress,
    force: bool = False,
    chat_id: int | None = None,
) -> str:
    """番号 → 下载（直链/磁力）→ 归一化文件名 → 按番号上传 Drive。"""
    sources = fetcher.sources(text)
    if not sources:
        return (
            "❌ 没找到可以下载的链接。\n发磁力或直链给我，例如：\n"
            "`magnet:?xt=urn:btih:...`\n`https://example.com/SSIS-123.mp4`"
        )

    code = codes.detect(text) or ""
    results: list[str] = []
    failures: list[str] = []

    for index, source in enumerate(sources, start=1):
        mark_pending(cfg, code or f"job{index}", url=source, stage="downloading", chat_id=chat_id)
        # 同一番号已在库里：除非 force，否则不重复下
        known = state.get(code) if code else None
        if known and known.get("link") and not force:
            results.append(f"♻️ {code} 已存在：{known['link']}")
            clear_pending(cfg, code)
            continue

        work = Path(cfg.tmp_root) / safe_dir_name(code or f"job{index}")
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        try:
            await progress(f"⏳ 下载 {index}/{len(sources)}…（磁力可能较慢）")
            if fetcher.is_magnet(source):
                await fetcher.download_magnet(source, work, timeout=cfg.magnet_timeout)
            else:
                await fetcher.download_http(client, source, work, cfg.max_bytes)
            fetcher.flatten(work)

            files = fetcher.media_files(work)
            if not files:
                raise RuntimeError("下载完成但没有找到视频文件")

            # 番号优先取消息里的，其次从文件名推断；都没有就用时间戳目录
            final_code = code or codes.detect(files[0].name) or ""
            folder = codes.safe_folder(final_code) if final_code else ""
            if not folder or len(folder) < 3:
                folder = f"unclassified-{datetime.now():%Y%m%d-%H%M%S}"

            # 单文件重命名为 <番号>.<ext>；多文件保留原名加序号前缀
            if len(files) == 1:
                target_name = f"{folder}{files[0].suffix.lower()}"
                files[0].rename(work / target_name)
                files = [work / target_name]
            else:
                for i, f in enumerate(files, start=1):
                    f.rename(work / f"{folder}-{i}{f.suffix.lower()}")
                files = fetcher.media_files(work)

            total = sum(f.stat().st_size for f in files)
            if quota_exceeded(cfg, total):
                raise RuntimeError(
                    f"今日上传量已达 {cfg.daily_upload_gb}GB 上限，文件留在 /downloads 可明天 /retry"
                )

            target = f"{cfg.remote}:{cfg.drive_root.strip('/')}/{folder}"
            await progress(f"⏳ 上传 {folder}（{total / 1073741824:.2f}GB）…")
            rc, _, err = await rclone(cfg, "mkdir", target, timeout=120.0)
            rc, _, err = await rclone(
                cfg, "copy", str(work), target,
                "--transfers", "2", "--drive-chunk-size", "64M",
                "--retries", "2", "--timeout", "30m",
                "--drive-stop-on-upload-limit",
                timeout=7200.0,
            )
            if rc != 0:
                if "uploadLimitExceeded" in err or "quota" in err.lower():
                    raise RuntimeError("Drive 上传配额已用完，明日再试")
                raise RuntimeError(f"rclone 上传失败：{err.strip()[:300]}")
            add_usage(cfg, total)

            link = await folder_link(cfg, target)
            state[folder] = {
                "code": folder,
                "path": target,
                "link": link,
                "files": [f.name for f in files],
                "size": total,
                "source": source[:300],
                "at": datetime.now().isoformat(timespec="seconds"),
            }
            core.save_state(cfg.state_path, state)
            clear_pending(cfg, folder)
            results.append(
                f"✅ {folder} · {total / 1073741824:.2f}GB"
                + (f"\n📁 {link}" if link else "")
            )
        except Exception as exc:  # 单条链接失败不影响其他
            log.exception("下载失败 %s", source[:80])
            failures.append(f"{source[:40]}…: {str(exc)[:160]}")
            mark_pending(cfg, code or f"job{index}", url=source, stage="failed", chat_id=chat_id)
        finally:
            if not failures:
                shutil.rmtree(work, ignore_errors=True)

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
    """删除某条推文的整目录：/del <tweet_id|链接>。必须带 --yes 确认。"""
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    cfg: Config = ctx.application.bot_data["cfg"]
    if user.id not in cfg.allowed:
        return
    args = ctx.args or []
    key = mentioned_key(args) or (core.tweet_id_from_url(args[0]) if args else None)
    if not key:
        await message.reply_text("用法：`/del <推文ID>`，确认执行加 `--yes`")
        return
    state: dict = ctx.application.bot_data["state"]
    record = state.get(key)
    if not record:
        await message.reply_text(f"未找到 {key} 的记录")
        return
    if "--yes" not in args:
        await message.reply_text(
            f"将删除：\n{record.get('path')}\n\n确认请发送 `/del {key} --yes`"
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
    clear_pending(cfg, key)
    await message.reply_text(f"🗑 已删除 {key}（文件在 Drive 回收站，可恢复）")


async def on_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message:
        await message.reply_text(
            "用法：\n"
            "• 发磁力或直链（可带番号，如 `SSIS-123 magnet:?xt=...`）→ 下载后按番号存 Drive\n"
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
        return

    code = codes.detect(text)
    status = await message.reply_text(
        f"⏳ 已收到{'　番号 ' + code if code else '下载任务'}…"
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
    """删除某条推文的整目录：/del <tweet_id|链接>。必须带 --yes 确认。"""
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    cfg: Config = ctx.application.bot_data["cfg"]
    if user.id not in cfg.allowed:
        return
    args = ctx.args or []
    key = mentioned_key(args) or (core.tweet_id_from_url(args[0]) if args else None)
    if not key:
        await message.reply_text("用法：`/del <推文ID>`，确认执行加 `--yes`")
        return
    state: dict = ctx.application.bot_data["state"]
    record = state.get(key)
    if not record:
        await message.reply_text(f"未找到 {key} 的记录")
        return
    if "--yes" not in args:
        await message.reply_text(
            f"将删除：\n{record.get('path')}\n\n确认请发送 `/del {key} --yes`"
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
    clear_pending(cfg, key)
    await message.reply_text(f"🗑 已删除 {key}（文件在 Drive 回收站，可恢复）")


async def on_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message:
        await message.reply_text(
            "用法：\n"
            "• 发磁力或直链（可带番号，如 `SSIS-123 magnet:?xt=...`）→ 下载后按番号存 Drive\n"
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
    url = core.extract_url(text)
    if not url:
        return

    status = await message.reply_text("⏳ 解析中…")
    sem: asyncio.Semaphore = ctx.application.bot_data["sem"]
    async with sem:
        try:
            text = await deliver(ctx, url, status)
        except Exception as exc:
            log.exception("处理失败: %s", url)
            text = f"❌ {str(exc)[:500]}"
    await edit(status, text)


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
            clear_pending(cfg, key)
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

    for binary in ("aria2c", "rclone"):
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
    app.add_handler(CommandHandler("retry", on_retry))
    app.add_handler(CommandHandler("list", on_list))
    app.add_handler(CommandHandler("del", on_delete))
    app.add_handler(CommandHandler(["help", "start"], on_help))
    log.info(
        "启动：remote=%s root=%s domains=%s 待补跑=%d",
        cfg.remote, cfg.drive_root, cfg.domains, len(app.bot_data["pending"]),
    )
    # drop_pending_updates=False：停机期间用户发的链接不再被 Telegram 侧丢弃；
    # 旧的积压由 processed_update 里的时间窗过滤，避免重启后一次性补刷历史消息。
    app.run_polling(drop_pending_updates=False, allowed_updates=["message"])


if __name__ == "__main__":
    main()
