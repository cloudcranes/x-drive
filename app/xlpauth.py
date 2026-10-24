"""一键登录：把用户浏览器引到迅雷账号中心授权页，登录后带 code 自动跳回来。

为什么这么做：迅雷的 access_token 12 小时过期，而 refresh_token 是**一次性**的
（续期即轮换）。只靠手工粘贴 credentials 那一段，迟早要在过期时重新抄；
而授权码流程会一次给出**一对** token（access + refresh），配好 refresh_token
之后程序就能自己续，再也不用管登录。

流程：
  1. 点「一键登录」→ 程序生成一个授权会话（随机 state + PKCE verifier），
     存起来，返回授权页 URL（含 redirect_uri 指向本服务的回调页）；
  2. 浏览器打开该 URL → 用户在迅雷页面登录并授权 → 迅雷带 code 跳回
     `GET /xlp/callback?code=…&state=…`；
  3. 回调页校验 state、拿 code 换 token，写进设置，页面提示「已登录」。

安全边界：
  · state 随机且一次性，用过即删（防 CSRF 换 token）；
  · redirect_uri 必须与授权时一致（迅雷侧会校验）；
  · 只在本服务自身监听，不对外暴露任何 token；
  · 回调页不回显 token，只说成功/失败。
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import threading
import time
from urllib.parse import urlencode

log = logging.getLogger("x-drive.xlpauth")

# 授权会话：state → {verifier, created}
_SESSIONS: dict[str, dict] = {}
_lock = threading.Lock()
SESSION_TTL = 600.0        # 十分钟内没登录完就作废


def _pkce_pair() -> tuple[str, str]:
    """(code_verifier, code_challenge)。S256。"""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def create_session() -> dict:
    """开一个授权会话，返回 {state, verifier, challenge}。"""
    state = secrets.token_urlsafe(24)
    verifier, challenge = _pkce_pair()
    with _lock:
        now = time.time()
        for k, v in list(_SESSIONS.items()):
            if now - float(v.get("created") or 0) > SESSION_TTL:
                _SESSIONS.pop(k, None)
        _SESSIONS[state] = {"verifier": verifier, "created": now}
    return {"state": state, "verifier": verifier, "challenge": challenge}


def take_session(state: str) -> str:
    """取出并**销毁**会话，返回 code_verifier（空串表示没有/过期）。

    一次性：取走就没了 —— state 可被重放的窗口因此关掉。
    """
    with _lock:
        item = _SESSIONS.pop((state or "").strip(), None)
    if not item:
        return ""
    if time.time() - float(item.get("created") or 0) > SESSION_TTL:
        return ""
    return str(item.get("verifier") or "")


def auth_url(base_url: str, state: str, challenge: str) -> str:
    """拼迅雷账号中心的授权 URL。

    参数取自公开前端的 authorizePage（i.xunlei.com/center/account/personal/oauth/）：
    client_id / response_type=code / redirect_uri / state / code_challenge。
    """
    redirect = f"{(base_url or '').rstrip('/')}/xlp/callback"
    params = {
        "client_id": "Xqp0kJBXWhwaTpB6",
        "response_type": "code",
        "redirect_uri": redirect,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": "profile offline pan sso user",
    }
    return "https://i.xunlei.com/center/account/personal/oauth/?" + urlencode(params)
