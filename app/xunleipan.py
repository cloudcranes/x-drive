"""迅雷云盘官方 API（api-pan.xunlei.com）的最小客户端：取文件直链。

用途：CD2 的 static / GetDownloadUrlPath 只能给**内网**直链（指向
host.docker.internal:19798）；要拿到浏览器/播放器/IDM 直接能用、外网可播的
链接，得走迅雷官方 Web API —— 也就是 greasyfork 脚本「迅雷云盘」
（ThunderPanLink）用的那套接口：

    GET /drive/v1/files?limit=100&parent_id={id}          列目录
    GET /drive/v1/files/{id}                              单文件详情

    → web_content_link：原始画质直链（脚本的「显示文件链接」用的就是它）
    → medias[]：云播转码的各档画质 {media_name, link.url}（可导出 m3u）

鉴权同脚本：`Authorization: {token_type} {access_token}` + `clientid`，
从浏览器 localStorage 的 `credentials_{clientid}` 里抄出来填到设置里。
直链**有有效期**（脚本说明里写了「链接都是时间限制，过期后重新获取」），
所以本模块不做缓存，每次现取。

只读接口：列目录、取详情。不动云盘内容。
"""
from __future__ import annotations

import json
import logging
import re
import time
from urllib.parse import quote

import httpx

from . import settings as settings_mod

log = logging.getLogger("x-drive.xunleipan")

API = "https://api-pan.xunlei.com"
# captcha 签发端点（在 xluser 账号服务上，不在 pan 上；实测 captcha/image 那种
# 图片验证码不需要——写操作走这个 shield 端点直接签发）
SHIELD_API = "https://xluser-ssl.xunlei.com"
# token 续期端点：refresh_token → 新 access_token（实测 200，不需要 client_secret）
AUTH_API = "https://xluser-ssl.xunlei.com/v1/auth/token"
# 账号中心授权页：用户在这里登录，授权后带 code 跳回我们的回调地址
OAUTH_PAGE = "https://i.xunlei.com/center/account/personal/oauth/"
# 公开前端里硬编码的 client_secret（bundles 里明文可见）。授权码换 token 要用。
OAUTH_CLIENT_SECRET = "Xqp0kC5LIQcyGSdKPYSzshgLKHxo"
OAUTH_CLIENT_ID = "Xqp0kJBXWhwaTpB6"
_TIMEOUT = 15.0


class PanError(RuntimeError):
    """官方 API 返回错误或响应不合契约。"""


def configured() -> bool:
    """clientid 与 access_token 都有才算能用。"""
    return bool(_setting("XLPAN_CLIENT_ID") and _setting("XLPAN_TOKEN"))


def refresh_access_token(refresh_token: str = "", *, client=None) -> dict:
    """用 refresh_token 换新的 access_token。

    端点：POST {AUTH_API}，body {grant_type: "refresh_token", refresh_token,
    client_id}。**不需要 client_secret**（实测：带与不带都是 200 —— 前端那份
    secret 是明文硬编码的，服务端对带 secret 的 refresh 并不做额外校验）。

    账号密码直登**不支持**：同一个端点用 grant_type=password 会回
    `currently not supported`（实测）。所以自动化登录只能靠 refresh_token：
    首次从浏览器抄一次，之后就能自己续。

    返回 {access_token, refresh_token, expires_in}；refresh_token 变了就一并返回
    （服务端会轮换，旧的作废）。
    """
    rt = (refresh_token or _setting("XLPAN_REFRESH_TOKEN")).strip()
    if not rt:
        raise PanError("缺少 refresh_token")
    body = {"grant_type": "refresh_token", "refresh_token": rt,
            "client_id": _setting("XLPAN_CLIENT_ID").strip() or "Xqp0kJBXWhwaTpB6"}
    own = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=_TIMEOUT)
    try:
        res = client.post(AUTH_API, json=body,
                          headers={"content-type": "application/json",
                                   "clientid": body["client_id"]})
    except httpx.HTTPError as exc:
        raise PanError(f"续期请求失败：{exc}") from exc
    finally:
        if own:
            client.close()
    if res.status_code != 200:
        raise PanError(f"续期失败：{res.status_code} {(res.text or '')[:140]}")
    data = res.json() or {}
    token = str(data.get("access_token") or "")
    if not token:
        raise PanError("续期响应里没有 access_token")
    out = {"access_token": token, "expires_in": int(data.get("expires_in") or 0)}
    new_rt = str(data.get("refresh_token") or "")
    if new_rt and new_rt != rt:
        out["refresh_token"] = new_rt       # 服务端轮换了：必须一并存
    return out


