"""视频兼容处理：把 High profile 的 mp4 转成 Main profile，老播放器也能播。

只在需要时转（默认 auto）：先 ffprobe 读 profile，已是 main/baseline 就直接跳过。
转码失败保留原文件，不影响入库。
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

OK_PROFILES = {"main", "baseline", "constrained baseline"}


async def _run(*args: str, timeout: float) -> tuple[int, str, str]:
    if shutil.which(args[0]) is None:
        # 镜像里缺 ffmpeg 时给出可读原因，而不是裸 FileNotFoundError
        return 127, "", f"缺少 {args[0]}（镜像未安装 ffmpeg）"
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return 124, "", "timeout"
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def video_profile(path: Path) -> str:
    rc, out, _ = await _run(
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=profile", "-of", "default=nw=1:nk=1",
        str(path), timeout=60.0,
    )
    return out.strip() if rc == 0 else ""


async def make_compatible(path: Path, mode: str = "auto") -> tuple[bool, str]:
    """返回 (是否转码, 原 profile 或错误摘要)。"""
    if mode == "off":
        return False, ""
    if shutil.which("ffprobe") is None or shutil.which("ffmpeg") is None:
        return False, "缺少 ffprobe/ffmpeg（镜像未安装 ffmpeg），已跳过转码"
    profile = await video_profile(path)
    if mode != "always" and profile.lower() in OK_PROFILES:
        return False, profile

    out = path.with_name(path.stem + ".compat.mp4")
    rc, _, err = await _run(
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(path),
        "-c:v", "libx264", "-profile:v", "main", "-level", "4.0",
        "-preset", "medium", "-crf", "18",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
        str(out), timeout=1800.0,
    )
    if rc != 0 or not out.exists() or out.stat().st_size == 0:
        out.unlink(missing_ok=True)
        return False, (err or "ffmpeg 失败").strip()[-200:]
    path.unlink()
    out.rename(path)
    return True, profile
