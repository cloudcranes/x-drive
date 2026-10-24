import httpx, json
from app import xunleipan
H = xunleipan._headers()
device = xunleipan._device_id()
c = httpx.Client(timeout=20.0)
BASE = "https://api-pan.xunlei.com"
SIGN = "https://xluser-ssl.xunlei.com"

def sign(action, meta=None):
    r = c.post(SIGN + "/v1/shield/captcha/init",
               headers={**H, "content-type": "application/json"},
               json={"client_id": "Xqp0kJBXWhwaTpB6", "action": action,
                     "device_id": device, "captcha_token": "", "meta": meta or {}})
    j = r.json() or {}
    return j.get("captcha_token") or "", j.get("expires_in")

def try_delete(tok, action, label):
    h = dict(H); h["x-captcha-token"] = tok
    r = c.delete(BASE + "/drive/v1/files/ZZ_notexist", headers=h)
    print("%-28s -> %s %s" % (label, r.status_code, (r.text or "")[:90].replace("\n", " ")))

# A: 用签发的 token 直接删
tok, exp = sign("DELETE:/drive/v1/files/ZZ_notexist")
print("signed expires_in =", exp)
try_delete(tok, None, "A sign(DELETE) -> delete")

# B: 先用旧 token 触发一次被拒，再签发，再删
old = H.get("x-captcha-token")
try_delete(old, None, "B1 old token -> delete")
tok2, exp2 = sign("DELETE:/drive/v1/files/ZZ_notexist", {"username": "", "phone_number": "", "email": ""})
try_delete(tok2, None, "B2 sign(withMeta) -> delete")

# C: 签发时带上旧 captcha_token
r = c.post(SIGN + "/v1/shield/captcha/init",
           headers={**H, "content-type": "application/json"},
           json={"client_id": "Xqp0kJBXWhwaTpB6",
                 "action": "DELETE:/drive/v1/files/ZZ_notexist",
                 "device_id": device, "captcha_token": old or "", "meta": {}})
j = r.json() or {}
tok3 = j.get("captcha_token") or ""
try_delete(tok3, None, "C sign(with old token) -> delete")
