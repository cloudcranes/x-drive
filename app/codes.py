"""番号识别：从消息文本或文件名里提取作品编号。

支持：
  SSIS-123 / ssis_123 / SSIS 123   （通用 字母-数字）
  FC2-PPV-1234567 / fc2ppv1234567
  HEYZO-1234
排除分辨率、编码等常见干扰项（1080、x264、WEB-DL …）。
"""
from __future__ import annotations

import re

FC2_RE = re.compile(r"\bFC2[-_ ]?PPV[-_ ]?(\d{6,8})\b", re.I)
HEYZO_RE = re.compile(r"\bHEYZO[-_ ]?(\d{3,5})\b", re.I)
GENERIC_RE = re.compile(r"\b([A-Za-z]{2,6})[-_ ]?(\d{2,5})\b")

BLOCK_PREFIX = {
    "WEB", "WEBDL", "WEBRIP", "BD", "BDRIP", "BRRIP", "HDRIP", "DVDRIP", "BLURAY",
    "REMUX", "X264", "X265", "H264", "H265", "AAC", "AC3", "DTS", "HEVC", "AVC",
    "MP4", "MKV", "AVI", "FHD", "HD", "SD", "UHD",
}
BLOCK_NUMBER = {"1080", "720", "480", "2160", "264", "265", "30", "60", "24", "25", "50"}

URL_RE = re.compile(r"(?:magnet:\?[^\s\"'<>]+|https?://[^\s\"'<>]+)", re.I)


def strip_urls(text: str) -> str:
    return URL_RE.sub(" ", text or "")


def normalize(code: str) -> str:
    """统一成 ABC-123 / FC2-PPV-1234567 这种形式。"""
    code = code.strip().upper().replace("_", "-").replace(" ", "-")
    code = re.sub(r"-+", "-", code)
    m = re.match(r"^FC2-?PPV-?(\d+)$", code)
    if m:
        return f"FC2-PPV-{m.group(1)}"
    m = re.match(r"^HEYZO-?(\d+)$", code)
    if m:
        return f"HEYZO-{m.group(1)}"
    return code


def detect(text: str) -> str | None:
    """从文本里找番号；优先 FC2、HEYZO，其次通用格式。"""
    if not text:
        return None
    # 先看原文（文件名里的番号常在 URL 之后），再看剥离 URL 的文本
    for source in (strip_urls(text), text):
        m = FC2_RE.search(source)
        if m:
            return normalize(f"FC2-PPV-{m.group(1)}")
        m = HEYZO_RE.search(source)
        if m:
            return normalize(f"HEYZO-{m.group(1)}")
        for match in GENERIC_RE.finditer(source):
            prefix, number = match.group(1).upper(), match.group(2)
            if prefix in BLOCK_PREFIX or prefix.isdigit():
                continue
            if number in BLOCK_NUMBER or len(number) < 2:
                continue
            return normalize(f"{prefix}-{number}")
    return None


def safe_folder(code: str) -> str:
    """番号用作目录名：只留字母数字和连字符。"""
    cleaned = re.sub(r"[^0-9A-Za-z-]+", "-", code or "").strip("-")
    return cleaned or "未分类"
