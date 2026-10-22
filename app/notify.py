"""Telegram 通知：只发「值得看的」，不刷屏。

为什么单独一个模块：下载重试、上传重试都可能一轮失败好几次，
每次都发一条等于把用户训练成忽略通知。这里按「事件键」去重，同一个
事件在一段时间内只发一次；发送失败也绝不影响主流程。

用标准库 urllib 直连 Bot API，不引入依赖（bot 那边已有 python-telegram-bot，
但网页进程不需要把它整个拉起来）。
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request

log = logging.getLogger("x-drive.notify")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
# 同一个事件多久内不重复发（秒）
DEDUPE_WINDOW = float(os.getenv("NOTIFY_DEDUPE_WINDOW", "1800") or 1800)
PROXY = os.getenv("TELEGRAM_PROXY", "") or os.getenv("HTTPS_PROXY", "")

# 最近发过的事件：事件键 → 时间。放内存即可，重启后重发一条不算事故
_SENT: dict[str, float] = {}


def configured() -> bool:
    return bool(TOKEN and CHAT_ID)


def _dedupe_key(kind: str, subject: str) -> str:
    return f"{kind}:{subject}"


def _post(text: str, timeout: float = 10.0) -> bool:
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": text[:3800],
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    handlers = []
    if PROXY:
        handlers.append(urllib.request.ProxyHandler({"https": PROXY, "http": PROXY}))
    opener = urllib.request.build_opener(*handlers) if handlers else urllib.request.build_opener()
    try:
        with opener.open(urllib.request.Request(url, data=data), timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:
        log.warning("Telegram 通知发送失败：%s", exc)
        return False


def notify(kind: str, subject: str, text: str, now: float | None = None,
           force: bool = False) -> bool:
    """发一条通知。同一 (kind, subject) 在窗口内只发一次。

    返回是否真的发了（未配置 / 被去重 / 发送失败都返回 False）。
    """
    if not configured():
        return False
    now = time.time() if now is None else now
    key = _dedupe_key(kind, subject)
    last = _SENT.get(key)
    if not force and last is not None and now - last < DEDUPE_WINDOW:
        return False
    if _post(text):
        _SENT[key] = now
        return True
    return False


def _esc(text: str) -> str:
    return (str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def job_done(code: str, size_text: str = "", link: str = "") -> bool:
    body = f"✅ <b>{_esc(code)}</b> 已归档"
    if size_text:
        body += f"\n{size_text}"
    if link:
        body += f"\n{_esc(link)}"
    # 完成事件按番号去重：重复上传不该反复提醒
    return notify("done", code, body, force=False)


def job_failed(code: str, error: str) -> bool:
    return notify("failed", code,
                  f"⚠️ <b>{_esc(code)}</b> 最终失败\n{_esc(str(error)[:400])}")


def job_deferred(code: str, until: float, reason: str = "") -> bool:
    when = time.strftime("%m-%d %H:%M", time.localtime(until))
    return notify("deferred", code,
                  f"⏸ <b>{_esc(code)}</b> 额度等待，{when} 自动重试"
                  + (f"\n{_esc(str(reason)[:200])}" if reason else ""))


def long_pending(code: str, days: float, note: str = "") -> bool:
    return notify("pending", code,
                  f"🕒 <b>{_esc(code)}</b> 等待已 {days:.0f} 天仍无资源"
                  + (f"\n{_esc(str(note)[:200])}" if note else ""))


def daily_summary(text: str, day: str) -> bool:
    """每日汇总：同一天只发一次。"""
    return notify("daily", day, f"📊 <b>每日汇总</b>\n{text}")


def reset_dedupe() -> None:
    """测试用：清掉去重记忆。"""
    _SENT.clear()
