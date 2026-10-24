from app import storage
b = storage.get()
target = "迅雷云盘/X-Drive/FC2-PPV-4519894/offkab@sukebei.txt"
# 先确认现状
try:
    files = b.list_files("迅雷云盘/X-Drive/FC2-PPV-4519894")
    print("before:", [(f.name, f.size) for f in files])
except Exception as exc:
    print("list ERR", str(exc)[:100])
    raise SystemExit(1)
if not any(f.name == "offkab@sukebei.txt" for f in files):
    print("目标不存在，不动")
    raise SystemExit(0)
# 只删这一个文件：CD2 DeleteFiles 是按路径删单文件
try:
    res = b.chan.call("DeleteFiles",
                      __import__("app.storage.proto.CloudDrive_pb2", fromlist=["x"]).MultiFileRequest(path=["/" + target]),
                      __import__("app.storage.proto.CloudDrive_pb2", fromlist=["x"]).FileOperationResult)
    print("DeleteFiles ->", res.success, res.errorMessage[:80])
except Exception as exc:
    print("DeleteFiles ERR", str(exc)[:140])
files = b.list_files("迅雷云盘/X-Drive/FC2-PPV-4519894")
print("after:", [(f.name, f.size) for f in files])
