"""迅雷下载层：token 自取、space 解析、投递参数、状态轮询、落盘校验，全离线。"""
import asyncio
import base64
import json
import os
import time
from pathlib import Path

import httpx
import pytest

from app import fetcher, workspace, xunlei

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

def test_file_index_selects_every_file():
    """sub_file_index 是区间不是序号：单文件 `--1,`，多文件 `0-<n-1>`。

    传 "0"/"1" 只会下种子里的第一个文件 —— 实测只落了一个 .srt 字幕。
    """
    assert xunlei.file_index(1) == "--1,"
    assert xunlei.file_index(2) == "0-1"
    assert xunlei.file_index(9) == "0-8"

def test_add_submits_with_real_space_and_full_range():
    seen = []
    async def raw(request):
        path = request.url.path.split("/index.cgi", 1)[-1] or "/"
        if path == "/":
            return httpx.Response(200, text=HTML)
        seen.append((request.method, path, dict(request.url.params),
                     json.loads(request.content) if request.content else {}))
        if path == "/device/info/watch":
            return httpx.Response(200, json={"target": "device_id#abc"})
        if path == "/drive/v1/tasks":
            return httpx.Response(200, json={"tasks": []})
        if path == "/drive/v1/files":
            return httpx.Response(200, json={"files": [
                {"id": "DL", "kind": "drive#folder", "params": {"RealPath": "/downloads/"}}]})
        if path == "/drive/v1/resource/list":
            return httpx.Response(200, json={"list": {"resources": [
                {"name": "SSIS-123", "file_name": "SSIS-123", "file_size": 1234,
                 "file_count": 3, "meta": {"url": "magnet:?xt=urn:btih:cc"}}]}})
        return httpx.Response(200, json={"HttpStatus": 0, "task": {"id": "T1"}})

    xl = xunlei.Xunlei("http://xl", parent_id="", tmp_root="/downloads")
    assert run(xl, raw, lambda c: xl.add(c, "magnet:?xt=urn:btih:cc", name="SSIS-123")) == "T1"

    submit = [s for s in seen if s[1] == "/drive/v1/task"][0]
    body = submit[3]
    assert submit[2]["space"] == "device_id#abc", "必须投到真实 space，否则进阴影空间"
    assert body["params"]["parent_folder_id"] == "DL", "下载目录 id 应自动识别"
    assert body["params"]["sub_file_index"] == "0-2", "三个文件要选 0-2，否则只下一个"
    assert body["params"]["total_file_count"] == "3"
    assert body["params"]["url"] == "magnet:?xt=urn:btih:cc"

def test_add_reuses_existing_task_for_same_infohash():
    """同一种子已在跑就别再投一份，直接跟着它。"""
    async def raw(request):
        path = request.url.path.split("/index.cgi", 1)[-1] or "/"
        if path == "/":
            return httpx.Response(200, text=HTML)
        if path == "/device/info/watch":
            return httpx.Response(200, json={"target": "device_id#abc"})
        if path == "/drive/v1/tasks":
            return httpx.Response(200, json={"tasks": [{
                "id": "OLD", "phase": xunlei.RUNNING,
                "params": {"info_hash": "aa" * 20}}]})
        return httpx.Response(200, json={"HttpStatus": 0, "task": {"id": "NEW"}})

    xl = xunlei.Xunlei("http://xl", parent_id="FOLDER")
    got = run(xl, raw, lambda c: xl.add(c, f"magnet:?xt=urn:btih:{'aa' * 20}"))
    assert got == "OLD", "重复投递会白占一次任务的下载配额"

# ---------------------------------------------------------------- 状态 / 控制

def _task(phase, progress=0, speed=0, checked=0, err="", real="/downloads/SSIS123"):
    return {"id": "T1", "phase": phase, "progress": progress, "message": "",
            "file_size": 1000, "space": "device_id#abc", "type": "user#download-url",
            "params": {"speed": str(speed), "checked_size": str(checked),
                       "error": err, "real_path": real}}

