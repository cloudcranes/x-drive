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


def test_usage_quota(tmp_path):
    from app import main

    cfg = main.Config(
        token="x", allowed={1}, parse_base="http://p", domains=["x.com"],
        remote="gdrive", drive_root="X", max_mb=10, concurrency=1,
        state_path=str(tmp_path / "s.json"), tmp_root=str(tmp_path / "dl"),
        rclone_config="/dev/null", transcode="off",
        pending_dir=str(tmp_path / "data" / "pending"), daily_upload_gb=1,
    )
    assert main.today_bytes(cfg) == 0
    main.add_usage(cfg, 600 * 1024**2)
    assert main.today_bytes(cfg) == 600 * 1024**2
    assert main.quota_exceeded(cfg, 300 * 1024**2) is False   # 900MB < 1GB
    assert main.quota_exceeded(cfg, 500 * 1024**2) is True    # 1.1GB > 1GB


def test_usage_resets_next_day(tmp_path):
    from app import main

    cfg = main.Config(
        token="x", allowed={1}, parse_base="http://p", domains=["x.com"],
        remote="gdrive", drive_root="X", max_mb=10, concurrency=1,
        state_path=str(tmp_path / "s.json"), tmp_root=str(tmp_path / "dl"),
        rclone_config="/dev/null", transcode="off",
        pending_dir=str(tmp_path / "data" / "pending"), daily_upload_gb=1,
    )
    path = main.usage_file(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"date": "2000-01-01", "bytes": 10**12}))
    assert main.today_bytes(cfg) == 0, "跨天必须清零"
