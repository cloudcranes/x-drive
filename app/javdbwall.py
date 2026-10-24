"""javdb 首页列表解析：把 javdb.com 的「最新」页面变成媒体墙的卡片。

为什么不走 API：javdb 没有公开 JSON 接口，只有 HTML。但它首页那 40 条
正好是「最新收录带磁链」，字段齐（番号、标题、封面、日期、评分、标签），
正是媒体墙要的东西 —— 所以单独一个模块解析它，别把正则塞进 wall.py。

实测（2026-10-24）：
  GET https://javdb.com/?page=N        → 每页 40 条，`?page=2` 可取下一页
  Cookie: over18=1; locale=zh          → 必须，否则 18 岁确认页
  每条：<a href="/v/xxx" class="box" title="标题">
          <div class="cover"><img src="https://c0.jdbstatic.com/covers/xx/xxx.jpg"></div>
          <div class="video-title"><strong>SIMM-912</strong> ひより</div>
          <div class="score">…4.29分…</div>
          <div class="meta">2026-10-03</div>
          <div class="tags">…<span class="tag">含磁鏈</span>…</div>

纪律与 meta.py 一致：抓不到就返回空列表，绝不抛给调用方；番号从
`video-title` 的 `<strong>` 里取（比猜 URL 里的 id 可靠）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re

import httpx

log = logging.getLogger("x-drive.javdbwall")

BASE = os.getenv("JAVDB_BASE", "https://javdb.com").rstrip("/")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
# 18 岁确认 + 中文界面：没有这两个 Cookie 拿到的是确认页，一条都解析不出来
COOKIES = {"over18": "1", "locale": "zh"}
TIMEOUT = 20.0
PER_PAGE = 40

# 一个条目：<a ... class="box" ...> 里依次出现 封面 / 番号+标题 / 评分 / 日期 / 标签
_ITEM_RE = re.compile(
    r'<a\s+href="(?P<href>/v/(?P<vid>[A-Za-z0-9]+))"[^>]*class="box"[^>]*'
    r'title="(?P<title>[^"]*)"[^>]*>(?P<body>.*?)</a>', re.S)
# 封面：`<div class="cover">` 里可能先有一个「中字可播放」的 span，不能要求 img 紧跟
_COVER_RE = re.compile(r'<div class="cover[^"]*">.*?<img[^>]*src="(?P<src>[^"]+)"', re.S)
_CODE_RE = re.compile(r"<div class=\"video-title\">\s*<strong>(?P<code>[^<]+)</strong>", re.S)
_DATE_RE = re.compile(r'<div class="meta">\s*(?P<date>[^<]*?)\s*</div>', re.S)
_SCORE_RE = re.compile(r"(?P<score>\d+(?:\.\d+)?)分")
_TAG_RE = re.compile(r'<span class="tag[^"]*"[^>]*>(?P<tag>[^<]+)</span>', re.S)
# 「还有下一页」：javdb 用 rel="next" 标出来（比数条数可靠）


def parse_list(html: str, limit: int = 0) -> list[dict]:
    """解析首页/列表页。返回 media-wall 那种卡片（code/title/cover/date/tags）。"""
    out: list[dict] = []
    for m in _ITEM_RE.finditer(html or ""):
        body = m.group("body")
        code_m = _CODE_RE.search(body)
        cover_m = _COVER_RE.search(body)
        if not code_m or not cover_m:
            continue                      # 缺番号或缺封面：跳过这个条目
        code = code_m.group("code").strip().upper()
        if not code:
            continue
        date_m = _DATE_RE.search(body)
        score_m = _SCORE_RE.search(body)
        tags = [t.strip() for t in _TAG_RE.findall(body) if t.strip()]
        if score_m:
            tags.insert(0, f"★{score_m.group('score')}")
        out.append({
            "code": code,
            "title": (m.group("title") or "").strip(),
            "cover": cover_m.group("src").strip(),
            "date": (date_m.group("date") if date_m else "").strip(),
            "tags": tags[:4],
            "url": f"{BASE}/v/{m.group('vid')}",
        })
        if limit and len(out) >= limit:
            break
    return out


async def search(keyword: str, client: httpx.AsyncClient | None = None) -> list[dict]:
    """按关键词搜 javdb（`/search?q=…&f=all`）。返回卡片列表，失败给空。

    实测 `q=MIMK` 能返回十几条；javdb 的搜索比 javbus 的窄一些（它按番号前缀
    与标题匹配），所以两条路都留着：关键词搜片默认问 javbus，这个作为备用源。
    """
    text = (keyword or "").strip()
    if not text:
        return []
    own = client is None
    cli = client or httpx.AsyncClient(follow_redirects=True, timeout=TIMEOUT,
                                      headers={"User-Agent": UA}, cookies=COOKIES)
    try:
        r = None
        for attempt in range(2):
            try:
                r = await cli.get(BASE + "/search", params={"q": text, "f": "all"})
                break
            except Exception as exc:
                if attempt == 1:
                    raise
                log.info("javdb 搜索第 1 次失败，重试：%s", exc)
                await asyncio.sleep(0.6)
        if r is None or r.status_code != 200:
            log.warning("javdb 搜索 %r 返回 %s", text, getattr(r, "status_code", "?"))
            return []
        return parse_list(r.text)
    except Exception as exc:
        log.warning("javdb 搜索 %r 失败：%s", text, exc)
        return []
    finally:
        if own:
            await cli.aclose()


