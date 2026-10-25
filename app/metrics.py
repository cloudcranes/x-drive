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
        if "迅雷" in text or "TMP_ROOT" in text:
            return "download_fail"
        return "error"
    if "⚠️" in text:
        return "partial"
    return "ok"


# ---------------------------------------------------------------- 阶段耗时

# 阶段名。云添加链路是 land（等迅雷云盘落地）/ organize（远端整理）/
# clean（清源目录）；download 与 upload 是旧本地链路的残留，保留以兼容
# 历史数据（老记录里还有它们）。
PHASES = ("download", "organize", "upload", "land", "clean")


def record_timing(path: str, timing: dict) -> None:
    """记录一次任务的阶段耗时（秒）。

    只保留最近 50 条明细 + 每个阶段的累计/最大/平均，用于回答
    「到底慢在哪一步」这种问题 —— 之前完全没有这方面的数据。
    """
    data = _load(path)
    phases = data.setdefault("phases", {})

    for key in PHASES:
        value = float(timing.get(key) or 0)
        if value <= 0:
            continue
        bucket = phases.setdefault(key, {"count": 0, "sum": 0.0, "max": 0.0})
        bucket["count"] = int(bucket.get("count", 0)) + 1
        bucket["sum"] = round(float(bucket.get("sum", 0)) + value, 3)
        bucket["max"] = round(max(float(bucket.get("max", 0)), value), 3)

    total = float(timing.get("total") or 0)
    if total > 0:
        bucket = phases.setdefault("total", {"count": 0, "sum": 0.0, "max": 0.0})
        bucket["count"] = int(bucket.get("count", 0)) + 1
        bucket["sum"] = round(float(bucket.get("sum", 0)) + total, 3)
        bucket["max"] = round(max(float(bucket.get("max", 0)), total), 3)

    recent = data.setdefault("recent_timings", [])
    recent.insert(0, {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "code": (timing.get("code") or "")[:40],
        "size": int(timing.get("size") or 0),
        "outcome": (timing.get("outcome") or "ok")[:16],
        **{k: round(float(timing.get(k) or 0), 2) for k in ("download", "organize", "upload", "total")},
    })
    del recent[50:]

    data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        tmp = f"{path}.tmp"
        Path(tmp).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def timing_snapshot(path: str) -> dict:
    """把累计值换算成平均值，并给出「上传吞吐」这类能直接比较的指标。"""
    data = _load(path)
    phases = data.get("phases") or {}
    out = {}
    for key, bucket in phases.items():
        count = int(bucket.get("count", 0)) or 0
        if not count:
            continue
        total_sum = float(bucket.get("sum", 0))
        out[key] = {
            "count": count,
            "avg": round(total_sum / count, 2),
            "max": round(float(bucket.get("max", 0)), 2),
            "sum": round(total_sum, 2),
        }

    recent = data.get("recent_timings") or []
    uploads = [r for r in recent if r.get("upload") and r.get("size")]
    throughput = None
    if uploads:
        rates = [r["size"] / r["upload"] / 1048576 for r in uploads if r["upload"] > 0]
        if rates:
            throughput = round(sum(rates) / len(rates), 2)

    return {
        "phases": out,
        "upload_mbps": throughput,
        "recent": recent[:20],
        "updated": data.get("updated", ""),
    }
