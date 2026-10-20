"""编排层测试：用 stub 替掉网络与 rclone，验证下载 → 上传 → 去重 → 回执。"""
import asyncio
import re
from types import SimpleNamespace

import pytest

from app import core, main


class FakeMessage:
    def __init__(self, chat_id=1):
        self.text = ""
        self.chat_id = chat_id

    async def edit_text(self, text):
        self.text = text


def make_ctx(tmp_path, state):
    cfg = main.Config(
        token="x",
        allowed={1},
        parse_base="http://parse:8000",
        domains=["x.com"],
        remote="gdrive",
        drive_root="X-Drive",
        max_mb=1,
        concurrency=1,
        state_path=str(tmp_path / "state.json"),
        tmp_root=str(tmp_path / "dl"),
        rclone_config=str(tmp_path / "rclone.conf"),
        pending_dir=str(tmp_path / "pending"),
        transcode="off",
    )
    app = SimpleNamespace(
        bot_data={"cfg": cfg, "http": object(), "state": state, "sem": asyncio.Semaphore(1)}
    )
    return SimpleNamespace(application=app, bot_data=app.bot_data)


def run(ctx, url, status=None):
    return asyncio.run(main.deliver(ctx, url, status or FakeMessage()))


@pytest.fixture(autouse=True)
def stub_network(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "expand_short", lambda client, url: asyncio.sleep(0, result=url))
    calls = []

    async def fake_download(client, url, dest, max_bytes):
        if "broken" in url:
            raise RuntimeError("下载 403")
        dest.write_bytes(b"x")
        return 1

    async def fake_rclone(cfg, *args, timeout=180.0):
        calls.append(args)
        if args[0] == "lsf":
            return 0, "01.jpg\n" if fake_rclone.files_present else "", ""
        return 0, "", ""

    fake_rclone.files_present = True
    monkeypatch.setattr(main, "download", fake_download)
    monkeypatch.setattr(main, "rclone", fake_rclone)
    return calls


def test_deliver_saves_all_media(tmp_path, stub_network, monkeypatch):
    async def fake_parse(client, cfg, url):
        return {
            "video_url": "https://video.twimg.com/v.mp4",
            "images": [{"url": "https://pbs.twimg.com/media/a.jpg?name=small"}],
            "title": "标题",
            "author": {"uid": "42", "name": "Elon Musk"},
        }

    monkeypatch.setattr(main, "parse_link", fake_parse)
    monkeypatch.setattr(main, "folder_link", lambda cfg, target: asyncio.sleep(0, result="https://drive/folder"))

    state = {}
    text = run(make_ctx(tmp_path, state), "https://x.com/u/status/123", FakeMessage())

    assert "1 图" in text and "1 视频" in text and "https://drive/folder" in text
    assert re.fullmatch(r"gdrive:X-Drive/Elon_Musk/\d{4}-\d{2}/123", state["123"]["path"])
    files = state["123"]["files"]
    assert len(files) == 2
    assert re.fullmatch(r"Elon_Musk_\d{4}-\d{2}-\d{2}_01\.mp4", files[0])
    assert re.fullmatch(r"Elon_Musk_\d{4}-\d{2}-\d{2}_02\.jpg", files[1])
    assert any(c[0] == "copy" for c in stub_network)
    assert not (tmp_path / "dl" / "123").exists(), "临时目录应清理"