def test_wait_returns_on_complete_with_real_progress():
    states = [_task(xunlei.RUNNING, 10, 1000, 100), _task(xunlei.RUNNING, 60, 1000, 600),
              _task(xunlei.DONE, 100, 0, 1000)]
    async def raw(request):
        path = request.url.path.split("/index.cgi", 1)[-1] or "/"
        if path == "/":
            return httpx.Response(200, text=HTML)
        if path == "/device/info/watch":
            return httpx.Response(200, json={"target": "device_id#abc"})
        return httpx.Response(200, json={"tasks": [states.pop(0)] if states else []})

    seen = []
    xl = xunlei.Xunlei("http://xl", parent_id="F")
    async def prog(pct, speed, task):
        seen.append(round(pct, 2))
    info = run(xl, raw, lambda c: xl.wait(c, "T1", poll=0, progress=prog))
    assert info["phase"] == xunlei.DONE
    assert seen[-1] == 1.0, "完成时必须回调到 100%"

def test_wait_raises_on_panel_error():
    async def raw(request):
        path = request.url.path.split("/index.cgi", 1)[-1] or "/"
        if path == "/":
            return httpx.Response(200, text=HTML)
        if path == "/device/info/watch":
            return httpx.Response(200, json={"target": "device_id#abc"})
        return httpx.Response(200, json={"tasks": [_task(xunlei.ERROR, err="任务超时")]})

    xl = xunlei.Xunlei("http://xl", parent_id="F")
    with pytest.raises(xunlei.XunleiError, match="任务超时"):
        run(xl, raw, lambda c: xl.wait(c, "T1", poll=0))

def test_wait_detects_dead_torrent():
    async def raw(request):
        path = request.url.path.split("/index.cgi", 1)[-1] or "/"
        if path == "/":
            return httpx.Response(200, text=HTML)
        if path == "/device/info/watch":
            return httpx.Response(200, json={"target": "device_id#abc"})
        return httpx.Response(200, json={"tasks": [_task(xunlei.RUNNING, 5, 0, 10)]})

    xl = xunlei.Xunlei("http://xl", parent_id="F")
    with pytest.raises(xunlei.Stalled):
        run(xl, raw, lambda c: xl.wait(c, "T1", poll=0, stall_timeout=0.001))

def test_wait_times_out():
    async def raw(request):
        path = request.url.path.split("/index.cgi", 1)[-1] or "/"
        if path == "/":
            return httpx.Response(200, text=HTML)
        if path == "/device/info/watch":
            return httpx.Response(200, json={"target": "device_id#abc"})
        return httpx.Response(200, json={"tasks": [_task(xunlei.PENDING, 0)]})

    xl = xunlei.Xunlei("http://xl", parent_id="F")
    with pytest.raises(xunlei.XunleiError, match="超时"):
        run(xl, raw, lambda c: xl.wait(c, "T1", poll=0, timeout=0.001, stall_timeout=0))

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

# ---------------------------------------------------------------- 落盘

def test_done_requires_video_and_quiet(tmp_path):
    work = tmp_path / "SSIS123"
    work.mkdir()
    (work / "a.mp4").write_bytes(b"x" * (2 << 20))
    assert not xunlei.done(work, min_age=60), "刚落的文件可能还在写"
    old = time.time() - 600
    os.utime(work / "a.mp4", (old, old))
    assert xunlei.done(work, min_age=30)

def test_done_rejected_while_temp_file_present(tmp_path):
    work = tmp_path / "SSIS123"
    work.mkdir()
    (work / "a.mp4").write_bytes(b"x" * (2 << 20))
    (work / "a.mp4.xltd").write_bytes(b"x")
    old = time.time() - 600
    for p in work.iterdir():
        os.utime(p, (old, old))
    assert not xunlei.done(work, min_age=30), ".xltd 还在就是在写"

def test_done_rejects_tiny_sample_only(tmp_path):
    work = tmp_path / "SSIS123"
    work.mkdir()
    (work / "sample.mp4").write_bytes(b"x" * 1024)
    old = time.time() - 600
    os.utime(work / "sample.mp4", (old, old))
    assert not xunlei.done(work, min_age=30)

def test_local_path_stays_under_tmp_root():
    assert xunlei.local_path("/downloads/SSIS123", "/downloads") == Path("/downloads/SSIS123")
    assert xunlei.local_path("/data/SSIS123", "/downloads") is None
    assert xunlei.local_path("", "/downloads") is None

def test_owned_matches_key_directory():
    assert xunlei.owned("/downloads/SSIS123", "/downloads", "SSIS-123")
    assert not xunlei.owned("/downloads/SSIS123/sub", "/downloads", "SSIS-123")
    assert not xunlei.owned("/downloads/moviepilot", "/downloads", "SSIS-123")

