"""按番号搜磁力：多源注册表，先到先得 + 统一字段 + 去重排序。

只按番号查，不做关键词乱搜。每个源一个 `async fetch(code, client) -> list[dict]`，
统一成：
    {"magnet", "title", "size_bytes", "size_text", "has_subtitle", "is_hd",
     "seeders", "source"}

路由：FC2 系列 javbus 必然 404，直接跳过；其余按 `SOURCES` 顺序逐个试，
第一个有结果的源即采用（先到先得，省流量）。单个源失败只记日志，不影响其他。
"""
from __future__ import annotations

import logging
import os
import re
import urllib.parse

import httpx

from . import javbus

log = logging.getLogger("x-drive.sources")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
TIMEOUT = 20.0

# 拼磁力用的 tracker：sukebei 的 RSS 只给 infoHash
TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://exodus.desync.com:6969/announce",
)

DEFAULT_SOURCES = "javbus,sukebei,bt4g"
# FC2 是用户投稿，javbus 没有条目（实测 404），走通用 BT 站
FC2_SOURCES = ("sukebei", "bt4g")

_SUB_RE = re.compile(r"中文字幕|字幕|中字|CHS|CHT|\bTC\b|\bSC\b|sub", re.I)
_HD_RE = re.compile(r"1080|720|\bFHD\b|\bHD\b|4K|2160", re.I)
_SIZE_RE = re.compile(r"([\d.]+)\s*(B|KB|KiB|MB|MiB|GB|GiB|TB|TiB)", re.I)
_UNITS = {"B": 1, "KB": 1000, "KIB": 1024, "MB": 1000 ** 2, "MIB": 1024 ** 2,
          "GB": 1000 ** 3, "GIB": 1024 ** 3, "TB": 1000 ** 4, "TIB": 1024 ** 4}

def size_bytes(text: str) -> int:
    """`468.27MB` / `4.0 GiB` → 字节；认不出返回 0。"""
    m = _SIZE_RE.search(text or "")
    if not m:
        return 0
    try:
        return int(float(m.group(1)) * _UNITS[m.group(2).upper()])
    except (KeyError, ValueError):
        return 0

def flags(title: str) -> tuple[bool, bool]:
    """从标题判字幕/高清。只做粗判，拿不到就当没有。"""
    return bool(_SUB_RE.search(title or "")), bool(_HD_RE.search(title or ""))

def magnet_of(info_hash: str, name: str = "") -> str:
    base = f"magnet:?xt=urn:btih:{info_hash}"
    if name:
        base += "&dn=" + urllib.parse.quote(name[:120])
    return base + "".join(f"&tr={urllib.parse.quote(t)}" for t in TRACKERS)

def configured() -> list[str]:
    """启用哪些源，按顺序。全不认识就用默认。"""
    raw = os.getenv("SOURCES", DEFAULT_SOURCES)
    names = [n.strip().lower() for n in raw.split(",") if n.strip()]
    known = [n for n in names if n in REGISTRY]
    return known or [n for n in DEFAULT_SOURCES.split(",") if n in REGISTRY]

def route(code: str) -> list[str]:
    """这个番号该问哪些源。"""
    enabled = configured()
    if (code or "").upper().startswith("FC2-"):
        picked = [n for n in FC2_SOURCES if n in enabled]
        return picked or [n for n in FC2_SOURCES if n in REGISTRY]
    return enabled

# ---------------------------------------------------------------- 各源

async def fetch_javbus(code: str, client: httpx.AsyncClient) -> list[dict]:
    items = await javbus.search_magnets(code, client=client, limit=20)
    return [{
        "magnet": m["magnet"],
        "title": m.get("title") or "",
        "size_bytes": int(m.get("size_bytes") or 0),
        "size_text": m.get("size_text") or "",
        "has_subtitle": bool(m.get("has_subtitle")),
        "is_hd": bool(m.get("is_hd")),
        "seeders": 0,                    # javbus 不给做种数
        "source": "javbus",
    } for m in items]

