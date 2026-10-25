"""直链接口：官方 API（外网）优先，CD2 GetDownloadUrlPath（内网）兜底。

CD2 的 GetDownloadUrlPath 实测返回 `/static/{SCHEME}/{HOST}/false/…?cloudname=…`
（expiresIn=0 不过期），占位符要换成 CD2 自身地址才是能用的 URL。
"""
from __future__ import annotations

import pytest

from app import storage, web
from app.storage.base import StorageError
from app.storage.clouddrive2 import CloudDrive2


class _Res:
    def __init__(self, path=""):
        self.downloadUrlPath = path
        self.expiresIn = 0


class FakeChan:
    def __init__(self, raw: str = "/static/{SCHEME}/{HOST}/false/a.mp4?cloudname=x"):
        self.raw = raw
        self.calls = []

    def call(self, method, request, response_cls, **kw):
        self.calls.append((method, getattr(request, "path", "")))
        if method == "GetDownloadUrlPath":
            return _Res(self.raw)
        return _Res("")


def test_download_url_replaces_placeholders():
    chan = FakeChan()
    cd2 = CloudDrive2("http://cd2.local:19798", channel=chan)
    url = cd2.download_url("迅雷云盘/我的资源/a.mp4")
    assert url == "http://cd2.local:19798/static/http/cd2.local:19798/false/a.mp4?cloudname=x"
    assert chan.calls[0][0] == "GetDownloadUrlPath"


def test_link_download_url_mode_picks_single_media():
    chan = FakeChan()
    cd2 = CloudDrive2("http://cd2.local:19798", channel=chan)
    cd2.link_mode = "download-url"
    entries = [type("E", (), {"name": "a.mp4", "size": 1})()]
    cd2.list_files = lambda dest: entries
    url = cd2.link("迅雷云盘/X-Drive/MIMK-290")
    assert url and "a.mp4" in url, url
    # 目录里只有一个媒体 → 用文件路径去取下载路径
    assert chan.calls[0][1].endswith("/MIMK-290/a.mp4"), chan.calls


def test_download_url_failure_returns_none():
    class BoomChan(FakeChan):
        def call(self, method, request, response_cls, **kw):
            raise StorageError("后端拒绝")

    cd2 = CloudDrive2("http://cd2.local:19798", channel=BoomChan())
    assert cd2.download_url("x/y.mp4") is None


# ─────────────────────────────────────────── web 侧直链接口

def _env(tmp_path, monkeypatch):
    from app import store as store_mod
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(web, "STORE", store_mod.Store(str(tmp_path / "jobs.db")))
    # 与线上同构的云盘挂载（迅雷云盘 / 归档根 / 收件目录）
    monkeypatch.setattr(web, "get_setting",
                        lambda key: {"CLOUD_DEST": "迅雷云盘/X-Drive",
                                     "CLOUD_INBOX": "迅雷云盘/我的资源"}.get(key, ""))


