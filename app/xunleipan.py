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

import logging
from urllib.parse import quote

import httpx

from . import settings as settings_mod

log = logging.getLogger("x-drive.xunleipan")

API = "https://api-pan.xunlei.com"
# captcha 签发端点（在 xluser 账号服务上，不在 pan 上；实测 captcha/image 那种
# 图片验证码不需要——写操作走这个 shield 端点直接签发）
SHIELD_API = "https://xluser-ssl.xunlei.com"
_TIMEOUT = 15.0


class PanError(RuntimeError):
    """官方 API 返回错误或响应不合契约。"""


def configured() -> bool:
    """clientid 与 access_token 都有才算能用。"""
    return bool(_setting("XLPAN_CLIENT_ID") and _setting("XLPAN_TOKEN"))


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


def list_files(parent_id: str = "", limit: int = 100, *, client=None,
               trashed: bool = False) -> list[dict]:
    """列一个目录（不给 parent_id 就是网盘根）。返回 [{id,name,kind,phase,trashed}]。

    trashed=True 时列的是**回收站**。注意过滤参数是
    `filters={"trashed":{"eq":true}}` —— 传 `trashed=true` 或 `filter=trash`
    都会被**静默忽略**（实测四种参数形态里只有 filters 生效），返回的仍是
    根目录的全量条目：照着它删会误伤活目录。
    """
    params: dict = {"limit": limit}
    if parent_id:
        params["parent_id"] = parent_id
    if trashed:
        params["filters"] = '{"trashed":{"eq":true}}'
    data = _get("/drive/v1/files", params, client=client)
    out = []
    for item in data.get("files") or []:
        out.append({"id": str(item.get("id") or ""), "name": str(item.get("name") or ""),
                    "kind": item.get("kind") or "",
                    "phase": str(item.get("phase") or ""),
                    "trashed": bool(item.get("trashed"))})
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
