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
from .base import (AuthError, Backend, FileEntry, NotFoundError, Progress, Quota,
                    StorageError)
from .cd2_grpc import UNARY, STREAM_IN, STREAM_OUT, Cd2Channel
from .proto import CloudDrive_pb2 as pb

log = logging.getLogger("x-drive.storage.cd2")

FINISHED, ERROR = 5, 2          # pb.Status_*：Finished / Error

# CD2 报「路径不存在」的各种说法（不同版本/不同接口措辞不一样）
_NOT_FOUND_MARKS = ("not found", "not_found", "找不到", "no such file", "不存在")


def _is_not_found(text: str) -> bool:
    """这段报错是不是「路径不存在」。"""
    low = (text or "").lower()
    return any(mark in low for mark in _NOT_FOUND_MARKS)


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

    # CD2 的 gRPC 服务端把单条消息限制在 4MB，而上传是按 chunk 切的。
    # chunk 恰好等于 4MB 时，protobuf 消息头（实测 14 字节）会把消息顶出上限，
    # 服务端直接回 "SERVER: Received message larger than max (4194318 vs.
    # 4194304)" —— **每一次上传都会失败**，而且是本地配置看起来完全正常的那种。
    #
    # 注意：抬客户端的 max_send_message_length 没用 —— 限制在服务端。
    # 所以只能把 chunk 压到上限之下。留 1MB 余量，3MB 实测可用。
    MAX_CHUNK_MB = 3.0

    def __init__(self, url: str, user: str = "", password: str = "", *,
                 tls: bool = False, ca_file: str = "", chunk_mb: float = 4,
                 call_timeout: float = 60.0, task_timeout: float = 7200.0,
                 link_mode: str = "none", link_template: str = "",
                 channel: Cd2Channel | None = None):
        wanted = float(chunk_mb or self.MAX_CHUNK_MB)
        if wanted > self.MAX_CHUNK_MB:
            log.warning("CD2_CHUNK_MB=%.1f 超过服务端单条消息上限，已压到 %.1fMB",
                        wanted, self.MAX_CHUNK_MB)
            wanted = self.MAX_CHUNK_MB
        self.chunk = max(1, int(wanted * 1024 * 1024))
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
    def _entries(self, path: str, *, force: bool = False) -> list:
        """列目录。force=True 时绕过 CD2 的目录缓存（见 _entries_fresh 的说明）。"""
        p = self._p(path)
        out = []
        try:
            stream = self.chan.call(
                "GetSubFiles",
                pb.ListSubFileRequest(path=p, forceRefresh=bool(force)),
                pb.SubFilesReply, mode=STREAM_OUT, timeout=120.0)
            for reply in stream:
                out.extend(reply.subFiles)
        except AuthError:
            raise
        except StorageError as exc:
            text = str(exc)
            # 「路径不存在」跟「读失败」要分开（2026-10-24 实测）：
            # 提交前给还不存在的收件目录拍快照是**正常**的，CD2 会回
            #     find by path: "..." not found under "..."
            # 这不是故障，是那个目录还没建。抛 NotFoundError 让调用方
            # 区分对待 —— 笼统报「列目录失败」会把正常流程变成失败任务
            # （MIMK-182 就是这么挂的）。
            if _is_not_found(text):
                raise NotFoundError(
                    f"clouddrive2 路径不存在（{p}）：{text}") from exc
            raise StorageError(f"clouddrive2 列目录失败（{p}）：{text}") from exc
        return out

    def _entries_fresh(self, path: str) -> list:
        """列目录；CD2 说「找不到」就作废缓存重读一次。

        为什么需要（2026-10-24 实测）：CD2 有自己的**目录缓存**。刚
        CreateFolder 建出来的目录，缓存里还没有 —— 紧接着 GetSubFiles 那个
        目录，CD2 服务端会回
            NOT_FOUND: find by path: "/迅雷云盘/我的资源/MAAN1199"
                       not found under "/迅雷云盘/我的资源"
        目录明明在（FindFileByPath 能查到），只是缓存没同步。

        重试要两步：先 ForceExpireDirCache 作废**父目录**那层缓存，
        再带 forceRefresh 重新解析路径。只带 forceRefresh 不够 ——
        实测它读的是同一份没作废的路径缓存，仍然报找不到。
        代价是两次远端往返，但只在「普通读报找不到」时才付。

        真的不存在（目录压根没建）时，重试也没用 —— 强刷后仍报找不到就
        按「不存在」抛出去，别让调用方以为还能重试。
        """
        try:
            return self._entries(path)
        except NotFoundError as exc:
            parent, _name = self._split(path)
            self._refresh_dir_cache(parent or path)
            try:
                return self._entries(path, force=True)
            except NotFoundError:
                raise exc from None      # 强刷后还是不存在：确实没有
            except StorageError:
                raise exc from None

    def _find(self, path: str):
        parent, name = self._split(path)
        if not name:
            return None
        for entry in self._entries_fresh(parent):
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
        return [e.name for e in self._entries_fresh(path) if e.isDirectory]

    def list_files(self, path: str) -> list[FileEntry]:
        return [FileEntry(e.name, int(e.size or 0)) for e in self._entries_fresh(path)
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
        exact_without_task = 0
        while time.monotonic() < deadline:
            try:
                items = self._uploads()
            except StorageError as exc:
                # 查询失败不是「任务已消失」，否则远端占位文件会被当成完成。
                raise StorageError(f"clouddrive2 读取上传任务失败：{exc}") from exc
            mine = next((i for i in items
                         if i.destPath in candidates
                         or i.destPath.endswith(target)), None)
            finished = mine is not None and mine.statusEnum == FINISHED
            if mine is not None:
                exact_without_task = 0
                if mine.statusEnum == ERROR:
                    raise StorageError(f"clouddrive2 上传失败（{Path(dest).name}）："
                                       f"{mine.errorMessage or 'CD2 未给原因'}")
                if progress and mine.size and not finished:
                    progress(min(mine.transferedBytes / mine.size, 0.99),
                             f"{Path(dest).name} {mine.transferedBytes * 100 // max(mine.size, 1)}%")
            if finished or mine is None:
                # 即使任务 Finished 也必须核验最终文件的精确大小。
                # 任务瞬间消失时要求非空文件连续两次精确大小回读；空文件
                # 需要 Finished 证据，避免将 CreateFile 的占位视作上传完成。
                found = self._find(dest)
                normalized = f"{parent}/{_normalize(name)}" if parent else _normalize(name)
                if found is None and normalized != dest:
                    found = self._find(normalized)
                actual = getattr(found, "size", None)
                exact = (found is not None and not getattr(found, "isDirectory", False)
                         and actual is not None and int(actual) == size)
                if exact and finished:
                    return
                exact_without_task = exact_without_task + 1 if exact and size > 0 else 0
                if exact_without_task >= 2:
                    return
                if finished:
                    raise StorageError(
                        f"clouddrive2 上传完成核验失败（{Path(dest).name}）："
                        f"远端大小 {actual!r}，预期 {size}")
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
        """提交云添加。**返回不代表成功** —— 调用方必须自己轮询目录是否真的出现。

        目录刚建出来就提交时，CD2 服务端可能因为**自己的目录缓存没同步**
        而回「not found under …」（2026-10-24 实测：建完 MAAN1199 立刻提交
        就报这个）。这时先强制刷新一次目录缓存再重试；仍不行才算真失败。
        """
        text = "\n".join(urls) if isinstance(urls, (list, tuple)) else str(urls or "")
        text = text.strip()
        if not text:
            raise StorageError("clouddrive2 云添加失败：链接为空")
        last: StorageError | None = None
        for attempt in (0, 1):
            try:
                res = self.chan.call("AddOfflineFiles",
                                     pb.AddOfflineFileRequest(urls=text,
                                                             toFolder=self._p(folder)),
                                     pb.FileOperationResult, timeout=90.0)
            except StorageError as exc:
                last = exc
                msg = str(exc).lower()
                if attempt == 0 and ("not found" in msg or "找不到" in msg):
                    # 缓存没同步：作废父目录那层缓存再试
                    self._expire_parent_cache(folder)
                    continue
                raise StorageError(f"clouddrive2 云添加失败：{exc}") from exc
            if res is not None and not res.success:
                reason = res.errorMessage or ""
                if attempt == 0 and ("not found" in reason.lower() or "找不到" in reason):
                    self._expire_parent_cache(folder)
                    continue
                raise StorageError(f"clouddrive2 云添加失败：{reason or 'CD2 未给原因'}")
            return
        raise StorageError(f"clouddrive2 云添加失败：{last or '未知原因'}")

    def _refresh_dir_cache(self, path: str) -> None:
        """让 CD2 把这一层的目录缓存作废（下次读重新解析路径）。

        刷新失败不抛 —— 真正的错误由调用方重试后报出来。
        """
        try:
            self.chan.call("ForceExpireDirCache", pb.FileRequest(path=self._p(path)),
                           Empty(), timeout=30.0)
        except Exception as exc:                    # noqa: BLE001 刷新只是辅助手段
            log.info("刷新 CD2 目录缓存失败（%s）：%s", path, exc)

    def _expire_parent_cache(self, path: str) -> None:
        """作废 path 的**父目录**那层缓存。

        「找不到某个子目录」这件事，坏掉的是父目录的路径解析缓存 ——
        实测作废父目录才有用，作废子目录自己没用。
        """
        parent, _name = self._split(path)
        self._refresh_dir_cache(parent or path)

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
    def download_url(self, path: str) -> str | None:
        """任意文件/目录的下载直链（GetDownloadUrlPath）。实测 expiresIn=0 不过期，
        返回 `/static/{SCHEME}/{HOST}/false/…?cloudname=…`，占位符换成 CD2 自身地址。"""
        try:
            info = self.chan.call(
                "GetDownloadUrlPath",
                pb.GetDownloadUrlPathRequest(path=self._p(path), preview=False),
                pb.DownloadUrlPathInfo, timeout=30.0)
        except StorageError as exc:
            log.warning("取 %s 的下载路径失败：%s", path, exc)
            return None
        raw = str(getattr(info, "downloadUrlPath", "") or "")
        if not raw:
            return None
        scheme = self.base.split("://", 1)[0]
        host = self.base.split("//", 1)[-1]
        raw = raw.replace("{SCHEME}", scheme, 1).replace("{HOST}", host, 1)
        return self.base + raw if raw.startswith("/") else raw

    def link(self, dest: str) -> str | None:
        """CD2 没有「创建分享」的接口（AddSharedLink 是「把别人的分享加进来」）。

        download-url 档走 CD2 的 GetDownloadUrlPath（实测返回
        `/static/{SCHEME}/{HOST}/false/…?cloudname=…`，expiresIn=0 不过期）：
        比手拼 static 多了 cloudname，多云盘混挂时定位正确。
        """
        if self.link_mode == "template" and self.link_template:
            return self.link_template.replace("{path}", quote(dest.strip("/"), safe="/"))
        if self.link_mode == "download-url":
            files = self.list_files(dest)
            media = [f for f in files if Path(f.name).suffix.lower() in
                     (".mp4", ".mkv", ".avi", ".wmv", ".ts", ".mov", ".rmvb", ".iso")]
            target = dest
            if len(files) == 1 or len(media) == 1:
                target = f"{dest.strip('/')}/{(media or files)[0].name}"
            return self.download_url(target)
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

    def remove_dir(self, dest: str) -> str:
        """删除**空**目录（非递归）。

        真机实测（2026-10-25）：`DeleteFile` 对空目录删得掉；对**非空**目录
        它会连里面的子目录一起删掉（递归语义）—— 所以这里先自己确认目录
        确实是空的，不空就拒绝，绝不把「递归删」当「删空壳」用。
        """
        target = self._p(dest)
        try:
            if self.list_dirs(dest) or self.list_files(dest):
                raise StorageError(f"clouddrive2 目录非空，不删：{dest}")
        except NotFoundError:
            return "目录已不存在"
        res = self.chan.call("DeleteFile", pb.FileRequest(path=target),
                              pb.FileOperationResult)
        if not res.success:
            raise StorageError(f"clouddrive2 删除空目录失败：{res.errorMessage or '未知原因'}")
        return "已删除"

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

    # ── 容量
    def quota(self, probe_path: str = "") -> "Quota":
        """问归档根那一层的容量。

        必须问 probe_path（归档根）而不是 "/"：CD2 的 "/" 是聚合根，
        报出来的是各家云盘容量简单相加、还含未挂载占位的离谱数字
        （实测 10553213.2GB）。根目录那一层才是用户真正在用的空间。
        """
        try:
            self.chan.login()
            space = self.chan.call(
                "GetSpaceInfo", pb.FileRequest(path=self._p(probe_path)),
                pb.SpaceInfo)
        except (StorageError, AuthError) as exc:
            log.warning("问 clouddrive2 容量失败：%s", exc)
            return Quota()
        return Quota(total=int(space.totalSpace or 0),
                     used=int(space.usedSpace or 0),
                     free=int(space.freeSpace or 0))

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
