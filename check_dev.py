from app import xunleipan, web
import base64, json

tok = str(web.get_setting("XLPAN_TOKEN") or "")
dev = str(web.get_setting("XLPAN_DEVICE_ID") or "")
seg = tok.split(".")[1]
pad = "=" * (-len(seg) % 4)
d = json.loads(base64.urlsafe_b64decode(seg + pad))
print("token iat =", d.get("iat"), "→", __import__("time").strftime("%H:%M:%S", __import__("time").localtime(d.get("iat") or 0)))
print("token exp =", d.get("exp"), "→", __import__("time").strftime("%H:%M:%S", __import__("time").localtime(d.get("exp") or 0)))
print("at_hash   =", str(d.get("at_hash"))[:20])
print("aud       =", d.get("aud"))
print("已存 deviceid =", dev[:40], "…")
print()
print("== 不带 device-id 试试")
h = xunleipan._headers(); h.pop("x-device-id", None)
import httpx
c = httpx.Client(timeout=15.0)
r = c.get(xunleipan.API + "/drive/v1/files", params={"limit": 1}, headers=h)
print("   ->", r.status_code, (r.text or "")[:110].replace("\n", " "))
print("== 带 device-id 试试")
r2 = c.get(xunleipan.API + "/drive/v1/files", params={"limit": 1}, headers=xunleipan._headers())
print("   ->", r2.status_code, (r2.text or "")[:110].replace("\n", " "))