def test_download_waits_for_settle_after_panel_reports_complete(tmp_path):
    work = tmp_path / "SSIS123"
    calls = []

    async def fake_find(client, url):
        return {}

    async def fake_add(client, url, name="", size=0):
        work.mkdir(exist_ok=True)
        (work / f"{name}.mp4").write_bytes(b"x" * (2 << 20))
        old = time.time() - 600
        os.utime(work / f"{name}.mp4", (old, old))
        return "T9"

    async def fake_wait(client, task_id, url="", **kw):
        return {"id": task_id, "phase": xunlei.DONE, "params": {"real_path": str(work)}}

    async def main():
        xl = xunlei.Xunlei("http://x", parent_id="F", tmp_root=str(tmp_path))
        xl.add, xl.wait, xl.find = fake_add, fake_wait, fake_find
        return await fetcher.download(xl, None, "magnet:?xt=urn:btih:aa", work,
                                      poll=0, settle=0)

    result = asyncio.run(main())
    assert result["task"] == "T9" and result["dir"] == work

# ---------------------------------------------------------------- 图片包

def test_done_accepts_image_only_pack(tmp_path):
    """写真图包只有图片：不能因为「没视频」判成没下完。"""
    work = tmp_path / "SSIS123"
    work.mkdir()
    for i in range(3):
        (work / f"{i}.jpg").write_bytes(b"x" * (2 << 20))
    old = time.time() - 600
    for p in work.iterdir():
        os.utime(p, (old, old))
    assert xunlei.done(work, min_age=30)

def test_content_lists_video_and_image(tmp_path):
    work = tmp_path / "j"
    work.mkdir()
    (work / "a.mp4").write_bytes(b"x" * (2 << 20))
    (work / "b.jpg").write_bytes(b"x" * (2 << 20))
    (work / "ad.txt").write_bytes(b"x" * (2 << 20))
    names = sorted(p.name for p in xunlei.content(work))
    assert names == ["a.mp4", "b.jpg"], "广告文本不算内容"

# ---------------------------------------------------------------- 挂载不一致

def test_download_fails_fast_when_path_outside_tmp_root(tmp_path):
    """迅雷把文件下到别处时不能退回去空等：立刻说清是挂载没对上。"""
    work = tmp_path / "SSIS123"

    async def fake_find(client, url):
        return {}

    async def fake_add(client, url, name="", size=0):
        return "T1"

    async def fake_wait(client, task_id, url="", **kw):
        return {"id": task_id, "phase": xunlei.DONE,
                "params": {"real_path": "/mnt/elsewhere/SSIS123"}}

    async def main():
        xl = xunlei.Xunlei("http://x", parent_id="F", tmp_root="/downloads")
        xl.add, xl.wait, xl.find = fake_add, fake_wait, fake_find
        await fetcher.download(xl, None, "magnet:?xt=urn:btih:aa", work, poll=0)

    with pytest.raises(RuntimeError, match="TMP_ROOT"):
        asyncio.run(main())

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

def test_wait_tolerates_task_not_listed_yet():
    """列表有延迟：前几轮查不到不能立刻判「被删了」。"""
    polls = {"n": 0}
    async def raw(request):
        path = request.url.path.split("/index.cgi", 1)[-1] or "/"
        if path == "/":
            return httpx.Response(200, text=HTML)
        if path == "/device/info/watch":
            return httpx.Response(200, json={"target": "device_id#abc"})
        polls["n"] += 1
        if polls["n"] <= 2:
            return httpx.Response(200, json={"tasks": []})
        return httpx.Response(200, json={"tasks": [_task(xunlei.DONE, 100)]})

    xl = xunlei.Xunlei("http://xl", parent_id="F")
    info = run(xl, raw, lambda c: xl.wait(c, "T1", poll=0))
    assert info["phase"] == xunlei.DONE

