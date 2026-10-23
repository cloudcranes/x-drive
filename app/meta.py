"""番号元数据：从 javdb 抓标题、日期、时长、导演、片商、评分、类别、演员、封面。

为什么要缓存到磁盘：一个番号的信息基本不变，抓一次就够。重复抓既慢又容易
被站点限流。缓存命中时零网络请求。

设计原则：
  · 抓不到就是 None，绝不抛异常给调用方 —— 元数据是锦上添花，不能影响下载上传
  · 番号必须精确匹配，宁可不显示，也不能把别的片子的信息挂到这个番号上
  · 请求之间留最小间隔，避免把站点打疼
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time

import httpx

from . import core, javbus

log = logging.getLogger("x-drive.meta")

BASE = "https://javdb.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
TIMEOUT = 20.0
# 两次外部请求之间的最小间隔（秒），只在本进程内生效
MIN_INTERVAL = float(os.getenv("META_MIN_INTERVAL", "1.5") or 0)
CACHE_PATH = os.getenv("META_PATH", "/data/meta.json")
# 优先用 javbus（JSON 接口，字段更全）；关掉或它挂了会回退到 javdb。
# 网页「设置 → 数据源」可以热改（settings.hot），改完下一次刮削就按新值走。
USE_JAVBUS = os.getenv("META_USE_JAVBUS", "1") not in ("0", "false", "False")


def _use_javbus() -> bool:
    from . import settings as settings_mod
    return bool(settings_mod.hot("META_USE_JAVBUS", USE_JAVBUS, bool))

_lock = threading.Lock()
_last_call = 0.0

# 详情页每个属性都长这样：<strong>片商:</strong>\n &nbsp;<span class="value">...</span>
_FIELD_RE = re.compile(
    r"<strong>(番號|日期|時長|導演|片商|評分|類別|演員):</strong>\s*(.*?)</div>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_COVER_RE = re.compile(r'<img[^>]*src="(https://[^"]*?/covers/[^"]+)"')
# 详情页「樣張」里的大图：和封面同源，用来做预览图墙
_PREVIEW_RE = re.compile(r'class="preview-images"[^>]*>.*?</div>', re.S)
# javdb 详情页：樣張是 <a class="tile-item" href="https://…/samples/…">
_DB_PREVIEW_RE = re.compile(r'href="(https://[^"]*?/samples/[^"]+)"')
_PREVIEW_IMG_RE = re.compile(r'href="(https://[^"]*?/samples/[^"]+)"|src="(https://[^"]*?/samples/[^"]+)"')
_CODE_IN_SEARCH = re.compile(
    r'href="(/v/[A-Za-z0-9]+)" class="box"[^>]*>(.*?)</a>', re.S)


def _text(raw: str) -> str:
    return _TAG_RE.sub("", raw).replace("&nbsp;", " ").strip()


def _values(raw: str) -> list[str]:
    """一个属性里的所有取值；有链接就取链接文字，否则取纯文本。"""
    links = [m.strip() for m in re.findall(r"<a[^>]*>([^<]+)</a>", raw)]
    if links:
        return [x for x in links if x]
    stripped = _text(raw)
    return [stripped] if stripped else []


def parse_detail(html: str, code: str) -> dict:
    """解析详情页。字段缺失就留空，不编造。"""
    fields = {m.group(1): m.group(2) for m in _FIELD_RE.finditer(html)}

    def one(name: str) -> str:
        vals = _values(fields.get(name, ""))
        return vals[0] if vals else ""

    def many(name: str) -> list[str]:
        return _values(fields.get(name, ""))

    title = ""
    m = re.search(r'<h2 class="title is-4">(.*?)</h2>', html, re.S)
    if m:
        # 标题里含 <strong>番号</strong>，先摘掉再用剩余的当标题
        body = re.sub(r"<strong>[^<]*</strong>", " ", m.group(1))
        title = re.sub(r"\s+", " ", _text(body)).strip()

    cover = ""
    m = _COVER_RE.search(html)
    if m:
        cover = m.group(1)

    # 预览图（樣張）：详情页里单独一块，抓不到就空列表
    previews: list[str] = []
    block = _PREVIEW_RE.search(html)
    if block:
        for hit in _PREVIEW_IMG_RE.finditer(block.group(0)):
            url = hit.group(1) or hit.group(2)
            if url and url not in previews:
                previews.append(url)
    if not previews:
        # javdb 版式：不管有没有 preview-images 块，都按 samples 链接兜底抓一次
        for url in _DB_PREVIEW_RE.findall(html):
            if url not in previews:
                previews.append(url)

    rating = one("評分")
    stars = ""
    if rating:
        m = re.search(r"(\d+(?:\.\d+)?)\s*分", rating)
        if m:
            stars = m.group(1)

    return {
        "code": code,
        "title": title,
        "date": one("日期"),
        "duration": one("時長"),
        "director": one("導演"),
        "maker": one("片商"),
        "rating": stars,
        "categories": many("類別"),
        "actors": many("演員"),
        "cover": cover,
        "previews": previews[:8],
        "url": f"{BASE}/search?q={code}",
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def parse_search(html: str, code: str) -> str | None:
    """从搜索结果里找精确匹配的那个番号，返回详情页路径。

    只认 <strong> 里和番号完全一致的那一条。搜 SSIS-123 时站点会顺带返回
    SSIS-023、SSIS-103 之类的相近结果，随便取第一条就会挂错信息。
    """
    want = (code or "").upper()
    for m in _CODE_IN_SEARCH.finditer(html):
        path, block = m.group(1), m.group(2)
        for hit in re.findall(r"<strong>([^<]+)</strong>", block):
            if hit.strip().upper() == want:
                return path
    # 兜底：有些版式把 strong 放在 a 标签外面
    for m in re.finditer(r"<strong>([^<]+)</strong>", html):
        if m.group(1).strip().upper() == want:
            head = html[: m.start()]
            paths = re.findall(r'href="(/v/[A-Za-z0-9]+)"', head)
            if paths:
                return paths[-1]
    return None


# ---------------------------------------------------------------- ID 解析
# 订阅要的是 javdb 的 actor/series ID，而元数据里只有名字。
# 这里按名字去站内搜索页取 ID，结果落盘缓存 —— 名字不变，ID 也不会变。

IDS_PATH = os.getenv("IDS_PATH", "/data/ids.json")
_COOKIES = {"over18": "1", "locale": "zh", "theme": "auto"}
_ID_RE = {
    "actor": re.compile(r'href="/actors/([^"?/#]+)'),
    "series": re.compile(r'href="/series/([^"?/#]+)'),
}
_NAV_IDS = {"censored", "uncensored", "western"}

def _id_key(kind: str, name: str) -> str:
    return f"{kind}:{(name or '').strip().lower()}"

def extract_id(html: str, name: str, kind: str = "actor") -> str | None:
    """从搜索页里找这个名字对应的 ID。

    要求名字和链接文字同时出现，且取最近的链接 —— 站内导航也有 /actors/xxx，
    光看第一个链接必然拿错（`/actors/censored` 之类）。
    """
    name = (name or "").strip()
    if not name:
        return None
    seg = "actors" if kind == "actor" else "series"
    anchor = re.compile(rf'href="/{seg}/([^"?/#]+)"[^>]*>(.*?)</a>', re.S)
    # 优先：名字就在这个链接自己的文字里（最稳）
    for m in anchor.finditer(html):
        if m.group(1) in _NAV_IDS:
            continue
        if name in _text(m.group(2)):
            return m.group(1)
    # 退一步：名字紧跟链接之后（站内卡片常把名字放在 <a> 外的 <strong> 里）
    for m in anchor.finditer(html):
        if m.group(1) in _NAV_IDS:
            continue
        tail = html[m.end():m.end() + 120]
        if name in _text(tail):
            return m.group(1)
    return None

def cached_id(name: str, kind: str = "actor") -> str | None:
    try:
        data = core.load_state(IDS_PATH)
    except Exception:
        return None
    value = data.get(_id_key(kind, name))
    return value if isinstance(value, str) and value else None

def _save_id(name: str, kind: str, value: str) -> None:
    try:
        core.update_state(IDS_PATH, {_id_key(kind, name): value})
    except OSError as exc:
        log.warning("ID 缓存写入失败：%s", exc)

async def resolve_id(name: str, kind: str = "actor", client: httpx.AsyncClient | None = None,
                     timeout: float = TIMEOUT) -> str | None:
    """名字 → javdb ID。查不到返回 None，不抛异常（订阅是锦上添花）。"""
    name = (name or "").strip()
    if not name:
        return None
    hit = cached_id(name, kind)
    if hit:
        return hit
    own = client is None
    cli = client or httpx.AsyncClient(follow_redirects=True, timeout=timeout, headers={"User-Agent": UA},
                                      cookies=_COOKIES)
    try:
        _throttle()
        r = await cli.get(f"{BASE}/search", params={"q": name, "f": kind})
        if r.status_code != 200:
            log.warning("javdb 搜索 %s 返回 %s", name, r.status_code)
            return None
        value = extract_id(r.text, name, kind)
        if value:
            _save_id(name, kind, value)
        return value
    except Exception as exc:
        log.warning("解析 %s 的 ID 失败：%s", kind, name, exc)
        return None
    finally:
        if own:
            await cli.aclose()

def load_cache(path: str | None = None) -> dict:
    return core.load_state(path or CACHE_PATH)


def save_cache(data: dict, path: str | None = None) -> None:
    core.save_state(path or CACHE_PATH, data)


def cached(code: str, path: str | None = None) -> dict | None:
    """只查缓存，不联网。看板渲染走这条路径。"""
    if not code:
        return None
    item = load_cache(path).get(code.upper())
    return item if isinstance(item, dict) else None


def _throttle() -> None:
    global _last_call
    if MIN_INTERVAL <= 0:
        return
    with _lock:
        wait = MIN_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def _save(info: dict, code: str, path: str | None = None) -> None:
    """写缓存。失败只记日志 —— 缓存写不进去不该让本次查询失败。

    已有缓存里多出来的字段（比如后来才加的预览图）要保住：
    源码升级后不必清缓存重抓，旧记录也照样能补上新字段。
    """
    try:
        with _lock:
            data = load_cache(path)
            old = data.get(code) if isinstance(data.get(code), dict) else {}
            data[code] = {**old, **info}
            save_cache(data, path)
    except OSError as exc:
        log.warning("元数据缓存写入失败：%s", exc)


async def lookup(code: str, path: str | None = None, client: httpx.AsyncClient | None = None,
                 timeout: float = TIMEOUT) -> dict | None:
    """按番号取元数据：先看缓存，没有再联网抓，抓到就写缓存。

    任何失败都返回 None，不抛异常 —— 调用方不该因为查不到信息而中断下载。
    """
    code = (code or "").strip().upper()
    if not code:
        return None
    hit = cached(code, path)
    if hit:
        return hit

    own = client is None
    cli = client or httpx.AsyncClient(follow_redirects=True, timeout=timeout,
                                      headers={"User-Agent": UA})
    try:
        # 优先 javbus：直接返回 JSON，字段更全（导演/片商/发行商/系列/精确时长），
        # 不用解析 HTML。它挂了再回退到 javdb。
        if _use_javbus():
            try:
                info = await javbus.movie(code, client=cli)
            except Exception as exc:
                log.warning("javbus 查 %s 出错：%s", code, exc)
                info = None
            if info and info.get("title"):
                if not info.get("rating"):
                    info["rating"] = ""
                _save(info, code, path)
                return info

        _throttle()
        r = await cli.get(f"{BASE}/search", params={"q": code}, headers={"User-Agent": UA})
        if r.status_code != 200:
            log.warning("javdb 搜索 %s 返回 %s", code, r.status_code)
            return None
        path_part = parse_search(r.text, code)
        if not path_part:
            log.info("javdb 没有 %s 的精确匹配", code)
            return None

        _throttle()
        r2 = await cli.get(BASE + path_part, headers={"User-Agent": UA})
        if r2.status_code != 200:
            log.warning("javdb 详情 %s 返回 %s", path_part, r2.status_code)
            return None
        info = parse_detail(r2.text, code)
        if not info.get("title"):
            log.info("%s 详情页没有解析出标题，不写缓存", code)
            return None

        info["url"] = BASE + path_part
        _save(info, code, path)
        return info
    except Exception as exc:
        log.warning("javdb 查询 %s 失败：%s", code, exc)
        return None
    finally:
        if own:
            await cli.aclose()
