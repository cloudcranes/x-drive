"""迅雷官方直链（api-pan.xunlei.com）：照 greasyfork 脚本的接口契约实现。

脚本「迅雷云盘」拿直链走两个接口：
  GET /drive/v1/files?parent_id=…        列目录
  GET /drive/v1/files/{id}               单文件详情 → web_content_link + medias[]
鉴权是浏览器 localStorage 里 credentials_{clientid} 的 token_type/access_token
加 clientid 头。直链有有效期，所以不缓存、每次现取。
"""
from __future__ import annotations

import json

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
    assert "x-captcha-token" not in got, "没配就不发这个头"


def test_headers_carry_captcha_token_when_set(monkeypatch):
    """风控头：captcha_invalid 是实测踩到的（API 400 error_code 9）。"""
    values = {"XLPAN_TOKEN": "t", "XLPAN_CLIENT_ID": "c",
              "XLPAN_DEVICE_ID": "", "XLPAN_CAPTCHA_TOKEN": "cap-9"}
    monkeypatch.setattr(xunleipan, "_setting", lambda key: values.get(key, ""))
    got = {}

    def handler(request: httpx.Request) -> httpx.Response:
        got.update(dict(request.headers))
        return httpx.Response(200, json={"files": []})

    xunleipan.list_files(client=client(handler))
    assert got["x-captcha-token"] == "cap-9"


def test_captcha_error_says_what_to_fill(monkeypatch):
    _env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "captcha_invalid", "error_code": 9})

    with pytest.raises(xunleipan.PanError) as err:
        xunleipan.list_files(client=client(handler))
    assert "x-captcha-token" in str(err.value)


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


def test_resolve_drops_mount_prefix_when_absent(monkeypatch):
    """CD2 路径带挂载名 `迅雷云盘/`，官方 API 根下没有它（实测）：自动去首段。"""
    _env(monkeypatch)
    listing = {
        "": [{"id": "c1", "name": "我的资源", "kind": "drive#folder", "trashed": False}],
        "c1": [{"id": "f1", "name": "迅雷11 绿色版.zip", "kind": "drive#file", "trashed": False}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        parent = request.url.params.get("parent_id", "")
        return httpx.Response(200, json={"files": listing.get(parent, [])})

    fid = xunleipan.resolve("迅雷云盘/我的资源/迅雷11 绿色版.zip", client=client(handler))
    assert fid == "f1"


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


# ─────────────────────────────────────────── captcha 签发与写操作

def test_sign_captcha_posts_contract(monkeypatch):
    """契约来自公开 chunk service-captcha：
    POST {shield}/v1/shield/captcha/init
    body {client_id, action, device_id, captcha_token, meta}
    """
    _env(monkeypatch)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"captcha_token": "ck0.new", "expires_in": 300})

    token, expires = xunleipan.sign_captcha("DELETE:/drive/v1/files/x",
                                            previous="ck0.old", client=client(handler))
    assert token == "ck0.new" and expires == 300
    assert seen["url"].endswith("/v1/shield/captcha/init")
    assert seen["body"]["action"] == "DELETE:/drive/v1/files/x"
    assert seen["body"]["captcha_token"] == "ck0.old", "必须回传旧 token 走轮换链"
    assert seen["body"]["device_id"]


def test_list_files_trashed_flag(monkeypatch):
    """回收站过滤必须用 filters（实测 trashed=true / filter=trash 都被静默忽略，
    照着那种列表删会误伤活目录）。"""
    _env(monkeypatch)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"files": []})

    xunleipan.list_files(trashed=True, client=client(handler))
    assert seen.get("filters") == '{"trashed":{"eq":true}}'
    assert "trashed" not in seen, "trashed=true 会被服务端忽略，必须用 filters"


def test_delete_file_rotates_captcha(monkeypatch):
    """删除流程：先带旧 token 打一次（被判 captcha）→ 回传旧 token 签发新的
    → 带新 token 再打一次才生效。跳过第一步会被判 captcha_invalid。"""
    _env(monkeypatch)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/shield/captcha/init"):
            calls.append("sign")
            return httpx.Response(200, json={"captcha_token": "ck0.fresh", "expires_in": 300})
        calls.append("delete")
        tok = request.headers.get("x-captcha-token")
        if tok == "ck0.fresh":
            return httpx.Response(200, json={})
        return httpx.Response(400, json={"error": "captcha_invalid"})

    out = xunleipan.delete_file("f1", client=client(handler))
    assert out == "已删除"
    assert calls == ["delete", "sign", "delete"], calls


def test_delete_file_reports_when_captcha_still_refused(monkeypatch):
    _env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/shield/captcha/init"):
            return httpx.Response(200, json={"captcha_token": "ck0.fresh", "expires_in": 300})
        return httpx.Response(400, json={"error": "captcha_invalid"})

    with pytest.raises(xunleipan.PanError) as err:
        xunleipan.delete_file("f1", client=client(handler))
    assert "captcha" in str(err.value)
