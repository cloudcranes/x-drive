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
                  media: dict | None = None, want_nfo: bool = True) -> dict:
    """把附件写进工作区，返回「写了哪些、失败哪些」。

    任何一步失败都只记在返回里：视频照样能传，缺的附件之后单独补。
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
        write_atomic(work / MANIFEST_NAME,
                     json.dumps(manifest(names, record, code=code, media=media),
                                ensure_ascii=False, indent=1))
        written.append(MANIFEST_NAME)
    except OSError as exc:
        failed.append({"name": MANIFEST_NAME, "error": str(exc)[:200]})
    return {"written": written, "failed": failed}
