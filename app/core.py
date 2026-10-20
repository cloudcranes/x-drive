"""基础工具：状态文件读写、URL 提取、大小格式化。

业务逻辑（番号识别、下载、上传）分别在 codes / fetcher / main 里。
"""
from __future__ import annotations

import json
import os
import re

URL_RE = re.compile(r"(?:magnet:\?[^\s\"'<>]+|https?://[^\s\"'<>]+)", re.I)
TRAILING = ").,;!?'\"”’》】」>"


def extract_urls(text: str) -> list[str]:
    """文本里所有可下载来源，去重保序。"""
    seen: set[str] = set()
    out: list[str] = []
    for raw in URL_RE.findall(text or ""):
        url = raw.rstrip(TRAILING)
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def extract_url(text: str) -> str | None:
    urls = extract_urls(text)
    return urls[0] if urls else None


def human_size(num: float) -> str:
    """字节 → 人类可读；整 GB 保留两位小数。"""
    if not num:
        return "—"
    for unit in ("B", "KB", "MB"):
        if num < 1024:
            return f"{num:.0f}{unit}"
        num /= 1024
    for unit in ("GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.2f}{unit}"
        num /= 1024
    return f"{num:.2f}TB"


def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
