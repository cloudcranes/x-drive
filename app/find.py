"""关键词搜索：按词找片子（不是按番号）。

搜索框回车走这条路（`/api/find`）：输入 `MIMK`、`SSIS` 这类前缀或标题里
的词，看看有哪些片。番号精确匹配是另一条路（`/api/magnets`，下载用）。

首页媒体墙已于 2026-10-25 移除（只展示已归档资源）——「最近上架 / 翻页」那套
取数逻辑（`feed` / `WALL_*` 设置 / `javbus.latest` / `javdbwall.latest`）
随之删掉；本模块只用到 `javbus.search` + `javdbwall.search` + 字段归一。

纪律：
  · 不抛异常：上游挂了只回 ok:false，页面照常用；
  · 缓存：同一个词 5 分钟内只打一次上游；
  · 只读：这里不发任何下载请求，点开之后仍走原来的「搜磁力 → 确认 → 下载」。
"""
from __future__ import annotations

import asyncio
import logging
import time

from . import javbus, javdbwall

log = logging.getLogger("x-drive.find")

# ─────────────────────────────────────────── 字段归一

_CODE_KEYS = ("id", "code", "number", "num", "movie_id", "movieId")
_COVER_KEYS = ("img", "cover", "thumb", "thumbnail", "poster", "image", "pic", "cover_url")
_TITLE_KEYS = ("title", "name", "caption", "label")
_DATE_KEYS = ("date", "release_date", "releaseDate", "updated", "pubdate", "published")


def _pick(row: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = row.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return ""


def normalize_item(row: dict) -> dict | None:
    """一条原始记录 → 媒体墙卡片。没有番号或封面的直接丢掉。"""
    if not isinstance(row, dict):
        return None
    code = _pick(row, _CODE_KEYS).upper()
    cover = _pick(row, _COVER_KEYS)
    if not code or not cover:
        return None
    tags = row.get("tags") or row.get("genres") or row.get("labels") or []
    if isinstance(tags, (str, dict)):
        tags = [tags]
    clean_tags = []
    for tag in tags if isinstance(tags, list) else []:
        text = tag.get("name") if isinstance(tag, dict) else tag
        text = str(text or "").strip()
        if text:
            clean_tags.append(text)
    return {
        "code": code,
        "title": _pick(row, _TITLE_KEYS),
        "cover": cover,
        "date": _pick(row, _DATE_KEYS),
        "tags": clean_tags[:4],
    }


# ─────────────────────────────────────────── 取数

def clear_cache() -> None:
    _SEARCH_CACHE.clear()


# 关键词搜索缓存：(关键词, 条数上限) -> (时间, 结果)。同一个词 5 分钟内只打一次上游。
# 上限也要进键：曾只按关键词缓存，先搜 60 条再要 1 条会拿到 60 条。
_SEARCH_CACHE: dict[tuple[str, int], tuple[float, dict]] = {}
SEARCH_TTL = 300.0


async def find_async(keyword: str, limit: int = 60) -> dict:
    """按关键词找片子（不是按番号）。返回像媒体墙那样的卡片列表。

    为什么要它：番号精确匹配是「下载」那条路的入口，但用户常先输入一个前缀
    （`MIMK`、`SSIS`）或标题里的词看看有什么。上游的 `/api/movies/search`
    就是干这个的，能返回 30 条，之前前端完全没用上 —— 输入 `MIMK` 回车
    什么都不发生，看着像搜索框坏了。
    """
    text = (keyword or "").strip()
    if not text:
        return {"ok": False, "query": "", "items": [], "error": "关键词为空"}
    cap = max(1, int(limit))
    key = (text, cap)
    hit = _SEARCH_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < SEARCH_TTL:
        return {**hit[1], "cached": True}
    try:
        # 两个源的结果不一样（javbus 按番号前缀，javdb 也看标题），
        # 所以搜索不跟着媒体墙的来源走，而是先问 javbus、空了再问 javdb。
        rows = await javbus.search(text)
        if not rows:
            rows = await javdbwall.search(text)
    except Exception as exc:                    # 上游挂了只回错误，不抛
        log.warning("关键词搜索失败（%s）：%s", text, exc)
        return {"ok": False, "query": text, "items": [], "error": str(exc)[:200]}
    seen = set()
    items = []
    for row in rows:
        item = normalize_item(row)
        if not item or item["code"] in seen:
            continue
        seen.add(item["code"])
        items.append(item)
        if len(items) >= cap:
            break
    result = {"ok": True, "query": text, "items": items, "error": ""}
    _SEARCH_CACHE[key] = (time.monotonic(), result)
    return {**result, "cached": False}


def find(keyword: str, limit: int = 60) -> dict:
    """同步入口（web 处理函数是同步的）。"""
    return asyncio.run(find_async(keyword, limit))