def _setting(key: str) -> str:
    """网页设置 > 环境变量 > 空。

    与 storage.settings() 同一个入口：bot、离线脚本、`--check` 这些进程里
    settings 没绑定也要能读到网页保存的值（缓存在 jobs.db 的 state 表）。
    """
    import os
    try:
        from . import storage as storage_mod
        value = storage_mod.settings().get(key)
        if value:
            return str(value)
    except Exception:                            # noqa: BLE001
        pass
    try:
        inst = settings_mod.bound()
        if inst is not None and inst.get(key):
            return str(inst.get(key))
    except Exception:                            # noqa: BLE001
        pass
    return str(os.getenv(key) or "")


def _device_id() -> str:
    """x-device-id：照脚本取 deviceid 点后前 32 字符（localStorage 里是 wdi10.<hex>）。"""
    raw = _setting("XLPAN_DEVICE_ID").strip()
    if not raw:
        return ""
    return raw.split(".", 1)[-1][:32] if "." in raw else raw[:32]


def parse_credentials(text: str) -> dict:
    """从一段文本里认出各种凭据，拆成设置项。

    浏览器 localStorage 的 `credentials_{clientid}` 那一项就是一段 JSON：
        {"token_type":"Bearer","access_token":"eyJ...","refresh_token":"a1....",
         "expires_in":43200,"sub":"814926528","user_id":"814926528"}
    粘一整段进来就能一次填齐，不必逐个框复制。

    也接受只粘其中一两个值（access_token / refresh_token 各自一行）。

    **device_id 是必需的**（实测：不带 x-device-id 的读接口回
    400 invalid_argument），但它**不在** credentials 那一项里 —— 在
    localStorage 的另一个键 `deviceid`，值形如 `wdi10.<64位hex>`。所以这段
    文本里也要能认出来（`deviceid: wdi10.…` 或裸的 `wdi10.…`）。

    返回 {设置键: 值}，认不出来的键不出现。
    """
    out: dict[str, str] = {}
    raw = (text or "").strip()
    if not raw:
        return out
    # 1) 整段 JSON
    data = None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        data = None
    if isinstance(data, dict):
        at = str(data.get("access_token") or "").strip()
        rt = str(data.get("refresh_token") or "").strip()
        if at:
            out["XLPAN_TOKEN"] = at
        if rt:
            out["XLPAN_REFRESH_TOKEN"] = rt
        # clientid：JWT 的 aud 字段就是它
        cid = _client_id_from_jwt(at)
        if cid:
            out["XLPAN_CLIENT_ID"] = cid
        # deviceid：可能混在同一个 JSON 里（有的导出工具会合并）
        dev = str(data.get("deviceid") or data.get("device_id") or "").strip()
        if dev:
            out["XLPAN_DEVICE_ID"] = dev
        return out
    # 2) 逐行找 key=value 或裸 token
    for line in raw.splitlines():
        line = line.strip().strip(",").strip('"')
        if not line:
            continue
        low = line.lower()
        if low.startswith("access_token"):
            out["XLPAN_TOKEN"] = line.split(":", 1)[-1].strip().strip('"')
        elif low.startswith("refresh_token"):
            out["XLPAN_REFRESH_TOKEN"] = line.split(":", 1)[-1].strip().strip('"')
        elif low.startswith("clientid"):
            out["XLPAN_CLIENT_ID"] = line.split(":", 1)[-1].strip().strip('"')
        elif low.startswith("deviceid") or low.startswith("device_id"):
            out["XLPAN_DEVICE_ID"] = line.split(":", 1)[-1].strip().strip('"')
    # 3) 裸的 JWT / a1.… / wdi10.… 也认
    if "XLPAN_TOKEN" not in out:
        for token in raw.split():
            if token.count(".") == 2 and token.startswith("eyJ"):
                out["XLPAN_TOKEN"] = token.strip('"')
                cid = _client_id_from_jwt(token)
                if cid:
                    out["XLPAN_CLIENT_ID"] = cid
                break
    if "XLPAN_REFRESH_TOKEN" not in out:
        for token in raw.split():
            if token.startswith("a1."):
                out["XLPAN_REFRESH_TOKEN"] = token.strip('"')
                break
    if "XLPAN_DEVICE_ID" not in out:
        for token in raw.split():
            if token.startswith("wdi10."):
                out["XLPAN_DEVICE_ID"] = token.strip('"')
                break
    # 4) 授权码：地址栏里 code=xxx 那一段（用户自己从浏览器授权页复制）
    if "XLPAN_AUTH_CODE" not in out:
        m = re.search(r"(?:^|[?&])code=([A-Za-z0-9._~-]+)", raw)
        if m:
            out["XLPAN_AUTH_CODE"] = m.group(1)
    return out


