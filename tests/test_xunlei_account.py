"""迅雷账号信息（面板 /device/info）：会员、每日下载限额、各盘容量。

为什么在意「每日下载限额」：非会员每天只有 3 个免费下载任务，用完了就得等
隔天（项目里对应 core.next_quota_retry 的次日 00:10）。这是这套部署最常撞的
坎 —— 磁盘还剩很多，任务却提交不上去。而且这个信息**只有面板给**：
云盘官方 API 那套只有 /drive/v1/about，没有会员也没有限额。
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app import xunlei

SPACE = "device_id#b9e13e35dc5b1899c68bfb34b62a5faa"

# 面板首页 HTML 里就带着 uiauth token，客户端从这儿自取
HTML = (
    "<html><script>\n"
    'function uiauth(value){ return "eyJhbGciOiJIUzI1NiJ9.eyJrZXkiOiJVSUF1dGgiLCJleHAiOjk5OTk5OTk5OTl9.sig" }\n'
    "</script></html>"
)

DEVICE_INFO = {
    "user": {
        "name": "18367438244",
        "id": "814926528",
        "picture": "https://xfile2.a.88cdn.com/file/k/avatar/default",
        "vip": {"is_vip": True, "level": 7, "vastype": "5", "isyear": True},
        "group": [{"id": "vip_1_7_5", "expires_at": "2027-05-12T16:00:00Z"}],
    },
    "downloads": [
        {"name": "", "path": "/downloads/", "usage": 421781487616,
         "limit": 2048267386880, "is_root_path": False},
        {"name": "", "path": "/data/", "usage": 348314157056,
         "limit": 955353399296, "is_root_path": True},
    ],
    "task_daily_limit": {
        "title": "今日3个免费下载任务数已用完，开通会员不限次数",
        "messages": ["<a href='paycenter'>开通会员</a>即可享受任务无限次下载"],
        "just_one": False,
    },
    "product_name": "群晖",
    "client_version": "3.21.0",
    "download_version": "9.1207.160.28",
}


def _panel(device_info=None, calls=None):
    info = DEVICE_INFO if device_info is None else device_info

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.split("/index.cgi", 1)[-1] or "/"
        if calls is not None:
            calls.append((request.method, path))
        if path == "/":
            return httpx.Response(200, text=HTML)
        if path == "/device/info/watch":
            return httpx.Response(200, json={"target": SPACE})
        if path == "/device/info":
            return httpx.Response(200, json=info)
        return httpx.Response(404, json={"error": "no route", "path": path})

    return handler


def _run(handler, fn):
    xl = xunlei.Xunlei("http://xl")

    async def main():
        async with xl.client() as client:
            client._transport = httpx.MockTransport(handler)
            return await fn(xl, client)

    return asyncio.run(main())


def test_account_reports_user_and_membership():
    info = _run(_panel(), lambda xl, c: xl.account(c))

    assert info["name"] == "18367438244"
    assert info["id"] == "814926528"
    assert info["is_vip"] is True
    assert info["vip_level"] == 7
    assert info["vip_expires_at"].startswith("2027-05-12")


def test_account_reports_daily_quota_exhausted():
    """每日下载限额用完了要说清楚 —— 这是最常撞的坎。"""
    info = _run(_panel(), lambda xl, c: xl.account(c))

    assert info["quota_hit"] is True
    assert "3个" in info["quota_title"]


def test_account_quota_not_hit_when_no_warning():
    """没有限额提示就是没撞限额，别误报。"""
    body = dict(DEVICE_INFO)
    body["task_daily_limit"] = {"title": "", "messages": None, "just_one": False}
    info = _run(_panel(body), lambda xl, c: xl.account(c))

    assert info["quota_hit"] is False


def test_account_reports_disk_usage():
    info = _run(_panel(), lambda xl, c: xl.account(c))

    disks = {d["path"]: d for d in info["disks"]}
    assert set(disks) == {"/downloads/", "/data/"}
    assert disks["/downloads/"]["usage"] == 421781487616
    assert disks["/downloads/"]["limit"] == 2048267386880


def test_account_handles_missing_vip():
    """非会员账号：user 里没有 vip 段也不能崩。"""
    body = dict(DEVICE_INFO)
    body["user"] = {"name": "x", "id": "1"}
    info = _run(_panel(body), lambda xl, c: xl.account(c))

    assert info["is_vip"] is False
    assert info["vip_level"] == 0
    assert info["name"] == "x"


def test_account_handles_empty_disks():
    body = dict(DEVICE_INFO)
    body["downloads"] = []
    info = _run(_panel(body), lambda xl, c: xl.account(c))

    assert info["disks"] == []


def test_account_uses_device_info_endpoint():
    calls = []
    _run(_panel(calls=calls), lambda xl, c: xl.account(c))

    assert ("GET", "/device/info") in calls
