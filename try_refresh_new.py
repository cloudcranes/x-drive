import httpx
from app import xunleipan
rt = "a1.z906yV8ciGrcsBEY7-6IUZO8xS4ifiXe_GMsUrVdRn33kVRfmCvpdYFHGbc"
c = httpx.Client(timeout=20.0)
r = c.post(xunleipan.AUTH_API,
           json={"grant_type": "refresh_token", "refresh_token": rt,
                 "client_id": "Xqp0kJBXWhwaTpB6"},
           headers={"content-type": "application/json", "clientid": "Xqp0kJBXWhwaTpB6"})
print("续期 ->", r.status_code)
txt = (r.text or "")
print("   ", txt[:200].replace("\n", " "))
if r.status_code == 200:
    j = r.json()
    at = j.get("access_token", "")
    print("新 access 长度 =", len(at))
    print("新 refresh =", (j.get("refresh_token") or "")[:20], "…")
    # 用新 token 直接试
    h = {"Authorization": "Bearer " + at, "clientid": "Xqp0kJBXWhwaTpB6",
         "x-device-id": xunleipan._device_id(), "content-type": "application/json"}
    r2 = c.get(xunleipan.API + "/drive/v1/files", params={"limit": 2}, headers=h)
    print("用新 token 列目录 ->", r2.status_code, (r2.text or "")[:100].replace("\n", " "))
