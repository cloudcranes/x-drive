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
