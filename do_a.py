import json
from app import web, xunleipan

# A：清掉回收站里早期 e2e 探测留下的残留
TARGET_NAMES = {"MIMK-290", "MIMK-290(1)", "_xd_probe2", "FC2-PPV-4856696"}
items = xunleipan.list_files(trashed=True)
print("回收站条目 =", len(items))
trash_dirs = [i for i in items if i["kind"].endswith("folder")]
print("回收站目录：")
for i in trash_dirs:
    print("   ", i["name"], "|", i["id"])
targets = [i for i in trash_dirs if i["name"] in TARGET_NAMES]
print("待删目标 =", [i["name"] for i in targets])
# 只删名字在清单里的，其他一律不动
for i in targets:
    try:
        out = xunleipan.delete_file(i["id"])
        print("delete", i["name"], "->", out)
    except Exception as exc:
        print("delete", i["name"], "ERR", str(exc)[:140])
after = xunleipan.list_files(trashed=True)
print("回收站剩余 =", len(after), "目录 =", [i["name"] for i in after if i["kind"].endswith("folder")])
