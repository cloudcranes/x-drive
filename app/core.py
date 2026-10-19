"""纯函数层：URL 提取、路径构建、文件命名、去重状态、回执文案。

不依赖网络与 Telegram，便于单测。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
TRAILING = ").,;!?'\"”’》】」>"
TWEET_ID_RE = re.compile(r"/status(?:es)?/(\d+)")
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".webm"}
KNOWN_EXTS = IMAGE_EXTS | VIDEO_EXTS


def extract_url(text: str) -> str | None:
    """取文本中第一个 http(s) 链接，去掉尾部标点。"""
    for raw in URL_RE.findall(text or ""):
        url = raw.rstrip(TRAILING)
        if "://" in url and len(urlsplit(url).netloc) > 0:
            return url
    return None


def domain_allowed(url: str, allowed: list[str] | tuple[str, ...]) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return False
    return any(host == d.lower() or host.endswith("." + d.lower()) for d in allowed)


def tweet_id_from_url(url: str) -> str | None:
    m = TWEET_ID_RE.search(urlsplit(url).path) or TWEET_ID_RE.search(url)
    return m.group(1) if m else None


def slug(name: str, limit: int = 40) -> str:
    """作者名转安全目录名；保留字母数字与 CJK，其余折叠为下划线。"""
    name = (name or "").strip()
    if not name:
        return ""
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff-]+", "_", name)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_-")
    return cleaned[:limit].strip("_-")


def target_dir(
    remote: str, root: str, tweet_id: str, uid: str, name: str, now: datetime | None = None
) -> str:
    """remote:root/<名>/<YYYY-MM>/<tweet_id>"""
    now = now or datetime.now()
    folder = slug(name) or slug(str(uid or "")) or "unknown"
    root = root.strip("/")
    path = f"{folder}/{now:%Y-%m}/{tweet_id}"
    return f"{remote}:{root}/{path}" if root else f"{remote}:{path}"


def media_filename(author_name: str, now: datetime, index: int, ext: str) -> str:
    """<作者>_<YYYY-MM-DD>_<序号>.<ext>"""
    return f"{slug(author_name) or 'unknown'}_{now:%Y-%m-%d}_{index:02d}{ext}"


def guess_ext(url: str, kind: str) -> str:
    ext = os.path.splitext(urlsplit(url).path)[1].lower()
    if ext in KNOWN_EXTS or ext == ".m3u8":
        return ext
    return ".mp4" if kind == "video" else ".jpg"


def orig_image_url(url: str) -> str:
    """pbs.twimg.com 图片改取原图。"""
    parts = urlsplit(url)
    if not (parts.hostname or "").startswith("pbs."):
        return url
    query = dict(parse_qsl(parts.query))
    query["name"] = "orig"
    return urlunsplit(parts._replace(query=urlencode(query)))


def media_entries(data: dict) -> list[tuple[str, str]]:
    """把解析结果摊平成 [(kind, url)]；kind 为 video 或 image。"""
    entries: list[tuple[str, str]] = []
    video = (data.get("video_url") or "").strip()
    if video:
        entries.append(("video", video))
    for img in data.get("images") or []:
        url = (img or {}).get("url") or ""
        if url:
            entries.append(("image", orig_image_url(url)))
    return entries


def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def receipt(link: str | None, saved: list[str], failures: list[str], title: str = "") -> str:
    """生成回执正文，控制在 TG 4096 字符内。"""
    images = sum(1 for f in saved if os.path.splitext(f)[1].lower() in IMAGE_EXTS)
    videos = sum(1 for f in saved if os.path.splitext(f)[1].lower() in VIDEO_EXTS)
    if not saved:
        head = "❌ 没有文件入库"
    else:
        bits = [b for b in (f"{images} 图" if images else "", f"{videos} 视频" if videos else "") if b]
        head = "✅ 已存 Drive · " + " ".join(bits or [f"{len(saved)} 文件"])
    lines = [head]
    if title:
        flat = " ".join(title.split())
        lines.append(flat[:80] + ("…" if len(flat) > 80 else ""))
    if link:
        lines.append(f"📁 {link}")
    if failures:
        lines.append(f"⚠️ {len(failures)} 项未入库：" + "；".join(failures)[:600])
    text = "\n".join(lines)
    return text[:3500]
