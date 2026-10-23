"""E5 回归：上传成功但「归档记账」失败时，任务记 failed 且原件/工作区必须留下。

缺陷：app/main.py 里 `uploaded = scraped["files"]` 把成功标志覆盖成文件清单（真值），
随后记账任一步抛异常 → 走 except 记 failed，finally 仍看到 uploaded 为真 →
把下载原件与工作区一并删掉，失败任务无从续传排错。

全部离线：假下载 / 假整理 / 假 rclone / 假链接，只造几 KB 级的假文件。
这里重建一份与 test_archive_flow.py 等价的桩（含下载目录的归属标记），
不改动那个文件。
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import core, main, metadata, workspace

CODEC = "SSIS-123"
BOT_VIDEO_BYTES = 1 << 20          # 1MB：满足 media_files 的最小体积
BOT_MAGNET = "SSIS-123 magnet:?xt=urn:btih:" + "ab" * 20

SCRAPED = {"code": CODEC, "title": "测试标题", "original_title": "原题",
           "summary": "简介", "release_date": "2026-01-02", "runtime": 120,
           "actors": ["甲"], "genres": ["剧情"], "maker": "片商",
           "cover_url": "http://x/cover.jpg", "provider": "MetaTube",
           "fetched_at": "2026-01-03T00:00:00"}


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    """断网：刮削 / 元数据查询都只返回空，封面取图函数是假的。"""
    async def no_scrape_bot(*a, **k):
        return {}

    async def no_lookup(*a, **k):
        return None

    async def no_probe(client, source):
        return None

    monkeypatch.setattr(main.metatube, "configured", lambda: False)
    monkeypatch.setattr(main.metatube, "scrape", no_scrape_bot)
    monkeypatch.setattr(main.meta, "lookup", no_lookup)
    monkeypatch.setattr(main.pipeline, "probe", no_probe)
    monkeypatch.setattr(main, "_STORE", None)          # 每个用例用自己 tmp_path 里的任务库
    monkeypatch.setattr(metadata, "fetch_image", lambda url, **k: b"")
    return tmp_path


def bot_cfg(tmp_path, **over):
    return main.Config(
        token="x", allowed={1}, remote="gdrive", drive_root="X-Drive", max_mb=10,
        concurrency=1, state_path=str(tmp_path / "state.json"),
        tmp_root=str(tmp_path / "dl"), rclone_config="/dev/null",
        pending_dir=str(tmp_path / "pending"),
        metrics_path=str(tmp_path / "metrics.json"),
        state_dir=str(tmp_path / "work"), jobs_db=str(tmp_path / "jobs.db"),
        daily_upload_gb=0, **over,
    )


def stub_bot(monkeypatch, calls):
    """假下载（建带归属标记的下载目录）/ 假整理 / 假 rclone / 假链接。"""

    async def fake_download(xl, client, source, dest, progress=None, **opts):
        # 真实下载由 fetcher 建目录并打归属标记；drop() 只认带标记的目录，
        # 少了这一步「成功路径应清理」的断言会变成空断言。
        main.workspace.claim(dest, workspace.key_of(CODEC), source, task_id="T")
        (dest / "raw.mp4").write_bytes(b"\x00" * BOT_VIDEO_BYTES)
        return {"task": "T", "dir": dest, "waited": 0.0, "reused": False}

    def fake_stage(source_dir, work, key):
        work.mkdir(parents=True, exist_ok=True)
        for p in source_dir.glob("*"):
            if p.is_file():
                (work / p.name).write_bytes(p.read_bytes())
        return list(work.glob("*"))

    async def fake_rclone(cfg, *args, timeout=180.0):
        if args[0] == "copy":
            calls["copied"] = sorted(p.name for p in Path(args[1]).iterdir()
                                     if p.is_file())
        return 0, "", ""

    async def fake_link(cfg, target):
        return "https://drive.example/bot"

    async def fake_scrape(code, provider="", client=None):
        return dict(SCRAPED)

    monkeypatch.setattr(main.pipeline, "download", fake_download)
    monkeypatch.setattr(main.pipeline, "stage_copy", fake_stage)
    monkeypatch.setattr(main, "rclone", fake_rclone)
    monkeypatch.setattr(main, "folder_link", fake_link)
    monkeypatch.setattr(main.metatube, "configured", lambda: True)
    monkeypatch.setattr(main.metatube, "scrape", fake_scrape)
    return calls


def run_bot(tmp_path, state):
    """asyncio.run 驱动一次 bot 投递；返回 (回复文本, cfg, 内存 state)。"""
    cfg = bot_cfg(tmp_path)
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data={
        "cfg": cfg, "state": state, "sem": asyncio.Semaphore(1), "http": object()}))
    msg = SimpleNamespace(chat_id=7, text="", edit_text=None)

    async def edit(t):
        msg.text = t
    msg.edit_text = edit
    reply = asyncio.run(main.deliver(ctx, BOT_MAGNET, msg))
    return reply, cfg, state


def job_row(cfg):
    """任务表里唯一那条任务（用于断言 stage 是 done 还是 failed）。"""
    rows = main.task_store(cfg).recent()
    assert rows, "任务表里应当有本次投递的任务"
    return rows[0]


def outcomes(cfg):
    """metrics 最近一条耗时的 outcome 字段。"""
    data = json.loads(Path(cfg.metrics_path).read_text(encoding="utf-8"))
    return data["recent_timings"][0]["outcome"]


def dirs_of(cfg):
    """本次任务的下载目录与工作区：目录键按最终归档目录名归一（SSIS-123 → SSIS123）。"""
    key = main.workspace.key_of(CODEC)
    return (main.workspace.staging(cfg.tmp_root, key),
            main.workspace.workspace(cfg.state_dir, key))


# ---------------------------------------------------------------- 1. 记账失败

def test_archive_write_failure_keeps_originals(tmp_path, monkeypatch):
    """update_state 抛异常：任务记 failed，下载目录与工作区都还在，且没留下归档记录。"""
    calls = {"copied": []}
    stub_bot(monkeypatch, calls)

    def boom(path, updates):
        raise OSError("磁盘满/状态文件不可写")

    monkeypatch.setattr(main.core, "update_state", boom)

    reply, cfg, state = run_bot(tmp_path, {})
    download_dir, work = dirs_of(cfg)

    # ① 任务被记为失败
    assert job_row(cfg)["stage"] == "failed", job_row(cfg)
    assert "⚠️ 1 项失败" in reply, reply
    # 状态文件里也确实没有这条归档（内存镜像 state 是调用方字典，不作断言依据）
    assert CODEC not in core.load_state(cfg.state_path), "记账失败不该留下归档记录"
    # ② 原件与工作区都还在（缺陷点：以前这里两个目录都被删了）
    assert download_dir.is_dir(), "记账失败却删掉了下载原件"
    assert (download_dir / "raw.mp4").is_file(), "下载原件文件不见了"
    assert work.is_dir(), "记账失败却删掉了工作区"
    # 附带确认：上传确实发生过，所以「保留」不是因为压根没上传
    assert "SSIS-123.mp4" in calls["copied"]
    # ③ 口径：这不是成功，metrics 不能记 ok
    assert outcomes(cfg) == "fail"


def test_archive_write_failure_on_archive_record(tmp_path, monkeypatch):
    """archive_record 抛异常（更靠前的一步）：同样保留原件、任务记 failed。"""
    stub_bot(monkeypatch, {"copied": []})
    real = main.metadata.archive_record

    def boom(*a, **k):
        raise ValueError("字段组装炸了")

    monkeypatch.setattr(main.metadata, "archive_record", boom)
    try:
        reply, cfg, state = run_bot(tmp_path, {})
    finally:
        monkeypatch.setattr(main.metadata, "archive_record", real)
    download_dir, work = dirs_of(cfg)

    assert job_row(cfg)["stage"] == "failed"
    assert download_dir.is_dir() and work.is_dir()
    assert CODEC not in core.load_state(cfg.state_path), "记账失败不该留下归档记录"
    assert outcomes(cfg) == "fail"


# ---------------------------------------------------------------- 2. 正常路径

def test_successful_archive_still_cleans_up(tmp_path, monkeypatch):
    """正常成功：记录落盘、任务 done、原件与工作区被清理、metrics 记 ok。"""
    calls = {"copied": []}
    stub_bot(monkeypatch, calls)

    reply, cfg, state = run_bot(tmp_path, {})
    download_dir, work = dirs_of(cfg)

    assert "✅ SSIS-123" in reply
    assert job_row(cfg)["stage"] == "done"
    rec = core.load_state(cfg.state_path)[CODEC]
    assert rec["link"] == "https://drive.example/bot"
    assert "SSIS-123.mp4" in {e["name"] for e in rec["manifest"]["files"]}
    assert not download_dir.exists(), "成功路径应当清理下载原件"
    assert not work.exists(), "成功路径应当清理工作区"
    assert outcomes(cfg) == "ok"
