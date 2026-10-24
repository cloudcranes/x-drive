import httpx, json
from app import xunleipan
H = xunleipan._headers()
device = xunleipan._device_id()
c = httpx.Client(timeout=20.0)
body = {"client_id": "Xqp0kJBXWhwaTpB6",
        "action": "DELETE:/drive/v1/files/ZZ_notexist",
        "device_id": device, "captcha_token": "", "meta": {}}
r = c.post("https://xluser-ssl.xunlei.com/v1/shield/captcha/init",
           headers={**H, "content-type": "application/json"}, json=body)
tok = (r.json() or {}).get("captcha_token") or ""
print("signed token len =", len(tok), "prefix =", tok[:4])
H2 = dict(H); H2["x-captcha-token"] = tok
r2 = c.delete("https://api-pan.xunlei.com/drive/v1/files/ZZ_notexist", headers=H2)
print("DELETE notexist ->", r2.status_code, (r2.text or "")[:160].replace("\n", " "))
