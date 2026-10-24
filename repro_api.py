import json, urllib.request

RAW = open("repro_parse.py").read().split('RAW = ', 1)[1].rsplit("\n\n", 1)[0]
RAW = RAW.strip()
if RAW.startswith("'"):
    RAW = RAW[1:-1]

body = json.dumps({"text": RAW}).encode()
req = urllib.request.Request("http://127.0.0.1:18091/api/xlp/credentials",
                             data=body, headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.loads(r.read())
        print("status =", r.status)
        for k, v in out.items():
            print("  %-12s = %s" % (k, str(v)[:90]))
except urllib.error.HTTPError as exc:
    print("HTTP", exc.code, exc.read()[:400].decode("utf-8", "replace"))
except Exception as exc:
    print("ERR", str(exc)[:200])