def parse_sukebei(xml: str) -> list[dict]:
    out = []
    for block in re.findall(r"<item>(.*?)</item>", xml, re.S):
        title = (re.search(r"<title>(.*?)</title>", block, re.S) or [None, ""])[1]
        title = re.sub(r"<!\[CDATA\[|\]\]>", "", title).strip()
        hit = re.search(r"<nyaa:infoHash>([0-9a-fA-F]{40})</nyaa:infoHash>", block)
        if not hit:
            continue
        size_text = (re.search(r"<nyaa:size>(.*?)</nyaa:size>", block) or [None, ""])[1]
        seeders = (re.search(r"<nyaa:seeders>(\d+)</nyaa:seeders>", block) or [None, "0"])[1]
        sub, hd = flags(title)
        out.append({
            "magnet": magnet_of(hit.group(1), title),
            "title": title,
            "size_bytes": size_bytes(size_text),
            "size_text": size_text.strip(),
            "has_subtitle": sub,
            "is_hd": hd,
            "seeders": int(seeders or 0),
            "source": "sukebei",
        })
    return out

async def fetch_sukebei(code: str, client: httpx.AsyncClient) -> list[dict]:
    r = await client.get("https://sukebei.nyaa.si/",
                         params={"page": "rss", "q": code, "c": "0_0", "f": "0"})
    r.raise_for_status()
    return parse_sukebei(r.text)

def parse_bt4g(xml: str) -> list[dict]:
    out = []
    for block in re.findall(r"<item>(.*?)</item>", xml, re.S):
        magnet = re.search(r"<link>(magnet:[^<]+)</link>", block)
        if not magnet:
            continue
        title = (re.search(r"<title>(.*?)</title>", block, re.S) or [None, ""])[1]
        title = re.sub(r"<!\[CDATA\[|\]\]>", "", title).strip()
        desc = (re.search(r"<description>(.*?)</description>", block, re.S) or [None, ""])[1]
        desc = re.sub(r"<!\[CDATA\[|\]\]>", "", desc)
        size_text = ""
        m = _SIZE_RE.search(desc)
        if m:
            size_text = f"{m.group(1)}{m.group(2)}"
        sub, hd = flags(title)
        out.append({
            "magnet": magnet.group(1).replace("&amp;", "&"),
            "title": title,
            "size_bytes": size_bytes(size_text),
            "size_text": size_text,
            "has_subtitle": sub,
            "is_hd": hd,
            "seeders": 0,                    # bt4g 不给做种数
            "source": "bt4g",
        })
    return out

async def fetch_bt4g(code: str, client: httpx.AsyncClient) -> list[dict]:
    r = await client.get("https://bt4gprx.com/search", params={"q": code, "page": "rss"})
    r.raise_for_status()
    return parse_bt4g(r.text)

REGISTRY = {
    "javbus": fetch_javbus,
    "sukebei": fetch_sukebei,
    "bt4g": fetch_bt4g,
}

# ---------------------------------------------------------------- 汇总

def rank(item: dict) -> tuple:
    """字幕优先，其次高清，再看做种数，最后体积。"""
    return (0 if item["has_subtitle"] else 1,
            0 if item["is_hd"] else 1,
            -int(item["seeders"] or 0),
            -int(item["size_bytes"] or 0))

def dedupe(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for item in items:
        hit = re.search(r"btih:([0-9a-zA-Z]+)", item.get("magnet") or "")
        key = hit.group(1).lower() if hit else item.get("magnet")
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out

async def search(code: str, client: httpx.AsyncClient | None = None,
                 limit: int = 8) -> list[dict]:
    """按番号搜候选；第一个有结果的源即采用。全失败返回 []。"""
    code = (code or "").strip().upper()
    if not code:
        return []
    own = client is None
    cli = client or httpx.AsyncClient(follow_redirects=True, timeout=TIMEOUT,
                                      headers={"User-Agent": UA})
    try:
        for name in route(code):
            fetch = REGISTRY.get(name)
            if not fetch:
                continue
            try:
                items = await fetch(code, cli)
            except Exception as exc:          # 单个源坏掉不该拖垮整次搜索
                log.warning("源 %s 搜 %s 失败：%s", name, code, str(exc)[:120])
                continue
            if items:
                log.info("源 %s 命中 %s 共 %d 条", name, code, len(items))
                return sorted(dedupe(items), key=rank)[:max(1, limit)]
        return []
    finally:
        if own:
            await cli.aclose()
