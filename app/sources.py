"""按番号搜磁力：多源注册表，合并全部源 + 统一字段 + 去重排序。

只按番号查，不做关键词乱搜。每个源一个 `async fetch(code, client) -> list[dict]`，
统一成：
    {"magnet", "title", "size_bytes", "size_text", "has_subtitle", "is_hd",
     "seeders", "source"}

路由：FC2 系列 javbus 必然 404，直接跳过；其余按 `SOURCES` 顺序**全部**问一遍，
把各源候选合并后按 infoHash 去重、按质量排序（先到先得只用一个源会漏掉别的源
里更好的那条）。单个源失败只记日志，不影响其他源。
"""
from __future__ import annotations

import asyncio
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
# 一次搜索的总预算（秒）：各源并发问，超过这个时间就采用已回来的结果。
# 比单个源的 TIMEOUT 短，因为慢源的候选缺席可以接受，卡住不能接受 ——
# 订阅轮询里每一部都要搜一次，一部卡 20 秒会拖垮整轮。
#
# 12 秒是按实测定出来的（2026-10-23，SSIS-123，三个源并发）：
#   javbus 1.2s / bt4g 5.4s / sukebei 6.4s，合计 6.4s。
# 8 秒偏紧 —— sukebei 稍慢一点就会被整源丢弃，白白少 22 条候选。
# 12 秒留了将近一倍的余量，又远小于单源 TIMEOUT=20（不会比原来更慢）。
SEARCH_DEADLINE = 12.0
# 预算用尽后取消慢源时，给它响应取消的宽限（秒）。超时就直接放弃：
# 等一个不配合的源把慢的时间等回来，比缺一条候选更糟。
CANCEL_GRACE = 0.5

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
    """字幕优先，其次高清，再看做种数，最后体积。

    用 .get 而不是下标：这条排序会用在两种形状不同的数据上 —— sources 的
    统一字段（has_subtitle / is_hd / seeders / size_bytes 都在）与
    javbus.magnets_for 的原始字段（键名不同）。缺哪个都当「没有」，
    不能因为少一个键就抛 KeyError。
    """
    return (0 if item.get("has_subtitle") else 1,
            0 if item.get("is_hd") else 1,
            -int(item.get("seeders") or 0),
            -int(item.get("size_bytes") or 0))

def dedupe(items: list[dict]) -> list[dict]:
    """按 infoHash 去重；同一资源在多个源都出现时**保留信息更全的那条**。

    合并多源后，同一个种子常常同时出现在 sukebei 与 bt4g：javbus 不给做种数，
    bt4g 也不给，只有 sukebei 给。原先「先到先留」会让 bt4g 那条的 seeders=0
    覆盖掉 sukebei 的真实做种数，排序时这条就被当成没人做种的。
    所以这里不丢字段：同 key 时逐字段取「有信息的那一份」。
    """
    seen: dict = {}
    order: list[str] = []
    for item in items:
        hit = re.search(r"btih:([0-9a-zA-Z]+)", item.get("magnet") or "")
        key = hit.group(1).lower() if hit else item.get("magnet")
        if key not in seen:
            seen[key] = dict(item)
            order.append(key)
            continue
        old = seen[key]
        for field in ("title", "size_bytes", "size_text", "seeders"):
            # 0 视为「没有信息」：javbus/bt4g 的 seeders 恒为 0，别盖掉真实值
            if not old.get(field) and item.get(field):
                old[field] = item[field]
        # 字幕/高清是布尔，认「真」的那个：任一源说带字幕就带字幕
        for field in ("has_subtitle", "is_hd"):
            if item.get(field):
                old[field] = True
    return [seen[k] for k in order]

async def search(code: str, client: httpx.AsyncClient | None = None,
                 limit: int = 8) -> list[dict]:
    """按番号搜候选：问遍所有可用源，合并后去重排序。全失败返回 []。

    以前是「先到先得」：第一个有结果的源即采用，后面的源根本不打。代价是
    候选池只有一个源的大小 —— 同一个番号在 sukebei 只有老种、在 bt4g 才有
    带种的高清时，那条更好的就永远看不见。现在合并全部源的候选，
    dedupe 按 infoHash 去重（同一资源只留一条），rank 决定先后。

    合并要等齐所有源，但**不等慢源**：给整次搜索一个总预算 SEARCH_DEADLINE，
    到点就用已经回来的结果，剩下还在跑的源取消掉。各源并发问（串行叠加三个源
    各 20 秒的超时会把一次搜索拖成一分钟），共享同一个 client（httpx 支持并发），
    但只有自己建的 client 才在 finally 里关 —— 传进来的 client 归调用方所有。

    慢源的候选因此可能缺席：这是刻意的取舍。宁可少一条候选，也不能让一次
    搜索卡住半分钟 —— 用户等不起，而且订阅轮询里每一部都要搜一次。
    """
    code = (code or "").strip().upper()
    if not code:
        return []
    own = client is None
    cli = client or httpx.AsyncClient(follow_redirects=True, timeout=TIMEOUT,
                                      headers={"User-Agent": UA})
    try:
        names = [n for n in route(code) if REGISTRY.get(n)]
        if not names:
            return []

        async def ask(name: str) -> list[dict]:
            return await REGISTRY[name](code, cli)

        tasks = {name: asyncio.ensure_future(ask(name)) for name in names}
        done, pending = await asyncio.wait(tasks.values(),
                                           timeout=SEARCH_DEADLINE)
        for name in names:
            if name in pending:
                # 预算用尽还没回：取消掉，别让它在后台挂着
                tasks[name].cancel()
        merged: list[dict] = []
        for name in names:
            task = tasks[name]
            if not task.done():
                log.info("源 %s 搜 %s 超时（超过 %.0fs），本次不采用",
                         name, code, SEARCH_DEADLINE)
                continue
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None:
                log.warning("源 %s 搜 %s 失败：%s", name, code, str(exc)[:120])
                continue
            items = task.result()
            if items:
                log.info("源 %s 命中 %s 共 %d 条", name, code, len(items))
                merged.extend(items)
        if pending:
            # 收尾要**有上限**：被取消的源如果迟迟不响应取消（慢 DNS、被占住的连接），
            # 在这里等下去等于把慢源的时间又等回来。给一个短的收尾预算，到点就走。
            # 未收尾的任务会在事件循环关闭时被回收，不至于留下未 await 的警告 ——
            # 它们已经被 cancel() 标记，asyncio.run 会替我们收尾。
            await asyncio.wait(pending, timeout=CANCEL_GRACE)
            for task in pending:
                if not task.done():
                    log.debug("源取消未即时生效，本次直接放弃")
        if not merged:
            return []
        return sorted(dedupe(merged), key=rank)[:max(1, limit)]
    finally:
        if own:
            await cli.aclose()
