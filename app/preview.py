"""磁力预览缓存：按 infohash 存 whatslink 的结果，带 TTL 与失败负缓存。

为什么单独存：whatslink 查同一条磁力要几秒，同一份候选在搜索结果、详情弹层、
提交前预览里会被看三次；不缓存就是把同一份网络请求打三遍，还容易被限流。

  · 键是 infohash（不含磁力里的 dn/tr，带 trackers 的副本也命中同一条）
  · 成功结果 TTL 7 天（文件名、体积、截图基本不变）
  · 失败负缓存 10 分钟：whatslink 抖动时不至于每次点击都重试三遍
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from . import core

log = logging.getLogger("x-drive.preview")

OK_TTL = 7 * 86400
FAIL_TTL = 10 * 60
_KEY_RE = re.compile(r"btih:([0-9a-zA-Z]{32,40})")

class PreviewCache:
    def __init__(self, path: str):
        self.path = path

    @staticmethod
    def key_of(magnet_url: str) -> str:
        hit = _KEY_RE.search(magnet_url or "")
        return hit.group(1).lower() if hit else ""

    def _all(self) -> dict:
        data = core.load_state(self.path)
        return data if isinstance(data, dict) else {}

    def get(self, magnet_url: str) -> dict | None:
        """命中且未过期才返回；负缓存返回 {}（表示「已知查不到」），未命中返回 None。"""
        key = self.key_of(magnet_url)
        if not key:
            return None
        item = self._all().get(key)
        if not isinstance(item, dict):
            return None
        age = time.time() - float(item.get("at") or 0)
        ttl = FAIL_TTL if item.get("failed") else OK_TTL
        if age > ttl:
            return None
        return {} if item.get("failed") else dict(item.get("info") or {})

    def put(self, magnet_url: str, info: dict | None) -> None:
        key = self.key_of(magnet_url)
        if not key:
            return
        record = {"at": time.time(), "failed": info is None, "info": info or {}}
        try:
            core.update_state(self.path, {key: record})
        except OSError as exc:
            log.warning("预览缓存写入失败：%s", exc)

    def prune(self, days: float = 30.0) -> int:
        data = self._all()
        cutoff = time.time() - max(1.0, days) * 86400
        stale = [k for k, v in data.items()
                 if not isinstance(v, dict) or float(v.get("at") or 0) < cutoff]
        for key in stale:
            data.pop(key, None)
        if stale:
            core.save_state(self.path, data)
        return len(stale)
