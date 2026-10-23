"""进程内的假 CloudDrive2 gRPC 服务：给存储适配器做离线测试。

为什么不用录包/mock：适配器的坑几乎都在「协议语义」上（上传是异步任务、
目录名会被归一化、删除要先试永久删），只有真发 gRPC 才测得到。这里用
`grpc.server` + 通用 handler 起一个内存文件树，速度与单测无异。

行为对标真机：
  · 目录名归一化：`*?:/\\<>|"` 转全角，尾部空白与点号削掉（真机就是这么干的）；
  · 上传先 CreateFile 拿句柄，WriteToFileStream 写入，完成才进 GetUploadFileList；
  · `fail_upload=True` 时任务以 Status_Error 结束，用来测失败路径；
  · token 可设置成「下次调用就过期」，用来测重登。
"""
from __future__ import annotations

from concurrent import futures
from dataclasses import dataclass, field

import grpc
from google.protobuf.empty_pb2 import Empty
from google.protobuf.timestamp_pb2 import Timestamp

from app.storage.proto import CloudDrive_pb2 as pb

SERVICE = "clouddrive.CloudDriveFileSrv"
FULLWIDTH = {ord(c): ord(c) + 65248 for c in '*?:/\\<>|"'}


def normalize(name: str) -> str:
    return name.translate(FULLWIDTH).rstrip(" \r\n\t\v\f\xa0.")


