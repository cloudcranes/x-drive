"""一键登录：授权会话 + PKCE + 回调换 token。

关键点：
  · state 随机且**一次性**（取走即销毁），防 CSRF 换 token；
  · PKCE S256（verifier/challenge 配对），授权页不接触 secret；
  · 授权码流程一次给**一对** token（access + refresh）—— 这是唯一能一次拿到
    refresh_token 的通道（它一次性，续期即轮换）。
  · 回调页不显示任何 token。
"""
from __future__ import annotations

import time

import httpx
import pytest

from app import xlpauth, xunleipan


# ---------------------------------------------------------------- 会话

def test_session_is_one_shot():
    s = xlpauth.create_session()
    v1 = xlpauth.take_session(s["state"])
    assert v1 == s["verifier"]
    assert xlpauth.take_session(s["state"]) == "", "state 只能用一次（防重放）"


def test_unknown_state_rejected():
    assert xlpauth.take_session("never-existed") == ""


def test_expired_session_rejected(monkeypatch):
    s = xlpauth.create_session()
    monkeypatch.setattr(xlpauth, "SESSION_TTL", -1.0)
    assert xlpauth.take_session(s["state"]) == "", "过期的会话不该拿到 verifier"


def test_pkce_pair_is_s256():
    import base64
    import hashlib
    s = xlpauth.create_session()
    digest = hashlib.sha256(s["verifier"].encode()).digest()
    want = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert s["challenge"] == want
    assert "=" not in s["verifier"] and "=" not in s["challenge"]


def test_auth_url_contains_everything():
    s = xlpauth.create_session()
    url = xlpauth.auth_url("http://192.168.1.170:18091", s["state"], s["challenge"])
    assert url.startswith("https://i.xunlei.com/center/account/personal/oauth/?")
    assert "client_id=Xqp0kJBXWhwaTpB6" in url
    assert "response_type=code" in url
    assert "redirect_uri=http%3A%2F%2F192.168.1.170%3A18091%2Fxlp%2Fcallback" in url
    assert f"state={s['state']}" in url
    assert "code_challenge_method=S256" in url


# ---------------------------------------------------------------- 换 token

def test_grant_with_authorization_code(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = httpx.Response(200, json={})  # 占位，下面覆盖
        import json as _j
        seen["body"] = _j.loads(request.content)
        return httpx.Response(200, json={
            "access_token": "AT", "refresh_token": "RT", "expires_in": 43200})

    c = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://xluser-ssl.xunlei.com")
    out = xunleipan.grant_authorization_code("the-code", "http://127.0.0.1/xlp/callback",
                                              "verifier-value", client=c)
    assert out["access_token"] == "AT"
    assert out["refresh_token"] == "RT", "授权码流程要一次拿到 refresh_token（它是一次性的）"
    assert seen["body"]["grant_type"] == "authorization_code"
    assert seen["body"]["code"] == "the-code"
    assert seen["body"]["redirect_uri"] == "http://127.0.0.1/xlp/callback"
    assert seen["body"]["code_verifier"] == "verifier-value"
    assert seen["body"]["client_secret"], "换 token 要带 client_secret"


def test_grant_rejects_missing_code():
    with pytest.raises(xunleipan.PanError):
        xunleipan.grant_authorization_code("", "http://x/cb")


def test_grant_reports_server_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    c = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://xluser-ssl.xunlei.com")
    with pytest.raises(xunleipan.PanError):
        xunleipan.grant_authorization_code("c", "http://x/cb", client=c)
