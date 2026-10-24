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


# ---------------------------------------------------------------- 轮换链加固

def test_auto_refresh_writes_back_and_verifies(monkeypatch):
    """续期后的新 refresh_token 必须真的写回设置（旧值已被消费，写不回就断了链）。"""
    _env(monkeypatch, token=_jwt(int(time.time()) + 10), refresh="old-rt")
    store = {}

    def fake_refresh(client=None):
        return {"access_token": "new-at", "refresh_token": "new-rt", "expires_in": 100}

    monkeypatch.setattr(xunleipan, "refresh_access_token", fake_refresh)

    import app.web as web_mod
    monkeypatch.setattr(web_mod, "set_settings",
                        lambda u: store.update(u) or {"ok": True, "saved": list(u)})
    monkeypatch.setattr(web_mod, "get_setting",
                        lambda k, d=None: store.get(k, d if d is not None else ""))

    out = xunleipan.auto_refresh()
    assert out["ok"] and out["rotated"] is True
    assert store["XLPAN_TOKEN"] == "new-at"
    assert store["XLPAN_REFRESH_TOKEN"] == "new-rt"


def test_auto_refresh_reports_when_writeback_failed(monkeypatch):
    """写回没生效必须报错：旧 refresh_token 已被消费，静默失败等于断链。"""
    _env(monkeypatch, token=_jwt(int(time.time()) + 10), refresh="old-rt")
    monkeypatch.setattr(xunleipan, "refresh_access_token",
                        lambda client=None: {"access_token": "new-at",
                                             "refresh_token": "new-rt", "expires_in": 1})
    import app.web as web_mod
    monkeypatch.setattr(web_mod, "set_settings", lambda u: {"ok": True, "saved": []})
    monkeypatch.setattr(web_mod, "get_setting", lambda k, d=None: "old-rt")

    with pytest.raises(xunleipan.PanError) as err:
        xunleipan.auto_refresh()
    assert "refresh_token" in str(err.value)


def test_auto_refresh_alerts_on_failure(monkeypatch):
    """续期失败要发通知：静默降级等于哑了。"""
    _env(monkeypatch, token=_jwt(int(time.time()) + 10), refresh="bad-rt")
    calls = []

    def boom(client=None):
        raise xunleipan.PanError("invalid_grant")

    monkeypatch.setattr(xunleipan, "refresh_access_token", boom)
    import app.notify as notify_mod
    monkeypatch.setattr(notify_mod, "xlp_auth_broken",
                        lambda reason: calls.append(reason) or True)
    with pytest.raises(xunleipan.PanError):
        xunleipan.auto_refresh()
    assert calls and "invalid_grant" in calls[0]
