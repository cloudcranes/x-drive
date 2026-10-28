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
import os
import re
import time
from urllib.parse import quote

import httpx

from . import settings as settings_mod
from . import xunleipan_sign

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


def apply_credentials(text: str, kind: str = "") -> dict:
    """解析并写入凭据，返回 {saved: [...], error: str, missing: [...]}。

    两种输入：
      1. 凭据（整段 JSON / 逐行 / 裸 token）—— 拆成设置项直接写；
      2. **授权码**（地址栏里 code=xxx）—— 先用 code 换出 access+refresh 一对，
         再写。授权码是一次性的，用掉即废（换出来的那对里 refresh 可续期）。

    kind 限定只处理哪一类（"cred" / "device" / "captcha"）—— 三个粘贴框分开提交，
    免得 deviceid 混进凭据里、或者凭据被当成 deviceid。

    missing 列出**还缺什么**：device_id 是必需的（实测读接口不带
    x-device-id 回 400 invalid_argument），而它不在 credentials 那一项里，
    只粘那一项的话程序会用不了 —— 必须明说，不能让人以为填好了。
    """
    if kind == "captcha":
        # captcha 只能手工粘：官方只认浏览器签的那枚（带设备签名），
        # 程序自己签的会被判「no client info found」。所以这里不做任何解析，
        # 只认 ck0. 开头的 token（浏览器 localStorage 里那枚）。
        raw = (text or "").strip()
        if not raw:
            return {"saved": [], "missing": [], "error": "没填 captcha"}
        # 粘的是 localStorage 那一项的整段 JSON 的话，取里面的 token
        token = raw
        if raw.startswith("{"):
            try:
                obj = json.loads(raw)
            except (ValueError, TypeError):
                obj = {}
            token = str(obj.get("token") or obj.get("captcha_token") or "")
        if not token.startswith("ck0."):
            return {"saved": [], "missing": [],
                    "error": "认不出 captcha（应以 ck0. 开头；"
                             "浏览器 localStorage 里 captcha_{clientid} 那项的 token）"}
        from . import web as web_mod
        res = web_mod.set_settings({"XLPAN_CAPTCHA_TOKEN": token})
        if not res.get("ok"):
            return {"saved": [], "missing": [],
                    "error": str(res.get("failed") or "写入失败")[:200]}
        return {"saved": ["XLPAN_CAPTCHA_TOKEN"], "error": "", "missing": [],
                "exchanged": "", "client_id": ""}
    parsed = parse_credentials(text)
    if not parsed:
        return {"saved": [], "missing": [], "error": "没认出任何凭据（要 access_token / refresh_token / 整段 JSON / 授权码）"}
    from . import web as web_mod
    code = parsed.pop("XLPAN_AUTH_CODE", "")
    if kind == "device":
        # 只处理 deviceid：别把误粘进来的 token 也写了
        dev = parsed.pop("XLPAN_DEVICE_ID", "")
        if not dev:
            return {"saved": [], "missing": [], "error": "没认出 deviceid（形如 wdi10.…）"}
        parsed = {"XLPAN_DEVICE_ID": dev} if dev else {}
    elif kind == "cred":
        parsed.pop("XLPAN_DEVICE_ID", None)      # 凭据框不碰 deviceid
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
    if not saved:
        return {"saved": [], "missing": [], "error": "没认出可填的内容"}
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