@dataclass
class FakeCd2:
    """一个够用的内存实现。测试里直接读它的 files/dirs 断言副作用。"""

    user: str = "u"
    password: str = "p"
    page_size: int = 2                  # 流式列目录时每批几条（顺便验证多批收集）
    upload_chunk_delay: float = 0.0
    fail_upload: bool = False
    expire_token_after: int = 0         # >0：这么多次调用后旧 token 失效
    calls: int = 0
    dirs: set[str] = field(default_factory=lambda: {"/"})
    files: dict[str, int] = field(default_factory=dict)      # 绝对路径 -> 字节数
    uploads: list = field(default_factory=list)
    handles: dict[int, str] = field(default_factory=dict)     # fileHandle -> 路径
    received: dict[str, int] = field(default_factory=dict)    # 路径 -> 已收字节
    token_serial: int = 1
    server: object = None
    port: int = 0

    # ── 生命周期
    def start(self) -> "FakeCd2":
        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
        self.server.add_generic_rpc_handlers((self._handler(),))
        self.port = self.server.add_insecure_port("127.0.0.1:0")
        self.server.start()
        return self

    def stop(self) -> None:
        if self.server is not None:
            self.server.stop(grace=0)
            self.server = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # ── 组装 handler
    def _handler(self):
        unary = {
            "GetToken": (pb.GetTokenRequest, pb.JWTToken, self.get_token),
            "GetSystemInfo": (Empty, pb.CloudDriveSystemInfo, self.get_system_info),
            "GetUploadFileList": (pb.GetUploadFileListRequest, pb.GetUploadFileListResult,
                                  self.get_upload_list),
            "CreateFolder": (pb.CreateFolderRequest, pb.CreateFolderResult, self.create_folder),
            "CreateFile": (pb.CreateFileRequest, pb.CreateFileResult, self.create_file),
            "CloseFile": (pb.CloseFileRequest, pb.FileOperationResult, self.close_file),
            "RenameFile": (pb.RenameFileRequest, pb.FileOperationResult, self.rename_file),
            "DeleteFile": (pb.FileRequest, pb.FileOperationResult, self.delete_one),
            "DeleteFiles": (pb.MultiFileRequest, pb.FileOperationResult,
                            lambda r, c: self.delete_many(r, c, permanent=False)),
            "DeleteFilesPermanently": (pb.MultiFileRequest, pb.FileOperationResult,
                                       lambda r, c: self.delete_many(r, c, permanent=True)),
            "DeleteFilePermanently": (pb.FileRequest, pb.FileOperationResult,
                                      lambda r, c: self.delete_one(r, c, permanent=True)),
            "GetSpaceInfo": (pb.FileRequest, pb.SpaceInfo,
                             lambda r, c: pb.SpaceInfo(totalSpace=100 << 30,
                                                       usedSpace=40 << 30,
                                                       freeSpace=60 << 30)),
            "GetUploadFileCount": (Empty, pb.GetUploadFileCountResult,
                                   lambda r, c: pb.GetUploadFileCountResult(
                                       fileCount=len(self.uploads))),
            "MoveFile": (pb.MoveFileRequest, pb.FileOperationResult, self.move_file),
        }
        out = {}
        for name, (req, resp, fn) in unary.items():
            out[name] = grpc.unary_unary_rpc_method_handler(
                self._wrap(req, resp, fn, auth=name != "GetToken"),
                request_deserializer=req.FromString,
                response_serializer=lambda m: m.SerializeToString())
        out["GetSubFiles"] = grpc.unary_stream_rpc_method_handler(
            self._wrap(pb.ListSubFileRequest, pb.SubFilesReply, self.get_sub_files),
            request_deserializer=pb.ListSubFileRequest.FromString,
            response_serializer=lambda m: m.SerializeToString())
        out["WriteToFileStream"] = grpc.stream_unary_rpc_method_handler(
            self._wrap(pb.WriteFileRequest, pb.WriteFileResult, self.write_stream),
            request_deserializer=pb.WriteFileRequest.FromString,
            response_serializer=lambda m: m.SerializeToString())
        return grpc.method_handlers_generic_handler(SERVICE, out)

    def _wrap(self, req_cls, resp_cls, fn, auth=True):
        def handler(request, context):
            self.calls += 1
            if auth:
                meta = dict(context.invocation_metadata() or ())
                token = (meta.get("authorization") or "").replace("Bearer ", "")
                if not token.startswith("t"):
                    context.abort(grpc.StatusCode.UNAUTHENTICATED, "no token")
                serial = int(token[1:] or 0)
                if self.expire_token_after and serial < self.token_serial:
                    context.abort(grpc.StatusCode.UNAUTHENTICATED, "token expired")
            return fn(request, context)
        return handler

    # ── 文件树工具
    def _children(self, path: str) -> list:
        parent = path.rstrip("/") or "/"
        prefix = "/" if parent == "/" else parent + "/"
        rows = []
        for d in sorted(self.dirs):
            if d != prefix.rstrip("/") and d.startswith(prefix) and "/" not in d[len(prefix):]:
                rows.append(pb.CloudDriveFile(name=d.rsplit("/", 1)[-1], fullPathName=d,
                                              isDirectory=True, fileType=pb.FileType_Directory))
        for f, size in sorted(self.files.items()):
            if f.startswith(prefix) and "/" not in f[len(prefix):]:
                rows.append(pb.CloudDriveFile(name=f.rsplit("/", 1)[-1], fullPathName=f,
                                              size=size, isDirectory=False,
                                              fileType=pb.FileType_File))
        return rows

    def _mkdirs(self, path: str) -> str:
        parts = [p for p in path.strip("/").split("/") if p]
        cur = ""
        for part in parts:
            wanted = f"{cur}/{part}" if cur else f"/{part}"
            if wanted not in self.dirs:
                self.dirs.add(wanted)
            cur = wanted
        return cur or "/"

    # ── RPC 实现
    def get_token(self, req, _ctx):
        if req.userName != self.user or req.password != self.password:
            return pb.JWTToken(success=False, errorMessage="bad credentials")
        stamp = Timestamp()
        stamp.FromSeconds(int(__import__("time").time()) + 3600)
        return pb.JWTToken(success=True, token=f"t{self.token_serial}", expiration=stamp)

    def get_system_info(self, _req, _ctx):
        return pb.CloudDriveSystemInfo(IsLogin=True, UserName=self.user, SystemReady=True)

    def get_sub_files(self, req, _ctx):
        rows = self._children(req.path)
        if not rows:
            # 真机对空目录返回一条空回复流；对不存在的路径报错
            if req.path.rstrip("/") and req.path not in self.dirs:
                _ctx.abort(grpc.StatusCode.NOT_FOUND, f"no such dir {req.path}")
            yield pb.SubFilesReply(subFiles=[])
            return
        for i in range(0, len(rows), self.page_size):
            yield pb.SubFilesReply(subFiles=rows[i:i + self.page_size])

    def create_folder(self, req, _ctx):
        name = normalize(req.folderName)
        parent = req.parentPath.rstrip("/") or "/"
        path = f"{parent}/{name}" if parent != "/" else f"/{name}"
        if path in self.dirs:
            return pb.CreateFolderResult(
                folderCreated=pb.CloudDriveFile(name=name, fullPathName=path, isDirectory=True),
                result=pb.FileOperationResult(success=False, errorMessage="folder exists"))
        self.dirs.add(path)
        return pb.CreateFolderResult(
            folderCreated=pb.CloudDriveFile(name=name, fullPathName=path, isDirectory=True),
            result=pb.FileOperationResult(success=True))

    def create_file(self, req, _ctx):
        parent = req.parentPath.rstrip("/") or "/"
        name = normalize(req.fileName)
        path = f"{parent}/{name}" if parent != "/" else f"/{name}"
        handle = len(self.handles) + 1
        self.handles[handle] = path
        self.received[path] = 0
        return pb.CreateFileResult(fileHandle=handle)

    def write_stream(self, requests, _ctx):
        handle = 0
        path = ""
        closed = False
        for chunk in requests:
            handle = chunk.fileHandle or handle
            path = self.handles.get(handle, path)
            if chunk.buffer:
                self.received[path] = self.received.get(path, 0) + len(chunk.buffer)
            closed = closed or bool(chunk.closeFile)
        if not path:
            _ctx.abort(grpc.StatusCode.INVALID_ARGUMENT, "unknown handle")
        size = self.received.get(path, 0)
        if self.fail_upload:
            self.uploads.append(pb.UploadFileInfo(key=str(handle), destPath=path, size=size,
                                                  statusEnum=pb.Status_Error,
                                                  errorMessage="cloud said no"))
        else:
            self.dirs.update({d for d in ("/",)})       # 保持根目录
            self.files[path] = size
            self.uploads.append(pb.UploadFileInfo(key=str(handle), destPath=path, size=size,
                                                  transferedBytes=size,
                                                  statusEnum=pb.Status_Finished))
        return pb.WriteFileResult(bytesWritten=size)

    def close_file(self, _req, _ctx):
        return pb.FileOperationResult(success=True)

    def get_upload_list(self, _req, _ctx):
        return pb.GetUploadFileListResult(totalCount=len(self.uploads),
                                         uploadFiles=self.uploads[:50])

    def rename_file(self, req, _ctx):
        src = req.theFilePath
        parent = src.rsplit("/", 1)[0] or "/"
        dst = f"{parent}/{normalize(req.newName)}"
        if src in self.files:
            self.files[dst] = self.files.pop(src)
            return pb.FileOperationResult(success=True, resultFilePaths=[dst])
        if src in self.dirs:
            # 目录改名要连带子孙（真机是整棵子树一起动）
            self.dirs = {dst + d[len(src):] if d == src or d.startswith(src + "/") else d
                         for d in self.dirs}
            self.files = {(dst + f[len(src):] if f.startswith(src + "/") else f): n
                          for f, n in self.files.items()}
            return pb.FileOperationResult(success=True, resultFilePaths=[dst])
        return pb.FileOperationResult(success=False, errorMessage="no such path")

    def move_file(self, req, _ctx):
        moved = []
        for src in req.theFilePaths:
            name = src.rsplit("/", 1)[-1]
            dst = f"{req.destPath.rstrip('/')}/{name}"
            if dst in self.files or dst in self.dirs:
                if req.conflictPolicy == pb.ConflictPolicy_Skip:
                    return pb.FileOperationResult(success=False, errorMessage="target exists")
            if src in self.files:
                self.files[dst] = self.files.pop(src)
                moved.append(dst)
            elif src in self.dirs:
                self.dirs.discard(src)
                self.dirs.add(dst)
                moved.append(dst)
        return pb.FileOperationResult(success=bool(moved), resultFilePaths=moved,
                                     errorMessage="" if moved else "nothing moved")

    def delete_one(self, req, _ctx, permanent=True):
        return self.delete_many(pb.MultiFileRequest(path=[req.path]), _ctx, permanent)

    def delete_many(self, req, _ctx, permanent=True):
        for path in req.path:
            prefix = path.rstrip("/") + "/"
            self.files.pop(path, None)
            for f in [f for f in self.files if f.startswith(prefix)]:
                self.files.pop(f, None)
            for d in [d for d in self.dirs if d == path or d.startswith(prefix)]:
                self.dirs.discard(d)
        return pb.FileOperationResult(success=True, resultFilePaths=list(req.path))

    # ── 测试辅助
    def add_file(self, path: str, size: int = 1) -> None:
        self._mkdirs(path.rsplit("/", 1)[0])
        self.files[path] = size
