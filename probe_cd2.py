from app import storage
b = storage.get()
# CD2 的回收站相关接口：DeleteFiles 走回收站，DeleteFilesPermanently 彻底删
# 先探 /trash 或 .trash 视图能不能列出回收站条目
for path in ("/trash", "trash", "迅雷云盘/.trash", "迅雷云盘/回收站"):
    try:
        dirs = b.list_dirs(path)
        files = b.list_files(path)
        print("probe", path, "-> dirs", dirs[:6], "files", [(f.name, f.size) for f in files][:4])
    except Exception as exc:
        print("probe", path, "ERR", str(exc)[:90])
