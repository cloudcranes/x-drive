"""迅雷登录自动化：refresh_token 续期（账号密码直登不支持）。

实测结论：
  · POST https://xluser-ssl.xunlei.com/v1/auth/token
      body {grant_type:"refresh_token", refresh_token, client_id} → 200，
      **不需要 client_secret**（前端那份 secret 明文硬编码，服务端不额外校验）。
  · 同一个端点 grant_type=password → 400 `currently not supported`：
    **账号密码直登不支持**，自动化只能靠 refresh_token。
  · access_token 是 JWT，exp 可读；临近过期自动续，写回设置。
"""
from __future__ import annotations

import base64
import json
import time

import httpx
import pytest

from app import xunleipan


def _jwt(exp: int) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"h.{body}.s"


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://xluser-ssl.xunlei.com")


def _env(monkeypatch, token="", refresh=""):
    values = {"XLPAN_TOKEN": token, "XLPAN_REFRESH_TOKEN": refresh,
              "XLPAN_CLIENT_ID": "Xqp0kJBXWhwaTpB6", "XLPAN_DEVICE_ID": "",
              "XLPAN_CAPTCHA_TOKEN": ""}
    monkeypatch.setattr(xunleipan, "_setting", lambda key: values.get(key, ""))
    return values


# ---------------------------------------------------------------- 续期

def test_refresh_token_grants_new_access_token(monkeypatch):
    _env(monkeypatch)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "token_type": "Bearer", "access_token": "new-token",
            "refresh_token": "new-refresh", "expires_in": 43200})

    out = xunleipan.refresh_access_token("old-refresh", client=client(handler))
    assert out["access_token"] == "new-token"
    assert out["refresh_token"] == "new-refresh", "服务端轮换了 refresh_token，要一并存"
    assert out["expires_in"] == 43200
    assert seen["url"].endswith("/v1/auth/token")
    assert seen["body"]["grant_type"] == "refresh_token"
    assert "client_secret" not in seen["body"], "续期不需要 client_secret"


def test_refresh_keeps_old_refresh_token_when_not_rotated(monkeypatch):
    _env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "new", "expires_in": 100})

    out = xunleipan.refresh_access_token("old-refresh", client=client(handler))
    assert "refresh_token" not in out, "没轮换就别覆盖旧的"


def test_refresh_failure_raises(monkeypatch):
    _env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_grant"})

    with pytest.raises(xunleipan.PanError):
        xunleipan.refresh_access_token("bad", client=client(handler))


def test_password_grant_is_not_supported(monkeypatch):
    """账号密码直登不支持——自动化只能靠 refresh_token。"""
    _env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "error": "invalid_argument",
            "error_description": "currently not supported"})

    with pytest.raises(xunleipan.PanError):
        xunleipan.refresh_access_token("x", client=client(handler))


# ---------------------------------------------------------------- 过期判断

def test_token_expiry_reads_jwt_exp():
    assert xunleipan._token_expiry(_jwt(1700000000)) == 1700000000
    assert xunleipan._token_expiry("not-a-jwt") == 0
    assert xunleipan._token_expiry("") == 0


def test_needs_refresh_only_when_close_to_expiry(monkeypatch):
    _env(monkeypatch, token=_jwt(int(time.time()) + 3600), refresh="r")
    assert xunleipan._needs_refresh() is False
    _env(monkeypatch, token=_jwt(int(time.time()) + 60), refresh="r")
    assert xunleipan._needs_refresh() is True


def test_needs_refresh_false_without_refresh_token(monkeypatch):
    """没配 refresh_token 就别想续（避免每次请求都去试）。"""
    _env(monkeypatch, token=_jwt(int(time.time()) + 10), refresh="")
    assert xunleipan._needs_refresh() is False


def test_auto_refresh_skips_when_not_expiring(monkeypatch):
    _env(monkeypatch, token=_jwt(int(time.time()) + 7200), refresh="r")
    out = xunleipan.auto_refresh()
    assert out["ok"] and out["skipped"] is True
