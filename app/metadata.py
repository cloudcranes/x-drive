"""把刮到的元数据落成随资源上传的附件：movie.nfo / metadata.json / poster.jpg / manifest.json。

为什么由我们自己生成而不是让媒体服务器刮：归档一上传就该是自洽的。
换一个播放器、重建索引、或者服务端哪天连不上，目录里的信息仍然完整。

四条纪律：
  · 缺字段就留空，绝不编造（宁可 NFO 里少一行，也不要假数据）；
  · 附件生成失败不影响视频上传，只登记「待补刮」；
  · 手工锁定过的字段，自动刮削不得覆盖；
  · 清单（manifest）不写自身哈希，免得自引用。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("x-drive.metadata")

NFO_NAME = "movie.nfo"
JSON_NAME = "metadata.json"
POSTER_NAME = "poster.jpg"
MANIFEST_NAME = "manifest.json"
FORMAT_VERSION = 1

# 这些字段是「用户自己定的」，自动刷新的默认行为是保住它们
LOCKABLE = ("title", "original_title", "summary", "release_date", "runtime", "director",
            "maker", "label", "series", "actors", "genres", "score")

# 两条刮削源的字段名**不同构**，站内记录必须归一到 LOCKABLE 这套名字上。
#
#   javbus（meta.lookup 填的）  →  metatube（补刮填的）
#   date         → release_date
#   duration     → runtime
#   rating       → score
#   cover        → cover_url
#   categories   → genres
#
# 不归一的后果很实在：站内记录的 `date`/`duration` 对 LOCKABLE 是**不可见**的，
# 补刮会以为 release_date/runtime 是空的，写进 MetaTube 的值 —— 两套值
# 挤在同一条记录里，界面读到的是哪套全看键名。实测 MIMK-186 就是这样：
# 站内有 date=2024-11-29，补刮预览却说 release_date「从 null 变成 2024-11-29」。
#
# 归一方向：**站内记录统一成 LOCKABLE 那套**（metatube 的键名），因为
# LOCKABLE、NFO、metadata.json 全是按它写的。
_ALIAS = {
    "date": "release_date",
    "duration": "runtime",
    "rating": "score",
    "cover": "cover_url",
    "categories": "genres",
    "review": "summary",
}


def normalize_meta(rec: dict) -> dict:
    """把一条元数据记录归一到站内那套字段名。

    只**补**不**搬**：老键留着（`date` 还在），新键写上同一个值。
    这样老记录不丢东西，新写的记录键名统一。
    """
    out = dict(rec or {})
    for old, new in _ALIAS.items():
        value = out.get(old)
        if _blank(value):
            continue
        if _blank(out.get(new)):
            out[new] = value
    return out


def _xml_escape(text: str) -> str:
    return (str(text or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _blank(value) -> bool:
    """「没有内容」的口径：None、空串、空容器、0。锁定判定与空值判定共用一份。"""
    return value is None or value == "" or value == 0 or (
        isinstance(value, (list, tuple, dict, set)) and not value)


def with_code_prefix(title: str, code: str) -> str:
    """标题带上番号前缀。

    站内惯例是「番号 + 空格 + 标题」—— 卡片上直接能看出是哪部。但两条源的
    标题格式不一样：javbus 带番号前缀，metatube 不带。补刮时若直接用新值，
    标题会从「MIMK-186 入り浸り…」变成「入り浸り…」，番号前缀就没了 ——
    标题与番号脱节，NFO 的 sorttitle 也跟着不对。

    这里统一：缺前缀就补上，已有前缀就不重复加。
    """
    text = (title or "").strip()
    prefix = (code or "").strip()
    if not text or not prefix:
        return text
    if text.upper().startswith(prefix.upper() + " ") or text.upper() == prefix.upper():
        return text
    return f"{prefix} {text}"


def merge(old: dict, fresh: dict, locks: set[str] | None = None) -> dict:
    """新数据盖旧数据，但锁定字段保旧值、空值不覆盖有效值。

    「锁定」尊重的是**用户写进去的那个值**：只要这个字段在记录里已经有内容
    （含空串、空列表、0 这种「故意留空」），锁住之后就一律保旧值 —— 以前这里
    还会把「空串/0」当成没有内容，于是用户「清空并锁定」之后下一次刮削又把值
    填回去，等于把用户的决定静默推翻。

    反过来，字段根本还没写过值（记录里没有这个键）时，锁定不能把首次刮到的
    内容挡在门外，否则一条新归档会永远没有标题。
    """
    locks = set(locks or ())
    out = dict(old or {})
    for key, value in (fresh or {}).items():
        if key in locks and key in out:
            continue
        if _blank(value):
            # 新值是空的就一律不写：旧值有内容时保旧值，旧值也空时保持原样 ——
            # 否则会写出「（空）→（空）」这种无意义的变化，用户看了白看
            continue
        out[key] = value
    return out


def to_nfo(record: dict) -> str:
    """生成 Kodi/Emby 认的 movie.nfo。只写有值的字段。"""
    rec = record or {}
    lines = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', "<movie>"]
    pairs = [("title", rec.get("title")), ("originaltitle", rec.get("original_title")),
             ("sorttitle", rec.get("code")), ("plot", rec.get("summary")),
             ("premiered", rec.get("release_date")), ("director", rec.get("director")),
             ("studio", rec.get("maker")), ("label", rec.get("label")),
             ("set", rec.get("series")), ("rating", rec.get("score"))]
    for tag, value in pairs:
        if value not in (None, "", 0):
            lines.append(f"  <{tag}>{_xml_escape(value)}</{tag}>")
    if rec.get("runtime"):
        try:
            lines.append(f"  <runtime>{int(rec['runtime'])}</runtime>")
        except (TypeError, ValueError):
            pass
    for actor in rec.get("actors") or []:
        lines.append(f"  <actor><name>{_xml_escape(actor)}</name></actor>")
    for genre in rec.get("genres") or []:
        lines.append(f"  <genre>{_xml_escape(genre)}</genre>")
    if rec.get("code"):
        lines.append(f'  <uniqueid type="num" default="true">{_xml_escape(rec["code"])}</uniqueid>')
    if rec.get("cover_url"):
        lines.append(f"  <thumb aspect=\"poster\">{_xml_escape(rec['cover_url'])}</thumb>")
    lines.append("</movie>")
    return "\n".join(lines) + "\n"


def to_metadata(record: dict, code: str = "", locks: set[str] | None = None,
                source: str = "") -> dict:
    """上传的 metadata.json：完整字段 + 来源与格式版本，不含任何鉴权信息。"""
    rec = dict(record or {})
    rec.pop("token", None)                 # 防御：token 永远不许进文件
    return {
        "format": FORMAT_VERSION,
        "code": (code or rec.get("code") or "").upper(),
        "scraper": source or rec.get("provider") or "",
        "fetched_at": rec.get("fetched_at") or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "locked": sorted(locks or ()),
        "fields": {k: rec.get(k, "") for k in LOCKABLE},
        "extra": {k: v for k, v in rec.items()
                  if k not in LOCKABLE and k not in ("token",)},
    }


def manifest(files: list[Path], record: dict, code: str = "",
             media: dict | None = None) -> dict:
    """归档清单：实际文件名、大小、媒体探测结果。

    不写「本文件自身的哈希」：清单随内容变化，算进去就是自引用。
    """
    entries = []
    for path in sorted(files):
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append({"name": path.name, "size": stat.st_size,
                        "ext": path.suffix.lower()})
    return {
        "format": FORMAT_VERSION,
        "code": (code or record.get("code") or "").upper(),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "files": entries,
        "total_size": sum(e["size"] for e in entries),
        "media": media or {},
        "meta_provider": record.get("provider") or "",
        "meta_fetched_at": record.get("fetched_at") or "",
    }


def archive_manifest(work: Path, names: list[str], scraped: dict,
                     code: str = "") -> dict:
    """归档记录里的清单：按工作区实际文件取大小，文件已不在就记 0（不编）。

    manifest.json 不列自己（自引用）；条目按名字排序，与远端补刮时的写法一致。
    """
    entries = []
    for name in dict.fromkeys(str(n) for n in names if n):
        path = work / name
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        entries.append({"name": name, "size": size, "ext": Path(name).suffix.lower()})
    entries.sort(key=lambda e: e["name"])
    media_entries = [e for e in entries if e["name"] != MANIFEST_NAME]
    return {**manifest([], scraped or {}, code=code),
            "files": media_entries,
            "total_size": sum(e["size"] for e in media_entries)}


def validate_archive_backend(record: dict, kind: str, remote: str = "") -> None:
    """Shared Web/Bot guard; a persisted location must not cross backends."""
    which = str((record or {}).get("backend") or "")
    if which and which != kind:
        raise ValueError(f"归档后端 {which} 与当前后端 {kind} 不符，保留记录")
    if which == "rclone":
        saved = record.get("remote") or str(record.get("path") or "").split(":", 1)[0]
        if saved and saved != remote:
            raise ValueError("归档 rclone remote 与当前配置不符，保留记录")


def archive_record(code: str, path: str, link: str | None, files, size: int,
                   source: str, work: Path, scraped: dict | None = None,
                   manifest_files: list[str] | None = None, infohash: str = "",
                   locks=None, pending: bool = False, at: str | None = None,
                   backend: str = "", root: str = "", rel: str = "", remote: str = "") -> dict:
    """一条归档记录的完整字段契约。web 的 _finish 与 bot 的 run_task 都写这一份。

    以前两条路径各写一遍，bot 写出的记录少了 meta/manifest/infohash/locked/
    scrape_pending，之后的补刮、去重、校验对着它就无据可依。

    缺数据就留空/null，绝不编造：没刮到就是空表，没 infohash 就不写这个键。
    manifest_files 是清单里的文件名，大小按 work 里的实际文件取；省略就用 files。
    """
    names = [str(p.name if isinstance(p, Path) else p) for p in (files or []) if p]
    # 元数据统一字段名：javbus 那套（date/duration/…）补上映射到 LOCKABLE 的
    # 键名上，不然后续补刮看不见站内的值（见 normalize_meta 的说明）。
    scraped = normalize_meta(scraped or {})
    record = {
        "code": code,
        "path": path,
        "link": link or "",
        "files": names,
        "size": int(size or 0),
        "source": (source or "")[:300],
        "at": at or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "meta": dict(scraped),
        "manifest": archive_manifest(work, manifest_files if manifest_files is not None
                                     else names, dict(scraped), code=code),
        "scrape_pending": bool(pending),
    }
    if backend:
        record.update(backend=backend, root=root, rel=rel)
        if remote:
            record["remote"] = remote
    if infohash:                              # 有才写，没有不编
        record["infohash"] = str(infohash)
    if locks:
        record["locked"] = sorted(set(locks))
    return record


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    """可选的大文件哈希。按块读，别把几 GB 一次塞进内存。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def write_atomic(path: Path, text: str) -> None:
    """先写临时文件再改名：中途崩不会留下半个 NFO。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def write_sidecar(work: Path, record: dict, files: list[Path], code: str = "",
                  locks: set[str] | None = None, poster: bytes | None = None,
                  media: dict | None = None, want_nfo: bool = True,
                  keep: list[dict] | None = None) -> dict:
    """把附件写进工作区，返回「写了哪些、失败哪些」。

    任何一步失败都只记在返回里：视频照样能传，缺的附件之后单独补。

    keep 是「清单里必须保留」的条目（视频等不在工作区的文件）：补刮时工作区里
    没有视频，不带上它，新清单就会把云端清单覆盖成只剩附件。
    """
    written, failed = [], []
    try:
        meta = to_metadata(record, code=code, locks=locks,
                           source=record.get("provider") or "")
        write_atomic(work / JSON_NAME, json.dumps(meta, ensure_ascii=False, indent=1))
        written.append(JSON_NAME)
    except OSError as exc:
        failed.append({"name": JSON_NAME, "error": str(exc)[:200]})

    if want_nfo:
        try:
            write_atomic(work / NFO_NAME, to_nfo(record))
            written.append(NFO_NAME)
        except OSError as exc:
            failed.append({"name": NFO_NAME, "error": str(exc)[:200]})

    if poster:
        try:
            (work / POSTER_NAME).write_bytes(poster)
            written.append(POSTER_NAME)
        except OSError as exc:
            failed.append({"name": POSTER_NAME, "error": str(exc)[:200]})

    try:
        names = [p for p in files if p.parent == work] + [
            work / n for n in (JSON_NAME, NFO_NAME, POSTER_NAME) if (work / n).exists()]
        blob = manifest(names, record, code=code, media=media)
        if keep:
            have = {e["name"] for e in blob["files"]}
            blob["files"] = sorted(blob["files"] + [{**e} for e in keep
                                                    if e.get("name") not in have],
                                   key=lambda e: e["name"])
            blob["total_size"] = sum(e.get("size") or 0 for e in blob["files"])
        write_atomic(work / MANIFEST_NAME, json.dumps(blob, ensure_ascii=False, indent=1))
        written.append(MANIFEST_NAME)
    except OSError as exc:
        failed.append({"name": MANIFEST_NAME, "error": str(exc)[:200]})
    return {"written": written, "failed": failed}


def fetch_image(url: str, limit: int = 12 << 20, timeout: float = 20.0) -> bytes:
    """取封面。不信任大小，超过 limit 就放弃 —— 不要为一张图把内存吃掉。"""
    import urllib.request

    if not str(url or "").lower().startswith(("http://", "https://")):
        return b""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = resp.read(limit + 1)
    except Exception as exc:
        log.info("取封面失败 %s：%s", url, exc)
        return b""
    return b"" if len(data) > limit else data