def _client_id_from_jwt(token: str) -> str:
    """从 JWT 的 aud 取出 clientid（迅雷的 clientid 就是 aud）。"""
    if not token or "." not in token:
        return ""
    import base64
    try:
        seg = token.split(".")[1]
        pad = "=" * (-len(seg) % 4)
        aud = (json.loads(base64.urlsafe_b64decode(seg + pad)) or {}).get("aud") or ""
        return str(aud)
    except Exception:                            # noqa: BLE001
        return ""


def apply_credentials(text: str) -> dict:
    """解析并写入凭据，返回 {saved: [...], error: str, missing: [...]}。

    两种输入：
      1. 凭据（整段 JSON / 逐行 / 裸 token）—— 拆成设置项直接写；
      2. **授权码**（地址栏里 code=xxx）—— 先用 code 换出 access+refresh 一对，
         再写。授权码是一次性的，用掉即废（换出来的那对里 refresh 可续期）。

    missing 列出**还缺什么**：device_id 是必需的（实测读接口不带
    x-device-id 回 400 invalid_argument），而它不在 credentials 那一项里，
    只粘那一项的话程序会用不了 —— 必须明说，不能让人以为填好了。
    """
    parsed = parse_credentials(text)
    if not parsed:
        return {"saved": [], "missing": [], "error": "没认出任何凭据（要 access_token / refresh_token / 整段 JSON / 授权码）"}
    from . import web as web_mod
    code = parsed.pop("XLPAN_AUTH_CODE", "")
    saved = dict(parsed)
    exchanged = ""
    if code:
        # 授权码 → 一对 token（这是唯一能一次拿到 refresh_token 的通道）
        try:
            got = grant_authorization_code(code)
        except Exception as exc:                # noqa: BLE001
            return {"saved": [], "missing": [], "error": f"授权码换 token 失败：{str(exc)[:200]}"}
        if got.get("access_token"):
            saved["XLPAN_TOKEN"] = got["access_token"]
        if got.get("refresh_token"):
            saved["XLPAN_REFRESH_TOKEN"] = got["refresh_token"]
        exchanged = "已用授权码换出 access_token 与 refresh_token"
    res = web_mod.set_settings(saved)
    if not res.get("ok"):
        return {"saved": [], "missing": [], "error": str(res.get("failed") or "写入失败")[:200]}
    missing = []
    for key, label in (("XLPAN_CLIENT_ID", "clientid"),
                       ("XLPAN_TOKEN", "access_token"),
                       ("XLPAN_DEVICE_ID", "deviceid")):
        if not str(web_mod.get_setting(key) or "").strip():
            missing.append(label)
    return {"saved": list(res.get("saved") or []), "error": "", "missing": missing,
            "exchanged": exchanged,
            "client_id": parsed.get("XLPAN_CLIENT_ID", "")}


