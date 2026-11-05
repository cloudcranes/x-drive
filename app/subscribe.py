"""订阅自动化：盯住演员或系列，出新片自动提交下载。

数据源统一用 JavBus API（`/api/movies?filterType=star|series`）：列表带番号与日期，
详情带 gid/uc，磁力接口给体积和字幕/高清标记。JavDB 网页只当回退。

三条安全线，缺一不可：
  · 游标（cursor）：只抓订阅之后出的片。没这条会把演员几百部历史片全下一遍。
  · 去重：已在归档、正在下载的番号一律跳过。
  · 上限：每次最多下几条 + 单片体积上限。没这条一次轮询就能把磁盘和 Drive 配额吃光。

JavBus 的 filterValue 只认站内 ID（演员如 `w5a`、系列如一串字母），不认名字，
所以订阅必须存「来源 + ID」两项。
"""
from __future__ import annotations

import logging
import re
import time
from urllib.parse import urlsplit

import httpx

from . import core, javbus, sources

log = logging.getLogger("x-drive.subscribe")

BASE = "https://javdb.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
TIMEOUT = 25.0
# 订阅流程一次要搜的候选池大小：多源合并后候选池变大，多取一些让「有字幕/有做种」
# 的那条能被挑到；太多会让每部都去翻几十条记录。
SEARCH_POOL = 30

_ITEM_SPLIT = re.compile(r'<div class="item">')
_PATH_RE = re.compile(r'href="(/v/[A-Za-z0-9]+)"')
# 番号结尾可能带字母（例：VSPDS-221AI 是 AI 重制版），只认结尾是数字会漏掉整页
_CODE_RE = re.compile(r"<strong>([A-Za-z]+-\d+[A-Za-z]*)</strong>")
_DATE_RE = re.compile(r'<div class="meta">\s*(\d{4}-\d{2}-\d{2})')
# 订阅对象可以是演员（/actors/xxx）或番号系列（/series/xxx）
_PATH_PREFIX = {"actor": "actors", "series": "series"}
_ID_RE = {
    # JavDB 是 /actors/xxx、/series/xxx；JavBus 是 /star/xxx、/series/xxx
    "actor": re.compile(r"/(?:actors|star)/([A-Za-z0-9]+)"),
    "series": re.compile(r"/series/([A-Za-z0-9]+)"),
}

# 详情页的磁力条目：<div class="item" data-size="5545" data-files="5" data-date="...">
_MAGNET_ITEM_RE = re.compile(
    r'<div class="item[^"]*"[^>]*data-size="(\d+)"[^>]*data-files="(\d+)"[^>]*>(.*?)</div>\s*</div>',
    re.S)
_MAGNET_HREF_RE = re.compile(r'href="(magnet:\?[^"]+)"')
_TAG_RE = re.compile(r'<span class="tag[^"]*">([^<]+)</span>')


def sub_id_of(value: str, kind: str = "actor") -> str:
    """从完整页面 URL 或裸 ID 里取出订阅对象 ID。

    kind 决定认哪种链接；裸 ID 两种都接受（都是 4~12 位字母数字）。
    """
    value = (value or "").strip()
    for k in ([kind] + [x for x in _PATH_PREFIX if x != kind]):
        m = _ID_RE[k].search(value)
        if m:
            return m.group(1)
    # JavBus 的站内 ID 可能很短（演员 w5a 只有三位），不能按四位起算
    return value if re.fullmatch(r"[A-Za-z0-9]{2,16}", value) else ""


# 番号前缀是纯大写字母（TCHB、MIMK），而 JavBus 站内系列 ID 都是小写字母数字
# （wdg、2t）。前缀不是站内 ID，系列筛选接口会 404；这类订阅改用搜索接口，
# 只收 code 以「前缀-」开头的作品，避免把同系列里的其他番号带进来。
_PREFIX_RE = re.compile(r"[A-Z]{2,10}")


def looks_like_series_prefix(value: str) -> bool:
    return bool(_PREFIX_RE.fullmatch((value or "").strip()))


def source_of(value: str) -> str:
    """链接里能看出站点就返回它；裸 ID 返回空串（交给调用方按用户选择）。

    以前不看链接主机，`https://javdb.com/actors/YnZ1K` 会被当成 JavBus 的 ID 存下来，
    结果查不到任何东西，用户只看到「没有新片」。
    """
    host = urlsplit(value or "").netloc.lower()
    if not host:
        return ""
    if "javdb" in host:
        return "javdb"
    if "javbus" in host:
        return "javbus"
    return ""


