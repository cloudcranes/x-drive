import httpx, json
from app import xunleipan
H = xunleipan._headers()
H.pop("x-captcha-token", None)
c = httpx.Client(timeout=20.0)
device = xunleipan._device_id()
print("device_id =", device[:16], "len =", len(device))
# 契约（公开 chunk service-captcha）：
# POST {apisBaseURL}/v1/shield/captcha/init
# body: {client_id, action, device_id, captcha_token, meta}
body = {"client_id": "Xqp0kJBXWhwaTpB6",
        "action": "DELETE:/drive/v1/files/x",
        "device_id": device,
        "captcha_token": "",
        "meta": {}}
r = c.post("https://api-gateway-pan.xunlei.com/v1/shield/captcha/init",
           headers={**H, "content-type": "application/json"}, json=body)
print("POST ->", r.status_code)
txt = (r.text or "")[:600]
print("body:", txt)