def _headers() -> dict:
    token = _setting("XLPAN_TOKEN").strip()
    if " " not in token:                         # 只给了 access_token 就补 Bearer
        token = f"Bearer {token}"
    out = {"Authorization": token,
           "clientid": _setting("XLPAN_CLIENT_ID").strip(),
           "content-type": "application/json"}
    device = _device_id()
    if device:
        out["x-device-id"] = device
    captcha = _setting("XLPAN_CAPTCHA_TOKEN").strip()
    if captcha:
        out["x-captcha-token"] = captcha
    return out


def _get(path: str, params: dict | None = None, *, client=None) -> dict:
    url = f"{API}{path}"
    own = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=_TIMEOUT)
    try:
        # 快过期就先续期：access_token 只活 12 小时（实测 JWT exp），
        # 续期靠 refresh_token，不需要人工再从浏览器抄一遍
        try:
            auto_refresh(client=client)
        except PanError as exc:
            log.info("自动续期未成功（继续用旧 token）：%s", exc)
        res = client.get(url, params=params, headers=_headers())
        if res.status_code != 200:
            text = res.text[:160]
            if "captcha" in text:
                raise PanError(
                    f"迅雷 API 要求验证码 token（captcha_invalid）："
                    f"把浏览器 localStorage 里 captcha 开头那项的 token 填到"
                    f"设置「迅雷 x-captcha-token」：{text}")
            raise PanError(f"迅雷云盘 API {res.status_code}：{text}")
        data = res.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise PanError(f"迅雷云盘 API 请求失败：{exc}") from exc
    finally:
        if own:
            client.close()
    if not isinstance(data, dict):
        raise PanError("迅雷云盘 API 返回不是 JSON 对象")
    err = data.get("error") or data.get("message") or ""
    if err and not data.get("files") and "id" not in data:
        raise PanError(f"迅雷云盘 API 错误：{str(err)[:120]}")
    return data


def _token_expiry(token: str) -> int:
    """从 JWT 里读 exp（秒）。读不出来给 0（当作不知道，保守处理）。"""
    if not token or "." not in token:
        return 0
    import base64
    import json as _json
    try:
        seg = token.split(".")[1]
        pad = "=" * (-len(seg) % 4)
        return int((_json.loads(base64.urlsafe_b64decode(seg + pad)) or {}).get("exp") or 0)
    except Exception:                           # noqa: BLE001 不是 JWT 就不知道
        return 0


def _needs_refresh(margin: int = 300) -> bool:
    """access_token 是不是快过期了（快到点就换，免得请求中途 401）。"""
    token = _setting("XLPAN_TOKEN").strip()
    if not token or not _setting("XLPAN_REFRESH_TOKEN").strip():
        return False
    exp = _token_expiry(token)
    if not exp:
        return False
    return exp - int(time.time()) < margin