def actor_id_of(value: str) -> str:
    """兼容旧调用。"""
    return sub_id_of(value, "actor")


def parse_items(html: str) -> list[dict]:
    """作品列表：番号、日期、详情页路径、是否标了含磁链。

    演员页（/actors/）和系列页（/series/）的条目结构实测一致，
    都是 div.item > a.box > .video-title strong + .meta 日期，所以共用一套解析。
    """
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
        # href 里的 & 被转义成 &amp;，还原成能直接交给迅雷的形式
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


def parse_actor_items(html: str) -> list[dict]:
    """兼容旧调用。"""
    return parse_items(html)


def page_title(html: str) -> str:
    """取页面标题里的名字（<title> 形如「系列名 | JavDB 成人影片數據庫」）。"""
    m = re.search(r"<title>([^<]*)</title>", html)
    if not m:
        return ""
    return m.group(1).split("|")[0].strip()[:60]


def load_subs(path: str) -> dict:
    return core.load_state(path)


def save_subs(path: str, data: dict) -> None:
    core.save_state(path, data)


def sub_filters(sub: dict | None) -> dict:
    """提取标准化订阅筛选条件。"""
    sub = sub or {}
    f = dict(sub.get("filters") or {})
    inc = [k.strip() for k in str(f.get("include_keywords", sub.get("include_keywords", "")) or "").split(",") if k.strip()]
    exc = [k.strip() for k in str(f.get("exclude_keywords", sub.get("exclude_keywords", "")) or "").split(",") if k.strip()]
    return {
        "full_series": bool(f.get("full_series", sub.get("full_series", False))),
        "require_subtitle": bool(f.get("require_subtitle", sub.get("require_subtitle", False))),
        "require_hd": bool(f.get("require_hd", sub.get("require_hd", False))),
        "min_seeders": max(0, int(f.get("min_seeders", sub.get("min_seeders", 0)) or 0)),
        "min_size_gb": max(0.0, float(f.get("min_size_gb", sub.get("min_size_gb", 0)) or 0)),
        "max_size_gb": max(0.0, float(sub.get("max_size_gb") or f.get("max_size_gb") or 0)),
        "include_keywords": ",".join(inc),
        "exclude_keywords": ",".join(exc),
    }


def match_item_keywords(code: str, title: str, include_keywords: str = "",
                        exclude_keywords: str = "") -> tuple[bool, str]:
    """判断番号与标题是否满足关键词过滤。返回 (是否合格, 原因)。"""
    text = f"{code} {title}".upper()
    if exclude_keywords:
        for kw in re.split(r"[,，\s|/]+", exclude_keywords):
            kw = kw.strip().upper()
            if kw and kw in text:
                return False, f"命中排除词「{kw}」"
    if include_keywords:
        kws = [k.strip().upper() for k in re.split(r"[,，\s|/]+", include_keywords) if k.strip()]
        if kws and not any(kw in text for kw in kws):
            return False, f"未包含指定关键词「{include_keywords}」"
    return True, ""


