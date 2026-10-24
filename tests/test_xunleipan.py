"""迅雷官方直链（api-pan.xunlei.com）：照 greasyfork 脚本的接口契约实现。

脚本「迅雷云盘」拿直链走两个接口：
  GET /drive/v1/files?parent_id=…        列目录
  GET /drive/v1/files/{id}               单文件详情 → web_content_link + medias[]
鉴权是浏览器 localStorage 里 credentials_{clientid} 的 token_type/access_token
加 clientid 头。直链有有效期，所以不缓存、每次现取。
"""
from __future__ import annotations

import httpx
import pytest

from app import xunleipan


def transport(handler):
    return httpx.MockTransport(handler)


def client(handler):
    return httpx.Client(transport=transport(handler), base_url=xunleipan.API)


def _env(monkeypatch, token="abc123", client_id="cid-1", device="dev-1"):
    monkeypatch.setattr(xunleipan, "_setting",
                        lambda key: {"XLPAN_TOKEN": token,
                                     "XLPAN_CLIENT_ID": client_id,
                                     "XLPAN_DEVICE_ID": device}.get(key, ""))


# ─────────────────────────────────────────── 鉴权头

def test_headers_carry_token_clientid_device(monkeypatch):
    _env(monkeypatch)
    got = {}

    def handler(request: httpx.Request) -> httpx.Response:
        got.update(dict(request.headers))
        return httpx.Response(200, json={"files": []})

    xunleipan.list_files(client=client(handler))
    assert got["authorization"] == "Bearer abc123", "裸 token 自动补 Bearer"
    assert got["clientid"] == "cid-1"
    assert got["x-device-id"] == "dev-1"


def test_headers_keep_explicit_bearer(monkeypatch):
    _env(monkeypatch, token="Bearer xyz")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer xyz"
        return httpx.Response(200, json={"files": []})

    xunleipan.list_files(client=client(handler))


def test_configured_needs_both_values(monkeypatch):
    _env(monkeypatch, token="", client_id="cid")
    assert xunleipan.configured() is False
    _env(monkeypatch, token="t", client_id="")
    assert xunleipan.configured() is False
    _env(monkeypatch)
    assert xunleipan.configured() is True


def test_device_id_follows_script_truncation(monkeypatch):
    """脚本取 deviceid 点后前 32 字符（localStorage 是 wdi10.<hex>）。"""
    _env(monkeypatch, device="wdi10." + "a" * 64)
    assert xunleipan._device_id() == "a" * 32
    _env(monkeypatch, device="plain-id-12345")
    assert xunleipan._device_id() == "plain-id-12345"


# ─────────────────────────────────────────── 列目录 / 详情

def test_list_files_parses_items(monkeypatch):
    _env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["parent_id"] == "pid1"
        return httpx.Response(200, json={"files": [
            {"id": "f1", "name": "MIMK-290.mp4", "kind": "drive#file",
             "phase": "PHASE_TYPE_COMPLETE", "trashed": False},
            {"id": "d1", "name": "子目录", "kind": "drive#folder", "trashed": False},
        ]})

    items = xunleipan.list_files("pid1", client=client(handler))
    assert [i["name"] for i in items] == ["MIMK-290.mp4", "子目录"]
    assert items[0]["kind"] == "drive#file"


def test_file_info_returns_web_content_link_and_medias(monkeypatch):
    """脚本的「显示文件链接」用 web_content_link；播放列表用 medias 各档。"""
    _env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "f1", "name": "MIMK-290.mp4", "size": 4408303886,
            "web_content_link": "https://dl.example/v1/signed",
            "medias": [
                {"media_name": "原始画质", "link": {"url": "https://ignored"}},
                {"media_name": "720P", "link": {"url": "https://dl.example/720"}},
            ]})

    info = xunleipan.file_info("f1", client=client(handler))
    assert info["link"] == "https://dl.example/v1/signed"
    # 「原始画质」按脚本约定用 web_content_link，不是 medias 里的 url
    assert info["medias"][0] == {"name": "原始画质", "url": "https://dl.example/v1/signed"}
    assert info["medias"][1] == {"name": "720P", "url": "https://dl.example/720"}


def test_api_error_raises_pan_error(monkeypatch):
    _env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "invalid_token"})

    with pytest.raises(xunleipan.PanError):
        xunleipan.file_info("f1", client=client(handler))


# ─────────────────────────────────────────── 路径解析与直链

def test_resolve_walks_path(monkeypatch):
    _env(monkeypatch)
    listing = {
        "": [{"id": "c1", "name": "迅雷云盘", "kind": "drive#folder", "trashed": False}],
        "c1": [{"id": "c2", "name": "X-Drive", "kind": "drive#folder", "trashed": False}],
        "c2": [{"id": "f1", "name": "MIMK-290.mp4", "kind": "drive#file", "trashed": False}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        parent = request.url.params.get("parent_id", "")
        return httpx.Response(200, json={"files": listing.get(parent, [])})

    fid = xunleipan.resolve("迅雷云盘/X-Drive/MIMK-290.mp4", client=client(handler))
    assert fid == "f1"
    assert xunleipan.resolve("迅雷云盘/没有这个", client=client(handler)) == ""


def test_direct_link_end_to_end(monkeypatch):
    _env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/drive/v1/files":
            parent = request.url.params.get("parent_id", "")
            rows = {"": [{"id": "f1", "name": "a.mp4", "kind": "drive#file",
                          "trashed": False}]}.get(parent, [])
            return httpx.Response(200, json={"files": rows})
        return httpx.Response(200, json={
            "name": "a.mp4", "web_content_link": "https://dl.example/x"})

    out = xunleipan.direct_link("a.mp4", client=client(handler))
    assert out["link"] == "https://dl.example/x" and out["name"] == "a.mp4"


def test_direct_link_missing_path_raises(monkeypatch):
    _env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"files": []})

    with pytest.raises(xunleipan.PanError):
        xunleipan.direct_link("没有的文件.mp4", client=client(handler))
