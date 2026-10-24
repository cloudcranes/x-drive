"""手动填凭据：粘一整段 JSON 就拆成设置项。

浏览器 localStorage 的 `credentials_{clientid}` 就是一段 JSON：
    {"token_type":"Bearer","access_token":"eyJ...","refresh_token":"a1....",
     "expires_in":43200,"sub":"...","user_id":"..."}
粘进来一次填齐；clientid 从 JWT 的 aud 自动取（迅雷的 clientid 就是 aud）。
"""
from __future__ import annotations

import base64
import json

import pytest

from app import xunleipan


def _jwt(aud: str = "Xqp0kJBXWhwaTpB6") -> str:
    body = base64.urlsafe_b64encode(json.dumps({"aud": aud, "exp": 1792882687}).encode())
    return "eyJ." + body.rstrip(b"=").decode() + ".sig"


CRED = json.dumps({
    "token_type": "Bearer",
    "access_token": _jwt(),
    "refresh_token": "a1.HMV-test",
    "expires_in": 43200,
    "sub": "814926528",
    "user_id": "814926528",
})


def test_parse_whole_json_fills_all_three():
    out = xunleipan.parse_credentials(CRED)
    assert out["XLPAN_TOKEN"].startswith("eyJ.")
    assert out["XLPAN_REFRESH_TOKEN"] == "a1.HMV-test"
    assert out["XLPAN_CLIENT_ID"] == "Xqp0kJBXWhwaTpB6", "clientid 从 JWT 的 aud 自动取"


def test_parse_bare_tokens():
    out = xunleipan.parse_credentials("a1.HMV-bare " + _jwt())
    assert out["XLPAN_REFRESH_TOKEN"] == "a1.HMV-bare"
    assert out["XLPAN_TOKEN"].startswith("eyJ.")


def test_parse_key_value_lines():
    text = 'access_token: eyJ.a.b\nrefresh_token: a1.zzz\nclientid: CID-9'
    out = xunleipan.parse_credentials(text)
    assert out["XLPAN_TOKEN"] == "eyJ.a.b"
    assert out["XLPAN_REFRESH_TOKEN"] == "a1.zzz"
    assert out["XLPAN_CLIENT_ID"] == "CID-9"


def test_parse_picks_up_device_id():
    """deviceid 是另一个 localStorage 键（形如 wdi10.…），但读接口必需它。"""
    text = "deviceid: wdi10." + "a" * 64
    out = xunleipan.parse_credentials(text)
    assert out["XLPAN_DEVICE_ID"] == "wdi10." + "a" * 64

    # 裸的 wdi10.… 也认
    out2 = xunleipan.parse_credentials("wdi10." + "b" * 64)
    assert out2["XLPAN_DEVICE_ID"] == "wdi10." + "b" * 64

    # 混在同一个 JSON 里也认（有的导出工具会合并）
    out3 = xunleipan.parse_credentials(json.dumps({
        "access_token": _jwt(), "refresh_token": "a1.x", "deviceid": "wdi10.ccc"}))
    assert out3["XLPAN_DEVICE_ID"] == "wdi10.ccc"


def test_device_id_is_required_by_the_api():
    """实测：读接口不带 x-device-id 回 400 invalid_argument —— 所以它是必需的。"""
    import httpx
    from app import xunleipan as xl

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["has"] = "x-device-id" in request.headers
        if "x-device-id" not in request.headers:
            return httpx.Response(400, json={"error": "invalid_argument"})
        return httpx.Response(200, json={"files": []})

    c = httpx.Client(transport=httpx.MockTransport(handler), base_url=xl.API)
    with pytest.raises(xl.PanError):
        xl.list_files(client=c)
    assert seen["has"] is False, "本次刻意不带 device-id"

    seen.clear()
    xl._headers_orig = xl._headers
    try:
        xl._headers = lambda: {k: v for k, v in xl._headers_orig().items()
                               if k != "x-device-id"}
        with pytest.raises(xl.PanError):
            xl.list_files(client=c)
        assert seen["has"] is False
    finally:
        xl._headers = xl._headers_orig


def test_parse_garbage_returns_empty():
    assert xunleipan.parse_credentials("随便一段没有凭据的话") == {}
    assert xunleipan.parse_credentials("") == {}


def test_client_id_from_jwt():
    assert xunleipan._client_id_from_jwt(_jwt("CID-X")) == "CID-X"
    assert xunleipan._client_id_from_jwt("not-jwt") == ""
    assert xunleipan._client_id_from_jwt("") == ""


def test_apply_writes_settings(monkeypatch):
    saved = {}
    import app.web as web_mod
    monkeypatch.setattr(web_mod, "set_settings",
                        lambda u: saved.update(u) or {"ok": True, "saved": list(u)})
    out = xunleipan.apply_credentials(CRED)
    assert out["error"] == ""
    assert saved["XLPAN_REFRESH_TOKEN"] == "a1.HMV-test"
    assert "XLPAN_TOKEN" in saved and "XLPAN_CLIENT_ID" in saved


def test_apply_reports_unrecognizable(monkeypatch):
    out = xunleipan.apply_credentials("没有凭据的一段话")
    assert out["saved"] == [] and "没认出" in out["error"]


# ---------------------------------------------------------------- 授权码

def test_parse_picks_up_auth_code():
    """地址栏里 code=xxx 那一段：用户自己从浏览器授权页复制过来。"""
    out = xunleipan.parse_credentials(
        "http://192.168.1.170:18091/xlp/callback?code=AbC-123_xyz&state=s")
    assert out["XLPAN_AUTH_CODE"] == "AbC-123_xyz"


def test_apply_with_auth_code_exchanges_for_pair(monkeypatch):
    """贴授权码 → 换出 access + refresh 一对（唯一能一次拿到 refresh 的通道）。"""
    saved = {}
    import app.web as web_mod
    monkeypatch.setattr(web_mod, "set_settings",
                        lambda u: saved.update(u) or {"ok": True, "saved": list(u)})
    monkeypatch.setattr(web_mod, "get_setting",
                        lambda k, d=None: saved.get(k, d if d is not None else ""))
    monkeypatch.setattr(xunleipan, "grant_authorization_code",
                        lambda code, **kw: {"access_token": "AT", "refresh_token": "RT",
                                            "expires_in": 43200})
    out = xunleipan.apply_credentials("code=THE-CODE")
    assert out["exchanged"], "应说明是授权码换来的"
    assert saved["XLPAN_TOKEN"] == "AT"
    assert saved["XLPAN_REFRESH_TOKEN"] == "RT"


def test_apply_with_bad_auth_code_reports_error(monkeypatch):
    import app.web as web_mod
    monkeypatch.setattr(web_mod, "set_settings", lambda u: {"ok": True, "saved": []})
    monkeypatch.setattr(xunleipan, "grant_authorization_code",
                        lambda code, **kw: (_ for _ in ()).throw(
                            xunleipan.PanError("invalid code")))
    out = xunleipan.apply_credentials("code=BAD")
    assert out["error"] and "授权码" in out["error"]