def new_sub(sub_id: str, name: str, cursor: str, max_per_run: int = 1,
            max_size_gb: float = 0, kind: str = "actor", source: str = "javbus",
            filters: dict | None = None) -> dict:
    clean_filters = sub_filters({"filters": filters, "max_size_gb": max_size_gb})
    return {
        "id": sub_id,
        # 来源决定用哪个接口查：javbus 认站内 ID，javdb 认 /actors/xxx
        "source": source if source in ("javbus", "javdb") else "javbus",
        "name": name or sub_id,
        "kind": kind if kind in _PATH_PREFIX else "actor",
        "cursor": cursor,
        "enabled": True,
        "max_per_run": max(1, int(max_per_run)),
        "max_size_gb": max(0.0, float(max_size_gb)),
        "filters": clean_filters,
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


async def discover_javbus(sub_id: str, cursor: str, client: httpx.AsyncClient | None = None,
                          kind: str = "actor", pages: int = 2,
                          full_series: bool = False) -> dict:
    """只做发现：把游标之后的**全部**作品列出来，不查磁力、不下判断。

    发现与提交分开才有得商量：发现是「这一页有什么」，提交是「这轮下几个」。
    混在一起就会出现「只处理 1 部却把游标推到整批最新」的漏项。

    游标只在**确实走到旧游标边界**时才前移：只翻 pages 页就宣布「发现完了」，
    积压超过两页时中间那段永久漏掉（后面的轮次全从新游标往后看）。
    没走到边界就不动游标，下一轮接着往回翻，靠待办表去重。

    当 full_series 为 True 时：支持全量历史回填，翻页深度自动扩展至翻到底或上限（默认 50 页）。
    """
    filter_type = "star" if kind == "actor" else "series"
    # 番号前缀不是站内系列 ID：系列筛选会 404，改用搜索接口按「前缀-」过滤
    prefix_mode = kind == "series" and looks_like_series_prefix(sub_id)
    result = {"items": [], "cursor": cursor, "error": "", "seen": 0, "pages_done": 0,
              "name": "", "caught_up": False}
    effective_pages = 50 if (full_series and pages <= 2) else max(1, pages)
    try:
        seen: dict[str, dict] = {}
        caught_up = (cursor == "" and not full_series)  # 首次追新：一页够定基线；全量：必须到底
        oldest = ""
        for page in range(1, effective_pages + 1):
            if prefix_mode:
                raw = await javbus.search(sub_id, page=page, client=client)
                batch = [it for it in raw
                         if (it.get("code") or "").upper().startswith(sub_id.upper() + "-")]
            else:
                batch = await javbus.list_movies(filter_type, sub_id, page=page, client=client)
            if not batch:
                if prefix_mode and page < effective_pages and raw:
                    continue                   # 这一页没命中前缀，继续往后翻
                caught_up = True               # 翻到底了
                break
            result["pages_done"] = page
            result["seen"] += len(batch)
            for it in batch:
                code = (it.get("code") or "").upper()
                date = it.get("date") or ""
                if not code:
                    continue
                if date and (not oldest or date < oldest):
                    oldest = date
                # 游标当天及之后的作品都算「还没处理过」，重叠回查交给待办表去重
                if cursor and date < cursor:
                    continue
                seen.setdefault(code, {**it, "code": code, "date": date})
            # 这一页已经翻到旧游标之前：边界触到了，可以安全前移
            if cursor and oldest and oldest < cursor:
                caught_up = True
                break
        result["items"] = sorted(seen.values(), key=lambda x: (x["date"], x["code"]))
        result["caught_up"] = caught_up
        dates = [it["date"] for it in result["items"] if it.get("date")]
        # 追上了才前移游标；没追上就保持原值，下一轮接着往回翻
        result["cursor"] = (max(dates) if (caught_up and dates) else "") or cursor
        # 只填了裸 ID 的订阅，用列表里带的演员/系列名补一个能看懂的名字
        names = [it.get("star") or it.get("series") for it in result["items"]]
        result["name"] = next((n for n in names if n), "")
        if not result["seen"]:
            if prefix_mode:
                result["error"] = (f"JavBus 搜不到 {sub_id} 开头的作品，"
                                   "或接口暂时不可用")
            else:
                result["error"] = (f"JavBus 没有返回作品（{sub_id or '空 ID'} 不是站内 ID，"
                                   "或接口暂时不可用）")
        return result
    except Exception as exc:
        log.warning("JavBus 发现订阅 %s 失败：%s", sub_id, exc)
        result["error"] = str(exc)[:200]
        return result


def pick_candidate(items: list[dict]) -> dict | None:
    """从多源候选里挑一条。

    口径：**有种子优先**，然后字幕 → 高清 → 做种数 → 体积
    （与 sources.rank 一致）。

    为什么「有没有种子」排在字幕前面：0 种子的种子迅雷云盘会「解析失败」
    （2026-10-24 实测 MIMK-267：选了 0 种子的那条，任务干等两小时）。
    字幕与高清是内容需求，做种数是**可达性** —— 下不下来，字幕再多也没用。

    体积仍作最后一项 —— 同等条件下大体积通常是画质更好的一版。
    """
    if not items:
        return None

    # 与手动搜索共用同一个排序器，避免 null/缺失字段在订阅里被误判成死种。
    return min(items, key=sources.rank)


def _choose(found: list[dict], code: str, max_size_gb: float,
            tried: list[str] | None,
            min_size_gb: float = 0,
            require_subtitle: bool = False,
            require_hd: bool = False,
            min_seeders: int = 0,
            exclude_keywords: str = "") -> tuple[dict | None, str]:
    """从候选里挑一条：先过滤（体积、字幕、高清、做种数、排除词、已试过），再排序挑最优。

    顺序不能反：以前是先挑最优再看体积，首选超限就整部放弃，明明还有合规
    候选也不下。tried 是「这个番号已经试过并失败的磁力」，换源时跳过，
    避免反复撞同一条死种。
    """
    if not found:
        return None, "还没有磁力"
    tried_set = {t for t in (tried or []) if t}
    usable = []
    too_big = 0
    too_small = 0
    no_sub = 0
    not_hd = 0
    low_seeders = 0
    kw_excluded = 0

    ex_kws = [k.strip().upper() for k in re.split(r"[,，\s|/]+", exclude_keywords) if k.strip()] if exclude_keywords else []

    for m in found:
        if m.get("magnet") in tried_set:
            continue
        size_bytes = int(m.get("size_bytes") or (int(m.get("size_mb") or 0) * 1024 * 1024))
        size_gb = size_bytes / (1024 ** 3) if size_bytes else 0
        if max_size_gb and size_gb > max_size_gb:
            too_big += 1
            continue
        if min_size_gb and size_gb < min_size_gb:
            too_small += 1
            continue

        tags = " ".join(m.get("tags") or [])
        name = m.get("name") or m.get("title") or ""
        mag_text = f"{name} {tags}".upper()

        if ex_kws and any(kw in mag_text for kw in ex_kws):
            kw_excluded += 1
            continue

        has_sub = bool(m.get("has_subtitle")) or ("中文字幕" in tags) or ("字幕" in tags) or ("-C" in name.upper()) or ("_C" in name.upper())
        if require_subtitle and not has_sub:
            no_sub += 1
            continue

        is_hd = bool(m.get("is_hd")) or ("高清" in tags) or ("HD" in tags.upper()) or ("1080" in mag_text) or ("2160" in mag_text) or ("4K" in mag_text)
        if require_hd and not is_hd:
            not_hd += 1
            continue

        seeders = sources.seeder_count(m)
        if min_seeders and seeders < min_seeders:
            low_seeders += 1
            continue

        usable.append(m)

    best = pick_candidate(usable)
    if not best:
        if too_big and not (no_sub or not_hd or too_small or low_seeders or kw_excluded):
            return None, f"{too_big} 条候选都超过上限 {max_size_gb}GB"
        reasons = []
        if no_sub:
            reasons.append(f"{no_sub} 条未含字幕")
        if not_hd:
            reasons.append(f"{not_hd} 条非高清")
        if too_big:
            reasons.append(f"{too_big} 条超过上限 {max_size_gb}GB")
        if too_small:
            reasons.append(f"{too_small} 条低于下限 {min_size_gb}GB")
        if low_seeders:
            reasons.append(f"{low_seeders} 条做种数不足 {min_seeders}")
        if kw_excluded:
            reasons.append(f"{kw_excluded} 条命中排除词")
        if reasons:
            return None, f"未满足筛选：{'；'.join(reasons)}"
        if tried_set:
            return None, "候选都试过了"
        return None, "还没有磁力"

    size_bytes = int(best.get("size_bytes") or (int(best.get("size_mb") or 0) * 1024 * 1024))
    return {
        "code": code,
        "magnet": best["magnet"],
        "size_mb": size_bytes // (1024 * 1024),
        "tags": list(best.get("tags") or []),
        "has_subtitle": bool(best.get("has_subtitle")),
        "is_hd": bool(best.get("is_hd")),
        "seeders": sources.seeder_count(best),
        "source": best.get("source") or "",
        "candidates": len(usable),
    }, ""


async def fetch_best(code: str, max_size_gb: float = 0,
                     client: httpx.AsyncClient | None = None,
                     tried: list[str] | None = None,
                     filters: dict | None = None) -> tuple[dict | None, str]:
    """给一个番号挑磁力。返回 (候选, 说明)。说明用于告诉用户为什么没下。"""
    f = filters or {}
    found = await sources.search(code, client=client, limit=SEARCH_POOL)
    return _choose(found, code, max_size_gb, tried,
                   min_size_gb=float(f.get("min_size_gb") or 0),
                   require_subtitle=bool(f.get("require_subtitle")),
                   require_hd=bool(f.get("require_hd")),
                   min_seeders=int(f.get("min_seeders") or 0),
                   exclude_keywords=str(f.get("exclude_keywords") or ""))


async def fetch_best_javbus(code: str, max_size_gb: float = 0,
                            client: httpx.AsyncClient | None = None,
                            tried: list[str] | None = None,
                            filters: dict | None = None) -> tuple[dict | None, str]:
    """只问 JavBus 一个源的版本。"""
    f = filters or {}
    found = await javbus.magnets_for(code, client=client)
    return _choose(found, code, max_size_gb, tried,
                   min_size_gb=float(f.get("min_size_gb") or 0),
                   require_subtitle=bool(f.get("require_subtitle")),
                   require_hd=bool(f.get("require_hd")),
                   min_seeders=int(f.get("min_seeders") or 0),
                   exclude_keywords=str(f.get("exclude_keywords") or ""))


async def check_javbus(sub_id: str, cursor: str, max_per_run: int = 1,
                       max_size_gb: float = 0, known_codes: set[str] | None = None,
                       client: httpx.AsyncClient | None = None,
                       kind: str = "actor", pages: int = 2,
                       filters: dict | None = None) -> dict:
    """一轮订阅（无待办表时的简化版）：发现后按日期从旧到新取前几个查磁力。"""
    known = {c.upper() for c in (known_codes or set())}
    today = time.strftime("%Y-%m-%d")
    f = filters or {}
    out = await discover_javbus(sub_id, cursor, client=client, kind=kind, pages=pages,
                                full_series=bool(f.get("full_series")))
    result = {"new": [], "cursor": cursor, "error": out["error"], "skipped": 0,
              "seen": out["seen"], "backlog": 0, "items": out["items"]}
    if out["error"] and not out["items"]:
        return result
    todo = [it for it in out["items"]
            if it["code"] not in known and it["date"] < today]
    result["backlog"] = len(todo)
    for it in todo[:max(1, int(max_per_run))]:
        item, why = await fetch_best_javbus(it["code"], max_size_gb, client=client, filters=filters)
        if not item:
            log.info("%s 暂不下载：%s", it["code"], why)
            result["skipped"] += 1
            continue
        item.update({"date": it["date"], "title": it.get("title") or ""})
        result["new"].append(item)
    result["cursor"] = out["cursor"]
    return result


async def check(sub_id: str, cursor: str, max_per_run: int = 1,
                max_size_gb: float = 0, known_codes: set[str] | None = None,
                client: httpx.AsyncClient | None = None, kind: str = "actor",
                source: str = "javdb") -> dict:
    """查一次订阅，返回要下载的条目（不真正提交，提交由调用方做）。

    任何一步失败都返回空结果，不抛异常 —— 订阅是后台任务，挂了不能拖累别处。
    """
    if kind in ("actor", "series") and source == "javbus":
        return await check_javbus(sub_id, cursor, max_per_run, max_size_gb,
                                  known_codes, client=client, kind=kind)
    known = {c.upper() for c in (known_codes or set())}
    own = client is None
    cli = client or httpx.AsyncClient(follow_redirects=True, timeout=TIMEOUT)
    result = {"new": [], "cursor": cursor, "error": "", "skipped": 0}
    try:
        prefix = _PATH_PREFIX.get(kind, "actors")
        html = await fetch(cli, f"{BASE}/{prefix}/{sub_id}")
        if not html:
            result["error"] = f"{prefix} 页抓取失败"
            return result

        result["name"] = page_title(html)
        items = parse_items(html)
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
                result["skipped"] += 1
                result["cursor"] = max(result["cursor"], it["date"])
                continue
            size_gb = best["size_mb"] / 1024.0
            if max_size_gb and size_gb > max_size_gb:
                log.info("%s 体积 %.2fGB 超过上限 %.1fGB，跳过", it["code"], size_gb, max_size_gb)
                result["skipped"] += 1
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
        log.warning("检查订阅 %s 失败：%s", sub_id, exc)
        result["error"] = str(exc)[:200]
        return result
    finally:
        if own:
            await cli.aclose()
