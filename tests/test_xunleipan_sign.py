"""迅雷风控签名与设备证明（xunleipan_sign）的契约测试。

背景：官方 API 现在读接口也要 captcha，且必须是**带设备签名**的那枚。
这两个测试守着最容易回归的两点：
  1. captcha_sign 的链必须与官方 JS 逐字节一致（改了盐就废了）；
  2. 设备证明的请求体字段不能变（服务端按这些字段校验）。
"""
from __future__ import annotations

import json

import httpx
import pytest

from app import xunleipan_sign as xs


# 官方 chunk utils-initial（模块 14 的 pubnHKAQ… 那条链）的输出。
# 这组值是「用官方 JS 算出来的」，Python 侧必须一模一样。
# 换句话说：把 raw 喂给官方 JS 得到 X，喂给本实现也必须得到 X。
JS_VECTORS = [
    ("Xqp0kJBXWhwaTpB61.93.13pan.xunlei.com"
     "d0e6c72e50357431c4d77729d6e563481792890000",
     "d804fc33ebe58ce797d1e049396f2682"),
    ("Xqp0kJBXWhwaTpB61.93.13pan.xunlei.com"
     "d0e6c72e50357431c4d77729d6e563481700000000",
     "8276b09f0c171042a87d6bc99472e89e"),
]


def test_sign_salts_count_and_shape():
    """链有 8 层；每层盐都是非空字符串。层数变了服务端就不认。"""
    assert len(xs.SIGN_SALTS) == 8
    assert all(s and isinstance(s, str) for s in xs.SIGN_SALTS)


@pytest.mark.parametrize("raw,expect", JS_VECTORS)
def test_captcha_sign_matches_official_js(raw, expect):
    """与官方 JS 的输出逐字节一致（回归护栏：盐或顺序改了这里就红）。

    官方那条链的入口是「一整条原始串」（clientId+version+pkg+deviceId+ts），
    这里直接喂原始串，绕开拼装。
    """
    assert xs.sign_raw(raw) == expect, (xs.sign_raw(raw), expect)
    # 拼装版本也要一致（同一套盐）
    ts = int(raw[-10:])
    prefix = raw[:-10]
    device = prefix.split("pan.xunlei.com")[-1]
    sign, got_raw = xs.captcha_sign(device, ts)
    assert got_raw == raw, (got_raw, raw)
    assert sign == "1." + expect, (sign, "1." + expect)


def test_sign_raw_is_pure_chain():
    """sign_raw 只做那 8 层 md5 —— 纯函数，好验。"""
    import hashlib
    v = "SEED"
    for salt in xs.SIGN_SALTS:
        v = hashlib.md5((v + salt).encode()).hexdigest()
    assert xs.sign_raw("SEED") == v


def test_captcha_sign_is_deterministic():
    a, _ = xs.captcha_sign("abc", 1700000000)
    b, _ = xs.captcha_sign("abc", 1700000000)
    assert a == b
    c, _ = xs.captcha_sign("abc", 1700000001)
    assert a != c, "timestamp 变了签名必须跟着变"


def test_device_id32_takes_first_32_after_dot():
    """官方脚本的取法：wdi10.<hex> 取点后前 32 字符。"""
    assert xs.device_id32("wdi10." + "a" * 64) == "a" * 32
    assert xs.device_id32("") == ""
    assert xs.device_id32("abcdef") == "abcdef"[:32]


def test_build_meta_has_required_fields():
    """meta 的字段名与官方 chunk 一致（少一个服务端就 400）。"""
    meta, raw, ts = xs.build_captcha_meta("d0e6c72e50357431c4d77729d6e56348", 1792890000)
    assert set(meta) == {"package_name", "client_version", "captcha_sign",
                          "timestamp", "user_id"}
    assert meta["package_name"] == "pan.xunlei.com"
    assert meta["client_version"] == "1.93.13"
    assert meta["captcha_sign"].startswith("1.")
    assert meta["timestamp"] == "1792890000"
    assert raw.endswith("1792890000")


class _Resp:
    def __init__(self, code, payload):
        self.status_code = code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _Client:
    """记录请求的假客户端。"""

    def __init__(self, report_payload=None):
        self.report_payload = report_payload or {"deviceid": "wdi10." + "b" * 64}
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return _Resp(200, {"ok": 1})

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        if "cmd=report" in url:
            return _Resp(200, self.report_payload)
        return _Resp(200, {"captcha_token": "ck0.x", "expires_in": 300})

    def close(self):
        pass


def test_device_proof_requests_algorithm_then_report():
    """设备证明：先取 algorithm（官方那条纯 JS 路径），再 POST report 换 deviceid。"""
    c = _Client()
    dev = xs.device_proof(client=c)
    assert dev == "wdi10." + "b" * 64
    methods = [(m, u) for m, u, _ in c.calls]
    assert methods[0][0] == "GET" and "cmd=algorithm" in methods[0][1]
    assert methods[1][0] == "POST" and "cmd=report" in methods[1][1]
    body = c.calls[1][2]["json"]
    assert set(body) == {"xl_fp_raw", "xl_fp", "version", "xl_fp_sign"}
    assert body["version"] == 2
    # xl_fp_sign = md5("a12247ffc2d9" + xl_fp_raw)
    import hashlib
    assert body["xl_fp_sign"] == hashlib.md5(
        (xs.DEVICE_SALT + body["xl_fp_raw"]).encode()).hexdigest()
    assert body["xl_fp"] == hashlib.md5(body["xl_fp_raw"].encode()).hexdigest()


def test_device_proof_raises_when_no_deviceid():
    c = _Client(report_payload={"error": "nope"})
    with pytest.raises(RuntimeError):
        xs.device_proof(client=c)
