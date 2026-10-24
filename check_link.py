from app import web
import urllib.request
out = web.cloud_direct_link(path="迅雷云盘/我的资源/迅雷11 绿色版.zip")
link = out.get("link") or ""
print("link =", link[:120])
if link:
    try:
        req = urllib.request.Request(link, method="HEAD")
        req.add_header("User-Agent", "Mozilla/5.0")
        with urllib.request.urlopen(req, timeout=20) as r:
            print("HEAD ->", r.status, "| 长度 =", r.headers.get("Content-Length"),
                  "| 类型 =", r.headers.get("Content-Type"))
            print("Host =", r.geturl().split("/")[2])
    except Exception as exc:
        print("HEAD ERR:", str(exc)[:140])
