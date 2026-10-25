"""faststart：把 MP4 的 moov 原子提到文件头，浏览器才认得了。

这类种子里的 MP4 是 `ftyp + mdat + moov` —— 索引在**最末尾**。浏览器只认
`ftyp + moov + mdat`，找不到就一直在等（readyState 停在 0），于是「播放」点了
没反应。ffprobe 能播是因为它会去末尾找 moov，浏览器不会。

测试用真的 ffmpeg 造两种文件：
  · moov 在**头部**（faststart 好的）—— 不该重排
  · moov 在**末尾**（这类种子的常态）—— 该重排，且重排后头部就有 moov
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app import faststart


def _ffmpeg_works() -> bool:
    """ffmpeg 能不能真的跑起来（不只是存在，还要不缺共享库）。"""
    exe = shutil.which("ffmpeg")
    if not exe:
        return False
    try:
        r = subprocess.run([exe, "-version"], capture_output=True, timeout=20)
    except Exception:
        return False
    return r.returncode == 0


needs_ffmpeg = pytest.mark.skipif(
    not _ffmpeg_works(),
    reason="需要可用的 ffmpeg（宿主机上可能缺 libOpenCL）")


def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def _make_sample(tmp_path: Path, seconds: int = 1) -> Path:
    """造一段小视频（testsrc + 静音音轨）。"""
    src = tmp_path / "sample.mp4"
    r = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc=size=160x90:rate=10:duration={seconds}",
         "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono:d={seconds}",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
         str(src)],
        capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr
    return src


def _make_sample_tail_moov(tmp_path: Path, seconds: int = 1) -> Path:
    """造一段 **moov 在末尾**的文件（不写 +faststart 就是这样）。"""
    src = tmp_path / "tail.mp4"
    r = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc=size=160x90:rate=10:duration={seconds}",
         "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono:d={seconds}",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
         str(src)],
        capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr
    # 去掉 faststart 标志：moov 落到文件末尾
    r2 = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(src), "-c", "copy", "-movflags", "-faststart", str(src)],
        capture_output=True, text=True, timeout=180)
    assert r2.returncode == 0, r2.stderr
    return src


@needs_ffmpeg
def test_ffmpeg_available_reports_reason():
    ok, why = faststart.ffmpeg_available()
    assert ok, why
    assert why


@needs_ffmpeg
def test_head_moov_file_needs_no_reorder(tmp_path):
    """faststart 好的文件：头部就有 moov，不用重排。"""
    src = _make_sample(tmp_path)
    assert faststart.has_moov_at_head(src) is True
    assert faststart.needs_faststart(src) is False


@needs_ffmpeg
def test_tail_moov_file_is_detected(tmp_path):
    """moov 在末尾的文件要被认出来 —— 这是这类种子的常态。"""
    src = _make_sample_tail_moov(tmp_path)
    assert faststart.has_moov_at_head(src) is False
    assert faststart.needs_faststart(src) is True


@needs_ffmpeg
def test_faststart_moves_moov_to_head(tmp_path):
    """重排后 moov 到了头部，浏览器就能起播了。"""
    src = _make_sample_tail_moov(tmp_path)
    assert faststart.needs_faststart(src) is True

    out = faststart.faststart_file(src, tmp_path)

    assert out.is_file()
    assert faststart.has_moov_at_head(out), "重排后 moov 必须在头部"
    # 视频不重编码：大小应当接近（只挪了索引）
    ratio = out.stat().st_size / src.stat().st_size
    assert 0.9 < ratio < 1.15, f"大小变化不该这么大：{ratio}"


@needs_ffmpeg
def test_faststart_refuses_file_already_fast(tmp_path):
    """已经是 faststart 的文件：直接拒绝，不做无意义的重排。"""
    src = _make_sample(tmp_path)
    with pytest.raises(faststart.FaststartError, match="不需要重排"):
        faststart.faststart_file(src, tmp_path)


def test_faststart_reports_missing_file(tmp_path):
    with pytest.raises(faststart.FaststartError, match="不存在"):
        faststart.faststart_file(tmp_path / "nope.mp4", tmp_path)


@needs_ffmpeg
def test_faststart_output_is_playable(tmp_path):
    """重排后的文件 ffprobe 仍能解析（duration / 轨道都在）。"""
    import json
    src = _make_sample_tail_moov(tmp_path, seconds=2)
    out = faststart.faststart_file(src, tmp_path)
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(out)],
        capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert float(d["format"]["duration"]) > 0
    kinds = {s["codec_type"] for s in d.get("streams", [])}
    assert "video" in kinds


# ─────────────────────────────────────────── 不依赖 ffmpeg 的核心判定

def _fake_mp4(path: Path, moov_at_head: bool) -> Path:
    """造一个「像 MP4」的文件：ftyp + 若干 mdat + moov（位置可调）。

    只用字节拼 —— 判定 moov 位置这件事不需要真视频。
    mdat 撑得比探测窗口（1MB）大，尾部 moov 才真的在窗口外。
    """
    ftyp = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2avc1mp41"
    mdat = b"\x00\x00\x10\x00mdat" + b"d" * (2 * 1024 * 1024)
    moov = b"\x00\x00\x00\x10moov" + b"m" * 16
    if moov_at_head:
        data = ftyp + moov + mdat
    else:
        data = ftyp + mdat + moov
    path.write_bytes(data)
    return path


def test_moov_detection_is_about_position_not_file_size(tmp_path):
    """判定只看 moov 在不在头部，与文件多大无关。"""
    head = _fake_mp4(tmp_path / "head.mp4", moov_at_head=True)
    tail = _fake_mp4(tmp_path / "tail.mp4", moov_at_head=False)
    assert faststart.has_moov_at_head(head) is True
    assert faststart.needs_faststart(head) is False
    assert faststart.has_moov_at_head(tail) is False
    assert faststart.needs_faststart(tail) is True


def test_missing_file_is_not_treated_as_empty(tmp_path):
    """读不到文件时不能当成「头部有 moov」—— 那会骗过「不需要重排」的判断。"""
    assert faststart.has_moov_at_head(tmp_path / "nope.mp4") is False
    assert faststart.needs_faststart(tmp_path / "nope.mp4") is True


def test_ffmpeg_available_reports_missing_binary(monkeypatch):
    """ffmpeg 不可用时要说清原因 —— 界面直接显示这句话。"""
    monkeypatch.setattr(faststart.shutil, "which", lambda name: None)
    monkeypatch.setattr(faststart, "FFMPEG", "definitely-not-ffmpeg")
    ok, why = faststart.ffmpeg_available()
    assert ok is False
    assert "definitely-not-ffmpeg" in why


# ─────────────────────────────────────────── 判定方向（纯字节，不依赖 ffmpeg）

def test_faststart_file_accepts_tail_moov_and_refuses_head_moov(tmp_path, monkeypatch):
    """重排的入口判定方向不能反。

    这里曾经写成「没有 moov 就报错」—— 于是**所有真正需要重排的文件**
    （moov 在末尾）都在入口被拒，任务报「这个文件的 moov 已经在头部」，
    驴唇不对马嘴，而且线上真跑一次要浪费 9 分钟下载才发现。

    这条测试不依赖 ffmpeg：入口判定发生在调用 ffmpeg 之前。
    """
    head = _fake_mp4(tmp_path / "head.mp4", moov_at_head=True)
    tail = _fake_mp4(tmp_path / "tail.mp4", moov_at_head=False)

    called = []

    def fake_run(*a, **k):
        called.append(a)
        raise RuntimeError("不该走到 ffmpeg")

    monkeypatch.setattr(faststart.subprocess, "run", fake_run)

    # 头部有 moov → 明确拒绝（本来就能播，不必重排）
    with pytest.raises(faststart.FaststartError, match="不需要重排"):
        faststart.faststart_file(head, tmp_path)
    assert called == [], "已经是 faststart 的文件不该去调 ffmpeg"

    # 尾部有 moov → **要**继续走到 ffmpeg（不该被入口拒掉）
    with pytest.raises(RuntimeError):
        faststart.faststart_file(tail, tmp_path)
    assert called, "真正需要重排的文件必须走到 ffmpeg"


def test_faststart_output_name_keeps_mp4_extension(tmp_path, monkeypatch):
    """临时产物的名字必须以 .mp4 结尾 —— ffmpeg 靠扩展名判断输出格式。

    线上真跑踩过：下载件叫 `MIMK-188.mp4.part`，临时名就变成
    `MIMK-188.mp4.faststart.part` —— ffmpeg 认不出这是什么格式，报
    「Unable to choose an output format」，5GB 白下了 9 分钟。
    """
    src = _fake_mp4(tmp_path / "MIMK-188.mp4.part", moov_at_head=False)
    seen = {}

    class FakeProc:
        returncode = 0
        stderr = b""

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        out = Path(cmd[-1])
        out.write_bytes(b"\x00\x00\x00\x10moov" + b"m" * 16)   # 假装产物有 moov
        return FakeProc()

    monkeypatch.setattr(faststart.subprocess, "run", fake_run)
    faststart.faststart_file(src, tmp_path)

    cmd = seen["cmd"]
    assert str(cmd[-1]).endswith(".mp4"), f"输出名要以 .mp4 结尾：{cmd[-1]}"
    assert "-f" in cmd and "mp4" in cmd, "还要显式指定输出格式，不靠扩展名猜"
