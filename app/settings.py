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

刮削源（MetaTube / JavBus）的地址、代理、超时以前也是启动参数，改一次就得
改 .env 再重启；现在收进这里，网页「设置 → 数据源」直接改、下一次调用即生效。
METATUBE_TOKEN 标成 secret：网页只能写、不回显，快照里只给「已设置 / 未设置」。

存储后端（STORAGE_BACKEND + OpenList / CloudDrive2 的地址与账号）也在这里：
切后端、改地址都只影响「下一次上传」。密码类同样标 secret。它们没有做成启动参数，
就是为了不用为了换一个网盘去改 .env 再重启容器。
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
    # ── 数据源：MetaTube（刮削后端）。地址/代理/超时改完下一次请求就生效。
    "METATUBE_URL": {"type": "str", "default": "", "group": "数据源",
                     "label": "MetaTube 地址",
                     "note": "例如 http://192.210.135.118:8090；留空 = 未配置刮削"},
    "METATUBE_TOKEN": {"type": "str", "default": "", "group": "数据源", "secret": True,
                       "label": "MetaTube Token",
                       "note": "服务端容器里的 TOKEN；只写不回显，留空表示不改"},
    "METATUBE_PROXY": {"type": "str", "default": "", "group": "数据源",
                       "label": "MetaTube 代理",
                       "note": "直连超时才填，例如 http://192.168.1.2:7893"},
    "METATUBE_TIMEOUT": {"type": "float", "default": 25.0, "group": "数据源",
                         "label": "MetaTube 超时 (秒)", "note": "单次请求超时"},
    "METATUBE_AUTH_HEADER": {"type": "str", "default": "Authorization", "group": "数据源",
                             "label": "鉴权头名",
                             "note": "默认 Authorization；服务端换了带法才改"},
    "METATUBE_AUTH_SCHEME": {"type": "str", "default": "Bearer", "group": "数据源",
                             "label": "鉴权前缀",
                             "note": "默认 Bearer；留空 = 只发裸 token"},
    # ── 数据源：JavBus（磁力/详情 JSON API，可选源，挂了自动回退 javdb）
    "JAVBUS_BASE": {"type": "str", "default": "https://javbus.master.us.kg", "group": "数据源",
                    "label": "JavBus API 地址",
                    "note": "自建 ovnrain/javbus-api 实例的根地址"},
    "JAVBUS_TIMEOUT": {"type": "float", "default": 25.0, "group": "数据源",
                       "label": "JavBus 超时 (秒)", "note": "单次请求超时"},
    "META_USE_JAVBUS": {"type": "bool", "default": True, "group": "数据源",
                        "label": "优先用 JavBus",
                        "note": "关掉就只走 javdb 页面解析"},
    # ── 存储：归档往哪儿放。默认还是 rclone；切到 openlist / clouddrive2 之后
    #    网页改完下一次上传就生效（连接实例会按配置指纹重建）。
    "STORAGE_BACKEND": {"type": "str", "default": "rclone", "group": "存储",
                        "label": "存储后端",
                        "choices": ["rclone", "openlist", "clouddrive2"],
                        "note": "rclone 用挂载进来的 rclone.conf；另两个走 HTTP/gRPC"},
    "STORAGE_ROOT": {"type": "str", "default": "", "group": "存储",
                     "label": "归档根目录",
                     "note": "留空 = 沿用 DRIVE_ROOT（默认 X-Drive）；OpenList/CD2 里是虚拟路径"},
    # OpenList（自建，AList v3 兼容 REST）
    "OPENLIST_URL": {"type": "str", "default": "", "group": "存储",
                     "label": "OpenList 地址",
                     "note": "例如 http://192.168.1.170:5244"},
    "OPENLIST_USER": {"type": "str", "default": "", "group": "存储",
                      "label": "OpenList 账号", "note": "管理员账号"},
    "OPENLIST_PASSWORD": {"type": "str", "default": "", "group": "存储", "secret": True,
                          "label": "OpenList 密码", "note": "只写不回显，留空表示不改"},
    "OPENLIST_OTP": {"type": "str", "default": "", "group": "存储",
                     "label": "OpenList 两步验证码",
                     "note": "账号开了 2FA 才填；每次登录要新码，一般是关掉 2FA 更省事"},
    "OPENLIST_TIMEOUT": {"type": "float", "default": 30.0, "group": "存储",
                         "label": "OpenList 请求超时 (秒)", "note": "列表/建目录这类短请求"},
    "OPENLIST_UPLOAD_TIMEOUT": {"type": "float", "default": 3600.0, "group": "存储",
                                "label": "OpenList 上传超时 (秒)", "note": "单个文件写入的读超时"},
    "OPENLIST_MULTIPART": {"type": "bool", "default": False, "group": "存储",
                           "label": "OpenList 分片上传",
                           "note": "服务端开了 multipart 才有效；不可用时自动退回单次 PUT"},
    "OPENLIST_MULTIPART_MB": {"type": "float", "default": 512.0, "group": "存储",
                              "label": "分片阈值 (MB)", "note": "超过这个大小才分片"},
    "OPENLIST_SHARE": {"type": "bool", "default": True, "group": "存储",
                       "label": "上传后建分享链接",
                       "note": "关掉就只显示路径；没有分享权限也自动退回路径"},
    "OPENLIST_SHARE_DAYS": {"type": "int", "default": 0, "group": "存储",
                            "label": "分享有效期 (天)", "note": "0 = 永久"},
    "OPENLIST_SHARE_PWD": {"type": "str", "default": "", "group": "存储",
                           "label": "分享提取码", "note": "留空 = 不要提取码"},
    # CloudDrive2（gRPC，19798）
    "CD2_URL": {"type": "str", "default": "", "group": "存储",
                "label": "CloudDrive2 地址",
                "note": "例如 http://192.168.1.170:19798（gRPC 与 Web 同端口）"},
    "CD2_USER": {"type": "str", "default": "", "group": "存储",
                 "label": "CloudDrive2 账号", "note": "注册用的邮箱"},
    "CD2_PASSWORD": {"type": "str", "default": "", "group": "存储", "secret": True,
                     "label": "CloudDrive2 密码", "note": "只写不回显，留空表示不改"},
    "CD2_TLS": {"type": "bool", "default": False, "group": "存储",
                "label": "CloudDrive2 用 TLS", "note": "服务端开了 HTTPS(19799) 才勾"},
    "CD2_CA_FILE": {"type": "str", "default": "", "group": "存储",
                    "label": "CloudDrive2 CA 文件", "note": "自签证书时填容器内路径"},
    "CD2_CHUNK_MB": {"type": "float", "default": 4.0, "group": "存储",
                     "label": "CloudDrive2 写入分片 (MB)", "note": "gRPC 单条消息的字节数"},
    "CD2_CALL_TIMEOUT": {"type": "float", "default": 60.0, "group": "存储",
                         "label": "CloudDrive2 调用超时 (秒)", "note": "普通 RPC"},
    "CD2_TASK_TIMEOUT": {"type": "float", "default": 7200.0, "group": "存储",
                         "label": "CloudDrive2 上传任务超时 (秒)",
                         "note": "等 CD2 把文件真正传上云的上限"},
    "CD2_LINK_MODE": {"type": "str", "default": "none", "group": "存储",
                      "label": "CloudDrive2 链接形态",
                      "choices": ["none", "static", "template"],
                      "note": "CD2 没有「创建分享」接口：none=显示路径，static=单文件直链，template=按模板拼"},
    "SHARE_LINK_TEMPLATE": {"type": "str", "default": "", "group": "存储",
                            "label": "链接模板",
                            "note": "配合 static 之外的自建浏览页，{path} 会被替换成 URL 编码后的路径"},
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

    # ---- 「有没有被显式配过」：设置表 > 环境变量；都没有返回 (False, None)。
    #      刮削源这类参数用它决定要不要回落到模块里的老常量（测试里 monkeypatch 的
    #      就是那些常量，不能被一份空的 settings 覆盖掉）。
    def explicit(self, key: str) -> tuple[bool, object]:
        spec = SETTINGS_SPEC.get(key)
        if spec is None:
            return False, None
        try:
            data = self._store.state_get(SETTINGS_KEY)
        except Exception:
            data = {}
        if key in data:
            try:
                return True, _coerce(spec["type"], data[key])
            except (TypeError, ValueError):
                return False, None
        env = os_getenv(key)
        if env is not None and env != "":
            try:
                return True, _coerce(spec["type"], env)
            except (TypeError, ValueError):
                return False, None
        return False, None

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

    # ---- 全部设置项的「当前值 + 来源」快照（网页设置面板用）
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
            if spec.get("secret"):
                # 密钥只写不回显：接口里给空值 + 是否已设置，别把 token 发给浏览器
                out[key] = {"value": "", "configured": bool(value),
                            "source": source, **spec}
            else:
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


# ───────────────────────── 进程内绑定：给「模块常量」型配置做热更新的桥
#
# metatube / javbus / meta 里那些 BASE、TOKEN、TIMEOUT 是模块导入时一次性读的
# 环境变量。要让网页改完立即生效，又不能把它们改成必传参数（会牵动全部调用方），
# 这里在 web / bot 启动时把 Settings 实例绑到模块级，取配置时先问它。
# 没绑定（单测、离线脚本）就照旧用模块常量。
_bound: Settings | None = None


def bind(instance: Settings | None) -> None:
    """把当前进程用的 Settings 绑给热更新桥。传 None 解绑。"""
    global _bound
    _bound = instance


def bound_get(key: str) -> tuple[bool, object]:
    """(是否显式配置, 值)。没绑定实例时一律当作「没配过」。"""
    inst = _bound
    if inst is None:
        return False, None
    try:
        return inst.explicit(key)
    except Exception:
        return False, None


def hot(key: str, fallback, cast=None):
    """热更新取值：显式配置优先，否则用调用方给的回落值。"""
    ok, value = bound_get(key)
    if not ok:
        return fallback
    if cast is None:
        return value
    try:
        return cast(value)
    except (TypeError, ValueError):
        return fallback