def _headers(device_id: str = "", captcha: str = "") -> dict:
    token = _setting("XLPAN_TOKEN").strip()
    if " " not in token:                         # 只给了 access_token 就补 Bearer
        token = f"Bearer {token}"
    out = {"Authorization": token,
           "clientid": _setting("XLPAN_CLIENT_ID").strip(),
           "content-type": "application/json"}
    device = device_id or _device_id()
    if device:
        out["x-device-id"] = device
    cap = captcha or _setting("XLPAN_CAPTCHA_TOKEN").strip()
    if cap:
        out["x-captcha-token"] = cap
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
        if res.status_code != 200 and "captcha" in (res.text or ""):
            # 被判 captcha_invalid：走一遍「设备证明 + 签名 + 轮换链」再打一次。
            #
            # 关键（2026-10-24 实测）：官方现在**读接口也要 captcha**，而且
            # 必须是「带设备签名」的那枚 —— 浏览器签的是三段
            # ck0.<470>.<290>，而只回传 client_id/device_id 签发出来的是
            # 单段 ck0.<260>，服务端不认（no client info found）。
            # 所以这里先换一枚 deviceid（官方那条纯 HTTP 设备证明路径），
            # 再用它算 captcha_sign 去签 captcha。
            device = ""
            try:
                device = xunleipan_sign.device_proof(client=client)
            except Exception as exc:              # noqa: BLE001 换不到就退回旧 deviceid
                log.info("换 deviceid 失败（用设置里的）：%s", exc)
                device = _setting("XLPAN_DEVICE_ID").strip()
            dev32 = xunleipan_sign.device_id32(device) or _device_id()
            meta, _raw, _ts = xunleipan_sign.build_captcha_meta(dev32)
            token, _exp = sign_captcha(f"GET:{path}", previous="", meta=meta,
                                       device_id=dev32, client=client)
            res = client.get(url, params=params,
                             headers={**_headers(dev32, token), "x-device-id": dev32})
        if res.status_code != 200:
            text = res.text[:160]
            if "captcha" in text:
                # 两种失败，处置不同：
                #   · no client info found —— captcha 不带设备签名，服务端不认。
                #     程序已经会自动换 deviceid 并签名；到这里还是失败，说明
                #     官方换了策略或设备证明这条路不通 —— 可以从浏览器
                #     localStorage 的 captcha_{clientid} 里手工取一枚填进设置。
                #   · 其它 —— 轮换链对不上，重新签一次通常就好了。
                detail = _captcha_detail(res.text)
                if "client info" in detail:
                    raise PanError(
                        "迅雷 API 拒绝了这次 captcha（no client info found）："
                        "这枚 captcha 不带设备签名，服务端不认。"
                        "程序已自动重签过；仍失败说明官方改了什么，"
                        "可以从浏览器 localStorage 的 captcha_{clientid} 里"
                        "手工取一枚填进设置"
                        f"（原始原因：{detail}）")
                raise PanError(
                    f"迅雷 API 仍要求验证码 token（captcha_invalid）："
                    f"轮换链对不上，需要从浏览器重新取一次 captcha：{text}")
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


def _captcha_detail(text: str) -> str:
    """从 400 响应里挑出服务端给的原因（error_details[0].detail）。

    迅雷有两种 captcha 失败，处置方式完全不同：
      · captcha_token is empty  —— 这次没带上 captcha
      · no client info found    —— captcha 签发成功但**没有设备签名**，
        服务端认不出这是哪台设备发出来的。这种不是「重签一次」能解决的。
    """
    try:
        data = json.loads(text[:2000])
    except (ValueError, TypeError):
        return ""
    for item in (data.get("error_details") or []):
        detail = str(item.get("detail") or "").strip()
        if detail:
            return detail
    return ""


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


# 续期失败后的冷却：refresh_token 失效时**每次请求**都会白跑一次续期
# （发请求 + 拿 400 + 抛异常），而结果都一样 —— 用户没重新授权之前，
# 再试一百次也一样。这个冷却把无谓的请求压下来。
#
# 冷却期过后仍会重试：万一是临时的网络故障，过一会儿自己就好了。
_REFRESH_FAILED_AT = 0.0
REFRESH_COOLDOWN = float(os.getenv("XLPAN_REFRESH_COOLDOWN", "900") or 900)


def _setting_float(key: str, fallback: float) -> float:
    """读一个浮点设置项，读不出来就用默认值。"""
    try:
        value = settings_mod.hot(key, fallback, float)
        return float(value)
    except (TypeError, ValueError):
        return float(fallback)


def _refresh_failed_recently() -> bool:
    return time.time() - _REFRESH_FAILED_AT < REFRESH_COOLDOWN


# 主动续期的检查间隔：多久看一次「access_token 快过期了」。
#
# 为什么要主动续（而不是等用到才续）：
# 续期只在**用到官方 API** 时才触发（_get 里调 auto_refresh）。而直链走的是
# CD2 的 gRPC、云添加落地判定走的是 CD2 目录指纹 —— 官方 API 可能**一整天
# 都没人碰**。等到真要用（取直链）时才续，那时 access_token 早就过期了。
#
# 实测踩过（2026-10-25）：access_token 10-25 09:18 过期，直到 19:49 才
# 第一次触发续期 —— 而 refresh_token 那时已经被**浏览器**轮换掉了
# （服务端轮换后旧值作废），于是第一次续期就 invalid_grant。
#
# 12 小时的窗口，每 30 分钟看一次足够：能在过期前 11.5 小时就续上，
# 留出充足余量，也不会给服务端添麻烦。
REFRESH_CHECK_INTERVAL = float(os.getenv("XLPAN_REFRESH_INTERVAL", "1800") or 1800)
# 提前多久续：过期前 1 小时动手，留足「续期请求慢/失败再重试」的余地
REFRESH_LEAD = float(os.getenv("XLPAN_REFRESH_LEAD", "3600") or 3600)


