"""javbus API 客户端（https://javbus.master.us.kg，ovnrain/javbus-api 的部署实例）。

比解析 javdb 页面可靠得多：直接返回 JSON，磁力带 numberSize（精确字节）、
isHD、hasSubtitle、shareDate，不用靠标签文字去猜。

磁力是两段式：详情接口给出 gid 和 uc，磁力接口必须带上这两个参数，
缺一个就返回 400（实测确认）。所以不能只凭番号猜磁力地址。

所有方法失败时返回 None 或空列表，不抛异常 —— 它是可选数据源，
挂了应该由调用方回退到 javdb，而不是让下载流程崩掉。
"""
from __future__ import annotations

import logging
import os
import re
import time

import httpx

log = logging.getLogger("x-drive.javbus")

BASE = os.getenv("JAVBUS_BASE", "https://javbus.master.us.kg").rstrip("/")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
TIMEOUT = float(os.getenv("JAVBUS_TIMEOUT", "25") or 25)

_CODE_IN_TITLE = re.compile(r"^([A-Za-z]+-\d+[A-Za-z]*)", re.I)


def _client(client: httpx.AsyncClient | None) -> tuple[httpx.AsyncClient, bool]:
    if client is not None:
        return client, False
    return httpx.AsyncClient(follow_redirects=True, timeout=TIMEOUT,
                             headers={"User-Agent": UA}), True


async def _get(client: httpx.AsyncClient | None, path: str) -> object | None:
    own_client, owned = _client(client)
    try:
        r = await own_client.get(BASE + path, headers={"User-Agent": UA})
        if r.status_code != 200:
            log.warning("javbus %s 返回 %s", path, r.status_code)
            return None
        return r.json()
    except Exception as exc:
        log.warning("javbus 请求 %s 失败：%s", path, exc)
        return None
    finally:
        if owned:
            await own_client.aclose()


def normalize_movie(raw: dict) -> dict:
    """把详情接口的 JSON 摊成我们内部用的字段，缺的留空不编造。"""
    def name_of(v) -> str:
        if isinstance(v, dict):
            return (v.get("name") or "").strip()
        return str(v or "").strip()

    return {
        "code": (raw.get("id") or "").strip().upper(),
        "title": (raw.get("title") or "").strip(),
        "date": (raw.get("date") or "").strip(),
        "duration": f"{raw.get('videoLength')} 分钟" if raw.get("videoLength") else "",
        "director": name_of(raw.get("director")),
        "maker": name_of(raw.get("producer")),
        "publisher": name_of(raw.get("publisher")),
        "series": name_of(raw.get("series")),
        "categories": [name_of(g) for g in (raw.get("genres") or []) if name_of(g)],
        "actors": [name_of(s) for s in (raw.get("stars") or []) if name_of(s)],
        "cover": (raw.get("img") or "").strip(),
        "rating": "",
        "source": "javbus",
        "gid": str(raw.get("gid") or ""),
        "uc": str(raw.get("uc") or ""),
    }


async def movie(code: str, client: httpx.AsyncClient | None = None) -> dict | None:
    """按番号取详情。番号不存在时接口返回 404，这里返回 None。"""
    code = (code or "").strip()
    if not code:
        return None
    data = await _get(client, f"/api/movies/{code}")
    if not isinstance(data, dict) or not data.get("id"):
        return None
    info = normalize_movie(data)
    info["url"] = f"{BASE}/api/movies/{code}"
    info["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    return info


def normalize_magnet(raw: dict) -> dict | None:
    link = (raw.get("link") or "").strip()
    if not link.lower().startswith("magnet:"):
        return None
    return {
        "magnet": link,
        "size_bytes": int(raw.get("numberSize") or 0),
        "size_text": (raw.get("size") or "").strip(),
        "is_hd": bool(raw.get("isHD")),
        "has_subtitle": bool(raw.get("hasSubtitle")),
        "share_date": (raw.get("shareDate") or "").strip(),
        "title": (raw.get("title") or "").strip(),
    }


async def magnets(code: str, gid: str, uc: str,
                  client: httpx.AsyncClient | None = None) -> list[dict]:
    """磁力列表。必须先拿到详情里的 gid/uc，缺一不可。"""
    if not code or gid == "" or uc == "":
        return []
    data = await _get(client, f"/api/magnets/{code}?gid={gid}&uc={uc}")
    if not isinstance(data, list):
        return []
    out = []
    for raw in data:
        item = normalize_magnet(raw if isinstance(raw, dict) else {})
        if item:
            out.append(item)
    return out


async def magnets_for(code: str, client: httpx.AsyncClient | None = None) -> list[dict]:
    """一步拿到某个番号的所有磁力：先查详情取 gid/uc，再查磁力。"""
    info = await movie(code, client=client)
    if not info:
        return []
    return await magnets(info["code"], info.get("gid", ""), info.get("uc", ""), client=client)


def pick_magnet(items: list[dict], prefer_subtitle: bool = True,
                prefer_hd: bool = True) -> dict | None:
    """挑一条磁力。

    这类片带字幕是刚需，所以字幕优先；同样条件下挑高清，再挑体积最大的
    —— 通常就是画质最好的那版。全部靠结构化字段判断，不靠猜标题。
    """
    if not items:
        return None

    def score(m: dict) -> tuple:
        sub = 0 if (prefer_subtitle and m.get("has_subtitle")) else 1
        hd = 0 if (prefer_hd and m.get("is_hd")) else 1
        return (sub, hd, -int(m.get("size_bytes") or 0))

    return sorted(items, key=score)[0]


def parse_movies(payload: object) -> list[dict]:
    """列表接口（演员/系列/搜索）里的影片条目。"""
    if not isinstance(payload, dict):
        return []
    out = []
    for raw in payload.get("movies") or []:
        if not isinstance(raw, dict):
            continue
        code = (raw.get("id") or "").strip().upper()
        if not code:
            continue
        out.append({
            "code": code,
            "date": (raw.get("date") or "").strip(),
            "title": (raw.get("title") or "").strip(),
            "cover": (raw.get("img") or "").strip(),
            "tags": list(raw.get("tags") or []),
        })
    return out


async def list_movies(filter_type: str, filter_value: str, page: int = 1,
                      client: httpx.AsyncClient | None = None) -> list[dict]:
    """演员（star）或系列（series）的作品列表，只取有磁力的。"""
    if not filter_type or not filter_value:
        return []
    path = (f"/api/movies?filterType={filter_type}"
            f"&filterValue={filter_value}&magnet=all&page={int(page)}")
    return parse_movies(await _get(client, path))


async def search(keyword: str, client: httpx.AsyncClient | None = None) -> list[dict]:
    if not keyword:
        return []
    from urllib.parse import quote
    return parse_movies(await _get(client, "/api/movies/search?keyword=" + quote(keyword)))


async def available(client: httpx.AsyncClient | None = None) -> bool:
    """这个数据源当前能不能用。订阅/元数据决定要不要回退时先问一下。"""
    data = await _get(client, "/api/movies?page=1")
    return isinstance(data, dict) and isinstance(data.get("movies"), list)
