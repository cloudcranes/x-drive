"""首页媒体墙：把「最近上架 / 搜索」的封面流摆到页面上，看到就点。

数据源不写死。默认用 javbus 的 JSON 列表接口（`/api/movies?page=N`），但这里
把它做成可替换的：换一个后端只需要在 `SOURCES` 里加一项，或者在设置里给
`WALL_URL` 填一个返回 JSON 的地址（模板里写 `{page}`），字段名由
`_pick()` 按常见别名猜 —— 不同实现（javbus / alist 索引 / 自建聚合）字段名
五花八门，硬编码一套名字等于只能接一家。

几条纪律：
  · 不抛异常：媒体墙是锦上添花，上游挂了页面照常用，只是这一块空着；
  · 缓存：同一个 (源, 页) 在 TTL 内只打一次上游，滚动翻页不会把对方打疼；
  · 只读：这里不发任何下载请求，点开之后仍走原来的「搜磁力 → 确认 → 下载」。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx

from . import javbus, javdbwall, settings as settings_mod

log = logging.getLogger("x-drive.wall")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
DEFAULT_TTL = 120.0
TIMEOUT = 20.0

# 源注册表：key -> 人话名字。加源就在 _fetch_custom/_fetch_javbus 之外再写一支，
# 然后登记到这里，前端下拉框是照着这份表渲染的。
SOURCES = {
    "javbus": "JavBus 最新",
    "javdb": "JavDB 最新",
    "custom": "自定义接口",
}


def source() -> str:
    return str(settings_mod.hot("WALL_SOURCE", "javbus") or "javbus").strip()


def enabled() -> bool:
    return bool(settings_mod.hot("WALL_ON", True))


def ttl() -> float:
    return float(settings_mod.hot("WALL_TTL", DEFAULT_TTL, float) or DEFAULT_TTL)


def _url_template() -> str:
    return str(settings_mod.hot("WALL_URL", "") or "").strip()


# ─────────────────────────────────────────── 字段归一

_CODE_KEYS = ("id", "code", "number", "num", "movie_id", "movieId")
_COVER_KEYS = ("img", "cover", "thumb", "thumbnail", "poster", "image", "pic", "cover_url")
_TITLE_KEYS = ("title", "name", "caption", "label")
_DATE_KEYS = ("date", "release_date", "releaseDate", "updated", "pubdate", "published")
_LIST_KEYS = ("movies", "items", "data", "results", "list", "records")
_MORE_KEYS = ("hasNextPage", "has_next", "hasMore", "has_more", "next")


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


def normalize_payload(payload: object, page: int, page_size: int = 30) -> tuple[list[dict], bool]:
    """整包响应 → (条目, 还有下一页)。认不出结构就当空列表。"""
    rows: list = []
    more = False
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        for key in _LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                rows = value
                break
        for container in (payload, payload.get("pagination") or {}, payload.get("meta") or {}):
            if not isinstance(container, dict):
                continue
            for key in _MORE_KEYS:
                if key in container:
                    value = container[key]
                    more = bool(value) and not isinstance(value, dict)
                    break
            else:
                continue
            break
    items = [item for item in (normalize_item(r) for r in rows) if item]
    if not items:
        return [], False
    if not more:
        # 上游没给分页信息时，用「这一页满没满」兜底（满页 = 可能还有）
        more = len(rows) >= page_size
    return items, more


# ─────────────────────────────────────────── 取数

async def _fetch_javbus(page: int) -> tuple[list[dict], bool]:
    items, more = await javbus.latest(page)
    out = [item for item in (normalize_item(r) for r in items) if item]
    return out, more


async def _fetch_javdb(page: int) -> tuple[list[dict], bool]:
    """javdb 首页「最新」：HTML 解析（它没有公开 JSON 接口）。"""
    rows, more = await javdbwall.latest(page)
    out = [item for item in (normalize_item(r) for r in rows) if item]
    return out, more


async def _fetch_custom(page: int) -> tuple[list[dict], bool]:
    template = _url_template()
    if not template:
        raise RuntimeError("没配 WALL_URL")
    url = template.replace("{page}", str(max(1, int(page))))
    if "{page}" not in template:                 # 只有一页的接口：第 2 页直接空
        if page > 1:
            return [], False
        url = template
    async with httpx.AsyncClient(follow_redirects=True, timeout=TIMEOUT,
                                 headers={"User-Agent": UA}) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"返回不是 JSON：{exc}") from exc
    return normalize_payload(payload, page)


_FETCHERS = {"javbus": _fetch_javbus, "javdb": _fetch_javdb, "custom": _fetch_custom}

# (source, page) -> (取数时间, 结果)。失败不缓存，下次还会重试。
_CACHE: dict[tuple[str, int], tuple[float, dict]] = {}


def clear_cache() -> None:
    _CACHE.clear()
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


async def feed_async(page: int = 1, which: str = "") -> dict:
    """取一页。任何异常都收成 {ok: false, error}，调用方不必再包一层 try。"""
    name = (which or source() or "javbus").strip()
    if name not in _FETCHERS:
        return {"ok": False, "source": name, "page": page,
                "items": [], "more": False,
                "error": f"未知来源：{name}（可用：{'、'.join(SOURCES)}）"}
    page = max(1, int(page or 1))
    key = (name, page)
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl():
        return {**hit[1], "cached": True}
    try:
        items, more = await _FETCHERS[name](page)
    except Exception as exc:                    # 上游挂了不该影响首页
        log.warning("媒体墙取数失败（%s 第 %s 页）：%s", name, page, exc)
        return {"ok": False, "source": name, "page": page, "items": [], "more": False,
                "error": str(exc)[:200]}
    result = {"ok": True, "source": name, "page": page, "items": items, "more": more,
              "error": ""}
    _CACHE[key] = (time.monotonic(), result)
    return {**result, "cached": False}


def feed(page: int = 1, which: str = "") -> dict:
    """同步入口（web 是标准库 http.server，处理函数是同步的）。"""
    return asyncio.run(feed_async(page, which))
