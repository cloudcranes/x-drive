"""可热更新的运行时参数：存任务库 state 表的 `settings` 键，网页可改、立即生效。

为什么要有这个文件：以前这些参数（配额、重试、并发、刮削开关……）都写在
模块顶层 `os.getenv(...)` 里，进程启动读一次就定死。想改得 SSH 上机改 .env
再重启。现在统一收进这里：

  · 读取优先级：state 表里的 settings > 环境变量 > 默认值；
  · 带 TTL 缓存：同一进程内短时间不重复打库；网页保存后 web 进程立刻失效缓存，
    bot 进程靠 TTL 到期自然读到新值（默认 5 秒，对「下次用到即生效」足够）；
  · 两个进程共享同一份：web 与 bot 挂同一个 ./data，state 表是同一个库文件。

哪些算「启动参数」不进这里（改了必须重启，见 README 的 .env 说明）：
  TELEGRAM_BOT_TOKEN / WEB_PORT / XUNLEI_URL / RCLONE_CONFIG_HOST /
  TMP_ROOT / STATE_DIR / 各类挂载路径。它们仍走 os.getenv。
"""
from __future__ import annotations

import os
import threading
import time

# 每一项：key=state 表里的键（也是环境变量名），后面跟 type/默认值/分组/说明。
# 类型用字符串标记，读写时据此转换，避免每个值手写一段 if。
SETTINGS_SPEC: dict[str, dict] = {
    # ── 配额
    "DAILY_UPLOAD_GB": {"type": "int", "default": 0, "group": "配额",
                        "label": "每日上传上限 (GB)", "note": "0 = 不限"},
    # ── 重试
    "RETRY_BASE_DELAY": {"type": "float", "default": 3.0, "group": "重试",
                         "label": "重试基础退避 (秒)", "note": "指数增长，封顶 60s"},
    "RETRIES": {"type": "int", "default": 2, "group": "重试",
                "label": "失败重试次数", "note": "网络给满、死种只给一次"},
    "STALL_TIMEOUT": {"type": "float", "default": 900.0, "group": "重试",
                      "label": "死种判定 (秒)", "note": "连续多久无新字节判死种，0=不检测"},
    "MAGNET_TIMEOUT": {"type": "float", "default": 7200.0, "group": "重试",
                       "label": "单任务总超时 (秒)", "note": "默认 2 小时"},
    # ── 上传
    "UPLOAD_CHUNK": {"type": "str", "default": "64M", "group": "上传",
                     "label": "上传分片", "note": "实测 64M 最优"},
    "UPLOAD_TRANSFERS": {"type": "int", "default": 4, "group": "上传",
                         "label": "上传并发数", "note": "实测 4 最优"},
    # ── 磁盘
    "DISK_RESERVE_GB": {"type": "float", "default": 5.0, "group": "磁盘",
                        "label": "磁盘预留 (GB)", "note": "永远不参与分配的空间"},
    "DISK_QUEUE_TIMEOUT": {"type": "float", "default": 0.0, "group": "磁盘",
                           "label": "排队最久等待 (秒)", "note": "0 = 一直等"},
    # ── 刮削
    "SCRAPE": {"type": "bool", "default": True, "group": "刮削",
               "label": "刮削元数据", "note": "关掉就只传视频不生成 NFO/封面"},
    "SCRAPE_NFO": {"type": "bool", "default": True, "group": "刮削",
                   "label": "生成 NFO", "note": "Kodi/Emby 认的 movie.nfo"},
    "SCRAPE_POSTER": {"type": "bool", "default": True, "group": "刮削",
                      "label": "取封面", "note": "poster.jpg"},
    # ── 归档回收
    "CLEANUP_MAX_GB": {"type": "float", "default": 0.0, "group": "归档回收",
                       "label": "回收容量上限 (GB)", "note": "0 = 关闭"},
    "CLEANUP_KEEP_RECENT": {"type": "int", "default": 10, "group": "归档回收",
                            "label": "回收保留最近几个", "note": "永远保留最近 N 个"},
    "CLEANUP_OLDER_DAYS": {"type": "float", "default": 0.0, "group": "归档回收",
                           "label": "回收只考虑多久前的", "note": "0 = 关闭"},
    # ── 面板
    "PANEL_CLEAN_DAYS": {"type": "float", "default": 0.0, "group": "面板",
                         "label": "面板失败记录保留天数", "note": "0 = 不自动清"},
    # ── 其它
    "DEFAULT_SIZE_GB": {"type": "float", "default": 4.0, "group": "其它",
                        "label": "默认体积估算 (GB)", "note": "体积查不到时的兜底"},
}

