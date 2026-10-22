"""What's the link? 辅助解析：提交磁力前先拿元信息。

接口：GET https://whatslink.info/api/v1/link?url=<magnet>
返回：{error, type(FOLDER/FILE/UNKNOWN), file_type, name, size, count, screenshots[]}

用途：
- 下载前就知道文件名与体积，能提前判配额，避免下完才发现太大
- 番号识别失败时还能从 name 里再试一次
- 网页上可以直接显示封面截图
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

import httpx

from . import codes

log = logging.getLogger("x-drive.whatslink")

API = "https://whatslink.info/api/v1/link"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://whatslink.info/",
}


async def lookup(client: httpx.AsyncClient, magnet: str, timeout: float = 20.0,
                 retries: int = 2) -> dict | None:
    """查磁力元信息；失败返回 None（不阻塞下载）。

    whatslink 偶尔会 500/超时（尤其是刚有人查询同一磁力时），重试两次即可。
    """
    if not magnet.lower().startswith("magnet:"):
        return None
    for attempt in range(retries + 1):
        data = await _once(client, magnet, timeout)
        if data is not None:
            return data
        if attempt < retries:
            await asyncio.sleep(1.5 * (attempt + 1))
    return None


async def _once(client: httpx.AsyncClient, magnet: str, timeout: float) -> dict | None:
    try:
        # 注意：用 params= 会把 magnet:? 里的 ?、& 一起转义，服务端会 500。
        # 只对分隔符做最小转义（magnet 里的 & 必须保留给服务端解析）。
        target = API + "?url=" + quote(magnet, safe=":?&=/")
        resp = await client.get(target, headers=HEADERS, timeout=timeout)
        if resp.status_code != 200:
            log.warning("whatslink 返回 %s", resp.status_code)
            return None
        data = resp.json()
    except Exception as exc:  # 辅助服务不可用不能影响主流程
        log.warning("whatslink 请求失败：%s", exc)
        return None

    if data.get("error"):
        log.warning("whatslink 报错：%s", data["error"])
        return None
    if data.get("type") == "UNKNOWN" or not data.get("size"):
        return None

    screenshots = [s.get("screenshot") for s in (data.get("screenshots") or []) if s.get("screenshot")]
    info = {
        "name": (data.get("name") or "").strip(),
        "size": int(data.get("size") or 0),
        "count": int(data.get("count") or 0),
        "type": data.get("type") or "",
        "file_type": data.get("file_type") or "",
        "screenshots": screenshots[:8],
    }
    log.info(
        "whatslink: %s（%s，%d 个文件）", info["name"], info["size"], info["count"]
    )
    return info


def guess_code(info: dict | None) -> str:
    """从元信息里的文件名反推番号。"""
    if not info:
        return ""
    return codes.detect(info.get("name") or "") or ""


def describe(info: dict | None) -> str:
    """一行摘要，用于 TG 回执与网页提示。"""
    if not info:
        return ""
    from .core import human_size

    bits = [info.get("name") or "未命名", human_size(info.get("size") or 0)]
    if info.get("count"):
        bits.append(f"{info['count']} 个文件")
    return " · ".join(bits)
