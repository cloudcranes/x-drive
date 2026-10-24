"""OpenList 适配器：走原生 REST（AList v3 兼容接口），不经 rclone。

对过的接口事实（OpenList v4.2.6）：
  · 登录 POST /api/auth/login {username,password,otp_code?} → data.token（JWT，48h），
    之后请求头是 `Authorization: <裸 token>`，没有 Bearer 前缀；
  · **业务错误也返回 HTTP 200**，真正状态在 body 的 code 里 —— 只检查 HTTP 状态
    会把「目录不存在」当成功，所以这里每个响应都过一遍 _ok()；
  · 上传 PUT /api/fs/put，目标路径放在 `File-Path` 头里，服务端会做 PathUnescape，
    所以要用 quote(path, safe="/") 编码（中文、空格、%、# 都靠它）；
  · 列目录 POST /api/fs/list 是分页的（per_page 上限 500，超出被夹住），
    必须按 has_more 翻完，否则校核会漏文件；
  · 分享 POST /api/share/create {files:[path]} → data.id，分享页是 {base}/s/{id}，
    删除 POST /api/share/delete?id=。账号需要有分享权限（创建者/管理员默认有）。

大文件：默认单个 PUT（与网页端上传同一条路）。开了 OPENLIST_MULTIPART 且文件
超过阈值时走 /api/fs/multipart/{init,chunk,complete}；服务端没启用会返回
403 "multipart upload is disabled"，这时自动退回单 PUT —— 不能因为分片不可用就
整单失败。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import quote

import httpx

from .. import netalias
from .base import AuthError, Backend, FileEntry, Progress, StorageError

PER_PAGE = 200                      # 服务端上限 500，取 200 留余量
CHUNK_RETRY = 6                     # 分片被流控（429/409）时的重试上限


class OpenList(Backend):
    kind = "openlist"

    def __init__(self, base: str, user: str = "", password: str = "", otp: str = "",
                 timeout: float = 30.0, upload_timeout: float = 3600.0,
                 multipart: bool = False, multipart_mb: float = 512,
                 share: bool = True, share_days: int = 0, share_pwd: str = "",
                 client: httpx.Client | None = None):
        raw = (base or "").rstrip("/")
        if not raw:
            raise AuthError("openlist：没配 OPENLIST_URL")
        # 容器里填了宿主机 LAN 地址会连不上，这里统一过一遍地址改写
        self.base, self.note = netalias.resolve(raw)
        if self.note:
            log.info("OpenList %s", self.note)
        self.user = user or ""
        self.password = password or ""
        self.otp = otp or ""
        self.timeout = float(timeout or 30)
        self.upload_timeout = float(upload_timeout or 3600)
        self.multipart = bool(multipart)
        self.multipart_mb = float(multipart_mb or 512)
        self.share_on = bool(share)
        self.share_days = int(share_days or 0)
        self.share_pwd = share_pwd or ""
        self._token = ""
        self._token_at = 0.0
        self._client = client or httpx.Client(base_url=self.base, follow_redirects=True)

    # ── 底层
    def _headers(self, extra: dict | None = None) -> dict:
        head = {"Authorization": self._token} if self._token else {}
        head.update(extra or {})
        return head

    def login(self) -> str:
        body = {"username": self.user, "password": self.password}
        if self.otp:
            body["otp_code"] = self.otp
        try:
            resp = self._client.post("/api/auth/login", json=body,
                                     timeout=self.timeout,
                                     headers={"Content-Type": "application/json"})
        except httpx.HTTPError as exc:
            raise StorageError(f"openlist 登录失败：{exc}") from exc
        if resp.status_code in (401, 403):
            raise AuthError("openlist：账号或密码错误（401）")
        payload = self._json_body(resp) or {}
        if int(payload.get("code") or 0) != 200:
            raise AuthError(f"openlist：登录失败 {payload.get('message') or payload.get('code')}")
        data = payload.get("data") or {}
        token = str(data.get("token") or "")
        if not token:
            raise AuthError("openlist：登录没拿到 token，检查账号/密码/两步验证")
        self._token, self._token_at = token, time.time()
        return token

    @staticmethod
    def _json_body(resp: httpx.Response) -> dict | None:
        try:
            return resp.json()
        except (ValueError, json.JSONDecodeError):
            return None

    def _ok(self, resp: httpx.Response, what: str, allow_exist: bool = False) -> object:
        """把 HTTP 状态 + body.code 一起判：OpenList 的错误常藏在 200 里。"""
        body = self._json_body(resp)
        code = int((body or {}).get("code") or resp.status_code or 0)
        message = str((body or {}).get("message") or resp.text[:200] or "")
        if resp.status_code in (401, 403) or code in (401, 403):
            raise AuthError(f"openlist {what}失败：{message or resp.status_code}")
        if resp.status_code >= 400 or code != 200:
            lowered = message.lower()
            if allow_exist and ("exist" in lowered or "已存在" in message):
                return None
            raise StorageError(f"openlist {what}失败：{message or resp.status_code}")
        return (body or {}).get("data")

    def _api(self, method: str, path: str, body: dict | None = None,
             timeout: float | None = None, allow_exist: bool = False):
        if not self._token:
            self.login()
        for attempt in (0, 1):
            try:
                resp = self._client.request(method, path, json=body,
                                            headers=self._headers(),
                                            timeout=timeout or self.timeout)
            except httpx.HTTPError as exc:
                raise StorageError(f"openlist 请求失败：{exc}") from exc
            if attempt == 0:
                # 注意别叫 body：那是**请求体**，覆盖掉之后重试会把响应当请求发出去
                payload = self._json_body(resp) or {}
                if resp.status_code == 401 or int(payload.get("code") or 0) == 401:
                    # token 过期（48h）：换一个再试一次，不循环。
                    # 注意 OpenList 的业务错误常带 HTTP 200，401 也可能只在 body 里。
                    self._token = ""
                    self.login()
                    continue
            return self._ok(resp, path.rsplit("/", 1)[-1], allow_exist=allow_exist)
        return None

    # ── 目录
    def ensure_dir(self, path: str) -> str:
        self._api("POST", "/api/fs/mkdir", {"path": self._p(path)}, allow_exist=True)
        return path.rstrip("/").rsplit("/", 1)[-1]

    def list_dirs(self, path: str) -> list[str]:
        data = self._api("POST", "/api/fs/dirs", {"path": self._p(path), "force_root": False})
        return [str((row or {}).get("name") or "") for row in (data or []) if (row or {}).get("name")]

    def list_files(self, path: str) -> list[FileEntry]:
        out: list[FileEntry] = []
        page = 1
        while True:
            data = self._api("POST", "/api/fs/list", {
                "path": self._p(path), "password": "", "page": page,
                "per_page": PER_PAGE, "refresh": False}) or {}
            for row in data.get("content") or []:
                if (row or {}).get("is_dir"):
                    continue
                out.append(FileEntry(str(row.get("name") or ""), int(row.get("size") or 0)))
            if not data.get("has_more") or page >= 1000:
                break
            page += 1
        return out

    # ── 写入
    def _ensure_token(self) -> None:
        if not self._token:
            self.login()

    def upload_file(self, local: Path, dest: str) -> None:
        local = Path(local)
        self._ensure_token()
        size = local.stat().st_size
        if self.multipart and size >= self.multipart_mb * 1024 * 1024:
            if self._multipart(local, dest, size):
                return
        self._put(local, dest, size)

    def _put(self, local: Path, dest: str, size: int) -> None:
        headers = self._headers({
            "Content-Type": "application/octet-stream",
            "Content-Length": str(size),
            "File-Path": quote(self._p(dest), safe="/"),
            "As-Task": "false",
        })
        try:
            with open(local, "rb") as handle:
                resp = self._client.put("/api/fs/put", content=handle, headers=headers,
                                        timeout=httpx.Timeout(connect=15,
                                                              read=self.upload_timeout,
                                                              write=self.upload_timeout,
                                                              pool=15))
        except (httpx.HTTPError, OSError) as exc:
            raise StorageError(f"openlist 上传失败（{local.name}）：{exc}") from exc
        self._ok(resp, f"上传 {local.name}")

    def _multipart(self, local: Path, dest: str, size: int) -> bool:
        """分片上传。服务端未启用（403）或字段不认识就返回 False，调用方退回单 PUT。"""
        if not self._token:
            self.login()
        try:
            resp = self._client.post(
                "/api/fs/multipart/init", headers=self._headers({
                    "File-Path": quote(self._p(dest), safe="/"),
                    "X-File-Size": str(size),
                    "Content-Type": "application/octet-stream",
                }), timeout=self.timeout)
            if resp.status_code == 403:
                return False                     # multipart upload is disabled
            data = self._ok(resp, "分片初始化") or {}
        except StorageError:
            return False
        upload_id = str(data.get("upload_id") or "")
        chunk = int(data.get("chunk_size") or 0)
        if not upload_id or chunk <= 0:
            return False
        total = max(1, (size + chunk - 1) // chunk)
        with open(local, "rb") as handle:
            for index in range(total):
                blob = handle.read(chunk)
                for attempt in range(CHUNK_RETRY):
                    try:
                        r = self._client.put(
                            "/api/fs/multipart/chunk",
                            content=blob,
                            headers=self._headers({"X-Upload-Id": upload_id,
                                                   "X-Chunk-Index": str(index),
                                                   "Content-Type": "application/octet-stream",
                                                   "Content-Length": str(len(blob))}),
                            timeout=httpx.Timeout(connect=15, read=self.upload_timeout,
                                                  write=self.upload_timeout, pool=15))
                    except httpx.HTTPError as exc:
                        raise StorageError(f"openlist 分片上传失败：{exc}") from exc
                    # 429 = 窗口满（流控）；409 = 同一片正在另一条连接上传。都可重试
                    if r.status_code in (409, 429):
                        time.sleep(min(1.0 + attempt * 0.5, 5.0))
                        continue
                    self._ok(r, f"分片 {index}")
                    break
                else:
                    raise StorageError(f"openlist 分片 {index}/{total} 始终被流控，稍后重试")
        done = self._client.post("/api/fs/multipart/complete",
                                 headers=self._headers({"X-Upload-Id": upload_id}),
                                 timeout=httpx.Timeout(connect=15, read=self.upload_timeout,
                                                       write=self.upload_timeout, pool=15))
        self._ok(done, "分片收尾")
        return True

    def upload_dir(self, local: Path, dest: str, *, transfers: int = 4,
                   chunk: str = "64M", progress: Progress | None = None) -> int:
        local = Path(local)
        self.ensure_dir(dest)
        files = sorted(p for p in local.rglob("*") if p.is_file())
        total = sum(p.stat().st_size for p in files) or 1
        done = 0
        for i, path in enumerate(files, start=1):
            self.upload_file(path, f"{dest.rstrip('/')}/{path.name}")
            done += path.stat().st_size
            if progress:
                progress(min(done / total, 1.0), f"{i}/{len(files)} {path.name}")
        return done

    # ── 分享 / 变更
    def link(self, dest: str) -> str | None:
        if not self.share_on:
            return None
        body: dict = {"files": [self._p(dest)], "pwd": self.share_pwd}
        if self.share_days > 0:
            body["expires"] = time.strftime(
                "%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + self.share_days * 86400))
        try:
            data = self._api("POST", "/api/share/create", body) or {}
        except StorageError:
            return None                          # 没分享权限就不给链接，不阻断上传
        sid = str(data.get("id") or "")
        return f"{self.base}/s/{sid}" if sid else None

    def drop_share(self, share_id: str) -> None:
        """删除建过的分享（归档被删时调用，别留一堆指向空目录的链接）。"""
        if not share_id:
            return
        try:
            self._api("POST", f"/api/share/delete?id={quote(share_id, safe='')}")
        except StorageError:
            pass

    def purge(self, dest: str) -> str:
        target = self._p(dest)
        parent, _, name = target.rstrip("/").rpartition("/")
        if not name:
            raise StorageError("openlist 删除失败：路径为空")
        self._api("POST", "/api/fs/remove", {"dir": parent or "/", "names": [name]})
        return "已删除"

    def move_file(self, src: str, dst: str) -> None:
        """单文件搬运：同目录改名走 rename，跨目录走 move（AList 的 move 只能改目录）。"""
        src_p, dst_p = self._p(src), self._p(dst)
        src_dir, _, src_name = src_p.rpartition("/")
        dst_dir, _, dst_name = dst_p.rpartition("/")
        if src_dir == dst_dir:
            self._api("POST", "/api/fs/rename", {"path": src_p, "name": dst_name})
            return
        self._api("POST", "/api/fs/move",
                  {"src_dir": src_dir or "/", "dst_dir": dst_dir or "/", "names": [src_name]})
        if dst_name != src_name:               # 跨目录 + 改名：两步走
            self._api("POST", "/api/fs/rename",
                      {"path": f"{dst_dir}/{src_name}", "name": dst_name})

    def rename_dir(self, src: str, new_name: str) -> None:
        self._api("POST", "/api/fs/rename", {"path": self._p(src), "name": new_name})

    # ── 自检
    def check(self, probe_path: str = "") -> tuple[bool, str]:
        try:
            if not self._token:
                self.login()
            version = ""
            try:
                pub = self._client.get("/api/public/settings", timeout=self.timeout)
                blob = self._json_body(pub) or {}
                version = str(((blob.get("data") or {}) if isinstance(blob.get("data"), dict)
                               else {}).get("version") or "")
            except httpx.HTTPError:
                pass
            probe = f"{self._p(probe_path) if probe_path else ''}/_xdrive_healthcheck"
            self.ensure_dir(probe)
            listing = self._api("POST", "/api/fs/list", {
                "path": self._p(""), "password": "", "page": 1,
                "per_page": 1, "refresh": False}) or {}
            writable = bool(listing.get("write", True))
            self.purge(probe)
            if not writable:
                return False, f"openlist {self.base}: 只读挂载，不能上传"
            note = f"（{self.note}）" if self.note else ""
            return True, "openlist {}: 可读写{}{}".format(
                self.base, f" · v{version}" if version else "", note)
        except AuthError as exc:
            return False, str(exc)
        except (StorageError, httpx.HTTPError) as exc:
            return False, f"openlist {self.base}: {exc}"

    # ── 路径
    @staticmethod
    def _p(path: str) -> str:
        """相对路径 → 服务端绝对路径（永远以 / 开头）。"""
        clean = "/" + (path or "").strip("/")
        return "/" if clean == "/" else clean
