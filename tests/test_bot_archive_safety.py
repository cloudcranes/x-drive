"""Bot 归档安全：云添加落地后，记账失败不许把云盘上的东西清掉。

2026-10-25 取消本地下载后，bot 与网页走同一条云添加链路：磁力投给迅雷云盘
→ 落地 → 远端整理 → 记账 → 清源目录。

原来这里测的是「本地下载目录与工作区不许被误删」（缺陷：`uploaded =
scraped["files"]` 把成功标志覆盖成文件清单，记账抛异常后 finally 仍看到
uploaded 为真 → 把原件与工作区一并删掉）。现在对应的风险变成：

  · **记账失败**时，云盘上的归档目录已经建好了 —— 那不是「本地残留」，
    那是已经上传成功的成果，不许因为记账步骤炸了就被清掉；
  · **成功**时，源目录（收件目录）要清掉，广告与种子名目录不留。

全部离线：落地、远端整理、远端删除都是假的。
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import cloudadd, cloudorganize, core, main, metadata, storage

CODEC = "SSIS-123"
VIDEO_BYTES = 1 << 20
BOT_MAGNET = "SSIS-123 magnet:?xt=urn:btih:" + "ab" * 20

SCRAPED = {"code": CODEC, "title": "测试标题", "original_title": "原题",
           "summary": "简介", "release_date": "2026-01-02", "runtime": 120,
           "actors": ["甲"], "genres": ["剧情"], "maker": "片商",
           "cover_url": "http://x/cover.jpg", "provider": "MetaTube",
           "fetched_at": "2026-01-03T00:00:00"}


class _Landing:
    """一次落地：正片 + 一个广告（广告要被清掉，正片要留下）。"""
    folder = "SEED-1"
    total = VIDEO_BYTES
    files = [{"name": "SSIS-123.mp4", "size": VIDEO_BYTES},
             {"name": "ad.txt", "size": 12}]


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    """断网：刮削 / 元数据查询都只返回空，封面取图函数是假的。"""
    async def no_scrape_bot(*a, **k):
        return {}

    async def no_lookup(*a, **k):
        return None

    monkeypatch.setattr(main.metatube, "configured", lambda: False)
    monkeypatch.setattr(main.metatube, "scrape", no_scrape_bot)
    monkeypatch.setattr(main.meta, "lookup", no_lookup)
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
        **over,
    )


def stub_bot(monkeypatch, calls):
    """假云添加：落地、远端整理、远端删除都是假的。"""
    monkeypatch.setattr(cloudadd, "ready", lambda get: (True, "ok"))
    monkeypatch.setattr(cloudadd, "destinations", lambda get: ("inbox", "dest"))
    monkeypatch.setattr(cloudadd, "task_inbox", lambda inbox, key: f"{inbox}/{key}")
    monkeypatch.setattr(storage, "kind", lambda: "clouddrive2")

    backend = SimpleNamespace(
        kind="clouddrive2", ensure_dir=lambda rel: rel, list_dirs=lambda rel: [],
        list_files=lambda rel: [], move_file=lambda *a: None,
        purge=lambda rel: calls["purged"].append(rel) or "已删除",
        link=lambda rel: "", offline_add=lambda *a: None,
        real_folder=lambda rel: rel.rsplit("/", 1)[-1])
    monkeypatch.setattr(storage, "get", lambda: backend)

    def fake_submit(backend_, source, my_inbox):
        return {"before": {}, "inbox": my_inbox}

    monkeypatch.setattr(cloudadd, "submit", fake_submit)

    async def fake_wait(backend_, my_inbox, before, source, **kw):
        return [_Landing()]

    monkeypatch.setattr(cloudadd, "wait_landing", fake_wait)
    monkeypatch.setattr(cloudorganize, "plan",
                        lambda source, files, folder_code="": SimpleNamespace(
                            source=source, target=folder_code,
                            move=[f"{folder_code}.mp4"], drop=["ad.txt"],
                            size=VIDEO_BYTES))
    monkeypatch.setattr(cloudorganize, "apply",
                        lambda backend_, src_root, plan, folder_code, dst_root="": {
                            "target": folder_code, "files": [f"{folder_code}.mp4"],
                            "size": VIDEO_BYTES})
    # 远端源目录里有一个广告文件待清；归档目录本身不在源目录里
    monkeypatch.setattr(cloudorganize, "cleanup_source",
                        lambda backend_, src_root, plan, dst_root="": (
                            calls["purged"].append(f"{src_root}/{plan.source}/ad.txt")
                            or "已清理已知丢弃文件，保留源目录外壳"))
    monkeypatch.setattr(main.metatube, "configured", lambda: True)

    async def fake_scrape(code, provider="", client=None):
        return dict(SCRAPED)

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


# ---------------------------------------------------------------- 1. 记账失败

def test_archive_write_failure_keeps_cloud_archive(tmp_path, monkeypatch):
    """记账抛异常：任务记 failed，但**云盘上的归档目录不能被清掉**。

    归档目录已经是上传成功的成果 —— 记账失败只说明站内记录没写进去，
    远端文件得留着等人处理，不是「残留」可以顺手删。
    """
    calls = {"purged": []}
    stub_bot(monkeypatch, calls)

    def boom(path, updates):
        raise OSError("磁盘满/状态文件不可写")

    monkeypatch.setattr(main.core, "update_state", boom)

    reply, cfg, state = run_bot(tmp_path, {})

    # ① 任务被记为失败
    assert job_row(cfg)["stage"] == "failed", job_row(cfg)
    assert "⚠️ 1 项失败" in reply, reply
    # 状态文件里也确实没有这条归档（内存镜像 state 是调用方字典，不作断言依据）
    assert CODEC not in core.load_state(cfg.state_path), "记账失败不该留下归档记录"
    # ② 远端归档目录没被当成残留删掉
    assert "dest/SSIS-123" not in calls["purged"], \
        f"记账失败却把远端归档删了：{calls['purged']}"
    # ③ 口径：这不是成功，metrics 不能记 ok
    assert outcomes(cfg) == "fail"


def test_archive_write_failure_on_archive_record(tmp_path, monkeypatch):
    """archive_record 抛异常（更靠前的一步）：同样不许删远端归档。"""
    calls = {"purged": []}
    stub_bot(monkeypatch, calls)
    real = main.metadata.archive_record

    def boom(*a, **k):
        raise ValueError("字段组装炸了")

    monkeypatch.setattr(main.metadata, "archive_record", boom)
    try:
        reply, cfg, state = run_bot(tmp_path, {})
    finally:
        monkeypatch.setattr(main.metadata, "archive_record", real)

    assert job_row(cfg)["stage"] == "failed"
    assert CODEC not in core.load_state(cfg.state_path), "记账失败不该留下归档记录"
    assert "dest/SSIS-123" not in calls["purged"], \
        f"记账失败却把远端归档删了：{calls['purged']}"
    assert outcomes(cfg) == "fail"


# ---------------------------------------------------------------- 2. 正常路径

def test_successful_archive_cleans_source_directory(tmp_path, monkeypatch):
    """正常成功：记录落盘、任务 done、源目录清掉、metrics 记 ok。"""
    calls = {"purged": []}
    stub_bot(monkeypatch, calls)

    reply, cfg, state = run_bot(tmp_path, {})

    assert "✅" in reply
    assert job_row(cfg)["stage"] == "done"
    rec = core.load_state(cfg.state_path)[CODEC]
    assert "SSIS-123.mp4" in {e["name"] for e in rec["manifest"]["files"]}
    assert rec["backend"] == "clouddrive2"
    assert rec["rel"] == "dest/SSIS-123", "按实际归档位置写，不跟全局根走"
    # 源目录（收件目录）清掉了 —— 只清已确认的丢弃项
    assert calls["purged"], "成功路径应当清源目录"
    for path in calls["purged"]:
        assert "dest/SSIS-123" not in path, "绝不能把归档目录本身清掉"
    assert outcomes(cfg) == "ok"