def test_direct_link_prefers_xunleipan(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    monkeypatch.setattr(web.xunleipan, "configured", lambda: True)
    monkeypatch.setattr(web.xunleipan, "direct_link", lambda p: {
        "name": "a.mp4", "link": "https://dl.example/signed",
        "medias": [{"name": "720P", "url": "https://dl.example/720"}]})
    out = web.cloud_direct_link(path="迅雷云盘/X-Drive/MIMK-290/a.mp4")
    assert out["ok"] and out["via"] == "xunleipan"
    assert out["link"] == "https://dl.example/signed"
    assert out["medias"][0]["name"] == "720P"


def test_direct_link_falls_back_to_cd2(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    monkeypatch.setattr(web.xunleipan, "configured", lambda: False)

    class Backend:
        kind = "clouddrive2"

        def download_url(self, path):
            return "http://cd2.local:19798/static/http/cd2.local:19798/false/a.mp4"

    monkeypatch.setattr(web, "storage_backend", lambda: Backend())
    out = web.cloud_direct_link(path="迅雷云盘/a.mp4")
    assert out["ok"] and out["via"] == "clouddrive2"
    assert out["link"].startswith("http://cd2.local:19798/static/")


def test_direct_link_rejects_traversal(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    out = web.cloud_direct_link(path="../etc/passwd")
    assert not out["ok"]


def test_direct_link_by_code_uses_archive_locator(tmp_path, monkeypatch):
    """按番号取直链：定位必须走归档记录（云归档在 CLOUD_DEST）。"""
    from app import core
    _env(tmp_path, monkeypatch)
    monkeypatch.setattr(web.xunleipan, "configured", lambda: True)
    seen = {}

    def fake_direct(p):
        seen["path"] = p
        return {"name": "a.mp4", "link": "https://dl/x", "medias": []}
    monkeypatch.setattr(web.xunleipan, "direct_link", fake_direct)

    class Backend:
        kind = "clouddrive2"

        def list_files(self, rel):
            seen["rel"] = rel
            return [type("E", (), {"name": "MIMK-290.mp4", "size": 1})()]

    monkeypatch.setattr(web, "storage_backend", lambda: Backend())
    core.save_state(web.STATE, {
        "MIMK-290": {"code": "MIMK-290", "rel": "迅雷云盘/X-Drive/MIMK-290",
                     "files": ["MIMK-290.mp4"]}})
    monkeypatch.setattr(storage, "root", lambda: "legacy-root")
    out = web.cloud_direct_link(code="MIMK-290")
    assert out["ok"] is True
    assert seen["rel"] == "迅雷云盘/X-Drive/MIMK-290", f"走了全局根：{seen}"
    assert seen["path"].endswith("MIMK-290.mp4")


@pytest.mark.parametrize("kind", ["openlist", "rclone", "clouddrive2"])
def test_code_link_does_not_query_unrelated_xunlei_cloud(tmp_path, monkeypatch, kind):
    from app import core
    _env(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(web.xunleipan, "configured", lambda: True)
    monkeypatch.setattr(web.xunleipan, "direct_link", lambda path: pytest.fail("cross-cloud lookup"))
    # Even a path named 迅雷云盘 is not an XLP location on another backend.
    root = "迅雷云盘" if kind != "clouddrive2" else "other-provider"
    class Backend:
        def list_files(self, path):
            return [type("E", (), {"name": "a.mp4"})()]
        def link(self, path):
            calls.append(path)
            return "https://actual-backend/file"
    backend = Backend()
    backend.kind = kind
    monkeypatch.setattr(web, "storage_backend", lambda: backend)
    saved = {"code": "AB-1", "backend": kind, "rel": f"{root}/AB-1"}
    if kind == "rclone":
        saved["remote"] = web.RCLONE_REMOTE
    core.save_state(web.STATE, {"AB-1": saved})
    out = web.cloud_direct_link(code="AB-1")
    assert out["ok"] and out["via"] == kind
    assert calls == [f"{root}/AB-1/a.mp4"]


# ─────────────────────────────────────────── 播放地址（同源转发）

def test_play_url_proxies_internal_hosts():
    """内网直链要换成同源播放地址 —— 浏览器连不上 host.docker.internal。"""
    assert web.play_url("http://host.docker.internal:19798/static/x.mp4").startswith(
        "/videoproxy?u=")
    assert web.play_url("http://127.0.0.1:19798/static/x.mp4").startswith("/videoproxy?u=")


def test_play_url_keeps_public_links():
    """外网地址（迅雷官方 API 的 web_content_link）浏览器自己就能连。"""
    assert web.play_url("https://dl.example/signed") == "https://dl.example/signed"
    assert web.play_url("") == ""


def test_direct_link_returns_play_url(tmp_path, monkeypatch):
    """CD2 那条路：返回同源播放地址 + 原始直链。"""
    _env(tmp_path, monkeypatch)
    monkeypatch.setattr(web.xunleipan, "configured", lambda: False)

    class Backend:
        kind = "clouddrive2"

        def download_url(self, path):
            return "http://host.docker.internal:19798/static/http/x/false/a.mp4"

    monkeypatch.setattr(web, "storage_backend", lambda: Backend())
    out = web.cloud_direct_link(path="迅雷云盘/a.mp4")
    assert out["ok"]
    assert out["play"].startswith("/videoproxy?u=")
    assert "host.docker.internal" in out["link"], "原始直链也要留着（备选）"


def test_video_proxy_rejects_foreign_hosts():
    """视频代理只转发云盘直链的宿主机地址，不做任意 URL 转发。"""
    assert "host.docker.internal" in web.VIDEO_HOSTS
    assert "example.com" not in web.VIDEO_HOSTS
    assert "evil.test" not in web.VIDEO_HOSTS


def test_direct_link_rejects_path_outside_archive_root(tmp_path, monkeypatch):
    """只给 path 不给 code 时，路径必须在归档根之下。

    否则登录用户可以对归档根之外的任意云盘文件取直链。
    """
    _env(tmp_path, monkeypatch)
    monkeypatch.setattr(web.xunleipan, "configured", lambda: False)
    monkeypatch.setattr(web, "get_setting",
                        lambda key: {"CLOUD_DEST": "迅雷云盘/X-Drive",
                                     "CLOUD_INBOX": "迅雷云盘/我的资源"}.get(key, ""))

    out = web.cloud_direct_link(path="别人的云盘/私密目录/secret.mp4")
    assert not out["ok"], "别的云盘挂载不该给直链"
    assert "云盘" in out["error"], out


def test_direct_link_allows_path_inside_archive_root(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    monkeypatch.setattr(web.xunleipan, "configured", lambda: False)
    class Backend:
        kind = "clouddrive2"

        def download_url(self, path):
            return "http://cd2.local:19798/static/x.mp4"

    monkeypatch.setattr(web, "storage_backend", lambda: Backend())
    out = web.cloud_direct_link(path="迅雷云盘/X-Drive/MIMK-186/a.mp4")
    assert out["ok"], out


def test_probe_playable_detects_moov_at_head(monkeypatch):
    """文件头有 moov（faststart 过的）→ 能直接播。"""
    payload = b"\x00\x00\x00\x18ftypisom" + b"\x00\x00\x00\x10moov" + b"x" * 100

    class Resp:
        def read(self, n=-1):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(web.urllib.request, "urlopen",
                        lambda *a, **k: Resp())
    assert web.probe_playable("http://cdn.local/a.mp4") is True


def test_probe_playable_detects_missing_moov(monkeypatch):
    """文件头没有 moov（索引在末尾）→ 浏览器会一直转圈。"""
    payload = b"\x00\x00\x00\x18ftypisom" + b"\x00\x00\x00\x10mdat" + b"x" * 100

    class Resp:
        def read(self, n=-1):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(web.urllib.request, "urlopen",
                        lambda *a, **k: Resp())
    assert web.probe_playable("http://cdn.local/a.mp4") is False


def test_probe_playable_returns_none_when_unreachable(monkeypatch):
    """探不到（网络/权限）时返回 None —— 「这条路走不通」，不是「不能播」。

    这跟 False 要分开：False 是「文件本身不能播」（索引在末尾），
    None 是「读不出字节」（网络不通 / 缓存没生效 / 被限流）。
    直链接口靠这个区别来决定「换另一条路」。
    """
    def boom(*a, **k):
        raise OSError("Connection refused")

    monkeypatch.setattr(web.urllib.request, "urlopen", boom)
    assert web.probe_playable("http://cdn.local/a.mp4") is None


def test_direct_link_reports_unknown_playable_when_all_unreachable(tmp_path, monkeypatch):
    """两条路都探不通 → playable=None（如实说「不知道」），不谎称能播。

    谎称能播的后果：前端以为文件没问题，用户对着转圈等 —— 而真因是
    云盘缓存没生效 / 网络不通。
    """
    _env(tmp_path, monkeypatch)
    monkeypatch.setattr(web.xunleipan, "configured", lambda: True)
    monkeypatch.setattr(web.xunleipan, "direct_link", lambda p: {
        "name": "a.mp4", "link": "https://cdn.local/a.mp4", "medias": []})
    monkeypatch.setattr(web, "probe_playable",
                        lambda link, probe_bytes=1 << 20: None)   # 全都探不通

    class Backend:
        kind = "clouddrive2"

        def download_url(self, path):
            return "http://cd2.local:19798/static/a.mp4"

    monkeypatch.setattr(web, "storage_backend", lambda: Backend())
    out = web.cloud_direct_link(path="迅雷云盘/X-Drive/MIMK-186/a.mp4")
    assert out["ok"], out
    assert out["playable"] is None, out


def test_direct_link_falls_back_to_cd2_when_official_unreachable(tmp_path, monkeypatch):
    """迅雷外网直链探不通 → 换 CD2 那条。"""
    _env(tmp_path, monkeypatch)
    monkeypatch.setattr(web.xunleipan, "configured", lambda: True)
    monkeypatch.setattr(web.xunleipan, "direct_link", lambda p: {
        "name": "a.mp4", "link": "https://cdn.local/a.mp4", "medias": []})
    monkeypatch.setattr(web, "probe_playable",
                        lambda link, probe_bytes=1 << 20: None)   # 官方那条探不通

    class Backend:
        kind = "clouddrive2"

        def download_url(self, path):
            return "http://cd2.local:19798/static/a.mp4"

    monkeypatch.setattr(web, "storage_backend", lambda: Backend())
    monkeypatch.setattr(web.urllib.request, "urlopen", lambda *a, **k: _Resp(b"xx" * 100))

    out = web.cloud_direct_link(path="迅雷云盘/X-Drive/MIMK-186/a.mp4")
    assert out["ok"], out
    assert out["via"] == "clouddrive2", out
    assert "cd2.local" in out["link"], out


class _Resp:
    def __init__(self, data):
        self._data = data

    def read(self, n=-1):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False