def auto_refresh(*, client=None) -> dict:
    """过期自动续期，并把新 token 写回设置。返回续期结果（没过期则 skipped=True）。

    续期失败会**发通知**：静默降级等于哑了——12 小时后所有直链/回收站/云端
    操作集体失败，用户可能几天后才发现。修好（续期成功）后清掉提醒去重，
    下次真失效时还能再提醒。
    """
    if not _needs_refresh():
        return {"ok": True, "skipped": True, "reason": "access_token 未临近过期"}
    try:
        out = refresh_access_token(client=client)
    except Exception as exc:                    # noqa: BLE001
        try:
            from . import notify
            notify.xlp_auth_broken(str(exc))
        except Exception:                       # noqa: BLE001 通知失败不影响主流程
            pass
        raise
    saved = {}
    if out.get("access_token"):
        saved["XLPAN_TOKEN"] = out["access_token"]
    if out.get("refresh_token"):
        saved["XLPAN_REFRESH_TOKEN"] = out["refresh_token"]
    if saved:
        try:
            from . import web as web_mod
            web_mod.set_settings(saved)
        except Exception as exc:                # noqa: BLE001 写回失败也要报告
            raise PanError(f"续期成功但写回设置失败：{exc}") from exc
        # 回读校验：refresh_token 是一次性的，写回没生效的话下一次续期必然
        # invalid_grant —— 而那时候旧值已经被消费掉了，只能重新从浏览器抄。
        # 所以这里必须确认新值真的在库里。
        try:
            from . import web as web_mod
            now_rt = str(web_mod.get_setting("XLPAN_REFRESH_TOKEN") or "")
            now_tok = str(web_mod.get_setting("XLPAN_TOKEN") or "")
        except Exception as exc:                # noqa: BLE001
            raise PanError(f"续期后回读设置失败：{exc}") from exc
        if out.get("refresh_token") and now_rt != out["refresh_token"]:
            raise PanError(
                "续期拿到了新的 refresh_token，但没写回设置（写回后回读不一致）"
                "—— 旧值已被消费，请从浏览器重新抄一次 refresh_token")
        if out.get("access_token") and now_tok != out["access_token"]:
            raise PanError("续期拿到了新的 access_token，但没写回设置（回读不一致）")
    try:
        from . import notify
        notify.xlp_auth_ok()
    except Exception:                           # noqa: BLE001
        pass
    return {"ok": True, "skipped": False, "expires_in": out.get("expires_in", 0),
            "rotated": bool(out.get("refresh_token"))}


def grant_authorization_code(code: str, redirect_uri: str = "", code_verifier: str = "",
                             *, client=None) -> dict:
    """用授权码换 token（access_token + refresh_token 一对）。

    端点与续期同一个：POST {AUTH_API}，body
    {grant_type: "authorization_code", code, client_id, client_secret,
     redirect_uri, code_verifier?}。

    **这是唯一能一次拿到「一对」token 的通道** —— 授权码流程会同时返回
    access_token 与 refresh_token，而 refresh_token 是**一次性**的
    （续期即轮换），所以只在续期时用它必然断链。

    现状说明（实测）：迅雷的授权页只认**已注册**的 redirect_uri
    （`oia-pan-ssl.xunlei.com` 那个），自定义地址会被判「无效请求」——
    白名单在迅雷服务端，程序改不了。所以全自动登录走不通，只能由用户
    自己从浏览器拿 code 贴进来（见 /api/xlp/code）。

    授权码本身是短时效的一次性凭证：拿到就要用，过期作废。
    """
    if not (code or "").strip():
        raise PanError("缺少授权码")
    body = {"grant_type": "authorization_code",
            "code": code.strip(),
            "client_id": OAUTH_CLIENT_ID,
            "client_secret": OAUTH_CLIENT_SECRET}
    if redirect_uri:
        body["redirect_uri"] = redirect_uri
    if code_verifier:
        body["code_verifier"] = code_verifier
    own = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=_TIMEOUT)
    try:
        res = client.post(AUTH_API, json=body,
                          headers={"content-type": "application/json",
                                   "clientid": OAUTH_CLIENT_ID})
    except httpx.HTTPError as exc:
        raise PanError(f"授权码换 token 失败：{exc}") from exc
    finally:
        if own:
            client.close()
    if res.status_code != 200:
        raise PanError(f"授权码换 token 失败：{res.status_code} {(res.text or '')[:160]}")
    data = res.json() or {}
    token = str(data.get("access_token") or "")
    if not token:
        raise PanError("授权码换 token：响应里没有 access_token")
    out = {"access_token": token, "expires_in": int(data.get("expires_in") or 0)}
    rt = str(data.get("refresh_token") or "")
    if rt:
        out["refresh_token"] = rt
    return out


