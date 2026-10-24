import httpx
from app import xunleipan

c = httpx.Client(timeout=20.0)
rt = "a1.z906yV8ciGrcsBEY7-6IUZO8xS4ifiXe_GMsUrVdRn33kVRfmCvpdYFHGbc"
r = c.post(xunleipan.AUTH_API,
           json={"grant_type": "refresh_token", "refresh_token": rt,
                 "client_id": "Xqp0kJBXWhwaTpB6"},
           headers={"content-type": "application/json", "clientid": "Xqp0kJBXWhwaTpB6"})
j = r.json()
at = j["access_token"]
H = {"Authorization": "Bearer " + at, "clientid": "Xqp0kJBXWhwaTpB6",
     "x-device-id": xunleipan._device_id(), "content-type": "application/json"}

old_cap = str(xunleipan._setting("XLPAN_CAPTCHA_TOKEN") or "")
print("存储里的旧 captcha 长度 =", len(old_cap))

for label, prev in (("回传旧 captcha", old_cap), ("回传空串", "")):
    body = {"client_id": "Xqp0kJBXWhwaTpB6", "action": "GET:/drive/v1/files",
            "device_id": xunleipan._device_id(), "captcha_token": prev, "meta": {}}
    rc = c.post(xunleipan.SHIELD_API + "/v1/shield/captcha/init", json=body, headers=H)
    try:
        jj = rc.json()
    except Exception:
        jj = {}
    tok = jj.get("captcha_token") or ""
    print("\n%s -> 签发 %s token=%s" % (label, rc.status_code, (tok[:14] + "…") if tok else "无"))
    if not tok:
        print("   ", (rc.text or "")[:120].replace("\n", " "))
        continue
    r2 = c.get(xunleipan.API + "/drive/v1/files", params={"limit": 3},
               headers={**H, "x-captcha-token": tok})
    print("   带它列目录 ->", r2.status_code, (r2.text or "")[:110].replace("\n", " "))
