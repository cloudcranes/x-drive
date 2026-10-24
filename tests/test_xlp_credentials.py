"""手动填凭据：粘一整段 JSON 就拆成设置项。

浏览器 localStorage 的 `credentials_{clientid}` 就是一段 JSON：
    {"token_type":"Bearer","access_token":"eyJ...","refresh_token":"a1....",
     "expires_in":43200,"sub":"...","user_id":"..."}
粘进来一次填齐；clientid 从 JWT 的 aud 自动取（迅雷的 clientid 就是 aud）。
"""
from __future__ import annotations

import base64
import json

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
