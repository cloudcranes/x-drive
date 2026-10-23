"""OpenList 适配器：用 httpx.MockTransport 顶一个 AList 兼容服务端。

要盯住的三个「协议怪癖」：
  · 业务错误也回 HTTP 200（真状态在 body 的 code 里）；
  · `File-Path` 头要 URL 编码，服务端才 PathUnescape 得回来；
  · 列目录是分页的，不翻完就会漏文件（校核会误报缺文件）。
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.storage.base import AuthError, StorageError
from app.storage.openlist import OpenList


class FakeOpenList:
    def __init__(self, user: str = "u", password: str = "p", per_page: int = 2):
        self.user = user
        self.password = password
        self.per_page = per_page
        self.dirs: set[str] = {"/"}
        self.files: dict[str, int] = {}
        self.shares: dict[str, list[str]] = {}
        self.logins = 0
        self.puts: list[tuple[str, int]] = []
        self.bodies: list[tuple[str, dict]] = []
        self.write = True
        self.mkdir_says_exists = False
        self.expire_tokens = False
        self.token_serial = 1

    # ── 伪服务端
    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/auth/login":
            body = json.loads(request.read() or b"{}")
            if body.get("username") != self.user or body.get("password") != self.password:
                return httpx.Response(200, json={"code": 401, "message": "wrong password"})
            self.logins += 1
            return httpx.Response(200, json={"code": 200, "data": {"token": f"tok{self.token_serial}"}})
        if path == "/api/public/settings":
            return httpx.Response(200, json={"code": 200, "data": {"version": "4.2.6"}})

        auth = request.headers.get("Authorization", "")
        if self.expire_tokens and auth != f"tok{self.token_serial}":
            return httpx.Response(200, json={"code": 401, "message": "token expired"})
        if not auth.startswith("tok"):
            return httpx.Response(401, json={"code": 401, "message": "no token"})

        body = {}
        if path.startswith("/api/fs/") or path.startswith("/api/share/"):
            if request.method != "GET" and path != "/api/fs/put":
                raw = request.read()
                if raw:
                    body = json.loads(raw)
                self.bodies.append((path, body))

        if path == "/api/fs/mkdir":
            target = body.get("path", "")
            if target in self.dirs:
                if self.mkdir_says_exists:
                    return httpx.Response(200, json={"code": 500, "message": "target already exist"})
                return httpx.Response(200, json={"code": 200, "data": None})
            self.dirs.add(target)
            return httpx.Response(200, json={"code": 200, "data": None})
        if path == "/api/fs/dirs":
            parent = body.get("path", "/")
            prefix = parent.rstrip("/") + "/"
            rows = [{"name": d[len(prefix):]} for d in sorted(self.dirs)
                    if d.startswith(prefix) and "/" not in d[len(prefix):]]
            return httpx.Response(200, json={"code": 200, "data": rows})
        if path == "/api/fs/list":
            parent = body.get("path", "/").rstrip("/") + "/"
            rows = [{"name": f[len(parent):], "size": n, "is_dir": False}
                    for f, n in sorted(self.files.items()) if f.startswith(parent)
                    and "/" not in f[len(parent):]]
            page = int(body.get("page") or 1)
            per = min(int(body.get("per_page") or 200), self.per_page)
            chunk = rows[(page - 1) * per: page * per]
            return httpx.Response(200, json={"code": 200, "data": {
                "content": chunk, "total": len(rows),
                "has_more": page * per < len(rows), "write": self.write}})
        if path == "/api/fs/put":
            encoded = request.headers.get("File-Path", "")
            from urllib.parse import unquote
            target = unquote(encoded)
            data = request.read()
            self.puts.append((encoded, len(data)))
            self.files[target] = len(data)
            return httpx.Response(200, json={"code": 200, "data": {"task": {"id": "t"}}})
        if path == "/api/fs/rename":
            src = body.get("path")
            parent = src.rsplit("/", 1)[0]
            dst = f"{parent}/{body.get('name')}"
            if src in self.dirs:
                # 目录改名连带子孙（真机整棵子树一起动）
                self.dirs = {dst + d[len(src):] if d == src or d.startswith(src + "/") else d
                             for d in self.dirs}
                self.files = {(dst + f[len(src):] if f.startswith(src + "/") else f): n
                              for f, n in self.files.items()}
            return httpx.Response(200, json={"code": 200, "data": None})
        if path == "/api/fs/remove":
            base = body.get("dir", "/").rstrip("/")
            for name in body.get("names") or []:
                target = f"{base}/{name}"
                self.dirs = {d for d in self.dirs if not (d == target or d.startswith(target + "/"))}
                self.files = {f: n for f, n in self.files.items()
                              if not (f == target or f.startswith(target + "/"))}
            return httpx.Response(200, json={"code": 200, "data": None})
        if path == "/api/share/create":
            sid = f"sid{len(self.shares) + 1}"
            self.shares[sid] = list(body.get("files") or [])
            return httpx.Response(200, json={"code": 200, "data": {"id": sid}})
        if path == "/api/share/delete":
            sid = request.url.params.get("id", "")
            self.shares.pop(sid, None)
            return httpx.Response(200, json={"code": 200, "data": None})
        return httpx.Response(404, json={"code": 404, "message": f"no route {path}"})

    # 测试辅助
    def add_dir(self, path: str) -> None:
        parts = [p for p in path.strip("/").split("/") if p]
        cur = ""
        for part in parts:
            cur = f"{cur}/{part}" if cur else f"/{part}"
            self.dirs.add(cur)

    def add_file(self, path: str, size: int = 1) -> None:
        self.add_dir(path.rsplit("/", 1)[0])
        self.files[path] = size


@pytest.fixture()
def fake():
    return FakeOpenList()


def backend(fake, **kw):
    client = httpx.Client(base_url="http://ol", transport=httpx.MockTransport(fake.handler))
    kw.setdefault("user", fake.user)
    kw.setdefault("password", fake.password)
    return OpenList("http://ol", client=client, **kw)


# ─────────────────────────────────────────── 鉴权

def test_login_is_cached(fake):
    ol = backend(fake)
    ol.ensure_dir("X-Drive/A")
    ol.list_dirs("X-Drive")
    assert fake.logins == 1, "token 48h 有效，不该每次调用都登一次"


def test_wrong_password_is_auth_error(fake):
    ol = backend(fake, password="nope")
    with pytest.raises(AuthError):
        ol.ensure_dir("X-Drive/A")


def test_expired_token_relogins_once(fake):
    ol = backend(fake)
    ol.ensure_dir("X-Drive/A")
    fake.token_serial = 2                       # 服务端只认新 token
    fake.expire_tokens = True
    assert ol.list_dirs("X-Drive") == ["A"]
    assert fake.logins == 2, "过期只重登一次，不循环"


def test_business_error_hides_in_http_200(fake):
    """code!=200 必须抛出，不能因为 HTTP 200 就当成功。"""
    ol = backend(fake)

    def erring(request):
        if request.url.path == "/api/fs/list":
            return httpx.Response(200, json={"code": 500, "message": "storage not found"})
        return fake.handler(request)

    ol._client = httpx.Client(base_url="http://ol", transport=httpx.MockTransport(erring))
    with pytest.raises(StorageError) as err:
        ol.list_files("X-Drive/A")
    assert "storage not found" in str(err.value)


# ─────────────────────────────────────────── 目录 / 分页

def test_mkdir_exist_is_tolerated(fake):
    fake.mkdir_says_exists = True
    ol = backend(fake)
    fake.add_dir("/X-Drive")
    assert ol.ensure_dir("X-Drive") == "X-Drive"


def test_list_files_paginates(fake):
    for i in range(5):
        fake.add_file(f"/X-Drive/A/f{i}.mp4", i + 1)
    ol = backend(fake)
    files = ol.list_files("X-Drive/A")
    assert [f.name for f in files] == [f"f{i}.mp4" for i in range(5)]
    assert [f.size for f in files] == [1, 2, 3, 4, 5]


def test_list_dirs_only_direct_children(fake):
    fake.add_dir("/X-Drive/A")
    fake.add_dir("/X-Drive/A/sub")
    fake.add_dir("/X-Drive/B")
    ol = backend(fake)
    assert sorted(ol.list_dirs("X-Drive")) == ["A", "B"]
    assert ol.list_dirs("X-Drive/A") == ["sub"]


# ─────────────────────────────────────────── 上传

def test_upload_file_encodes_file_path_header(fake, tmp_path):
    local = tmp_path / "中 文#1.mp4"
    local.write_bytes(b"abc")
    fake.add_dir("/X-Drive/A")
    ol = backend(fake)
    ol.upload_file(local, "X-Drive/A/中 文#1.mp4")

    encoded, size = fake.puts[0]
    assert size == 3
    assert "%23" in encoded and "%20" in encoded, "空格与 # 必须编码，否则服务端解不回来"
    assert encoded.startswith("/X-Drive/A/")
    assert "/X-Drive/A/中 文#1.mp4" in fake.files


def test_upload_dir_reports_progress(fake, tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    (work / "a.mp4").write_bytes(b"x" * 10)
    (work / "b.nfo").write_bytes(b"y" * 5)
    seen = []
    ol = backend(fake)
    size = ol.upload_dir(work, "X-Drive/A", progress=lambda r, n: seen.append(r))
    assert size == 15
    assert seen and seen[-1] == pytest.approx(1.0)
    assert fake.files["/X-Drive/A/a.mp4"] == 10


# ─────────────────────────────────────────── 分享 / 删除 / 改名

def test_share_create_returns_s_link(fake):
    fake.add_dir("/X-Drive/A")
    ol = backend(fake)
    url = ol.link("X-Drive/A")
    assert url == "http://ol/s/sid1"
    assert fake.shares["sid1"] == ["/X-Drive/A"]


def test_share_disabled_returns_none(fake):
    ol = backend(fake, share=False)
    assert ol.link("X-Drive/A") is None


def test_no_share_permission_does_not_break_upload(fake):
    ol = backend(fake)
    fake.handler_orig = fake.handler

    def deny(request):
        if request.url.path == "/api/share/create":
            return httpx.Response(200, json={"code": 403, "message": "permission denied"})
        return fake.handler_orig(request)

    ol._client = httpx.Client(base_url="http://ol", transport=httpx.MockTransport(deny))
    assert ol.link("X-Drive/A") is None, "没权限就退回显示路径，不能阻断上传"


def test_drop_share_deletes_it(fake):
    ol = backend(fake)
    ol.link("X-Drive/A")
    sid = next(iter(fake.shares))
    ol.drop_share(sid)
    assert not fake.shares


def test_purge_uses_dir_and_names(fake):
    fake.add_file("/X-Drive/A/a.mp4", 1)
    fake.add_file("/X-Drive/A/sub/b.mp4", 1)
    ol = backend(fake)
    assert ol.purge("X-Drive/A") == "已删除"
    assert ("/api/fs/remove", {"dir": "/X-Drive", "names": ["A"]}) in fake.bodies
    assert not [f for f in fake.files if f.startswith("/X-Drive/A/")]


def test_rename_dir(fake):
    fake.add_dir("/X-Drive/OLD")
    ol = backend(fake)
    ol.rename_dir("X-Drive/OLD", "NEW")
    assert ("/api/fs/rename", {"path": "/X-Drive/OLD", "name": "NEW"}) in fake.bodies


# ─────────────────────────────────────────── 自检

def test_check_reports_version(fake):
    ol = backend(fake)
    ok, detail = ol.check()
    assert ok and "4.2.6" in detail


def test_check_flags_readonly(fake):
    fake.write = False
    ol = backend(fake)
    ok, detail = ol.check()
    assert not ok and "只读" in detail


def test_check_without_url_is_explicit():
    with pytest.raises(AuthError) as err:
        OpenList("")
    assert "OPENLIST_URL" in str(err.value)
