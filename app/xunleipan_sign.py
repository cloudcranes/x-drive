"""迅雷风控签名与设备证明：从官方前端 chunk 反推出的纯 Python 实现。

背景（2026-10-24 实测）：官方 API 现在**读接口也要 captcha**，而
`POST /v1/shield/captcha/init` 签发出来的 captcha 服务端不认
（400 `no client info found`）—— 因为它不带**设备签名**。

浏览器签的那枚是三段 `ck0.<470>.<290>`，第三段解出来是 protobuf：
clientId / version / packageName / deviceId / timestamp / 签名。

于是要把官方那套抄成 Python，两件事：

1. **设备证明**（纯 HTTP，不需要原生客户端）：
       GET  {origin}/risk?cmd=algorithm&t={ts}
            → 返回一段 JS，定义 xl_al(x) = md5("a12247ffc2d9" + x)
       POST {origin}/risk?cmd=report
            body {xl_fp_raw, xl_fp, version:2, xl_fp_sign}
            → {"deviceid": "wdi10.<64hex>"}
   这条是**官方前端在没有原生客户端时走的纯 JS 路径**（chunk
   service-device-sign 的 DeviceSignUtil._requestDeviceSign），
   实测在服务器上直接跑得通。

2. **captcha_sign**：官方是
       captcha_sign = algVersion + "." + 链式 md5
   链有 8 层，每层是 `md5(上一层 + 固定盐)`。盐与顺序是从官方 chunk
   `utils-initial`（模块 14 的 pubnHKAQ… 导出）静态解析 + 逐层黑盒
   验证得到的，与官方 JS 的输出**逐字节一致**（有测试守着）。

   原始串的格式（从官方 chunk 的 `U(e)` 反推）：
       raw = clientId + version + packageName + deviceId + timestamp
   version 用客户端版本号（实测 1.93.13 有效；2.0.12 会被判
   invalid captcha_sign）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid

import httpx

log = logging.getLogger("x-drive.xunleipan.sign")

# 迅雷账号服务（同时是 captcha 签发端点与设备证明端点）
ORIGIN = "https://xluser-ssl.xunlei.com"

# 官方前端用的 clientId / 包名 / 客户端版本
CLIENT_ID = "Xqp0kJBXWhwaTpB6"
PACKAGE_NAME = "pan.xunlei.com"
CLIENT_VERSION = "1.93.13"
ALG_VERSION = "1"

# 设备证明用的固定盐（xl_al 的实现：md5("a12247ffc2d9" + x)）
DEVICE_SALT = "a12247ffc2d9"

# captcha_sign 的链：每层 md5(上一层 + 盐)
# 顺序与盐取自官方 chunk utils-initial 的模块 14（pubnHKAQ… 那条链），
# 逐层用「G(md5(x+salt)) == F(x)」黑盒验证过。
SIGN_SALTS = (
    "Q0HaIAoI3sasyPWsryjOulcq",
    "1Px33Ag0Kr4JijR741ur6Zo/HqOfOTBKH7XGckqGx1uFF3ngme7DAHJx8yd15En",
    "8en0IoATgahB9Br7ItSmSt",
    "oEIg9ldpW/AzcNIg7IpKSWIlltZSsabolxJMsSJgvSdN8v7DR8ukwsYlGLw0YT",
    "cUHVguVfmsJqLcbiN+Pcml0FCt8PySoen",
    "Q7qCtf9p",
    "dcW1N1",
    "En7YUHQPnEiUoLssIFK+MuhQ0QCTt2bk5zP",
)


def md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def sign_raw(raw: str) -> str:
    """原始串 → 链式 md5 的结果（不含 algVersion 前缀）。

    官方那条链的入口就是「一整条原始串」：
        clientId + version + packageName + deviceId + timestamp
    """
    value = raw
    for salt in SIGN_SALTS:
        value = md5(value + salt)
    return value


def captcha_sign(device_id: str, timestamp: int | None = None) -> tuple[str, str]:
    """算 captcha_sign。返回 (sign, 原始串)。

    sign = "1." + 8 层 md5
    """
    ts = int(time.time()) if timestamp is None else int(timestamp)
    raw = f"{CLIENT_ID}{CLIENT_VERSION}{PACKAGE_NAME}{device_id}{ts}"
    return f"{ALG_VERSION}.{sign_raw(raw)}", raw


def device_proof(client: httpx.Client | None = None) -> str:
    """走官方那条纯 HTTP 设备证明路径，返回 deviceid（wdi10.<64hex>）。

    没有原生客户端时官方前端就走这条：
        GET  /risk?cmd=algorithm&t={ms}   → 拿到 xl_al 的定义（其实是
                                              md5("a12247ffc2d9" + x)）
        POST /risk?cmd=report               → 换 deviceid
    """
    own = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=15.0)
    try:
        ts = int(time.time() * 1000)
        raw = str(uuid.uuid4())
        xl_fp = md5(raw)
        xl_fp_sign = md5(DEVICE_SALT + raw)
        # 先取 algorithm（服务端会校验这条链路；拿不到就直接算，代价一样）
        try:
            client.get(f"{ORIGIN}/risk?cmd=algorithm&t={ts}", timeout=10.0)
        except httpx.HTTPError as exc:                # noqa: BLE001
            log.debug("取 risk algorithm 失败（不影响换 deviceid）：%s", exc)
        res = client.post(
            f"{ORIGIN}/risk?cmd=report",
            json={"xl_fp_raw": raw, "xl_fp": xl_fp, "version": 2,
                  "xl_fp_sign": xl_fp_sign},
            headers={"content-type": "application/json", "clientid": CLIENT_ID},
        )
        if res.status_code != 200:
            raise RuntimeError(f"换 deviceid 失败：{res.status_code} {res.text[:120]}")
        data = res.json() or {}
        device_id = str(data.get("deviceid") or "")
        if not device_id:
            raise RuntimeError(f"换 deviceid 失败：响应里没有 deviceid（{res.text[:120]}）")
        return device_id
    finally:
        if own:
            client.close()


def device_id32(device_id: str) -> str:
    """x-device-id 用的 32 位：wdi10.<hex> 取点后前 32 字符（官方脚本同款）。"""
    raw = (device_id or "").strip()
    if not raw:
        return ""
    return raw.split(".", 1)[-1][:32] if "." in raw else raw[:32]


def build_captcha_meta(device_id: str, timestamp: int | None = None) -> tuple[dict, str, int]:
    """构造签发 captcha 用的 meta。返回 (meta, 原始串, timestamp)。"""
    ts = int(time.time()) if timestamp is None else int(timestamp)
    sign, raw = captcha_sign(device_id, ts)
    meta = {
        "package_name": PACKAGE_NAME,
        "client_version": CLIENT_VERSION,
        "captcha_sign": sign,
        "timestamp": str(ts),
        "user_id": "814926528",
    }
    return meta, raw, ts
