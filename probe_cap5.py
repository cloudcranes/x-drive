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
j = r.json() or {}
print("status", r.status_code, "keys:", sorted(j.keys()))
for k, v in j.items():
    sv = str(v)
    print("  %-16s = %s" % (k, sv[:120]))
# 再看 captcha_token 是否含 expires_at（base64 段）
tok = j.get("captcha_token") or ""
if "." in tok:
    import base64
    for part in tok.split(".")[:2]:
        try:
            pad = "=" * (-len(part) % 4)
            print("  seg:", base64.urlsafe_b64decode(part + pad)[:300])
        except Exception as exc:
            print("  seg ERR", str(exc)[:60])