def sign_captcha(action: str, *, previous: str = "", meta: dict | None = None,
                 client=None) -> tuple[str, int]:
    """签发一个 captcha token（写操作的风控凭据）。返回 (token, expires_in)。

    契约来自公开前端 chunk `service-captcha`：
        POST {shield}/v1/shield/captcha/init
        body: {client_id, action, device_id, captcha_token, meta}

    **关键**：`captcha_token` 要回传**上一次用过的** token（首次传空串）——
    服务端按轮换链校验。实测：回传旧 token 签发出来的才能通过写操作；
    传空串签发的会被判 captcha_invalid（读操作反而都能过，所以容易误判为可用）。
    """
    own = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=_TIMEOUT)
    body = {"client_id": _setting("XLPAN_CLIENT_ID").strip() or "Xqp0kJBXWhwaTpB6",
            "action": action,
            "device_id": _device_id(),
            "captcha_token": previous or "",
            "meta": meta or {}}
    try:
        res = client.post(f"{SHIELD_API}/v1/shield/captcha/init",
                          headers={**_headers(), "content-type": "application/json"},
                          json=body)
        if res.status_code != 200:
            raise PanError(f"签发 captcha 失败：{res.status_code} {res.text[:120]}")
        data = res.json() or {}
    except (httpx.HTTPError, ValueError) as exc:
        raise PanError(f"签发 captcha 请求失败：{exc}") from exc
    finally:
        if own:
            client.close()
    token = str(data.get("captcha_token") or "")
    if not token:
        raise PanError("签发 captcha 没拿到 token")
    return token, int(data.get("expires_in") or 0)


def delete_file(file_id: str, *, client=None) -> str:
    """删一个文件（回收站里的也能删）。返回一句回执。

    **写操作要 captcha**，流程（实测）：
      1. 带当前 token 打一次 → 被判 captcha_invalid（说明该换了）；
      2. 用「回传旧 token」的方式签发新的；
      3. 带新 token 再打一次 → 生效。
    直接拿新签的 token 去打会被拒（轮换链对不上），所以第 1 步不能省。
    """
    own = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=_TIMEOUT)
    fid = str(file_id or "").strip()
    if not fid:
        raise PanError("缺少 file_id")
    url = f"{API}/drive/v1/files/{quote(fid, safe='')}"
    try:
        res = client.delete(url, headers=_headers())
        if res.status_code != 400 or "captcha" not in (res.text or ""):
            return _finish_delete(res)
        old = _setting("XLPAN_CAPTCHA_TOKEN").strip()
        token, _exp = sign_captcha(f"DELETE:/drive/v1/files/{fid}",
                                   previous=old, client=client)
        res = client.delete(url, headers={**_headers(), "x-captcha-token": token})
        if res.status_code == 400 and "captcha" in (res.text or ""):
            raise PanError("captcha 仍被拒：轮换链对不上（浏览器里刷新一次 captcha 再试）")
        return _finish_delete(res)
    finally:
        if own:
            client.close()


def _finish_delete(res) -> str:
    if res.status_code == 200:
        return "已删除"
    j = {}
    try:
        j = res.json() or {}
    except ValueError:
        pass
    err = str(j.get("error") or j.get("error_description") or res.text or "")[:120]
    if res.status_code == 404:
        return "文件不存在"
    raise PanError(f"删除失败：{res.status_code} {err}")


def untrash(file_id: str, *, client=None) -> str:
    """把回收站里的条目恢复回去。

    契约来自公开 bundle：`PATCH /drive/v1/files/{id}/untrash`，带 captcha
    （withCaptcha=true）。写操作同样要 captcha 轮换：先打一次被拒 → 回传旧
    token 签发新的 → 再打一次。
    """
    own = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=_TIMEOUT)
    fid = str(file_id or "").strip()
    if not fid:
        raise PanError("缺少 file_id")
    url = f"{API}/drive/v1/files/{quote(fid, safe='')}/untrash"
    try:
        res = client.patch(url, headers=_headers())
        if res.status_code != 400 or "captcha" not in (res.text or ""):
            return _finish_delete(res)
        old = _setting("XLPAN_CAPTCHA_TOKEN").strip()
        token, _exp = sign_captcha(f"PATCH:/drive/v1/files/{fid}/untrash",
                                   previous=old, client=client)
        res = client.patch(url, headers={**_headers(), "x-captcha-token": token})
        if res.status_code == 400 and "captcha" in (res.text or ""):
            raise PanError("captcha 仍被拒：轮换链对不上（浏览器里刷新一次 captcha 再试）")
        return _finish_delete(res)
    finally:
        if own:
            client.close()


