"""轻量运行指标：失败分类计数 + 最近成功时间 + 待办数。

写 json 小文件，web 容器直接读，不引入监控依赖。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

OUTCOMES = ("ok", "partial", "no_media", "parse_fail", "download_fail", "upload_fail", "error")


def _load(path: str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def bump(path: str, outcome: str, detail: str = "") -> None:
    """累加一次结果。outcome 取 OUTCOMES 之一，未知值归到 error。"""
    outcome = outcome if outcome in OUTCOMES else "error"
    data = _load(path)
    total = data.setdefault("total", {})
    total[outcome] = int(total.get(outcome, 0)) + 1
    data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    if outcome in ("ok", "partial"):
        data["last_success"] = data["updated"]
    if detail:
        failures = data.setdefault("recent_failures", [])
        failures.insert(0, {"at": data["updated"], "outcome": outcome, "detail": detail[:200]})
        del failures[5:]
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        tmp = f"{path}.tmp"
        Path(tmp).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def snapshot(path: str) -> dict:
    data = _load(path)
    total = data.get("total") or {}
    done = int(total.get("ok", 0)) + int(total.get("partial", 0))
    attempts = sum(int(v) for v in total.values())
    return {
        "total": total,
        "attempts": attempts,
        "succeeded": done,
        "success_rate": round(done / attempts * 100, 1) if attempts else None,
        "last_success": data.get("last_success", ""),
        "recent_failures": data.get("recent_failures", []),
        "updated": data.get("updated", ""),
    }


def classify(text: str) -> str:
    """从回执文本推断结果类型，避免每个返回点都手工标记。"""
    if text.startswith("❌"):
        if "解析" in text:
            return "parse_fail"
        if "上传" in text or "rclone" in text:
            return "upload_fail"
        if "没有可下载" in text:
            return "no_media"
        return "error"
    if "⚠️" in text:
        return "partial"
    return "ok"
