"""订阅自动化：盯住某个演员，出新的自动提交下载。

javdb 的演员页 `/actors/<id>` 每部作品都带番号、日期和「含磁鏈」标记；
详情页带若干条磁力，每条又有体积和标签。所以整条链路不需要别的数据源。

三条安全线，缺一不可：
  · 游标（cursor）：只抓订阅之后出的片。没这条会把演员几百部历史片全下一遍。
  · 去重：已在归档、正在下载的番号一律跳过。
  · 上限：每次最多下几条 + 单片体积上限。没这条一次轮询就能把磁盘和 Drive 配额吃光。
"""
from __future__ import annotations

import logging
import re
import time

import httpx

from . import core

log = logging.getLogger("x-drive.subscribe")

BASE = "https://javdb.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
TIMEOUT = 25.0

_ITEM_SPLIT = re.compile(r'<div class="item">')
_PATH_RE = re.compile(r'href="(/v/[A-Za-z0-9]+)"')
_CODE_RE = re.compile(r"<strong>([A-Za-z]+-\d+)</strong>")
_DATE_RE = re.compile(r'<div class="meta">\s*(\d{4}-\d{2}-\d{2})')
_ACTOR_ID_RE = re.compile(r"/actors/([A-Za-z0-9]+)")

# 详情页的磁力条目：<div class="item" data-size="5545" data-files="5" data-date="...">
_MAGNET_ITEM_RE = re.compile(
    r'<div class="item[^"]*"[^>]*data-size="(\d+)"[^>]*data-files="(\d+)"[^>]*>(.*?)</div>\s*</div>',
    re.S)
_MAGNET_HREF_RE = re.compile(r'href="(magnet:\?[^"]+)"')
_TAG_RE = re.compile(r'<span class="tag[^"]*">([^<]+)</span>')


def actor_id_of(value: str) -> str:
    """接受完整演员页 URL 或裸 ID，取出 ID。"""
    value = (value or "").strip()
    m = _ACTOR_ID_RE.search(value)
    if m:
        return m.group(1)
    return value if re.fullmatch(r"[A-Za-z0-9]{4,12}", value) else ""


def parse_actor_items(html: str) -> list[dict]:
    """演员页的作品列表：番号、日期、详情页路径、是否标了含磁链。"""
    out = []
    for block in _ITEM_SPLIT.split(html)[1:]:
        path = _PATH_RE.search(block)
        code = _CODE_RE.search(block)
        date = _DATE_RE.search(block)
        if not (path and code and date):
            continue
        title = re.sub(r"<[^>]+>", " ", block)
        title = re.sub(r"\s+", " ", title).strip()
        out.append({
            "code": code.group(1).upper(),
            "date": date.group(1),
            "path": path.group(1),
            "has_magnet": "含磁鏈" in block,
            "title": title[:200],
        })
    return out


def parse_magnets(html: str) -> list[dict]:
    """详情页的磁力列表：链接、体积(MB)、文件数、标签。"""
    out = []
    for m in _MAGNET_ITEM_RE.finditer(html):
        size_mb, files, body = int(m.group(1)), int(m.group(2)), m.group(3)
        href = _MAGNET_HREF_RE.search(body)
        if not href:
            continue
        # href 里的 & 被转义成 &amp;，还原成能直接交给 aria2 的形式
        link = href.group(1).replace("&amp;", "&")
        tags = [t.strip() for t in _TAG_RE.findall(body)]
        out.append({"magnet": link, "size_mb": size_mb, "files": files, "tags": tags})
    # 同一条可能被重复匹配，按链接去重
    seen, uniq = set(), []
    for m in out:
        key = m["magnet"].split("&dn=")[0].lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(m)
    return uniq


def pick_magnet(items: list[dict], prefer=("中文字幕", "高清")) -> dict | None:
    """挑一条磁力。

    优先带中文字幕的（有字幕对这类片是刚需），其次高清，最后挑体积最大的
    —— 通常就是画质最好的那一版。体积未知的排后面。
    """
    if not items:
        return None

    def score(item: dict) -> tuple:
        # prefer 是有序的，位置越靠前越重要，所以取「命中的最小下标」，
        # 而不是数命中几个 —— 数个数会让「中文字幕」和「高清」权重相同。
        tags = " ".join(item.get("tags") or [])
        ranks = [i for i, want in enumerate(prefer) if want in tags]
        best = min(ranks) if ranks else len(prefer)
        return (best, -int(item.get("size_mb") or 0))

    return sorted(items, key=score)[0]


