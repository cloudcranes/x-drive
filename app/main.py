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

from . import core, fallback, media, metrics

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
    transcode: str = "auto"
    pending_dir: str = "/data/pending"
    metrics_path: str = "/data/metrics.json"
    daily_upload_gb: int = 0

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
            transcode=(os.getenv("VIDEO_TRANSCODE", "auto").strip().lower() or "auto"),
            pending_dir=os.getenv("PENDING_DIR", "/data/pending"),
            metrics_path=os.getenv("METRICS_PATH", "/data/metrics.json"),
            daily_upload_gb=int(os.getenv("DAILY_UPLOAD_GB", "0") or 0),
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
    url: str,
    progress,
    force: bool = False,
    chat_id: int | None = None,
) -> str:
    """解析 → 下载 → 转码 → 上传。progress(text) 用来更新外部消息，便于重启补跑时复用。"""
    expanded = await expand_short(client, url)
    key = cache_key(expanded)
    hit = state.get(key) or {}
    if hit.get("link") and not force:
        rc, out, _ = await rclone(cfg, "lsf", hit["path"], timeout=120.0)
        if rc == 0 and out.strip():
            await progress("♻️ 此前已存过，复用链接")
            return core.receipt(hit["link"], hit.get("files", []), [], hit.get("title", ""))

    mark_pending(cfg, key, url=expanded, stage="parsing", chat_id=chat_id)
    await progress("⏳ 解析媒体…")
    try:
        data = await parse_link(client, cfg, expanded)
    except Exception as exc:
        # 敏感推文 syndication 返回 TweetTombstone，parse-video-py 会报「没有找到视频或图片」
        log.warning("主解析器失败：%s，尝试 fxtwitter 兜底", exc)
        data = {}
    entries = core.media_entries(data)
    # 主解析器拿到视频时会丢图片、多视频只取第一个；fxtwitter 的媒体更完整。
    # 只要它给出的条目更多就采用它，否则保留主结果。
    if core.is_x_url(expanded):
        alt = await fallback.fetch(client, key)
        if alt:
            alt_entries = core.media_entries(alt)
            if len(alt_entries) > len(entries):
                log.info(
                    "改用完整媒体集合：%d → %d 项（原结果疑似漏图/漏视频）",
                    len(entries), len(alt_entries),
                )
                data, entries = alt, alt_entries
    if not entries:
        return "❌ 该推文没有可下载的图片或视频（敏感内容或已删除）"

    author = data.get("author") or {}
    title = (data.get("title") or "").strip()
    stamp = datetime.now()
    target = core.target_dir(
        cfg.remote, cfg.drive_root, key, author.get("uid") or "", author.get("name") or "", stamp
    )
    tmp = Path(cfg.tmp_root) / safe_dir_name(key)
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)

    saved: list[str] = []
    origins: dict[str, str] = {}
    failures: list[str] = []
    link: str | None = None
    try:
        for index, (kind, media_url) in enumerate(entries, start=1):
            if ".m3u8" in media_url.lower():
                failures.append(f"{index}.HLS 流不转码")
                continue
            name = core.media_filename(
                author.get("name") or "", stamp, index, core.guess_ext(media_url, kind)
            )
            try:
                await download(client, media_url, tmp / name, cfg.max_bytes)
                saved.append(name)
                if kind == "video":
                    # 视频本身不能当 <img> 用，缩略图用封面图
                    origins[name] = core.video_cover(data)
                else:
                    origins[name] = core.orig_image_url(media_url)
                if kind == "video":
                    await progress("⏳ 转码兼容 Main profile…")
                    changed, info = await media.make_compatible(tmp / name, cfg.transcode)
                    if changed:
                        log.info("已转码 %s（原 profile %s）", name, info or "?")
                    elif info and info.lower() not in media.OK_PROFILES:
                        log.warning("转码未生效 %s：%s", name, info)
                await progress(f"⏳ 下载 {len(saved)}/{len(entries)}")
            except Exception as exc:  # 单个媒体失败不中断整条推文
                failures.append(f"{name}: {str(exc)[:80]}")

        if saved:
            (tmp / "meta.json").write_text(
                json.dumps(
                    {
                        "source": expanded,
                        "tweet_id": key,
                        "title": title,
                        "author": author,
                        "files": saved,
                        "saved_at": datetime.now().isoformat(timespec="seconds"),
                    },
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
            incoming = sum(f.stat().st_size for f in tmp.iterdir() if f.is_file())
            if quota_exceeded(cfg, incoming):
                used = today_bytes(cfg) / 1024**3
                mark_pending(cfg, key, url=expanded, stage="quota", chat_id=chat_id)
                return (
                    "⛔ 今日上传量已达上限：已传 "
                    f"{used:.1f}GB，本次 {incoming / 1024**3:.1f}GB，超出 {cfg.daily_upload_gb}GB/日 限制。"
                    "文件已保留在 /downloads，可用 /retry 明天补传。"
                )
            mark_pending(cfg, key, url=expanded, stage="uploading", chat_id=chat_id)
            await progress("⏳ 上传 Drive…")
            rc, _, err = await rclone(cfg, "mkdir", target, timeout=120.0)
            rc, _, err = await rclone(
                cfg,
                "copy",
                str(tmp),
                target,
                "--transfers",
                "4",
                "--drive-chunk-size",
                "64M",
                "--retries",
                "2",
                "--timeout",
                "10m",
                "--drive-stop-on-upload-limit",
                timeout=3600.0,
            )
            if rc != 0:
                if "uploadLimitExceeded" in err or "quota" in err.lower():
                    raise RuntimeError("Google Drive 上传配额已用完，明日再试（已保留待办）")
                raise RuntimeError(f"rclone 上传失败：{err.strip()[:300]}")
            add_usage(cfg, incoming)
            link = await folder_link(cfg, target)
            state[key] = {
                "path": target,
                "link": link,
                "files": saved,
                "orig": origins,
                "title": title,
                "author": author.get("name") or "",
                "source": expanded,
                "failures": failures,
                "at": datetime.now().isoformat(timespec="seconds"),
            }
            core.save_state(cfg.state_path, state)
            clear_pending(cfg, key)
        if failures:
            # 部分成功也要留待办，方便 /retry 补抓
            mark_pending(cfg, key, url=expanded, stage="partial", chat_id=chat_id, failures=failures)
        return core.receipt(link, saved, failures, title)
    except Exception:
        # 失败保留临时文件与待办，重启后可补跑；成功路径在下面清理
        mark_pending(cfg, key, url=expanded, stage="failed", chat_id=chat_id)
        raise
    finally:
        if not failures and link:
            shutil.rmtree(tmp, ignore_errors=True)


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

    url = pick_url(ctx.args or [], "")
    if not url and message.reply_to_message:
        url = core.extract_url(
            message.reply_to_message.text or message.reply_to_message.caption or ""
        )
    if not url:
        latest = sorted(pending.values(), key=lambda p: p.get("ts", 0), reverse=True)
        if not latest:
            await message.reply_text("没有待重试的任务。用法：`/retry <链接>` 或回复一条含链接的消息")
            return
        url = latest[0].get("url") or ""
    if not url or not core.domain_allowed(url, cfg.domains):
        await message.reply_text("⛔ 需要一条 x.com 链接")
        return

    status = await message.reply_text("⏳ 重试中…")
    sem: asyncio.Semaphore = ctx.application.bot_data["sem"]
    async with sem:
        try:
            text = await deliver(ctx, url, status, force=True)
        except Exception as exc:
            log.exception("重试失败: %s", url)
            text = f"❌ 重试失败：{str(exc)[:400]}（可用 /retry 再试）"
    await edit(status, text)


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
            "• 直接发 x.com 链接 → 自动存 Drive\n"
            "• `/retry <链接>` 或回复消息 → 重试上次没入库的部分\n"
            "• `/del <推文ID>` → 查看将删除的目录，加 `--yes` 执行\n"
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
    if not core.domain_allowed(url, cfg.domains):
        await message.reply_text("⛔ 只支持：" + " / ".join(cfg.domains))
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
    """启动时补跑上次没完成/部分完成的推文，并把结果发回原会话。"""
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
        # 1. parse 服务存活 + 解析接口真的可用（打 / 只能证明进程在）
        try:
            resp = await client.get(
                f"{cfg.parse_base}/video/share/url/parse",
                params={"url": "https://x.com/i/status/20"},
                timeout=40.0,
            )
            body = resp.json()
            # 404/解析失败都算服务活着，只有连不上或 5xx 才算挂
            alive = resp.status_code < 500 and isinstance(body, dict) and "code" in body
            print(f"parse 解析接口: HTTP {resp.status_code} code={body.get('code')} {'OK' if alive else '异常'}")
            ok &= alive
        except Exception as exc:
            print(f"parse 服务 {cfg.parse_base}: 不可达 {exc}")
            ok = False

        # 2. Telegram token 是否有效
        try:
            me = await client.get(f"https://api.telegram.org/bot{cfg.token}/getMe", timeout=15.0)
            info = me.json()
            good = bool(info.get("ok"))
            print(f"telegram token: {'OK @' + info['result']['username'] if good else info.get('description','无效')}")
            ok &= good
        except Exception as exc:
            print(f"telegram token: 校验失败 {exc}")
            ok = False

    # 3. Drive 可读 + 可写（写权限只有真写一次才能确认）
    root = f"{cfg.remote}:{cfg.drive_root.strip('/')}" if cfg.drive_root else f"{cfg.remote}:"
    rc, _, err = await rclone(cfg, "lsd", root, timeout=90.0)
    if rc != 0:
        rc, _, err = await rclone(cfg, "lsd", f"{cfg.remote}:", timeout=90.0)
    print(f"rclone {cfg.remote}: {'OK' if rc == 0 else err.strip()[:300]}")
    ok &= rc == 0
    if rc == 0:
        # 用 mkdir + rmdir 验证写权限（rclone copyto 不支持 stdin）
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
