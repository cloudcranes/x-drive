import httpx, json
from app import xunleipan

c = httpx.Client(timeout=20.0)
# 1) 用 refresh_token 续期，拿新 token
rt = "a1.z906yV8ciGrcsBEY7-6IUZO8xS4ifiXe_GMsUrVdRn33kVRfmCvpdYFHGbc"
r = c.post(xunleipan.AUTH_API,
           json={"grant_type": "refresh_token", "refresh_token": rt,
                 "client_id": "Xqp0kJBXWhwaTpB6"},
           headers={"content-type": "application/json", "clientid": "Xqp0kJBXWhwaTpB6"})
j = r.json()
at = j["access_token"]
new_rt = j.get("refresh_token", "")
print("续期 ok，新 access 长度 =", len(at), "| 新 refresh =", new_rt[:16], "…")

H = {"Authorization": "Bearer " + at, "clientid": "Xqp0kJBXWhwaTpB6",
     "x-device-id": xunleipan._device_id(), "content-type": "application/json"}

print("\n2) 不带 captcha 列目录")
r2 = c.get(xunleipan.API + "/drive/v1/files", params={"limit": 2}, headers=H)
print("   ->", r2.status_code, (r2.text or "")[:80].replace("\n", " "))

print("\n3) 签发 captcha（带这个新 token）")
body = {"client_id": "Xqp0kJBXWhwaTpB6", "action": "GET:/drive/v1/files",
        "device_id": xunleipan._device_id(), "captcha_token": "", "meta": {}}
r3 = c.post(xunleipan.SHIELD_API + "/v1/shield/captcha/init", json=body, headers=H)
print("   ->", r3.status_code)
try:
    jj = r3.json()
    tok = jj.get("captcha_token") or ""
    print("   captcha 长度 =", len(tok), "| expires_in =", jj.get("expires_in"), "| err =", str(jj.get("error") or "")[:60])
    if tok:
        print("\n4) 带 captcha 再列目录")
        r4 = c.get(xunleipan.API + "/drive/v1/files", params={"limit": 3},
                   headers={**H, "x-captcha-token": tok})
        print("   ->", r4.status_code, (r4.text or "")[:120].replace("\n", " "))
except Exception as exc:
    print("   解析失败", str(exc)[:80], (r3.text or "")[:120])
