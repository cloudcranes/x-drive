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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from telegram import Message, Update
from telegram.error import TelegramError
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from . import core, media

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


async def deliver(ctx: ContextTypes.DEFAULT_TYPE, url: str, status: Message) -> str:
    cfg: Config = ctx.application.bot_data["cfg"]
    client: httpx.AsyncClient = ctx.application.bot_data["http"]
    state: dict = ctx.application.bot_data["state"]

    expanded = await expand_short(client, url)
    key = cache_key(expanded)
    hit = state.get(key) or {}
    if hit.get("link"):
        rc, out, _ = await rclone(cfg, "lsf", hit["path"], timeout=120.0)
        if rc == 0 and out.strip():
            await edit(status, "♻️ 此前已存过，复用链接")
            return core.receipt(hit["link"], hit.get("files", []), [], hit.get("title", ""))

    await edit(status, "⏳ 解析媒体…")
    data = await parse_link(client, cfg, expanded)
    entries = core.media_entries(data)
    if not entries:
        return "❌ 该推文没有可下载的图片或视频"

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
                origins[name] = core.orig_image_url(media_url)
                if kind == "video":
                    await edit(status, "⏳ 转码兼容 Main profile…")
                    changed, info = await media.make_compatible(tmp / name, cfg.transcode)
                    if changed:
                        log.info("已转码 %s（原 profile %s）", name, info or "?")
                    elif info and info.lower() not in media.OK_PROFILES:
                        log.warning("转码未生效 %s：%s", name, info)
                await edit(status, f"⏳ 下载 {len(saved)}/{len(entries)}")
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
            await edit(status, "⏳ 上传 Drive…")
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
                timeout=3600.0,
            )
            if rc != 0:
                raise RuntimeError(f"rclone 上传失败：{err.strip()[:300]}")
            link = await folder_link(cfg, target)
            state[key] = {
                "path": target,
                "link": link,
                "files": saved,
                "orig": origins,
                "title": title,
                "author": author.get("name") or "",
                "source": expanded,
                "at": datetime.now().isoformat(timespec="seconds"),
            }
            core.save_state(cfg.state_path, state)
        return core.receipt(link, saved, failures, title)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def edit(message: Message, text: str) -> None:
    try:
        await message.edit_text(text)
    except TelegramError:
        pass


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not message.text or not user:
        return
    cfg: Config = ctx.application.bot_data["cfg"]
    if user.id not in cfg.allowed:
        await message.reply_text(
            f"⛔ 未授权。你的 user id：{user.id}，把它加进 ALLOWED_USER_IDS 后重启。"
        )
        return
    url = core.extract_url(message.text)
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


# ---------------------------------------------------------------- 启动


async def self_check(cfg: Config) -> bool:
    ok = True
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{cfg.parse_base}/", timeout=20.0)
            print(f"parse 服务 {cfg.parse_base}: HTTP {resp.status_code}")
            ok &= resp.status_code == 200
        except httpx.HTTPError as exc:
            print(f"parse 服务 {cfg.parse_base}: 不可达 {exc}")
            ok = False
    rc, _, err = await rclone(cfg, "lsd", f"{cfg.remote}:{cfg.drive_root}", timeout=90.0)
    if rc != 0:
        rc, _, err = await rclone(cfg, "lsd", f"{cfg.remote}:", timeout=90.0)
    print(f"rclone {cfg.remote}: {'OK' if rc == 0 else err.strip()[:300]}")
    ok &= rc == 0
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
        .build()
    )
    app.bot_data.update(
        cfg=cfg,
        state=core.load_state(cfg.state_path),
        sem=asyncio.Semaphore(cfg.concurrency),
        http=httpx.AsyncClient(
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; x-drive/1.0)",
                "Referer": "https://x.com/",
            },
        ),
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    log.info("启动：remote=%s root=%s domains=%s", cfg.remote, cfg.drive_root, cfg.domains)
    app.run_polling(drop_pending_updates=True, allowed_updates=["message"])


if __name__ == "__main__":
    main()
