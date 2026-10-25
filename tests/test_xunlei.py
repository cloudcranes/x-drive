"""迅雷面板只读客户端：token 自取、space 解析、任务查询与控制，全离线。

2026-10-25 起本地下载已取消，本模块只服务界面上的「迅雷面板」折叠区：
看外部迅雷里还有什么任务，必要时暂停/继续/删它的**任务记录**。
投递/等待/落盘判定那套用例随本地下载一起删掉了。
"""
import asyncio
import base64
import json
import os
import time

import httpx
import pytest

from app import xunlei

HTML = (
    "<html><script>\n"
    'function uiauth(value){ return "eyJhbGciOiJIUzI1NiJ9.eyJrZXkiOiJVSUF1dGgiLCJleHAiOjk5OTk5OTk5OTl9.sig" }\n'
    "</script></html>"
)
TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJrZXkiOiJVSUF1dGgiLCJleHAiOjk5OTk5OTk5OTl9.sig"

def panel(routes: dict, calls: list | None = None):
    """假的迅雷面板：routes 的键是 (METHOD, path)，值可以是 dict 或 (status, body)。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.split("/index.cgi", 1)[-1] or "/"
        if calls is not None:
            calls.append((request.method, path, request.url.params, request.content))
        key = (request.method, path)
        if key not in routes:
            return httpx.Response(404, json={"error": "no route", "path": path})
        value = routes[key]
        if isinstance(value, tuple):
            return httpx.Response(value[0], json=value[1])
        return httpx.Response(200, json=value)
    return handler

def run(xl, handler, fn):
    async def main():
        async with xl.client() as client:
            client._transport = httpx.MockTransport(handler)
            return await fn(client)
    return asyncio.run(main())

# ---------------------------------------------------------------- token

def test_token_is_read_from_panel_html():
    """token 不用手抄：首页 HTML 里的 uiauth 就是它（面板 3.21 起如此）。"""
    xl = xunlei.Xunlei("http://xl")
    handler = panel({("GET", "/"): (200, {"html": HTML})})
    # 返回体是 HTML 不是 JSON，单独造一个 handler
    async def raw(request):
        return httpx.Response(200, text=HTML)
    assert run(xl, raw, lambda c: xl.token(c)) == TOKEN

def test_token_cached_for_a_minute():
    xl = xunlei.Xunlei("http://xl")
    calls = []
    async def raw(request):
        calls.append(1)
        return httpx.Response(200, text=HTML)
    async def twice(client):
        await xl.token(client)
        await xl.token(client)
    run(xl, raw, twice)
    assert len(calls) == 1, "一分钟内不该反复抓首页"

def test_configured_token_is_fallback():
    """老面板取不到 uiauth 时才用配置里的 token。"""
    xl = xunlei.Xunlei("http://xl", token=TOKEN)
    async def raw(request):
        return httpx.Response(200, text="<html>no uiauth here</html>")
    assert run(xl, raw, lambda c: xl.token(c)) == TOKEN

def test_token_expiry_helper():
    assert not xunlei.token_expired(TOKEN)
    assert xunlei.token_expired("garbage")

# ---------------------------------------------------------------- space / 投递

def test_space_comes_from_watch_target():
    xl = xunlei.Xunlei("http://xl", parent_id="F")
    handler = panel({
        ("GET", "/"): (200, {}),
        ("POST", "/device/info/watch"): {"target": "device_id#abc"},
    })
    async def raw(request):
        if request.url.path.endswith("/index.cgi/"):
            return httpx.Response(200, text=HTML)
        return httpx.Response(200, json={"target": "device_id#abc"})
    assert run(xl, raw, lambda c: xl.space(c)) == "device_id#abc"

def test_space_rejects_unexpected_shape():
    xl = xunlei.Xunlei("http://xl", parent_id="F")
    async def raw(request):
        if request.url.path.endswith("/index.cgi/"):
            return httpx.Response(200, text=HTML)
        return httpx.Response(200, json={"device_space": ""})
    with pytest.raises(xunlei.SubmitError, match="space"):
        run(xl, raw, lambda c: xl.space(c))

def test_parent_id_is_discovered_from_space():
    """下载目录 id 自己找，别让人去 F12 里抄 —— 抄错就是「成功但没下」。"""
    async def raw(request):
        path = request.url.path.split("/index.cgi", 1)[-1] or "/"
        if path == "/":
            return httpx.Response(200, text=HTML)
        if path == "/device/info/watch":
            return httpx.Response(200, json={"target": "device_id#abc"})
        if path == "/drive/v1/files":
            return httpx.Response(200, json={"files": [
                {"id": "CLOUD", "kind": "drive#folder", "name": "我的资源", "params": {}},
                {"id": "DL", "kind": "drive#folder", "name": "downloads",
                 "params": {"RealPath": "/downloads/"}},
            ]})
        return httpx.Response(200, json={})

    xl = xunlei.Xunlei("http://xl", parent_id="")
    assert run(xl, raw, lambda c: xl.parent(c)) == "DL"

def test_configured_parent_wins_without_listing():
    calls = []
    async def raw(request):
        path = request.url.path.split("/index.cgi", 1)[-1] or "/"
        calls.append(path)
        if path == "/":
            return httpx.Response(200, text=HTML)
        return httpx.Response(200, json={})

    xl = xunlei.Xunlei("http://xl", parent_id="FOLDER")
    assert run(xl, raw, lambda c: xl.parent(c)) == "FOLDER"
    assert "/drive/v1/files" not in calls, "配了就不该再多打一次列表"

def test_parent_raises_when_no_download_folder():
    async def raw(request):
        path = request.url.path.split("/index.cgi", 1)[-1] or "/"
        if path == "/":
            return httpx.Response(200, text=HTML)
        if path == "/device/info/watch":
            return httpx.Response(200, json={"target": "device_id#abc"})
        return httpx.Response(200, json={"files": [{"id": "X", "kind": "drive#folder",
                                                    "name": "别处", "params": {}}]})

    xl = xunlei.Xunlei("http://xl", parent_id="")
    with pytest.raises(xunlei.SubmitError, match="下载目录"):
        run(xl, raw, lambda c: xl.parent(c))
# ---------------------------------------------------------------- 状态 / 控制

def _task(phase, progress=0, speed=0, checked=0, err="", real="/downloads/SSIS123"):
    return {"id": "T1", "phase": phase, "progress": progress, "message": "",
            "file_size": 1000, "space": "device_id#abc", "type": "user#download-url",
            "params": {"speed": str(speed), "checked_size": str(checked),
                       "error": err, "real_path": real}}
def test_remove_uses_method_delete():
    """删任务走 `method/delete/drive/v1/tasks`：普通 DELETE 会被面板判 402。"""
    calls = []
    async def raw(request):
        path = request.url.path.split("/index.cgi", 1)[-1] or "/"
        if path == "/":
            return httpx.Response(200, text=HTML)
        if path == "/device/info/watch":
            return httpx.Response(200, json={"target": "device_id#abc"})
        calls.append((request.method, path, dict(request.url.params)))
        return httpx.Response(200, json={})

    xl = xunlei.Xunlei("http://xl", parent_id="F")
    run(xl, raw, lambda c: xl.remove(c, "T1"))
    method, path, params = calls[-1]
    assert (method, path) == ("POST", "/method/delete/drive/v1/tasks")
    assert params["task_ids"] == "T1" and params["space"] == "device_id#abc"

def test_pause_uses_patch_with_spec():
    calls = []
    async def raw(request):
        path = request.url.path.split("/index.cgi", 1)[-1] or "/"
        if path == "/":
            return httpx.Response(200, text=HTML)
        if path == "/device/info/watch":
            return httpx.Response(200, json={"target": "device_id#abc"})
        if path == "/drive/v1/tasks":
            return httpx.Response(200, json={"tasks": [_task(xunlei.RUNNING, 5)]})
        calls.append((request.method, path, json.loads(request.content)))
        return httpx.Response(200, json={})

    xl = xunlei.Xunlei("http://xl", parent_id="F")
    run(xl, raw, lambda c: xl.pause(c, "T1"))
    method, path, body = calls[-1]
    assert (method, path) == ("POST", "/method/patch/drive/v1/task")
    assert json.loads(body["set_params"]["spec"])["phase"] == "pause"
def test_find_matches_by_url_before_infohash_is_known():
    """刚投的任务 info_hash 还是空的，这时候只能靠 url 认。"""
    url = "magnet:?xt=urn:btih:dd" * 1
    async def raw(request):
        path = request.url.path.split("/index.cgi", 1)[-1] or "/"
        if path == "/":
            return httpx.Response(200, text=HTML)
        if path == "/device/info/watch":
            return httpx.Response(200, json={"target": "device_id#abc"})
        return httpx.Response(200, json={"tasks": [{
            "id": "NEW", "phase": xunlei.PENDING, "params": {"url": url, "info_hash": ""}}]})

    xl = xunlei.Xunlei("http://xl", parent_id="F")
    assert run(xl, raw, lambda c: xl.find(c, url)).get("id") == "NEW"
def test_token_uses_absolute_url(monkeypatch):
    """取 token 必须用绝对地址。

    相对路径在已设 base_url 的客户端上会被 httpx 当成新地址，
    报 "Request URL is missing an 'http://' or 'https://' protocol"，
    面板明明可达却永远取不到 token（真实实例上踩过）。
    """
    seen = {}

    class FakeResp:
        status_code = 200
        text = 'function uiauth(value){ return "jwt-token" }'

        def raise_for_status(self):
            return None

    class FakeClient:
        async def get(self, url, **kw):
            seen["url"] = url
            return FakeResp()

    xl = xunlei.Xunlei("http://panel.local:2345", "", "/tmp", "")
    token = asyncio.run(xl.token(FakeClient()))
    assert token == "jwt-token"
    assert seen["url"].startswith("http://panel.local:2345/"), f"必须是绝对地址，实际 {seen['url']}"