# 设置键在 state 表里的名字
SETTINGS_KEY = "settings"
# 缓存多久（秒）：web 进程网页保存后主动失效；bot 进程靠这个 TTL 到期读到新值。
# 5 秒对「下次用到即生效」足够，也不至于让热路径频繁打库。
TTL = 5.0

# 类型转换：读出来（从 JSON/字符串）→ Python 值；写进去（Python 值）→ 存 JSON。
_CAST = {
    "int": lambda v: int(v),
    "float": lambda v: float(v),
    "str": lambda v: str(v),
    "bool": lambda v: str(v).strip().lower() not in ("0", "false", "no", "off", ""),
}


def _coerce(kind: str, value):
    """把任意来源的值（环境变量字符串 / JSON 里的值）转成声明类型。"""
    if kind == "bool":
        return _CAST["bool"](value)
    return _CAST[kind](value)


class Settings:
    """一组可热更新参数。每个进程一个实例，共享同一份 state 表。

    缓存策略：`get` 优先用缓存，缓存超 TTL 才回查 state 表；`set` 写库后立即
    清本进程缓存（web 保存后自己马上读到新值）。bot 进程不调用 set，只靠 TTL。
    """

    def __init__(self, store):
        self._store = store          # store.Store 实例，有 state_get/state_put
        self._lock = threading.Lock()
        self._cache: dict[str, tuple] = {}   # key -> (value, fetched_at)

    # ---- 读单个值（带缓存）
    def get(self, key: str):
        spec = SETTINGS_SPEC.get(key)
        if spec is None:
            raise KeyError(f"未知设置项 {key}")
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[1] < TTL:
                return hit[0]
        value = self._read(key, spec)
        with self._lock:
            self._cache[key] = (value, time.time())
        return value

    # ---- 读「当前生效」的原始来源（网页展示用：区分来自设置/环境变量/默认）
    def _read(self, key: str, spec: dict):
        # 1. state 表里的 settings
        try:
            data = self._store.state_get(SETTINGS_KEY)
        except Exception:
            data = {}
        if key in data:
            return _coerce(spec["type"], data[key])
        # 2. 环境变量
        env = os_getenv(key)
        if env is not None and env != "":
            return _coerce(spec["type"], env)
        # 3. 默认值
        return spec["default"]

    # ---- 全部 17 项的「当前值 + 来源」快照（网页设置面板用）
    def snapshot(self) -> dict:
        try:
            data = self._store.state_get(SETTINGS_KEY)
        except Exception:
            data = {}
        out = {}
        for key, spec in SETTINGS_SPEC.items():
            if key in data:
                value = _coerce(spec["type"], data[key])
                source = "settings"
            else:
                env = os_getenv(key)
                if env is not None and env != "":
                    value = _coerce(spec["type"], env)
                    source = "env"
                else:
                    value = spec["default"]
                    source = "default"
            out[key] = {"value": value, "source": source, **spec}
        return out

    # ---- 写（网页保存）。校验类型，写库，清本进程缓存。
    def set(self, key: str, value) -> bool:
        spec = SETTINGS_SPEC.get(key)
        if spec is None:
            return False
        try:
            coerced = _coerce(spec["type"], value)
        except (TypeError, ValueError):
            return False
        try:
            data = self._store.state_get(SETTINGS_KEY)
            data[key] = coerced
            self._store.state_put({SETTINGS_KEY: data})
        except Exception:
            return False
        with self._lock:
            self._cache.pop(key, None)
        return True

    # ---- 批量写（网页一次保存多个）
    def set_many(self, updates: dict) -> dict:
        ok, failed = {}, {}
        for key, value in updates.items():
            if self.set(key, value):
                ok[key] = True
            else:
                failed[key] = "类型不匹配或写库失败"
        return {"ok": failed == {}, "saved": sorted(ok), "failed": failed}


# 环境变量读取：独立成函数便于测试替换。
def os_getenv(key: str):
    return os.getenv(key)
