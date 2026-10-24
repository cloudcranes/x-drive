import httpx, time
from app import xunleipan
H = xunleipan._headers()
c = httpx.Client(timeout=15.0)
for i in range(3):
    try:
        r = c.get(xunleipan.API + "/drive/v1/files", params={"trashed": "true"}, headers=H)
        j = r.json() or {}
        n = len(j.get("files") or [])
        e = str(j.get("error") or "")[:40]
        print("GET #%d -> %s files=%d error=%s" % (i + 1, r.status_code, n, e))
    except Exception as exc:
        print("GET #%d ERR %s" % (i + 1, str(exc)[:70]))
    time.sleep(1)
print("--- DELETE notexist id")
r = c.delete(xunleipan.API + "/drive/v1/files/ZZ_notexist", headers=H)
print("DELETE ->", r.status_code, (r.text or "")[:130].replace("\n", " "))