def test_stall_tries_pause_resume_before_giving_up():
    """死种先「暂停→继续」重连一次，仍不动才判死：实测能救回卡住的种子。"""
    polls = {"n": 0}
    actions = []

    async def raw(request):
        path = request.url.path.split("/index.cgi", 1)[-1] or "/"
        if path == "/":
            return httpx.Response(200, text=HTML)
        if path == "/device/info/watch":
            return httpx.Response(200, json={"target": "device_id#abc"})
        if path == "/drive/v1/tasks":
            polls["n"] += 1
            # 前两轮有进展，之后彻底停住
            done = 100 if polls["n"] <= 2 else 200
            return httpx.Response(200, json={"tasks": [_task(xunlei.RUNNING, 5, 0, done)]})
        if path.startswith("/method/"):
            actions.append(path)
            return httpx.Response(200, json={})
        return httpx.Response(200, json={})

    xl = xunlei.Xunlei("http://xl", parent_id="F")
    with pytest.raises(xunlei.Stalled):
        run(xl, raw, lambda c: xl.wait(c, "T1", poll=0, stall_timeout=0.001))
    assert "/method/patch/drive/v1/task" in actions, "放弃前应先试一次重连"

def test_stall_revive_can_be_disabled():
    actions = []
    async def raw(request):
        path = request.url.path.split("/index.cgi", 1)[-1] or "/"
        if path == "/":
            return httpx.Response(200, text=HTML)
        if path == "/device/info/watch":
            return httpx.Response(200, json={"target": "device_id#abc"})
        if path == "/drive/v1/tasks":
            return httpx.Response(200, json={"tasks": [_task(xunlei.RUNNING, 5, 0, 10)]})
        actions.append(path)
        return httpx.Response(200, json={})

    xl = xunlei.Xunlei("http://xl", parent_id="F")
    with pytest.raises(xunlei.Stalled):
        run(xl, raw, lambda c: xl.wait(c, "T1", poll=0, stall_timeout=0.001, revive_stall=False))
    assert actions == [], "关掉就不该动任务状态"

def test_download_uses_real_dir_when_xunlei_renames(tmp_path):
    """迅雷重名时落盘成 `<名字>-1`：上层必须拿返回的 dir，而不是自己拼的 dest。"""
    dest = tmp_path / "SSIS123"

    async def fake_find(client, url):
        return {}

    async def fake_add(client, url, name="", size=0):
        # 真实迅雷建目录时不会带我们的标记；归属由 fetcher 在确认落盘后补
        (tmp_path / "SSIS123-1").mkdir()
        return "T1"

    async def fake_wait(client, task_id, url="", **kw):
        real = tmp_path / "SSIS123-1"
        (real / "a.mp4").write_bytes(b"x" * (2 << 20))
        old = time.time() - 600
        os.utime(real / "a.mp4", (old, old))
        return {"id": task_id, "phase": xunlei.DONE, "params": {"real_path": str(real)}}

    async def main():
        xl = xunlei.Xunlei("http://x", parent_id="F", tmp_root=str(tmp_path))
        xl.add, xl.wait, xl.find = fake_add, fake_wait, fake_find
        return await fetcher.download(xl, None, "magnet:?xt=urn:btih:aa", dest, poll=0, settle=0)

    out = asyncio.run(main())
    assert out["dir"] == tmp_path / "SSIS123-1", "必须用迅雷上报的目录"
    assert workspace.owned(tmp_path / "SSIS123-1", "SSIS123"), "重名目录落盘后要补归属标记"

def test_download_refuses_foreign_directory_on_reuse(tmp_path):
    """复用别人手工建的任务时，它的目录不归我们，不能当自己的下载结果。"""
    dest = tmp_path / "SSIS123"

    async def fake_find(client, url):
        return {"id": "OLD", "phase": xunlei.RUNNING, "params": {}}

    async def fake_add(client, url, name="", size=0):
        return "OLD"

    async def fake_wait(client, task_id, url="", **kw):
        other = tmp_path / "HandMade"
        other.mkdir(exist_ok=True)
        return {"id": task_id, "phase": xunlei.DONE, "params": {"real_path": str(other)}}

    async def main():
        xl = xunlei.Xunlei("http://x", parent_id="F", tmp_root=str(tmp_path))
        xl.add, xl.wait, xl.find = fake_add, fake_wait, fake_find
        await fetcher.download(xl, None, "magnet:?xt=urn:btih:aa", dest, poll=0, settle=0)

    with pytest.raises(RuntimeError, match="已有同一个种子在下载"):
        asyncio.run(main())
    assert not (tmp_path / "HandMade").exists(), "别人的目录不该被建出来"


