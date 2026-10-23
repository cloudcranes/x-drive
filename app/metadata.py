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


def _xml_escape(text: str) -> str:
    return (str(text or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def merge(old: dict, fresh: dict, locks: set[str] | None = None) -> dict:
    """新数据盖旧数据，但锁定字段保旧值、空值不覆盖有效值。"""
    locks = set(locks or ())
    out = dict(old or {})
    for key, value in (fresh or {}).items():
        if key in locks and out.get(key) not in (None, "", [], 0):
            continue
        empty = value in (None, "", [], 0)
        if empty and out.get(key) not in (None, "", [], 0):
            continue
        if isinstance(value, list) and not value:
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


def archive_record(code: str, path: str, link: str | None, files, size: int,
                   source: str, work: Path, scraped: dict | None = None,
                   manifest_files: list[str] | None = None, infohash: str = "",
                   locks=None, pending: bool = False, at: str | None = None) -> dict:
    """一条归档记录的完整字段契约。web 的 _finish 与 bot 的 run_task 都写这一份。

    以前两条路径各写一遍，bot 写出的记录少了 meta/manifest/infohash/locked/
    scrape_pending，之后的补刮、去重、校验对着它就无据可依。

    缺数据就留空/null，绝不编造：没刮到就是空表，没 infohash 就不写这个键。
    manifest_files 是清单里的文件名，大小按 work 里的实际文件取；省略就用 files。
    """
    names = [str(p.name if isinstance(p, Path) else p) for p in (files or []) if p]
    record = {
        "code": code,
        "path": path,
        "link": link or "",
        "files": names,
        "size": int(size or 0),
        "source": (source or "")[:300],
        "at": at or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "meta": dict(scraped or {}),
        "manifest": archive_manifest(work, manifest_files if manifest_files is not None
                                     else names, dict(scraped or {}), code=code),
        "scrape_pending": bool(pending),
    }
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


def build_attachments(work: Path, code: str, files: list[Path], scrape,
                      locks=None, old: dict | None = None, want_nfo: bool = True,
                      want_poster: bool = True, fetch=fetch_image) -> dict:
    """刮元数据 → 取封面 → 写 sidecar → 返回上传清单。web 与 bot 共用这一份。

    scrape 是「按番号取一条归一化元数据」的普通函数，由调用方负责跑协程
    （web 用 asyncio.run，bot 在自己的事件循环里 await），这样两边都能注入与替换；
    fetch 是「按 URL 取字节」的函数（同上）。这一层不读任务库、不管 extra：
    记录 extra 是调用方的事，这里只管工作区里的文件。

    刮削失败绝不阻断上传：record 空着、pending 置真，视频照常进清单。

    返回 {"record", "files", "written", "failed", "pending"}：
      files 是「视频 + 附件」一起上传的清单；pending=True 表示要补刮。
    """
    work = Path(work)
    record: dict = {}
    try:
        record = scrape(code) or {}          # 调用方给的协程，已含番号校验
    except Exception as exc:                 # 刮削是可选环节
        log.info("刮 %s 失败（不影响上传）：%s", code, exc)
    if record and not isinstance(record, dict):   # 形状不对就丢掉，别写坏附件
        log.warning("刮削返回的不是记录（%s），丢弃", type(record).__name__)
        record = {}
    locked = set(locks or ())
    if locked and record:
        record = merge(old or {}, record, locks=locked)

    poster = b""
    if want_poster and record.get("cover_url"):
        poster = fetch(record["cover_url"])
        if not poster:
            log.info("封面取不到：%s", record["cover_url"])

    out = {"record": record, "files": list(files), "written": [], "failed": [],
           "pending": False}
    try:
        out.update(write_sidecar(work, record, list(files), code=code, locks=locked,
                                 poster=poster or None, want_nfo=want_nfo))
    except Exception as exc:
        log.warning("生成元数据附件失败（不影响上传）：%s", exc)
        out["failed"] = [{"name": "attachments", "error": str(exc)[:200]}]
    # 视频 + 附件一起进上传清单：附件也是归档的一部分
    extras = [work / n for n in (JSON_NAME, NFO_NAME, POSTER_NAME, MANIFEST_NAME)
              if (work / n).is_file()]
    out["files"] = sorted(set(Path(p) for p in files) | set(extras))
    out["pending"] = not (record and record.get("title")) or bool(out["failed"])
    return out
