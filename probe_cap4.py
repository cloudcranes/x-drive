import httpx
from app import xunleipan
H = xunleipan._headers()
device = xunleipan._device_id()
c = httpx.Client(timeout=20.0)
body = {"client_id": "Xqp0kJBXWhwaTpB6",
        "action": "DELETE:/drive/v1/files/ZZ_notexist",
        "device_id": device, "captcha_token": "", "meta": {}}

def sign(base, hdrs, b):
    r = c.post(base + "/v1/shield/captcha/init",
               headers={**hdrs, "content-type": "application/json"}, json=b)
    j = r.json() if r.headers.get("content-type","").startswith("application/json") else {}
    return (j or {}).get("captcha_token") or "", r.status_code, (r.text or "")[:120]

# 变体1：在 api-pan 域名下签发
for base in ("https://api-pan.xunlei.com", "https://xluser-ssl.xunlei.com"):
    tok, st, txt = sign(base, H, body)
    h2 = dict(H); h2["x-captcha-token"] = tok
    r2 = c.delete("https://api-pan.xunlei.com/drive/v1/files/ZZ_notexist", headers=h2)
    print("sign@%s -> %s toklen=%d | DELETE -> %s %s" % (base, st, len(tok), r2.status_code, (r2.text or "")[:70].replace("\n"," ")))