def test_reused_task_never_gets_ownership_mark(tmp_path):
    """复用平台里别的任务时，绝不给它的目录打归属标记 —— 打了就会被清理。"""
    dest = tmp_path / "SSIS123"
    foreign = tmp_path / "external-task-dir"
    foreign.mkdir()
    (foreign / "someone-else.mp4").write_bytes(b"x" * (2 << 20))

    async def fake_find(client, url):
        return {"id": "EXTERNAL", "phase": xunlei.RUNNING, "params": {}}

    async def main():
        xl = xunlei.Xunlei("http://x", parent_id="F", tmp_root=str(tmp_path))
        xl.find = fake_find
        await fetcher.download(xl, None, "magnet:?xt=urn:btih:aa", dest, poll=0, settle=0)

    with pytest.raises(RuntimeError):
        asyncio.run(main())
    assert not (foreign / ".x-drive-owner").exists(), "外部任务目录不能带我们的标记"
    assert (foreign / "someone-else.mp4").exists()


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


def test_download_resumes_own_task_after_restart(tmp_path):
    """重启后同种子任务还在：只要证据（任务 id）对得上，就该续接而不是报错。

    以前一律按「别人提交的任务」处理，自己的任务也续不上 —— 重试永远卡在这里。
    """
    dest = tmp_path / "SSIS321"

    async def fake_find(client, url):
        return {"id": "MINE", "phase": xunlei.RUNNING, "params": {}}

    async def fake_add(client, url, name="", size=0):
        raise AssertionError("续接时不该重新投递")

    async def fake_wait(client, task_id, url="", **kw):
        real = dest
        real.mkdir(exist_ok=True)
        (real / "a.mp4").write_bytes(b"x" * (2 << 20))
        old = time.time() - 600
        os.utime(real / "a.mp4", (old, old))
        return {"id": task_id, "phase": xunlei.DONE, "params": {"real_path": str(real)}}

    async def main():
        xl = xunlei.Xunlei("http://x", parent_id="F", tmp_root=str(tmp_path))
        xl.add, xl.wait, xl.find = fake_add, fake_wait, fake_find
        return await fetcher.download(
            xl, None, "magnet:?xt=urn:btih:aa", dest, poll=0, settle=0,
            session={"task": "MINE"})

    out = asyncio.run(main())
    assert out["task"] == "MINE"
    assert workspace.owned(dest, "SSIS321"), "续接自己的任务后目录要有归属标记"


def test_download_resumes_own_task_by_recorded_infohash(tmp_path):
    """任务 id 没存下来（投递后立刻崩了）时，可用 infohash 证据认回自己的任务。"""
    dest = tmp_path / "SSIS322"
    btih = xunlei.info_hash_of("magnet:?xt=urn:btih:9bef115987e7f72dc1262c20994d4b2db3665c94")

    async def fake_find(client, url):
        return {"id": "MINE2", "phase": xunlei.RUNNING,
                "params": {"info_hash": btih}}

    async def fake_wait(client, task_id, url="", **kw):
        dest.mkdir(exist_ok=True)
        (dest / "a.mp4").write_bytes(b"x" * (2 << 20))
        old = time.time() - 600
        os.utime(dest / "a.mp4", (old, old))
        return {"id": task_id, "phase": xunlei.DONE, "params": {"real_path": str(dest)}}

    async def main():
        xl = xunlei.Xunlei("http://x", parent_id="F", tmp_root=str(tmp_path))
        xl.wait, xl.find = fake_wait, fake_find
        xl.add = None
        return await fetcher.download(
            xl, None, f"magnet:?xt=urn:btih:{btih}", dest, poll=0, settle=0,
            session={"infohash": btih})

    asyncio.run(main())
    assert workspace.owned(dest, "SSIS322")


def test_download_still_refuses_foreign_task_without_evidence(tmp_path):
    """没有本任务证据时，同种子任务仍算别人的，不许接管。"""
    dest = tmp_path / "SSIS323"

    async def fake_find(client, url):
        return {"id": "OTHER", "phase": xunlei.RUNNING,
                "params": {"info_hash": "ffffffffffffffffffffffffffffffffffffffff"}}

    async def main():
        xl = xunlei.Xunlei("http://x", parent_id="F", tmp_root=str(tmp_path))
        xl.find = fake_find
        await fetcher.download(xl, None, "magnet:?xt=urn:btih:aa", dest, poll=0,
                               settle=0, session={"infohash": "aaaa"})

    with pytest.raises(RuntimeError, match="已有同一个种子在下载"):
        asyncio.run(main())