def refresh_loop(interval: float | None = None, lead: float | None = None,
                 sleep=time.sleep) -> None:
    """定时主动续期 access_token。

    在过期前 `lead` 秒动手续，不等「有人用到官方 API」才续 ——
    那时候往往已经晚了（refresh_token 可能已被别的客户端轮换掉）。

    续期失败不致命：记日志、发通知（去重），等下一轮再试。
    连续失败很久就在日志里点明「凭据可能已被作废，请重新抄一次」。
    """
    # 节奏可以调（设置面板 → 存储 → 迅雷凭据提前续期 / 续期检查间隔）
    every = float(interval if interval is not None
                  else _setting_float("XLPAN_REFRESH_INTERVAL", REFRESH_CHECK_INTERVAL))
    margin = float(lead if lead is not None
                    else _setting_float("XLPAN_REFRESH_LEAD", REFRESH_LEAD))
    failed_rounds = 0
    while True:
        try:
            if _needs_refresh(margin):
                log.info("迅雷凭据临近过期（margin %d 秒），主动续期", int(margin))
                try:
                    out = auto_refresh()
                    if out.get("skipped"):
                        # auto_refresh 内部用的是**默认** margin（300 秒），
                        # 比循环的 margin 严 —— 所以循环认为「该续」而它认为
                        # 「还早」是正常的。日志要说清是哪个判据。
                        log.info("主动续期未执行：%s（循环 margin %d 秒，"
                                 "auto_refresh 判定用的是 300 秒）",
                                 out.get("reason") or "", int(margin))
                    else:
                        failed_rounds = 0
                        log.info("主动续期成功，新 token 有效期 %s 秒",
                                 out.get("expires_in", 0))
                except Exception as exc:            # noqa: BLE001 续期失败不致命
                    failed_rounds += 1
                    log.warning("主动续期失败（第 %d 次）：%s", failed_rounds, exc)
                    if failed_rounds >= 3:
                        # 连着几轮都续不上：多半不是网络抖动，是凭据真的废了
                        # （被别的客户端轮换掉了）—— 得说清楚，让人去处理
                        log.error("迅雷凭据连续 %d 次续不上，多半已被作废"
                                  "（浏览器登录同一账号会轮换 refresh_token）"
                                  " —— 请到「设置 → 存储 → 迅雷登录」重新抄一次凭据",
                                  failed_rounds)
        except Exception as exc:                    # noqa: BLE001 循环本身不能死
            log.warning("主动续期轮询出错：%s", exc)
        sleep(every)


