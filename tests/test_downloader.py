"""番号下载流程：识别、归档路径、去重、失败保留，全部离线 stub。"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import codes, core, fetcher, main, pipeline


def make_cfg(tmp_path, **over):
    return main.Config(
        token="x", allowed={1}, parse_base="http://p", domains=["x.com"],
        remote="gdrive", drive_root="X-Drive", max_mb=10, concurrency=1,
        state_path=str(tmp_path / "state.json"), tmp_root=str(tmp_path / "dl"),
        rclone_config="/dev/null",
        pending_dir=str(tmp_path / "pending"), metrics_path=str(tmp_path / "metrics.json"),
        daily_upload_gb=0, **over,
    )


class Msg:
    chat_id = 7
    def __init__(self): self.text = ""
    async def edit_text(self, t): self.text = t


@pytest.fixture(autouse=True)
def no_whatslink(monkeypatch):
    """单测不联网：元信息查询直接返回 None。"""
    async def none(client, source):
        return None
    monkeypatch.setattr(pipeline, "probe", none)


@pytest.fixture
def stub(monkeypatch, tmp_path):
    """把下载与 rclone 都换成假的：写一个 2MB 的假视频。"""
    calls = {"copy": [], "body": b"x" * (2 << 20)}

    async def fake_http(client, url, dest_dir, max_bytes):
        dest_dir.mkdir(parents=True, exist_ok=True)
        p = dest_dir / "SSIS-123.mp4"
        p.write_bytes(calls["body"])
        return p

    async def fake_rclone(cfg, *args, timeout=180.0):
        if args[0] == "lsf":
            return 0, "SSIS-123.mp4\n" if calls.get("uploaded") else "", ""
        if args[0] == "copy":
            calls["copy"].append(args[2])
            calls["uploaded"] = True
        return 0, "", ""

    async def fake_link(cfg, target):
        return "https://drive/folder/" + target.rsplit("/", 1)[-1]

    async def fake_download(aria, client, source, work, progress=None):
        await fake_http(None, source, work, 1 << 30)

    monkeypatch.setattr(fetcher, "download_http", fake_http)
    monkeypatch.setattr(pipeline, "download", fake_download)
    monkeypatch.setattr(main, "rclone", fake_rclone)
    monkeypatch.setattr(main, "folder_link", fake_link)
    return calls


def run(tmp_path, state, text, cfg=None, force=False):
    cfg = cfg or make_cfg(tmp_path)
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data={
        "cfg": cfg, "state": state, "sem": asyncio.Semaphore(1), "http": object()}))
    msg = Msg()
    reply = asyncio.run(main.deliver(ctx, text, msg, force=force))
    return reply, msg, cfg, state


def test_code_detection():
    assert codes.detect("SSIS-123 magnet:?xt=urn:btih:aa") == "SSIS-123"
    assert codes.detect("FC2-PPV-1234567") == "FC2-PPV-1234567"
    assert codes.detect("heyzo_1234.mp4") == "HEYZO-1234"
    assert codes.detect("ABCD-456 1080p x264") == "ABCD-456"
    assert codes.detect("WEB-DL 1080p") is None


def test_sources_extracts_magnet_and_http():
    got = fetcher.sources("看这个 magnet:?xt=urn:btih:aa 还有 https://a.com/b.mp4")
    assert got[0].startswith("magnet:") and got[1].startswith("https://")
    assert len(fetcher.sources("magnet:?xt=urn:btih:aa magnet:?xt=urn:btih:aa")) == 1


def test_download_and_archive_by_code(tmp_path, stub):
    reply, msg, cfg, state = run(tmp_path, {}, "SSIS-123 magnet:?xt=urn:btih:aa")
    assert "✅ SSIS-123" in reply
    assert stub["copy"] == ["gdrive:X-Drive/SSIS-123"], "按番号建一级目录"
    assert state["SSIS-123"]["code"] == "SSIS-123"
    assert state["SSIS-123"]["files"] == ["SSIS-123.mp4"], "单文件重命名为番号"
    assert state["SSIS-123"]["size"] == 2 << 20


def test_unknown_code_falls_back_to_unclassified(tmp_path, stub, monkeypatch):
    async def fake_http(client, url, dest_dir, max_bytes):
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / "video_only.mp4").write_bytes(b"x" * (2 << 20))
    monkeypatch.setattr(fetcher, "download_http", fake_http)

    async def fake_download(aria, client, source, work, progress=None):
        await fake_http(None, source, work, 1 << 30)
    monkeypatch.setattr(pipeline, "download", fake_download)

    reply, _, cfg, state = run(tmp_path, {}, "magnet:?xt=urn:btih:bb")
    key = next(iter(state))
    assert key.startswith("unclassified-"), "识别不到番号时用时间戳目录"
    assert "✅" in reply


def test_duplicate_code_skipped_unless_force(tmp_path, stub):
    state = {"SSIS-123": {"code": "SSIS-123", "path": "gdrive:X-Drive/SSIS-123",
                          "link": "https://drive/old", "files": ["SSIS-123.mp4"]}}
    reply, _, _, _ = run(tmp_path, state, "SSIS-123 magnet:?xt=urn:btih:aa")
    assert "♻️" in reply and "https://drive/old" in reply
    assert stub["copy"] == [], "已存在不应重复上传"

    reply2, _, _, _ = run(tmp_path, state, "SSIS-123 magnet:?xt=urn:btih:aa", force=True)
    assert stub["copy"], "force 时必须重新下载上传"


def test_no_source_message(tmp_path, stub):
    reply, _, _, _ = run(tmp_path, {}, "SSIS-123 帮我下")
    assert "没找到可以下载的链接" in reply


def test_failure_keeps_pending(tmp_path, stub, monkeypatch):
    async def boom(client, url, dest_dir, max_bytes):
        raise RuntimeError("下载中断")

    monkeypatch.setattr(fetcher, "download_http", boom)
    async def boom_download(aria, client, source, work, progress=None):
        raise RuntimeError("下载中断")
    monkeypatch.setattr(pipeline, "download", boom_download)
    reply, _, cfg, _ = run(tmp_path, {}, "SSIS-123 magnet:?xt=urn:btih:aa")
    assert "失败" in reply
    assert list(Path(cfg.pending_dir).glob("*.json")), "失败要留待办"


def test_quota_blocks_upload(tmp_path, stub, monkeypatch):
    import dataclasses

    async def fake_http(client, url, dest_dir, max_bytes):
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / "SSIS-123.mp4").write_bytes(b"x" * (2 << 20))
    monkeypatch.setattr(fetcher, "download_http", fake_http)

    async def fake_download(aria, client, source, work, progress=None):
        await fake_http(None, source, work, 1 << 30)
    monkeypatch.setattr(pipeline, "download", fake_download)

    cfg = dataclasses.replace(make_cfg(tmp_path), daily_upload_gb=1)
    main.add_usage(cfg, 1023 * 1024**2)   # 距 1GB 上限只剩 1MB
    reply, _, _, state = run(tmp_path, {}, "SSIS-999 magnet:?xt=urn:btih:cc", cfg=cfg)
    assert "上限" in reply
    assert stub["copy"] == [], "超限不应上传"


def test_human_size():
    from app.core import human_size
    assert human_size(0) == "—"
    assert human_size(512) == "512B"
    assert human_size(1536) == "2KB"
    assert human_size(5 * 1024**2) == "5MB"
    assert human_size(2 * 1024**3) == "2.00GB"


def test_extract_urls_dedup():
    from app.core import extract_urls
    text = "SSIS-123 magnet:?xt=urn:btih:aa https://a.com/b.mp4 magnet:?xt=urn:btih:aa"
    got = extract_urls(text)
    assert len(got) == 2 and got[0].startswith("magnet:")