def list_files(parent_id: str = "", limit: int = 100, *, client=None,
               trashed: bool = False) -> list[dict]:
    """列一个目录（不给 parent_id 就是网盘根）。返回 [{id,name,kind,phase,trashed}]。

    trashed=True 时只返回**回收站**里的条目。

    注意两处坑（都是实测踩出来的）：
      · 过滤参数是 `filters={"trashed":{"eq":true}}`；传 `trashed=true` 或
        `filter=trash` 会被**静默忽略**，返回的仍是根目录全量；
      · 即便用了 filters，服务端仍会把**活目录**条目一起返回（只是带
        trashed=false 标志）。所以必须在客户端再按标志过滤一遍 ——
        照着未过滤的列表删，会把 X-Drive 这种活目录误移进回收站。
    """
    params: dict = {"limit": limit}
    if parent_id:
        params["parent_id"] = parent_id
    if trashed:
        params["filters"] = '{"trashed":{"eq":true}}'
    data = _get("/drive/v1/files", params, client=client)
    out = []
    for item in data.get("files") or []:
        entry = {"id": str(item.get("id") or ""), "name": str(item.get("name") or ""),
                 "kind": item.get("kind") or "",
                 "phase": str(item.get("phase") or ""),
                 "trashed": bool(item.get("trashed"))}
        if trashed and not entry["trashed"]:
            continue                       # 活目录条目：不在回收站，跳过
        out.append(entry)
    return out


def file_info(file_id: str, *, client=None) -> dict:
    """单文件详情：直链与各档转码流都在这里。

    返回 {name, link, medias: [{name, url}]}；link 是 web_content_link（原始
    画质）。medias 里 `原始画质` 那条按脚本约定也用 web_content_link。
    """
    data = _get(f"/drive/v1/files/{quote(str(file_id), safe='')}", client=client)
    link = str(data.get("web_content_link") or "")
    medias = []
    for item in data.get("medias") or []:
        name = str(item.get("media_name") or "")
        url = link if name == "原始画质" else str((item.get("link") or {}).get("url") or "")
        if url:
            medias.append({"name": name, "url": url})
    return {"name": str(data.get("name") or ""), "link": link, "medias": medias,
            "size": int(data.get("size") or 0)}


def resolve(path: str, *, client=None) -> str:
    """把 `迅雷云盘/X-Drive/SSIS-555/xx.mp4` 这样的路径解析成 file_id。

    逐级下钻、按名字匹配；找不到返回空串。目录里用第一个同名命中。

    首段是 CD2 的挂载名（官方 API 根下**没有**它——实测根目录直接是
    `我的资源`/`X-Drive`/…），完整路径找不到时自动去掉首段再试一次。
    """
    parts = [p for p in (path or "").strip("/").split("/") if p]
    for candidate in (parts, parts[1:]):
        if not candidate:
            continue
        found = _walk(candidate, client=client)
        if found:
            return found
    return ""


def _walk(parts: list[str], *, client=None) -> str:
    parent = ""
    found = ""
    for part in parts:
        hit = ""
        for item in list_files(parent, client=client):
            if item["name"] == part and not item["trashed"]:
                hit = item["id"]
                break
        if not hit:
            return ""
        found = hit
        parent = hit
    return found


def direct_link(path: str, *, client=None) -> dict:
    """按路径取直链。返回 {name, link, medias}，link 为空串表示取不到。"""
    file_id = resolve(path, client=client)
    if not file_id:
        raise PanError(f"迅雷云盘里找不到 {path}")
    return file_info(file_id, client=client)
