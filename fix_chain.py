import httpx
from app import web, xunleipan

c = httpx.Client(timeout=20.0)
# 1) 用用户给的新 refresh_token 续期
rt = "a1.z906yV8ciGrcsBEY7-6IUZO8xS4ifiXe_GMsUrVdRn33kVRfmCvpdYFHGbc"
r = c.post(xunleipan.AUTH_API,
           json={"grant_type": "refresh_token", "refresh_token": rt,
                 "client_id": "Xqp0kJBXWhwaTpB6"},
           headers={"content-type": "application/json", "clientid": "Xqp0kJBXWhwaTpB6"})
j = r.json()
at, new_rt = j["access_token"], j.get("refresh_token", "")
print("1) 续期 ok | 新 access", len(at), "字符 | 新 refresh", new_rt[:16], "…")

H = {"Authorization": "Bearer " + at, "clientid": "Xqp0kJBXWhwaTpB6",
     "x-device-id": xunleipan._device_id(), "content-type": "application/json"}

# 2) 回传「上一次用过的 captcha」签发新的
old_cap = str(xunleipan._setting("XLPAN_CAPTCHA_TOKEN") or "")
body = {"client_id": "Xqp0kJBXWhwaTpB6", "action": "GET:/drive/v1/files",
        "device_id": xunleipan._device_id(), "captcha_token": old_cap, "meta": {}}
rc = c.post(xunleipan.SHIELD_API + "/v1/shield/captcha/init", json=body, headers=H)
cap = (rc.json() or {}).get("captcha_token") or ""
print("2) 签发 captcha", rc.status_code, "|", (cap[:14] + "…") if cap else "无")

# 3) 带新 captcha 验证
r2 = c.get(xunleipan.API + "/drive/v1/files", params={"limit": 3},
           headers={**H, "x-captcha-token": cap})
print("3) 列目录 ->", r2.status_code)
files = (r2.json() or {}).get("files") or []
print("   条目:", [f.get("name") for f in files])

# 4) 全部保存（含轮换后的 captcha 与 refresh_token）
saved = web.set_settings({"XLPAN_TOKEN": at, "XLPAN_REFRESH_TOKEN": new_rt,
                           "XLPAN_CAPTCHA_TOKEN": cap})
print("4) 保存 ->", saved.get("ok"), saved.get("saved"))