def auto_refresh(*, client=None) -> dict:
    """过期自动续期，并把新 token 写回设置。返回续期结果（没过期则 skipped=True）。

    续期失败会**发通知**：静默降级等于哑了——12 小时后所有直链/回收站/云端
    操作集体失败，用户可能几天后才发现。修好（续期成功）后清掉提醒去重，
    下次真失效时还能再提醒。
    """
    global _REFRESH_FAILED_AT
    if not _needs_refresh():
        return {"ok": True, "skipped": True, "reason": "access_token 未临近过期"}
    if _refresh_failed_recently():
        # 刚续期失败过，冷却期内不再试 —— 每次请求都白跑一次续期没有意义
        return {"ok": False, "skipped": True,
                "reason": f"上次续期失败，冷却中（{int(REFRESH_COOLDOWN)} 秒）"}
    try:
        out = refresh_access_token(client=client)
    except Exception as exc:                    # noqa: BLE001
        _REFRESH_FAILED_AT = time.time()
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
    _REFRESH_FAILED_AT = 0.0          # 续期成功：清掉冷却
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
                 device_id: str = "", client=None) -> tuple[str, int]:
    """签发一个 captcha token（写操作的风控凭据）。返回 (token, expires_in)。

    契约来自公开前端 chunk `service-captcha`：
        POST {shield}/v1/shield/captcha/init
        body: {client_id, action, device_id, captcha_token, meta}

    **关键**：meta 里必须有 `captcha_sign` —— 服务端要校验这个风控签名，
    少了它直接 400 `invalid captcha_sign`；签出来的 captcha 也不带设备
    签名，服务端会回 `no client info found`（那枚 captcha 根本不能用）。

    签名的算法与设备证明在 `xunleipan_sign` 里（从官方 chunk 反推的纯
    Python 版，与官方 JS 输出逐字节一致）。

    `device_id` 必须是**签名时用的那一枚**（服务端会核对 meta 里的签名
    与 body 里的 device_id 对不对得上）—— 传空则用设置里的。

    `captcha_token` 要回传**上一次用过的** token（首次传空串）——
    服务端按轮换链校验。
    """
    own = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=_TIMEOUT)
    if not previous:
        # 链的当前节点：上一次签发/用过的那个（存在设置里）
        previous = _setting("XLPAN_CAPTCHA_TOKEN").strip()
    device = device_id or _device_id()
    if not meta or "captcha_sign" not in meta:
        # 没给签名就自己算一份（设备 id 用设置里的）
        meta, _raw, _ts = xunleipan_sign.build_captcha_meta(device)
    body = {"client_id": _setting("XLPAN_CLIENT_ID").strip() or OAUTH_CLIENT_ID,
            "action": action,
            "device_id": device,
            "captcha_token": previous or "",
            "meta": meta or {}}
    try:
        res = client.post(f"{SHIELD_API}/v1/shield/captcha/init",
                          headers={**_headers(), "content-type": "application/json"},
                          json=body)
        if res.status_code != 200:
            raise PanError(f"签发 captcha 失败：{res.status_code} {res.text[:160]}")
        data = res.json() or {}
    except (httpx.HTTPError, ValueError) as exc:
        raise PanError(f"签发 captcha 请求失败：{exc}") from exc
    finally:
        if own:
            client.close()
    token = str(data.get("captcha_token") or "")
    if not token:
        raise PanError("签发 captcha 没拿到 token")
    _remember_captcha(token)
    return token, int(data.get("expires_in") or 0)


def _remember_captcha(token: str) -> None:
    """把**刚签发**的 captcha 记下来：它就是轮换链的当前节点。"""
    if not token:
        return
    try:
        from . import web as web_mod
        web_mod.set_settings({"XLPAN_CAPTCHA_TOKEN": token})
    except Exception as exc:                    # noqa: BLE001 记不住不该阻断主流程
        log.warning("记下 captcha 失败：%s", exc)


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


def offline_tasks(*, client=None) -> list[dict]:
    """列迅雷云盘的**离线下载任务**（/drive/v1/tasks）。

    云添加投给迅雷之后，迅雷那边的任务状态只有这里查得到 —— CD2 的
    ListAllOfflineFiles 对迅雷云盘不支持（实测 "cloud account not found"）。

    返回 [{id, name, phase, progress, message, error_detail, url}]。
    phase 是 PHASE_TYPE_*：RUNNING / SUCCESS / ERROR 等；message 是迅雷
    给的人话（比如「解析失败，请重试或更换链接」）。
    """
    data = _get("/drive/v1/tasks", client=client)
    out = []
    for t in data.get("tasks") or []:
        if t.get("type") != "offline":
            continue
        params = t.get("params") or {}
        out.append({
            "id": str(t.get("id") or ""),
            "name": str(t.get("name") or ""),
            "phase": str(t.get("phase") or ""),
            "progress": t.get("progress"),
            "message": str(t.get("message") or ""),
            "error_detail": str(params.get("error_detail") or ""),
            "url": str(params.get("url") or ""),
        })
    return out


def offline_task_by_hash(info_hash: str, *, client=None) -> dict | None:
    """按 infohash 找对应的离线任务（找不到返回 None）。"""
    want = (info_hash or "").strip().lower()
    if not want:
        return None
    for t in offline_tasks(client=client):
        url = t.get("url") or ""
        if want in url.lower():
            return t
    return None


