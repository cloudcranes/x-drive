"""图片托管：JavBus 封面与 DMM 预览图落到 Cloudflare R2，页面只读 R2 直链。

本机连不上 JavBus 图片域名，DMM 图片虽能直连但为了统一访问也走 R2；入库时
让 Cloudflare Worker 回源一次（Worker 会把图片写进 R2），浏览器不请求代理。
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request

log = logging.getLogger("x-drive.coverstore")

R2_BASE = os.getenv("R2_COVER_BASE", "https://covers.master.us.kg").rstrip("/")
# 只用于服务端把新封面回源进 R2；页面展示不走这里。
SOURCE_BASE = os.getenv("JAVBUS_IMG_BASE", "https://javbus.img.master.us.kg").rstrip("/")
_R2_PREFIX = "javbus"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

_JB_RE = re.compile(r"^https?://(?:www\.)?javbus\.com(/pics/.*)$", re.I)
_PROXY_RE = re.compile(r"^https?://javbus\.img\.master\.us\.kg(/pics/.*)$", re.I)
_DMM_RE = re.compile(r"^https?://(pics\.dmm\.co\.jp|awsimgsrc\.dmm\.co\.jp)(/.*)$", re.I)
_R2_RE = re.compile(r"^https?://[^/]+/javbus/((?:www\.)?javbus\.com|pics\.dmm\.co\.jp|awsimgsrc\.dmm\.co\.jp)(/.*)$", re.I)

_started = False
_start_lock = threading.Lock()


def _parts(url: str) -> tuple[str, str]:
    raw = str(url or "").strip()
    match = _JB_RE.match(raw) or _PROXY_RE.match(raw)
    if match:
        return "www.javbus.com", match.group(1)
    match = _DMM_RE.match(raw)
    if match:
        return match.group(1).lower(), match.group(2)
    match = _R2_RE.match(raw)
    if match:
        return match.group(1).lower(), match.group(2)
    return "", ""


def public_url(url: str) -> str:
    """JavBus / DMM / 旧代理 URL → R2 直链；其它地址原样返回。"""
    raw = str(url or "").strip()
    if not raw:
        return raw
    if R2_BASE and raw.startswith(f"{R2_BASE}/{_R2_PREFIX}/"):
        return raw
    host, path = _parts(raw)
    return f"{R2_BASE}/{_R2_PREFIX}/{host}{path}" if host and R2_BASE else raw


def source_url(url: str) -> str:
    """R2 缺失时用来回源的地址（Worker 会顺手写入 R2）。"""
    host, path = _parts(url)
    if not host or not SOURCE_BASE:
        return ""
    if host.endswith("dmm.co.jp"):
        target = urllib.parse.quote(f"https://{host}{path}", safe="")
        return f"{SOURCE_BASE}/?url={target}"
    return f"{SOURCE_BASE}{path}"


def _ok(url: str) -> bool:
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def _fetch(url: str) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:
        log.warning("图片回源失败 %s：%s", url, exc)
        return False


def warm(url: str) -> str:
    """确保图片在 R2；返回 R2 直链（回源失败时也返回它，由前端显示占位）。"""
    dest = public_url(url)
    if not dest:
        return dest
    if _ok(dest):
        return dest
    src = source_url(url)
    if src:
        _fetch(src)
    return dest


def backfill(urls: list[str]) -> dict:
    warmed, failed = [], []
    for url in dict.fromkeys(str(u or "").strip() for u in urls):
        dest = public_url(url)
        if not dest or _ok(dest):
            continue
        src = source_url(url)
        (warmed if src and _fetch(src) else failed).append(dest)
    if warmed:
        log.info("图片补入 R2：%d 张", len(warmed))
    if failed:
        log.warning("图片补入 R2 失败：%d 张", len(failed))
    return {"warmed": warmed, "failed": failed}


def warm_meta(meta: dict | None, *, background: bool = True) -> None:
    """新归档落盘后立刻补图，不等 30 分钟一轮的全量扫描。"""
    if not _started:
        return
    meta = meta or {}
    urls = [meta.get("cover"), meta.get("cover_url")]
    urls.extend(meta.get("previews") or [])
    urls = list(dict.fromkeys(str(u).strip() for u in urls if u))
    if not urls:
        return

    def run() -> None:
        for url in urls:
            try:
                warm(url)
            except Exception as exc:            # noqa: BLE001
                log.warning("新归档补图失败 %s：%s", url, exc)

    if background:
        threading.Thread(target=run, daemon=True, name="cover-warm-task").start()
    else:
        run()


def start_warmer(urls_fn, interval: float = 1800.0) -> None:
    """后台把归档封面/预览图补进 R2；urls_fn 返回当前所有图片地址。"""
    global _started
    if os.getenv("COVER_WARM", "1") in ("0", "false", "False"):
        return
    with _start_lock:
        if _started:
            return
        _started = True

    def loop() -> None:
        time.sleep(10)
        while True:
            try:
                backfill(list(urls_fn() or []))
            except Exception as exc:
                log.warning("图片补入 R2 出错：%s", exc)
            time.sleep(max(60.0, interval))

    threading.Thread(target=loop, daemon=True, name="cover-warmer").start()
