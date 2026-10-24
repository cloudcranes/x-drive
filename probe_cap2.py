import httpx, json, time
from app import xunleipan
H = xunleipan._headers()
H.pop("x-captcha-token", None)
device = xunleipan._device_id()
body = {"client_id": "Xqp0kJBXWhwaTpB6",
        "action": "DELETE:/drive/v1/files/x",
        "device_id": device,
        "captcha_token": "",
        "meta": {}}
c = httpx.Client(timeout=20.0)
for base in ("https://xluser-ssl.xunlei.com",
              "https://xluser2-ssl.xunlei.com",
              "https://api-shoulei-ssl.xunlei.com"):
    try:
        r = c.post(base + "/v1/shield/captcha/init",
                   headers={**H, "content-type": "application/json"}, json=body)
        print(base, "->", r.status_code, (r.text or "")[:220].replace("\n", " "))
    except Exception as exc:
        print(base, "ERR", str(exc)[:90])