def info_hash_of(url: str) -> str:
    """从 magnet 里取 infohash（小写 hex）。

    app/xunlei.py 里也有一份（那是本机迅雷面板的），这里给云盘那条链用 ——
    两边都是「从 magnet 里抠 btih」，但云盘模块不该依赖本机面板模块。
    """
    m = re.search(r"xt=urn:btih:([0-9a-fA-F]{40})", url or "")
    return m.group(1).lower() if m else ""


def list_files(parent_id: str = "", limit: int = 100, *, client=None,
               trashed: bool = False) -> list[dict]:
    """列一个目录（不给 parent_id 就是网盘根）。返回 [{id,name,kind,phase,trashed}]。

    trashed=True 时只返回**回收站**里的条目。

    注意三处坑（都是实测踩出来的）：
      · 过滤参数是 `filters={"trashed":{"eq":true}}`；传 `trashed=true` 或
        `filter=trash` 会被**静默忽略**，返回的仍是根目录全量；
      · 即便用了 filters，服务端仍会把**活目录**条目一起返回（只是带
        trashed=false 标志）。所以必须在客户端再按标志过滤一遍 ——
        照着未过滤的列表删，会把 X-Drive 这种活目录误移进回收站。
      · **反过来也一样**：默认列表会把 trashed=true 的条目一起吐回来。
        这些是「删了又同名重建」留下的墓碑（实测：自检每 180 秒建删一次
        探针目录，一个月堆了 478 条同名 `_xdrive_healthcheck`）。网页里
        看不到它们，但 API 每次都返回 —— 不过滤的话调用方会以为目录里塞满了
        垃圾。所以 trashed=False 时也得按标志过滤掉。
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
        if trashed != entry["trashed"]:
            continue                       # 要回收站就跳过活的，要活目录就跳过墓碑
        out.append(entry)
    return out


def file_info(file_id: str, *, client=None) -> dict:
    """单文件详情：直链与各档转码流都在这里。

    返回 {name, link, medias: [{name, url}]}。

    **link 用加速档（medias[].link.url），不用 web_content_link**：
    同一份文件，两个 URL 的速度差 **20 倍**（实测 2026-10-26，MIMK-186）：
      · web_content_link（普通）→ 0.08 MB/s（读 256KB 要 3.3 秒）
      · medias[].link（加速）  → 1.57 MB/s（0.2 秒）
    参数差别（实测）：
      · spr  = flow → **vip**（走 SVIP 加速通道 —— 这就是迅雷 App 快的根因）
      · ms/th = 5242880/5242880 → **10000000/1000000**（分片大小与超时）
      · vb   = 0 → **真实码率**；vc = — → **h264**
    加速档还自带 `token_type: TOKEN_TYPE_ACCELERATION` 与 expire，
    说明它是**专门的加速令牌**，不是普通下载链接改个参数。

    加速档取不到时（老文件 / 没开）退回 web_content_link —— 不能因此
    就没直链。
    """
    data = _get(f"/drive/v1/files/{quote(str(file_id), safe='')}", client=client)
    plain = str(data.get("web_content_link") or "")
    medias = []
    accel = ""
    for item in data.get("medias") or []:
        name = str(item.get("media_name") or "")
        item_link = item.get("link") or {}
        url = str(item_link.get("url") or "")
        # 「原始画质」那条：优先用**它自己的** link（带加速参数），
        # 没有才退回 web_content_link。
        if not url:
            url = plain
        if url:
            medias.append({"name": name, "url": url})
            if name == "原始画质" and url and url != plain:
                accel = url
    link = accel or plain
    # 加速档自带的 token（TOKEN_TYPE_ACCELERATION）：实测带不带都能下，
    # 但带上更接近迅雷 App 的行为，也更可能在服务端命中更快的通道。
    accel_token = ""
    accel_token_type = ""
    for item in data.get("medias") or []:
        if str(item.get("media_name") or "") == "原始画质":
            item_link = item.get("link") or {}
            accel_token = str(item_link.get("token") or "")
            accel_token_type = str(item_link.get("token_type") or "")
            break
    return {"name": str(data.get("name") or ""), "link": link, "medias": medias,
            "size": int(data.get("size") or 0),
            # 加速档单独给出来：调用方要能判断「这条是不是加速的」
            "accelerated": bool(accel),
            "plain_link": plain,
            "accel_token": accel_token,
            "accel_token_type": accel_token_type,
            "expire": str(((data.get("medias") or [{}])[0].get("link") or {}).get("expire") or "")}


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
