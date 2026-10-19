"""视频兼容转码的编排测试：全部 stub 掉子进程。"""
import asyncio
from pathlib import Path

import pytest

from app import media


@pytest.fixture
def video(tmp_path):
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"x" * 1024)
    return p


def test_auto_skips_main_profile(video, monkeypatch):
    calls = []

    async def fake_run(*args, timeout):
        calls.append(args)
        return 0, "Main\n", ""

    monkeypatch.setattr(media, "_run", fake_run)
    changed, info = asyncio.run(media.make_compatible(video, "auto"))
    assert changed is False and info == "Main"
    assert len(calls) == 1 and calls[0][0] == "ffprobe"


def test_auto_transcodes_high_profile(video, monkeypatch):
    calls = []

    async def fake_run(*args, timeout):
        calls.append(args)
        if args[0] == "ffprobe":
            return 0, "High\n", ""
        Path(args[-1]).write_bytes(b"y" * 2048)
        return 0, "", ""

    monkeypatch.setattr(media, "_run", fake_run)
    changed, info = asyncio.run(media.make_compatible(video, "auto"))
    assert changed is True and info == "High"
    assert video.read_bytes() == b"y" * 2048
    assert not (video.parent / "clip.compat.mp4").exists()
    ffmpeg = calls[1]
    assert ffmpeg[0] == "ffmpeg" and "-profile:v" in ffmpeg and "main" in ffmpeg


def test_always_transcodes_even_if_main(video, monkeypatch):
    async def fake_run(*args, timeout):
        if args[0] == "ffprobe":
            return 0, "Main\n", ""
        Path(args[-1]).write_bytes(b"z")
        return 0, "", ""

    monkeypatch.setattr(media, "_run", fake_run)
    changed, _ = asyncio.run(media.make_compatible(video, "always"))
    assert changed is True


def test_off_does_nothing(video, monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("off 模式不应调用 ffmpeg")

    monkeypatch.setattr(media, "_run", boom)
    assert asyncio.run(media.make_compatible(video, "off")) == (False, "")


def test_failure_keeps_original(video, monkeypatch):
    async def fake_run(*args, timeout):
        if args[0] == "ffprobe":
            return 0, "High\n", ""
        return 1, "", "编码器炸了"

    monkeypatch.setattr(media, "_run", fake_run)
    changed, err = asyncio.run(media.make_compatible(video, "auto"))
    assert changed is False and "编码器炸了" in err
    assert video.read_bytes() == b"x" * 1024
    assert not (video.parent / "clip.compat.mp4").exists()
