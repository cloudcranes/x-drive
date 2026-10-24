import httpx
from app import xunleipan
tok = str(xunleipan._setting("XLPAN_TOKEN") or "")
dev = xunleipan._device_id()
c = httpx.Client(timeout=15.0)
base = {"Authorization": "Bearer " + tok, "clientid": "Xqp0kJBXWhwaTpB6",
        "x-device-id": dev, "content-type": "application/json"}
print("== 各种头组合 ==")
combos = [
    ("全带", base),
    ("无 device", {k: v for k, v in base.items() if k != "x-device-id"}),
    ("无 clientid", {k: v for k, v in base.items() if k != "clientid"}),
    ("只 Authorization", {"Authorization": "Bearer " + tok}),
    ("加 captcha", {**base, "x-captcha-token": "x"}),
]
for name, h in combos:
    r = c.get(xunleipan.API + "/drive/v1/files", params={"limit": 1}, headers=h)
    print("  %-16s -> %s %s" % (name, r.status_code, (r.text or "")[:70].replace("\n", " ")))
