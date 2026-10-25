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
# 带分隔符的强匹配：`MIMK-285` 比 `SIS001` 更可信，规范名优先用它
GENERIC_RE_STRICT = re.compile(r"\b([A-Za-z]{2,6})[-_ ](\d{2,5})\b")
# 番号后面粘连着字母后缀：`MIMK-267ch`（中文版）、`ABC-123C`。
# 这种写法以前认不出来，于是归档目录名用了磁力原名（实测 mimk-186ch），
# 站内既不是番号、也跟别的记录对不上。
# 规则：数字后面跟 1~3 个字母，且字母不是常见的画质/编码词（见 BLOCK_SUFFIX）。
#
# 注意这里不能用 `\b` 收尾：数字与字母之间**没有**词边界（`267ch` 是一个词），
# 写成 `\b` 会让整条规则永远不匹配 —— 数字后要用「非字母」或结尾来断开。
GENERIC_RE_SUFFIX = re.compile(r"\b([A-Za-z]{2,6})[-_ ](\d{2,5})([A-Za-z]{1,3})(?![A-Za-z0-9])")

BLOCK_PREFIX = {
    "WEB", "WEBDL", "WEBRIP", "BD", "BDRIP", "BRRIP", "HDRIP", "DVDRIP", "BLURAY",
    "REMUX", "X264", "X265", "H264", "H265", "AAC", "AC3", "DTS", "HEVC", "AVC",
    "MP4", "MKV", "AVI", "FHD", "HD", "SD", "UHD",
}
BLOCK_NUMBER = {"1080", "720", "480", "2160", "264", "265", "30", "60", "24", "25", "50"}
# 番号后面的字母后缀里，这些是画质/编码/版本标记，不是「中文版」那种后缀 ——
# `MIMK-267U`（多语种版）该认成 MIMK-267，但 `MIMK-267H264`（编码）不是。
# 注意 **CH 不在里面**：它就是「中文版」的意思（实测 MIMK-267ch / mimk-186ch）。
BLOCK_SUFFIX = {"H", "H264", "H265", "X", "X264", "X265", "U", "C", "JP", "EN"}

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


def canonical(code: str) -> str:
    """规范名：能识别出番号就返回规范化结果，识别不出返回空串（不动它）。

    规范 = 大写、单连字符、按类型归一：
      `ssis_123`、`SSIS 123`   → `SSIS-123`
      `fc2ppv1234567`          → `FC2-PPV-1234567`
      `MIMK-286-中文字幕`      → `MIMK-286`
      `madoubt.com MIMK-290`   → `MIMK-290`
    """
    text = code or ""
    # 粘连写法（SIS001）容易抢先被识别，先只认带分隔符的强匹配
    for match in GENERIC_RE_STRICT.finditer(strip_urls(text)):
        prefix, number = match.group(1).upper(), match.group(2)
        if prefix in BLOCK_PREFIX or number in BLOCK_NUMBER:
            continue
        return normalize(f"{prefix}-{number}")
    # 番号后面粘连着字母后缀：`MIMK-267ch`（中文版）、`ABC-123C`
    for match in GENERIC_RE_SUFFIX.finditer(strip_urls(text)):
        prefix, number, suffix = match.group(1).upper(), match.group(2), match.group(3).upper()
        if prefix in BLOCK_PREFIX or number in BLOCK_NUMBER:
            continue
        if suffix in BLOCK_SUFFIX:
            continue
        return normalize(f"{prefix}-{number}")
    found = detect(text)
    return found.upper() if found else ""

def safe_folder(code: str) -> str:
    """番号用作目录名：只留字母数字和连字符。"""
    cleaned = re.sub(r"[^0-9A-Za-z-]+", "-", code or "").strip("-")
    return cleaned or "未分类"


def pair(text: str) -> dict[str, str]:
    """把每个链接和它自己的番号配对，返回 {链接: 番号}。

    一条消息里可以有多个资源：
        SSIS-123 magnet:?xt=urn:btih:aa ABP-456 magnet:?xt=urn:btih:bb
    detect() 只返回第一个匹配，直接套给所有链接会让第二个链接被归到 SSIS-123，
    于是被判成「已存在」而根本不下载。这里改成按链接切段，各段各自识别。

    规则：番号属于它后面第一个链接（写成 `链接 番号` 也认，取链接后面那一段）；
    某个链接前没有番号时，退回用整条消息里唯一的番号（只有一条时才有意义）。
    """
    from .core import extract_urls

    text = text or ""
    urls = extract_urls(text)
    if not urls:
        return {}

    # 按链接位置切段：第 i 段 = 第 i 个链接之前、上一个链接之后的文本
    marks = []
    cursor = 0
    for url in urls:
        at = text.find(url, cursor)
        if at < 0:
            at = cursor
        marks.append((at, at + len(url), url))
        cursor = at + len(url)

    out: dict[str, str] = {}
    for i, (start, end, url) in enumerate(marks):
        prev_end = marks[i - 1][1] if i else 0
        segment = text[prev_end:start]
        found = detect(segment)
        if not found:
            # 番号写在链接之后的写法：只看紧跟这条链接的一小段
            tail_end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
            found = detect(text[end:tail_end])
        if found:
            out[url] = found

    if len(urls) == 1:
        # 单条链接的常规写法是「番号 + 链接」或「链接 + 番号」，都已经覆盖；
        # 这里再兜底一次整条消息，兼容番号出现在很远的文字里。
        if not out.get(urls[0]):
            only = detect(text)
            if only:
                out[urls[0]] = only
    else:
        # 多条链接共用同一个番号没有意义；没识别出的就留空走文件名识别
        leftovers = [u for u in urls if u not in out]
        uniq = {v for v in out.values()}
        if len(leftovers) == len(urls) and len(uniq) == 1:
            for url in leftovers:
                out[url] = next(iter(uniq))
    return out
