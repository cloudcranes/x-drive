"""指标与配额逻辑的单测：全部走文件，不碰网络。"""
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from app import metrics


@pytest.fixture
def store(tmp_path):
    return str(tmp_path / "metrics.json")


def test_bump_and_snapshot(store):
    metrics.bump(store, "ok")
    metrics.bump(store, "ok")
    metrics.bump(store, "partial")
    metrics.bump(store, "parse_fail", "该推文没有可下载的图片或视频")

    snap = metrics.snapshot(store)
    assert snap["total"]["ok"] == 2
    assert snap["succeeded"] == 3          # ok + partial 都算成功
    assert snap["attempts"] == 4
    assert snap["success_rate"] == 75.0
    assert snap["last_success"]
    assert snap["recent_failures"][0]["detail"]


def test_bump_unknown_outcome_counts_as_error(store):
    metrics.bump(store, "whatever")
    assert metrics.snapshot(store)["total"]["error"] == 1


def test_recent_failures_capped(store):
    for i in range(9):
        metrics.bump(store, "upload_fail", f"err{i}")
    assert len(metrics.snapshot(store)["recent_failures"]) == 5


def test_classify():
    assert metrics.classify("✅ 已存 Drive · 1 图") == "ok"
    assert metrics.classify("✅ 已存 Drive · 1 图\n⚠️ 1 项未入库：x") == "partial"
    assert metrics.classify("❌ 解析失败：超时") == "parse_fail"
    assert metrics.classify("❌ 该推文没有可下载的图片或视频") == "no_media"
    assert metrics.classify("❌ rclone 上传失败：x") == "upload_fail"
    assert metrics.classify("❌ 别的错误") == "error"


def test_snapshot_on_missing_file(store):
    snap = metrics.snapshot(store)
    assert snap["attempts"] == 0 and snap["success_rate"] is None


def test_usage_quota(tmp_path, monkeypatch):
    from app import main, store

    cfg = main.Config(
        token="x", allowed={1},
        remote="gdrive", drive_root="X", max_mb=10, concurrency=1,
        state_path=str(tmp_path / "s.json"), tmp_root=str(tmp_path / "dl"),
        rclone_config="/dev/null",
        pending_dir=str(tmp_path / "data" / "pending"),
        jobs_db=str(tmp_path / "jobs.db"),
    )
    monkeypatch.setattr(main, "_STORE", None)
    store.Store(cfg.jobs_db).state_put({"settings": {"DAILY_UPLOAD_GB": 1}})
    assert main.today_bytes(cfg) == 0
    main.add_usage(cfg, 600 * 1024**2)
    assert main.today_bytes(cfg) == 600 * 1024**2
    assert main.quota_exceeded(cfg, 300 * 1024**2) is False   # 900MB < 1GB
    assert main.quota_exceeded(cfg, 500 * 1024**2) is True    # 1.1GB > 1GB


def test_usage_resets_next_day(tmp_path, monkeypatch):
    from app import main, store

    cfg = main.Config(
        token="x", allowed={1},
        remote="gdrive", drive_root="X", max_mb=10, concurrency=1,
        state_path=str(tmp_path / "s.json"), tmp_root=str(tmp_path / "dl"),
        rclone_config="/dev/null",
        pending_dir=str(tmp_path / "data" / "pending"),
        jobs_db=str(tmp_path / "jobs.db"),
    )
    monkeypatch.setattr(main, "_STORE", None)
    store.Store(cfg.jobs_db).state_put({"settings": {"DAILY_UPLOAD_GB": 1}})
    path = main.usage_file(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"date": "2000-01-01", "bytes": 10**12}))
    assert main.today_bytes(cfg) == 0, "跨天必须清零"


def test_record_timing_accumulates_and_averages(tmp_path):
    from app import metrics
    path = str(tmp_path / "m.json")
    metrics.record_timing(path, {"code": "A-1", "size": 100 * 1048576,
                                 "download": 10, "organize": 1, "upload": 5, "total": 16})
    metrics.record_timing(path, {"code": "A-2", "size": 100 * 1048576,
                                 "download": 20, "organize": 1, "upload": 5, "total": 26})

    snap = metrics.timing_snapshot(path)
    assert snap["phases"]["download"]["count"] == 2
    assert snap["phases"]["download"]["avg"] == 15.0
    assert snap["phases"]["download"]["max"] == 20.0
    assert snap["phases"]["upload"]["avg"] == 5.0
    assert snap["phases"]["total"]["avg"] == 21.0


def test_timing_throughput_is_mbps(tmp_path):
    from app import metrics
    path = str(tmp_path / "m.json")
    # 100MB 传 5 秒 = 20MB/s
    metrics.record_timing(path, {"code": "B-1", "size": 100 * 1048576, "upload": 5, "total": 6})
    assert metrics.timing_snapshot(path)["upload_mbps"] == 20.0


def test_timing_skips_zero_phases(tmp_path):
    """失败的下载没有 upload 阶段，不能污染上传平均耗时。"""
    from app import metrics
    path = str(tmp_path / "m.json")
    metrics.record_timing(path, {"code": "C-1", "download": 3, "outcome": "download_fail"})
    snap = metrics.timing_snapshot(path)
    assert "upload" not in snap["phases"]
    assert snap["phases"]["download"]["count"] == 1


def test_timing_recent_is_capped(tmp_path):
    from app import metrics
    path = str(tmp_path / "m.json")
    for i in range(80):
        metrics.record_timing(path, {"code": f"X-{i}", "download": 1, "total": 1})
    assert len(metrics.timing_snapshot(path)["recent"]) == 20   # 快照取前 20
    raw = metrics._load(path)["recent_timings"]
    assert len(raw) == 50, "明细最多存 50 条，避免文件无限增长"


def test_timing_survives_corrupt_file(tmp_path):
    from app import metrics
    path = tmp_path / "m.json"
    path.write_text("{not json")
    metrics.record_timing(str(path), {"code": "D-1", "download": 2, "total": 2})
    assert metrics.timing_snapshot(str(path))["phases"]["download"]["count"] == 1

def test_classify_download_failures():
    assert metrics.classify("❌ 迅雷任务失败：任务超时") == "download_fail"
    assert metrics.classify("❌ 下载失败：迅雷把文件下到了 /mnt/x，不在 TMP_ROOT=/downloads 下") == "download_fail"
