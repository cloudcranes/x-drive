"""MetaTube 刮削后端客户端（用户自建服务，不重部署、不改服务端配置）。

已实测的接口契约（MetaTube v1.4.0-c625846，2026-10-23 在宿主机上验证）：

  GET /                                  → {"data":{"app":"metatube","version":...}}   无需鉴权
  GET /v1/providers                       → {"data":{"movie_providers":{名:站点}, ...}} 无需鉴权
  GET /v1/movies/search?q=<番号>           → {"data":[完整影片对象...]}   需鉴权 ✅ 实测 200
  GET /v1/movies/search?q=<番号>&provider= → 同上                    需鉴权 ✅ 实测 200
  GET /v1/movies/<id>/<provider>          → 404 provider not found（详情路由不可用）
  GET /v1/movies/search/<q>/<provider>    → 404（路径式写法不存在）

关键结论：**搜索接口本身就返回完整字段**（标题、演员、封面、日期、简介等），
所以不需要再打一次详情接口；详情路由在 404 上不必依赖。

鉴权：`Authorization: Bearer <TOKEN>`，TOKEN 取服务端容器环境变量 TOKEN。
服务端没配 TOKEN 时换任何带法都是 401 —— 这类差异只能靠实测发现。

纪律：token 只在这里用，不回显到日志、接口响应、元数据文件或导出包。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time

import httpx

log = logging.getLogger("x-drive.metatube")

BASE = os.getenv("METATUBE_URL", "").rstrip("/")
TOKEN = os.getenv("METATUBE_TOKEN", "")
# 默认 Bearer；服务端若用别的头名，改环境变量即可，不用改代码
AUTH_HEADER = os.getenv("METATUBE_AUTH_HEADER", "Authorization")
AUTH_SCHEME = os.getenv("METATUBE_AUTH_SCHEME", "Bearer")
# 真机在公网：本机直连超时，需走代理或内网穿透，这里统一交给 httpx 的 proxy 设置
PROXY = os.getenv("METATUBE_PROXY", "")
TIMEOUT = float(os.getenv("METATUBE_TIMEOUT", "25") or 25)
# 刮削结果缓存目录：同一个番号不要每轮都去打一次外部服务
CACHE_DIR = os.getenv("METATUBE_CACHE", "")


def configured() -> bool:
    return bool(BASE)


def _headers() -> dict:
    """带上鉴权头。scheme 留空就只发裸 token。"""
    if not TOKEN:
        return {}
    value = f"{AUTH_SCHEME} {TOKEN}".strip() if AUTH_SCHEME else TOKEN
    return {AUTH_HEADER: value}


def _client() -> httpx.AsyncClient:
    kwargs = {"follow_redirects": True, "timeout": TIMEOUT}
    if PROXY:
        kwargs["proxy"] = PROXY
    return httpx.AsyncClient(**kwargs)


def _cache_key(kind: str, name: str) -> str:
    return hashlib.sha1(f"{kind}:{name}".encode()).hexdigest()


def cache_get(code: str) -> dict:
    """本地缓存：服务挂了也能用上次刮到的数据，不比「什么都没有」更差。"""
    if not CACHE_DIR:
        return {}
    path = os.path.join(CACHE_DIR, _cache_key("movie", code) + ".json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def cache_put(code: str, record: dict) -> None:
    if not CACHE_DIR or not record:
        return
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, _cache_key("movie", code) + ".json")
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("刮削缓存写入失败 %s：%s", code, exc)


def normalize(raw: dict, provider: str = "") -> dict:
    """把 MetaTube 的返回压成我们自己的字段集。

    只搬真实存在的字段，缺什么就空着 —— 绝不为了「看起来完整」编造。
    """
    data = raw or {}
    number = str(data.get("number") or "").strip()
    return {
        "code": number.upper(),
        "title": (data.get("title") or "").strip(),
        "original_title": (data.get("original_title") or "").strip(),
        "summary": (data.get("summary") or "").strip(),
        # 服务端给的是 RFC3339（2021-07-14T00:00:00Z），NFO 要的是 2021-07-14
        "release_date": (data.get("release_date") or "").strip()[:10],
        "runtime": int(data.get("runtime") or 0),
        "director": (data.get("director") or "").strip(),
        "maker": (data.get("maker") or "").strip(),
        "label": (data.get("label") or "").strip(),
        "series": (data.get("series") or "").strip(),
        "actors": [a for a in (data.get("actors") or []) if a],
        "genres": [g for g in (data.get("genres") or []) if g],
        "score": data.get("score") or 0,
        "cover_url": (data.get("cover_url") or data.get("big_cover_url") or "").strip(),
        "preview_urls": [u for u in (data.get("preview_images") or []) if u],
        "provider": (provider or data.get("provider") or "").strip(),
        "source_url": (data.get("homepage") or "").strip(),
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


async def providers(client: httpx.AsyncClient | None = None) -> dict:
    """启用的刮削源。无需鉴权，用它验证「服务通不通」最省事。"""
    own = client is None
    c = client or _client()
    try:
        r = await c.get(f"{BASE}/v1/providers")
        r.raise_for_status()
        return (r.json() or {}).get("data") or {}
    finally:
        if own:
            await c.aclose()


async def search(query: str, provider: str = "",
                 client: httpx.AsyncClient | None = None) -> list[dict]:
    """按番号搜。带 provider 时只查该源，否则查全部。

    路由是查询串形式 `/v1/movies/search?q=...` —— 路径式 `/search/<q>/<p>` 实测 404。
    """
    own = client is None
    c = client or _client()
    params = {"q": query}
    if provider:
        params["provider"] = provider
    try:
        r = await c.get(f"{BASE}/v1/movies/search", params=params, headers=_headers())
        if r.status_code == 401:
            raise RuntimeError(
                "MetaTube 拒绝了鉴权（401）：请核对 METATUBE_TOKEN，"
                "必要时用 METATUBE_AUTH_HEADER / METATUBE_AUTH_SCHEME 改带法")
        r.raise_for_status()
        return (r.json() or {}).get("data") or []
    finally:
        if own:
            await c.aclose()


async def movie(number: str, provider: str = "",
                client: httpx.AsyncClient | None = None,
                fallback: bool = True) -> dict:
    """按番号取元数据。返回归一化字段；取不到返回空表，绝不抛给调用方。

    先看搜索接口：它返回的字段已经够归档用（实测）。
    搜索没有命中且允许 fallback 时，再试 `/v1/movies/<id>/<provider>`——
    该路由在 v1.4.0 上实测报 `provider not found`，留着只为兼容别的版本。
    """
    own = client is None
    c = client or _client()
    try:
        found = await search(number, provider, client=c)
        for item in found:
            rec = normalize(item)
            if matches(number, rec):
                return rec
        if found:
            # 有结果但番号不符：交给调用方判断，不擅自取第一条
            return normalize(found[0])
        if not fallback:
            return {}
        prov = provider or ""
        url = f"{BASE}/v1/movies/{number}/{prov}" if prov else f"{BASE}/v1/movies/{number}"
        r = await c.get(url, headers=_headers())
        if r.status_code in (401, 404):
            return {}
        r.raise_for_status()
        return normalize((r.json() or {}).get("data") or {}, prov)
    except Exception as exc:
        log.info("MetaTube 查询 %s 失败：%s", number, exc)
        return {}
    finally:
        if own:
            await c.aclose()


async def scrape(code: str, provider: str = "",
                 client: httpx.AsyncClient | None = None) -> dict:
    """刮一个番号：先查缓存 → 打服务 → 只合并新增字段。

    合并而不是整条替换：部分查询返回的字段少，整条覆盖会丢掉以前的演员、
    简介等 —— 元数据一旦丢字段，用户看到的归档就残缺了。
    """
    cached = cache_get(code)
    fresh = await movie(code, provider, client=client)
    if not fresh:
        return cached
    merged = {**cached}
    for key, value in fresh.items():
        if isinstance(value, list):
            if value:
                merged[key] = value
        elif value not in ("", 0, None):
            merged[key] = value
        else:
            merged.setdefault(key, value)
    # 番号以请求为准：服务端有时回带版本串（如 abc-123），不能覆盖我们要的键
    merged["code"] = (cached.get("code") or code).upper()
    cache_put(code, merged)
    return merged


def matches(code: str, record: dict) -> bool:
    """返回的元数据是不是这个番号。

    MetaTube 会按相似度返回一堆结果，标题像不算数 —— 只看规范化后的番号。
    """
    want = "".join(ch for ch in (code or "").upper() if ch.isalnum())
    got = "".join(ch for ch in (record.get("code") or "").upper() if ch.isalnum())
    return bool(want) and want == got
