"""CloudDrive2 适配器：直连它的 gRPC 服务（默认 19798），不经 rclone / WebDAV。

两条必须记住的行为差异（都踩过）：

1. **上传是异步任务**。`WriteToFileStream` 只是把字节交给 CD2，它再排队往网盘
   上传。所以 `upload_dir` 必须轮询 `GetUploadFileList` 到 `Status_Finished` 才
   返回 —— 否则清单里会记上一个云端还不存在的文件，校核与补刮全错。
   轮询期间能拿到 `transferedBytes/globalBytesPerSecond`，顺手喂给界面。

2. **名字会被归一化**。CD2 把 `*?:/\\<>|"` 换成全角、削掉尾部空白与点号。
   所以目录名一律以 `CreateFolder.folderCreated.name` 为准，不能拿我们算出的
   字符串去记账（`verify`/`rename` 都要按真实名拼路径）。`real_folder()` 就是
   给上层问「实际落地叫什么」用的。

写入流程：`CreateFile` 拿句柄 → `WriteToFileStream` 流式写（最后一条
`closeFile=true`）→ 轮询任务 → 用 `FindFileByPath` 复核一次，确保不是空任务。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

from google.protobuf.empty_pb2 import Empty

from .. import netalias
from .base import AuthError, Backend, FileEntry, Progress, StorageError
from .cd2_grpc import UNARY, STREAM_IN, STREAM_OUT, Cd2Channel
from .proto import CloudDrive_pb2 as pb

log = logging.getLogger("x-drive.storage.cd2")

FINISHED, ERROR = 5, 2          # pb.Status_*：Finished / Error


def _human(size: int) -> str:
    """字节数说成人话（自检那一行用，别写成 10553213.2GB）。"""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if value < 1024 or unit == "PB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{value:.1f}PB"
# CD2 的文件名归一化规则（把 Windows 非法字符换成全角），用来匹配上传任务里的
# destPath —— 否则带 `:` 的文件名会被它改名，我们按原名轮询就永远等不到。
_FULLWIDTH = {ord(c): ord(c) + 65248 for c in '*?:/\\<>|"'}


def _normalize(name: str) -> str:
    return name.translate(_FULLWIDTH).rstrip(" \r\n\t\v\f\xa0.")


class CloudDrive2(Backend):
    kind = "clouddrive2"

    def __init__(self, url: str, user: str = "", password: str = "", *,
                 tls: bool = False, ca_file: str = "", chunk_mb: float = 4,
                 call_timeout: float = 60.0, task_timeout: float = 7200.0,
                 link_mode: str = "none", link_template: str = "",
                 channel: Cd2Channel | None = None):
        self.chunk = max(1, int(float(chunk_mb or 4) * 1024 * 1024))
        self.link_mode = (link_mode or "none").strip().lower()
        self.link_template = (link_template or "").strip()
        resolved, self.note = netalias.resolve(url)
        if self.note:
            log.info("CloudDrive2 %s", self.note)
        parts = urlsplit(resolved if "//" in resolved else f"//{resolved}")
        scheme = (parts.scheme or "http").lower()
        self.base = f"{scheme}://{parts.netloc or parts.path}"
        self.chan = channel or Cd2Channel(resolved, user, password, tls=tls, ca_file=ca_file,
                                         timeout=call_timeout, task_timeout=task_timeout)

    # ── 路径
    @staticmethod
    def _p(path: str) -> str:
        return "/" + (path or "").strip("/")

    @staticmethod
    def _split(path: str) -> tuple[str, str]:
        clean = (path or "").strip("/")
        if "/" not in clean:
            return "", clean
        return tuple(clean.rsplit("/", 1))            # type: ignore[return-value]

    # ── 目录读取
    def _entries(self, path: str) -> list:
        p = self._p(path)
        out = []
        try:
            stream = self.chan.call("GetSubFiles", pb.ListSubFileRequest(path=p),
                                    pb.SubFilesReply, mode=STREAM_OUT, timeout=120.0)
            for reply in stream:
                out.extend(reply.subFiles)
        except AuthError:
            raise
        except StorageError as exc:
            raise StorageError(f"clouddrive2 列目录失败（{p}）：{exc}") from exc
        return out

    def _find(self, path: str):
        parent, name = self._split(path)
        if not name:
            return None
        for entry in self._entries(parent):
            if entry.name == name:
                return entry
        return None

    def ensure_dir(self, path: str) -> str:
        """逐级建目录，返回最后一级的**真实名**（CD2 可能改名）。"""
        actual = ""
        for part in [p for p in (path or "").strip("/").split("/") if p]:
            wanted = f"{actual}/{part}" if actual else part
            found = self._find(wanted)
            if found is not None:
                if not found.isDirectory:
                    raise StorageError(f"clouddrive2：{wanted} 是文件，不是目录")
                actual = wanted
                continue
            # 父目录是「已经确认过的那一层」，不是 wanted 的 dirname ——
            # 一开始写成 split(actual) 会把子目录建到根下（曾把 /X-Drive/A 建成了 /A）
            res = self.chan.call("CreateFolder",
                                 pb.CreateFolderRequest(parentPath=self._p(actual),
                                                        folderName=part),
                                 pb.CreateFolderResult)
            if not res.result.success:
                reason = res.result.errorMessage or ""
                if "exist" in reason.lower() or "已存在" in reason:
                    actual = wanted              # 并发/缓存不同步：当已存在处理
                    continue
                raise StorageError(f"clouddrive2 建目录失败（{part}）：{reason or '未知原因'}")
            name = res.folderCreated.name or part
            actual = f"{actual}/{name}" if actual else name
        return actual.rsplit("/", 1)[-1]

    def real_folder(self, path: str) -> str:
        return self.ensure_dir(path)

    def list_dirs(self, path: str) -> list[str]:
        return [e.name for e in self._entries(path) if e.isDirectory]

    def list_files(self, path: str) -> list[FileEntry]:
        return [FileEntry(e.name, int(e.size or 0)) for e in self._entries(path)
                if not e.isDirectory]

    # ── 写入
    def upload_file(self, local: Path, dest: str) -> None:
        local = Path(local)
        parent, name = self._split(dest)
        self.ensure_dir(parent)
        size = local.stat().st_size
        created = self.chan.call("CreateFile",
                                 pb.CreateFileRequest(parentPath=self._p(parent), fileName=name),
                                 pb.CreateFileResult)
        handle = int(created.fileHandle or 0)
        if not handle:
            raise StorageError(f"clouddrive2 建文件失败：{name}")

        def stream():
            pos = 0
            with open(local, "rb") as fh:
                while True:
                    blob = fh.read(self.chunk)
                    if not blob:
                        break
                    yield pb.WriteFileRequest(fileHandle=handle, startPos=pos,
                                              length=len(blob), buffer=blob, closeFile=False)
                    pos += len(blob)
            # 空文件也要发一条带 closeFile 的收尾，否则句柄不会关闭、任务不会入队
            yield pb.WriteFileRequest(fileHandle=handle, startPos=pos, length=0,
                                      buffer=b"", closeFile=True)

        try:
            self.chan.call("WriteToFileStream", stream, pb.WriteFileResult,
                           mode=STREAM_IN, timeout=self.chan.task_timeout)
        except Exception:
            try:            # 流断了要补关句柄，否则 CD2 侧会留下临时文件
                self.chan.call("CloseFile", pb.CloseFileRequest(fileHandle=handle),
                               pb.FileOperationResult)
            except Exception:
                pass
            raise
        self._wait_upload(dest, size)

    def _uploads(self) -> list:
        res = self.chan.call("GetUploadFileList",
                             pb.GetUploadFileListRequest(getAll=True, itemsPerPage=200,
                                                         pageNumber=1),
                             pb.GetUploadFileListResult, timeout=60.0)
        return list(res.uploadFiles)

    def _wait_upload(self, dest: str, size: int,
                     progress: Progress | None = None) -> None:
        target = self._p(dest)
        parent, name = self._split(dest)
        candidates = {target}
        candidates.add(f"{self._p(parent)}/{_normalize(name)}".replace("//", "/"))
        deadline = time.monotonic() + self.chan.task_timeout
        interval = 1.0
        while time.monotonic() < deadline:
            try:
                items = self._uploads()
            except StorageError:
                items = []
            mine = next((i for i in items
                         if i.destPath in candidates
                         or i.destPath.endswith(target)), None)
            if mine is not None:
                if mine.statusEnum == FINISHED:
                    return
                if mine.statusEnum == ERROR:
                    raise StorageError(f"clouddrive2 上传失败（{Path(dest).name}）："
                                       f"{mine.errorMessage or 'CD2 未给原因'}")
                if progress and mine.size:
                    progress(min(mine.transferedBytes / mine.size, 0.99),
                             f"{Path(dest).name} {mine.transferedBytes * 100 // max(mine.size, 1)}%")
            else:
                # 任务瞬间完成时列表里可能已经查不到：直接问最终路径是否落地
                found = self._find(dest)
                if found is not None and (not found.size or int(found.size) >= size):
                    return
            time.sleep(interval)
            interval = min(interval * 1.3, 5.0)
        raise StorageError(f"clouddrive2 等待上传任务超时（{Path(dest).name}，"
                           f"{int(self.chan.task_timeout)}s）")

    def upload_dir(self, local: Path, dest: str, *, transfers: int = 4,
                   chunk: str = "64M", progress: Progress | None = None) -> int:
        local = Path(local)
        self.ensure_dir(dest)
        files = sorted(p for p in local.rglob("*") if p.is_file())
        total_bytes = sum(p.stat().st_size for p in files) or 1
        done = 0
        for index, path in enumerate(files, start=1):
            self.upload_file(path, f"{dest.rstrip('/')}/{path.name}")
            done += path.stat().st_size
            if progress:
                progress(min(done / total_bytes, 1.0), f"{index}/{len(files)} {path.name}")
        return done

    # ── 云添加（离线下载）：把磁力/直链直接扔进云盘，不留本地文件
    #
    # 实测（CD2 1.0.18 + 迅雷云盘，2026-10-24）三条必须记住的行为：
    #   1. `AddOfflineFiles` 一旦被接受就回 `success=True`，**哪怕磁力是假的**
    #      —— 返回值不能当成功，只能靠「云盘里真的出现目录」判定；
    #   2. `GetOfflineQuotaInfo` / `ListAllOfflineFiles` / `ListOfflineFilesByPath`
    #      对迅雷云盘**全部不支持**（`supportOfflineDownloadManagement=False`），
    #      所以没有进度、没有任务列表、不能暂停/重试；
    #   3. 落地形态是 `toFolder/<种子名>/` 一个目录（实测 15 秒后出现，5 个文件）。
    def offline_add(self, urls: str | list[str], folder: str) -> None:
        """提交云添加。**返回不代表成功** —— 调用方必须自己轮询目录是否真的出现。"""
        text = "\n".join(urls) if isinstance(urls, (list, tuple)) else str(urls or "")
        text = text.strip()
        if not text:
            raise StorageError("clouddrive2 云添加失败：链接为空")
        try:
            res = self.chan.call("AddOfflineFiles",
                                 pb.AddOfflineFileRequest(urls=text, toFolder=self._p(folder)),
                                 pb.FileOperationResult, timeout=90.0)
        except StorageError as exc:
            raise StorageError(f"clouddrive2 云添加失败：{exc}") from exc
        if res is not None and not res.success:
            raise StorageError(f"clouddrive2 云添加失败：{res.errorMessage or 'CD2 未给原因'}")

    def cloud_roots(self) -> list[dict]:
        """列出「支持离线下载」的云盘（给设置页选 CLOUD_INBOX 用）。"""
        try:
            apis = self.chan.call("GetAllCloudApis", Empty(), pb.CloudAPIList, timeout=30.0)
        except StorageError as exc:
            raise StorageError(f"clouddrive2 读取云盘列表失败：{exc}") from exc
        return [{"name": a.name, "user": a.userName, "path": a.path or f"/{a.name}",
                 "locked": bool(a.isLocked)}
                for a in apis.apis if a.name and not a.isLocked]

    def offline_supported(self, folder: str) -> tuple[bool, str]:
        """这个目录所在的云盘能不能离线下载（看 CD2 给的能力位）。

        探测方式是问目录自己的 `canOfflineDownload`：这比猜云盘类型可靠，
        而且换云盘（115 / 阿里）时不用改代码。
        """
        try:
            entry = self.chan.call("FindFileByPath",
                                   pb.FindFileByPathRequest(path=self._p(folder)),
                                   pb.CloudDriveFile, timeout=20.0)
        except StorageError as exc:
            return False, f"读不到 {folder}：{exc}"
        if getattr(entry, "canOfflineDownload", False):
            manage = getattr(entry, "supportOfflineDownloadManagement", False)
            return True, "可离线下载" + ("（可管理）" if manage else "（不可查询进度/不可暂停）")
        return False, f"{folder} 所在云盘不支持离线下载"

    # ── 分享 / 变更
    def link(self, dest: str) -> str | None:
        """CD2 没有「创建分享」的接口（AddSharedLink 是「把别人的分享加进来」）。"""
        if self.link_mode == "template" and self.link_template:
            return self.link_template.replace("{path}", quote(dest.strip("/"), safe="/"))
        if self.link_mode == "static":
            files = self.list_files(dest)
            media = [f for f in files if Path(f.name).suffix.lower() in
                     (".mp4", ".mkv", ".avi", ".wmv", ".ts", ".mov", ".rmvb", ".iso")]
            if len(files) == 1 or len(media) == 1:
                name = (media or files)[0].name
                path = quote(f"{self._p(dest)}/{name}", safe="/")
                host = self.base.split("//", 1)[-1]
                scheme = self.base.split("://", 1)[0]
                return f"{self.base}/static/{scheme}/{host}/False/{path}"
        return None

    def purge(self, dest: str) -> str:
        target = self._p(dest)
        res = self.chan.call("DeleteFilesPermanently", pb.MultiFileRequest(path=[target]),
                             pb.FileOperationResult)
        if res.success:
            return "已删除"
        reason = res.errorMessage or "永久删除失败"
        res2 = self.chan.call("DeleteFiles", pb.MultiFileRequest(path=[target]),
                              pb.FileOperationResult)
        if res2.success:
            return f"已移入回收站（{reason}）"
        raise StorageError(f"clouddrive2 删除失败：{res2.errorMessage or reason}")

    def move_file(self, src: str, dst: str) -> None:
        """单文件搬进目标目录（必要时再改名）。目标已存在时明确失败，不覆盖。

        这是**两步远端操作**（MoveFile + RenameFile），不是原子的：移动成功、
        改名失败时文件已经在目标目录里、用的是旧名，而调用方的回滚清单里
        还没有它 —— 就这么抛错会留一个谁都不知道的孤儿。所以改名失败时
        在这里尽力把文件搬回去；搬回去也失败就报出**实际位置**，不许含糊。
        """
        src_parent, src_name = self._split(src)
        dst_parent, dst_name = self._split(dst)
        if not src_name or not dst_name:
            raise StorageError("clouddrive2 移动失败：路径为空")
        self.ensure_dir(dst_parent)
        if self._find(dst) is not None:
            raise StorageError(f"目标已存在：{dst_name}")
        res = self.chan.call("MoveFile",
                             pb.MoveFileRequest(theFilePaths=[self._p(src)],
                                                destPath=self._p(dst_parent),
                                                conflictPolicy=pb.ConflictPolicy_Skip),
                             pb.FileOperationResult, timeout=300.0)
        if not res.success:
            raise StorageError(f"clouddrive2 移动失败：{res.errorMessage or '未知原因'}")
        if dst_name != src_name:
            renamed = self.chan.call("RenameFile",
                                     pb.RenameFileRequest(
                                         theFilePath=f"{self._p(dst_parent)}/{src_name}",
                                         newName=dst_name),
                                     pb.FileOperationResult, timeout=120.0)
            if not renamed.success:
                why = renamed.errorMessage or "未知原因"
                # 补偿：把已经搬进目标目录的文件搬回原位
                back = self.chan.call("MoveFile",
                                      pb.MoveFileRequest(
                                          theFilePaths=[f"{self._p(dst_parent)}/{src_name}"],
                                          destPath=self._p(src_parent),
                                          conflictPolicy=pb.ConflictPolicy_Skip),
                                      pb.FileOperationResult, timeout=300.0)
                if back is not None and back.success:
                    raise StorageError(
                        f"clouddrive2 改名失败（已回滚到原目录）：{why}")
                raise StorageError(
                    f"clouddrive2 改名失败，且回滚失败：{why}；"
                    f"文件实际在 {dst_parent}/{src_name}（旧名），需要人工处理")

    def rename_dir(self, src: str, new_name: str) -> None:
        parent, old = self._split(src)
        if not old:
            raise StorageError("clouddrive2 改名失败：路径为空")
        if self._find(f"{parent}/{new_name}" if parent else new_name) is not None:
            raise StorageError("目标已存在")
        res = self.chan.call("RenameFile",
                             pb.RenameFileRequest(theFilePath=self._p(src), newName=new_name),
                             pb.FileOperationResult)
        if not res.success:
            raise StorageError(f"clouddrive2 改名失败：{res.errorMessage or '未知原因'}")

    # ── 自检
    def check(self, probe_path: str = "") -> tuple[bool, str]:
        try:
            self.chan.login()
            info = self.chan.call("GetSystemInfo", Empty(), pb.CloudDriveSystemInfo)
            if getattr(info, "hasError", False):
                return False, f"clouddrive2 {self.chan.target}: {info.SystemMessage or '服务报错'}"
            if not getattr(info, "SystemReady", False):
                return False, f"clouddrive2 {self.chan.target}: 服务还没就绪"
            # 配额要问「归档根目录」那一层：问 "/" 拿到的是聚合根，
            # 数字离谱到 10553213.2GB（各家云盘容量简单相加，还含未挂载的占位）
            try:
                space = self.chan.call(
                    "GetSpaceInfo", pb.FileRequest(path=self._p(probe_path)),
                    pb.SpaceInfo)
                free = f" · 剩余 {_human(space.freeSpace)}" if space.freeSpace > 0 else ""
            except StorageError:
                free = ""
            # 在归档根目录下试写：CD2 的 "/" 是聚合根，不允许建目录（会报
            # create_folder is not supported），拿它当探针等于自欺欺人
            base = probe_path.strip("/")
            probe = f"{base}/_xdrive_healthcheck" if base else "_xdrive_healthcheck"
            try:
                self.ensure_dir(probe)
                self.purge(probe)
            except StorageError as exc:
                return False, (f"clouddrive2 {self.chan.target}: 能登录，但"
                               f"{'/' + base if base else '根目录'} 下建不了目录：{exc}")
            where = f" · 归档根 /{base}" if base else ""
            note = f"（{self.note}）" if getattr(self, "note", "") else ""
            return True, f"clouddrive2 {self.chan.target}: 可读写{free}{where}{note}"
        except AuthError as exc:
            return False, str(exc)
        except StorageError as exc:
            return False, f"clouddrive2 {self.chan.target}: {exc}"
