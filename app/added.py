"""云盘里「已经添加过」的资源索引：搜磁力时据此提示哪些是重复的。

要解决的问题：用户搜一个番号，搜出 8 条磁力，他不知道哪条云盘里已经有了。
点一下才发现「已经加过了」，白等一轮落地才发现。

怎么判断「已添加」——两条互补的证据：

  1. **归档记录里的 infohash**：本项目自己的账本（state 表）。资源身份用
     infohash 判，与磁力字符串无关（同一种子换 tracker 也是同一个）。
  2. **迅雷云盘离线任务表**（/drive/v1/tasks）：正在落地中的任务，url 里带
     infohash。任务做完就从表里消失，所以它只覆盖「进行中」那一段。

两条都只读，不碰云盘内容。查不到就当作「没有」——宁可少提示，不可错提示
（把没下过的说成下过了，用户会以为是自己记错）。

**番号匹配**是第二层：同名不同资源（不同版本、不同压制组）算不同资源，
只提示「这部已经下过」而不是逐条标重复。用户看到提示后自己判断要不要换版本。
"""
from __future__ import annotations

import logging
import os
import re

from . import codes as codes_mod
from . import store

log = logging.getLogger("x-drive.added")

# 云盘目录名 → 番号。目录名可能是 MIMK-188，也可能带后缀（mimk-188ch）。
_DIR_CODE_RE = re.compile(r"([A-Za-z]{2,6})-?\s*(\d{2,5})")


def _hash_of(magnet: str) -> str:
    """磁力 → infohash（小写 hex）。取不到返回空串。"""
    match = re.search(r"btih:([0-9a-fA-F]{20,40})", magnet or "")
    return match.group(1).lower() if match else ""


def _code_of(text: str) -> str:
    """从目录名/标题里提取番号。MIMK-188 → MIMK-188。"""
    raw = (text or "").strip()
    if not raw:
        return ""
    match = _DIR_CODE_RE.search(raw)
    if not match:
        return ""
    try:
        return codes_mod.normalize(f"{match.group(1)}-{match.group(2)}")
    except Exception:
        return ""


class AddedIndex:
    """已添加资源的索引：infohash → 记录，番号 → 记录。

    `by_hash` 是「这个资源本身已经下过」；
    `by_code` 是「这个番号已经下过（可能是别的版本）」。
    """

    def __init__(self) -> None:
        self.by_hash: dict[str, dict] = {}
        self.by_code: dict[str, dict] = {}

    def add(self, *, infohash: str = "", code: str = "", record: dict | None = None) -> None:
        info = (infohash or "").strip().lower()
        num = codes_mod.normalize(code) if code else ""
        rec = record or {}
        if info and info not in self.by_hash:
            self.by_hash[info] = rec
        if num and num not in self.by_code:
            self.by_code[num] = rec

    def match_hash(self, magnet: str) -> dict | None:
        """这条磁力的资源本身下过没有。"""
        info = _hash_of(magnet)
        if not info:
            return None
        return self.by_hash.get(info)

    def match_code(self, code: str) -> dict | None:
        """这个番号下过没有（可能是别的版本）。"""
        num = codes_mod.normalize(code) if code else ""
        if not num:
            return None
        return self.by_code.get(num)

    def __len__(self) -> int:
        return len(self.by_hash)


def from_state(state: dict, store_obj=None) -> AddedIndex:
    """从归档账本（state 表）建索引。

    归档记录里 infohash 常常是空的 —— 只有 source 是 magnet 时才填。
    所以这里两个来源都读：infohash 字段本身，以及 source 里的 magnet。
    """
    index = AddedIndex()
    for key, record in (state or {}).items():
        if not isinstance(record, dict):
            continue
        info = str(record.get("infohash") or "").strip().lower()
        if not info:
            info = _hash_of(str(record.get("source") or ""))
        num = codes_mod.normalize(str(record.get("code") or key)) if (record.get("code") or key) else ""
        index.add(infohash=info, code=num, record=record)
    return index


def from_jobs(store_obj, limit: int = 200) -> AddedIndex:
    """从任务表建索引：已提交过的任务（含进行中）也算「已添加」。

    任务表比归档账本全：失败的、被取消的也在里面 —— 失败的资源多半已经
    落地在云盘了（只是没整理完），照样不该再下一遍。
    """
    index = AddedIndex()
    try:
        jobs = store_obj.recent(limit)
    except Exception as exc:
        log.warning("读任务表建索引失败：%s", exc)
        return index
    for job in jobs:
        if not isinstance(job, dict):
            continue
        extra = job.get("extra") or {}
        if not isinstance(extra, dict):
            extra = {}
        info = str(extra.get("infohash") or "").strip().lower()
        if not info:
            info = _hash_of(str(job.get("source") or ""))
        index.add(infohash=info, code=str(job.get("code") or ""), record=job)
    return index


def from_cloud_tasks(client=None) -> AddedIndex:
    """从迅雷云盘的离线任务表建索引：正在落地中的任务。

    任务做完就从表里消失，所以它只补「进行中」这一段 —— 已完成的靠归档账本。
    """
    index = AddedIndex()
    try:
        from . import xunleipan
        tasks = xunleipan.offline_tasks(client=client)
    except Exception as exc:
        log.info("读迅雷云盘离线任务失败（按没有处理）：%s", exc)
        return index
    for task in tasks:
        info = _hash_of(str(task.get("url") or ""))
        index.add(infohash=info, code="", record=task)
    return index


def build(state: dict, store_obj=None, include_cloud_tasks: bool = True) -> AddedIndex:
    """建完整索引：归档账本 + 任务表 + 云盘进行中任务。"""
    index = from_state(state or {})
    if store_obj is not None:
        merged = from_jobs(store_obj)
        for info, rec in merged.by_hash.items():
            index.by_hash.setdefault(info, rec)
        for num, rec in merged.by_code.items():
            index.by_code.setdefault(num, rec)
    if include_cloud_tasks:
        cloud = from_cloud_tasks()
        for info, rec in cloud.by_hash.items():
            index.by_hash.setdefault(info, rec)
    return index


def build_for(state_path: str, jobs_db: str, include_cloud_tasks: bool = True) -> AddedIndex:
    """按路径建索引（bot 与 web 各自的入口都用这个）。"""
    state = {}
    try:
        with open(state_path, encoding="utf-8") as fh:
            import json
            data = json.load(fh)
            state = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        state = {}
    store_obj = None
    try:
        store_obj = store.Store(jobs_db)
    except Exception as exc:
        log.warning("打开任务库失败：%s", exc)
    return build(state, store_obj, include_cloud_tasks=include_cloud_tasks)


def annotate(items: list[dict], index: AddedIndex, code: str = "") -> list[dict]:
    """给搜索结果打上「已添加」标记。

    标记有两种，含义不同，界面上要分开说：

      · `added_exact` —— **这条磁力本身**已经下过（infohash 相同）。
        再点一次是纯重复，别浪费一次落地。
      · `added_code` —— 这个**番号**下过别的版本。可能是不同压制组，
        未必重复，所以只作提示。

    原样返回 items（就地补字段），调用方不用改读取方式。
    """
    if not index:
        return items
    code_rec = index.match_code(code) if code else None
    for item in items or []:
        if not isinstance(item, dict):
            continue
        exact = index.match_hash(str(item.get("magnet") or ""))
        if exact:
            item["added_exact"] = True
            item["added_exact_path"] = str(exact.get("path") or exact.get("rel") or "")
        elif code_rec:
            item["added_code"] = True
    return items