def load_subs(path: str) -> dict:
    return core.load_state(path)


def save_subs(path: str, data: dict) -> None:
    core.save_state(path, data)


def new_sub(actor_id: str, name: str, cursor: str, max_per_run: int = 1,
            max_size_gb: float = 0) -> dict:
    return {
        "id": actor_id,
        "name": name or actor_id,
        "kind": "actor",
        "cursor": cursor,
        "enabled": True,
        "max_per_run": max(1, int(max_per_run)),
        "max_size_gb": max(0.0, float(max_size_gb)),
        "added_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "last_run": "",
        "last_result": "",
    }


def candidates(items: list[dict], cursor: str, known_codes: set[str]) -> list[dict]:
    """从演员页条目里筛出「游标之后 + 含磁链 + 还没归档」的，按日期从旧到新。

    从旧到新是有意为之：一次轮询拿到多部时先下最早那部，游标才能稳步前移，
    不会因为一直只下最新的而把中间的漏掉。
    """
    picked = []
    for it in items:
        if not it.get("has_magnet"):
            continue
        if cursor and it["date"] <= cursor:
            continue
        if it["code"].upper() in known_codes:
            continue
        picked.append(it)
    picked.sort(key=lambda x: (x["date"], x["code"]))
    return picked


async def fetch(client: httpx.AsyncClient, url: str, timeout: float = TIMEOUT) -> str | None:
    try:
        r = await client.get(url, headers={"User-Agent": UA}, timeout=timeout)
        if r.status_code != 200:
            log.warning("javdb 返回 %s：%s", r.status_code, url)
            return None
        return r.text
    except Exception as exc:
        log.warning("javdb 请求失败 %s：%s", url, exc)
        return None


async def check(actor_id: str, cursor: str, max_per_run: int = 1,
                max_size_gb: float = 0, known_codes: set[str] | None = None,
                client: httpx.AsyncClient | None = None) -> dict:
    """查一次订阅，返回要下载的条目（不真正提交，提交由调用方做）。

    任何一步失败都返回空结果，不抛异常 —— 订阅是后台任务，挂了不能拖累别处。
    """
    known = {c.upper() for c in (known_codes or set())}
    own = client is None
    cli = client or httpx.AsyncClient(follow_redirects=True, timeout=TIMEOUT)
    result = {"new": [], "cursor": cursor, "error": ""}
    try:
        html = await fetch(cli, f"{BASE}/actors/{actor_id}")
        if not html:
            result["error"] = "演员页抓取失败"
            return result

        items = parse_actor_items(html)
        todo = candidates(items, cursor, known)[: max(1, int(max_per_run))]
        if not todo:
            # 即使没有新片，也把游标推到本页最新，避免下次重复比对老条目
            dates = [i["date"] for i in items if i.get("date")]
            if dates:
                result["cursor"] = max([cursor or ""] + dates)
            return result

        for it in todo:
            detail = await fetch(cli, BASE + it["path"])
            if not detail:
                continue
            magnets = parse_magnets(detail)
            best = pick_magnet(magnets)
            if not best:
                log.info("%s 详情页没有磁力，跳过", it["code"])
                continue
            size_gb = best["size_mb"] / 1024.0
            if max_size_gb and size_gb > max_size_gb:
                log.info("%s 体积 %.2fGB 超过上限 %.1fGB，跳过", it["code"], size_gb, max_size_gb)
                result["cursor"] = max(result["cursor"], it["date"])
                continue
            result["new"].append({
                "code": it["code"],
                "date": it["date"],
                "magnet": best["magnet"],
                "size_mb": best["size_mb"],
                "tags": best["tags"],
                "title": it["title"],
            })
            result["cursor"] = max(result["cursor"], it["date"])
        return result
    except Exception as exc:
        log.warning("检查订阅 %s 失败：%s", actor_id, exc)
        result["error"] = str(exc)[:200]
        return result
    finally:
        if own:
            await cli.aclose()
