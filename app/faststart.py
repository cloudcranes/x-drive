"""MP4 faststart：把 moov 原子提到文件头，让浏览器能流式起播。

**为什么需要**：这类种子里的 MP4 是 `ftyp + mdat + moov` 的结构 —— 视频数据
在前、索引（moov）在**最末尾**。浏览器只认 `ftyp + moov + mdat`：它先读文件
头找 moov，找不到就一直等（readyState 停在 0），于是「播放」按钮点了没反应。

ffprobe 之类能播的工具会去文件末尾找 moov，所以看不出问题 —— 浏览器不会。

实测（2026-10-25，五个 MIMK 归档全部如此）：moov 在偏移 5,630,381,630，
大小 10,326,580 字节，正好结束于文件末尾。

**怎么做**：用 ffmpeg `-c copy -movflags +faststart` 重排 —— 视频不重编码，
只是把索引挪到前面。实测 60 秒的片段 8.6 秒转完，产物头部就有 moov。

代价：整片 5GB 要下载一遍再上传一遍（约 12 分钟 + 5GB 空间）。
所以这是**按需**做的：只在用户点「修复可播性」时才跑，不是归档时的默认动作。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("x-drive.faststart")

# ffmpeg 的可执行名（宿主机与容器里都叫 ffmpeg）
FFMPEG = os.getenv("FFMPEG_BIN", "ffmpeg")
# 一次转封装的最长时限：整片 5GB 实测约 12 分钟，留足余量
TRANSCODE_TIMEOUT = float(os.getenv("FASTSTART_TIMEOUT", "3600") or 3600)
# 探测头部时读多少字节：moov 在头时够用，不在时一眼看出
PROBE_BYTES = 1 << 20
# moov 特征：'moov' 四个字节
MOOV_MAGIC = b"moov"


class FaststartError(RuntimeError):
    """faststart 处理失败（原因要说清楚，界面直接显示给用户）。"""


def ffmpeg_available() -> tuple[bool, str]:
    """ffmpeg 能不能用。返回 (可用, 说明)。"""
    exe = shutil.which(FFMPEG) or FFMPEG
    try:
        r = subprocess.run([exe, "-version"], capture_output=True, timeout=20)
    except FileNotFoundError:
        return False, f"找不到 {FFMPEG}"
    except Exception as exc:
        return False, f"调用 {FFMPEG} 失败：{exc}"
    if r.returncode != 0:
        return False, f"{FFMPEG} 不可用：{(r.stderr or b'').decode(errors='replace')[:120]}"
    return True, "可用"


def has_moov_at_head(path: Path) -> bool:
    """文件头（1MB 内）有没有 moov —— 有就是 faststart 的，能直接播。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(PROBE_BYTES)
    except OSError:
        return False
    return MOOV_MAGIC in head


def needs_faststart(path: Path) -> bool:
    """这个文件需要重排吗（moov 不在头部）。"""
    return not has_moov_at_head(path)


def faststart_file(src: Path, work_dir: Path | None = None) -> Path:
    """把 src 重排成 moov 前置的 MP4，返回新文件的路径。

    原文件不动 —— 重排是**另存**，调用方决定要不要用新文件替换。
    视频不重编码（`-c copy`），只是挪索引。
    """
    src = Path(src)
    if not src.is_file():
        raise FaststartError(f"文件不存在：{src}")
    if not has_moov_at_head(src):
        raise FaststartError("这个文件的 moov 已经在头部，不需要重排")
    exe = shutil.which(FFMPEG) or FFMPEG
    work = Path(work_dir or tempfile.gettempdir())
    work.mkdir(parents=True, exist_ok=True)
    # 先写临时名，成功后再改名：中途失败不会留个半截文件顶着真名
    tmp = work / (src.stem + ".faststart" + src.suffix)
    try:
        if tmp.exists():
            tmp.unlink()
        r = subprocess.run(
            [exe, "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(src), "-c", "copy", "-movflags", "+faststart", str(tmp)],
            capture_output=True, timeout=TRANSCODE_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise FaststartError(f"重排超时（超过 {int(TRANSCODE_TIMEOUT)} 秒）") from None
    except FileNotFoundError:
        raise FaststartError(f"找不到 {FFMPEG}") from None
    except Exception as exc:
        raise FaststartError(f"重排失败：{exc}") from None
    if r.returncode != 0:
        err = (r.stderr or b"").decode(errors="replace").strip()
        raise FaststartError(f"重排失败：{err[:200] or '未知原因'}")
    if not tmp.is_file() or tmp.stat().st_size == 0:
        raise FaststartError("重排没有产出文件")
    if not has_moov_at_head(tmp):
        raise FaststartError("重排后的文件仍然没有前置的 moov")
    return tmp