def test_deliver_reuses_cache(tmp_path, stub_network, monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("命中缓存不应再解析")

    monkeypatch.setattr(main, "parse_link", boom)
    state = {"123": {"path": "gdrive:X-Drive/a/2026-10/123", "link": "https://drive/old", "files": ["01.jpg"], "title": "t"}}
    text = run(make_ctx(tmp_path, state), "https://x.com/u/status/123")

    assert "https://drive/old" in text
    assert not any(c[0] == "copy" for c in stub_network)


def test_deliver_empty_media(tmp_path, stub_network, monkeypatch):
    monkeypatch.setattr(main, "parse_link", lambda *a, **k: asyncio.sleep(0, result={"video_url": "", "images": []}))
    text = run(make_ctx(tmp_path, {}), "https://x.com/u/status/9")
    assert "没有可下载" in text


def test_deliver_partial_failure(tmp_path, stub_network, monkeypatch):
    async def fake_parse(client, cfg, url):
        return {
            "video_url": "",
            "images": [
                {"url": "https://pbs.twimg.com/media/broken.jpg"},
                {"url": "https://pbs.twimg.com/media/ok.jpg"},
            ],
        }

    monkeypatch.setattr(main, "parse_link", fake_parse)
    monkeypatch.setattr(main, "folder_link", lambda cfg, target: asyncio.sleep(0, result=None))
    state = {}
    text = run(make_ctx(tmp_path, state), "https://x.com/u/status/77")

    assert "1 图" in text and "未入库" in text
    assert len(state["77"]["files"]) == 1 and state["77"]["files"][0].endswith("_02.jpg")


def test_pending_written_on_partial_and_cleared_on_success(tmp_path, stub_network, monkeypatch):
    """部分失败要留待办；全部成功要清掉待办。"""
    async def fake_parse(client, cfg, url):
        return {"video_url": "", "images": [
            {"url": "https://pbs.twimg.com/media/broken.jpg"},
            {"url": "https://pbs.twimg.com/media/ok.jpg"},
        ]}

    monkeypatch.setattr(main, "parse_link", fake_parse)
    monkeypatch.setattr(main, "folder_link", lambda cfg, target: asyncio.sleep(0, result="https://d/x"))
    ctx = make_ctx(tmp_path, {})
    cfg = ctx.bot_data["cfg"]
    run(ctx, "https://x.com/u/status/77")

    from pathlib import Path
    pending = list(Path(cfg.pending_dir).glob("*.json"))
    assert pending, "部分失败必须留下待办，供 /retry 补抓"
    import json as _json
    rec = _json.loads(pending[0].read_text())
    assert rec["stage"] == "partial" and rec["failures"]


def test_force_reruns_when_failures_recorded(tmp_path, stub_network, monkeypatch):
    """有失败项时 force 会重新解析并重跑。"""
    calls = []

    async def fake_parse(client, cfg, url):
        calls.append(url)
        return {"video_url": "", "images": [{"url": "https://pbs.twimg.com/media/ok.jpg"}]}

    monkeypatch.setattr(main, "parse_link", fake_parse)
    monkeypatch.setattr(main, "folder_link", lambda cfg, target: asyncio.sleep(0, result="https://d/y"))
    state = {"123": {"path": "gdrive:X-Drive/a/2026-10/123", "link": "https://old",
                     "files": ["a.jpg"], "title": "t", "failures": ["01.jpg: 下载 403"]}}
    text = asyncio.run(main.deliver(make_ctx(tmp_path, state), "https://x.com/u/status/123",
                                    FakeMessage(), force=True))
    assert calls, "有失败项时必须重新解析"
    assert "https://d/y" in text


def test_retry_skips_completed_record(tmp_path, stub_network, monkeypatch):
    """已完整入库的记录，/retry 不应重复上传。"""
    async def boom(*a, **k):
        raise AssertionError("已完成的记录不该再解析")

    monkeypatch.setattr(main, "parse_link", boom)
    state = {"123": {"path": "gdrive:X-Drive/a/2026-10/123", "link": "https://drive/ok",
                     "files": ["a.jpg"], "title": "t", "failures": []}}
    msg = FakeMessage()
    text = asyncio.run(main.deliver(make_ctx(tmp_path, state), "https://x.com/u/status/123",
                                    msg, force=True))
    assert "https://drive/ok" in text
    assert "无需重试" in msg.text, "应通过进度消息说明跳过原因"
