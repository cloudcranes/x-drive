"""展示页服务测试：真实起 http.server，验证 HTML 与 /api/tasks。"""
import base64
import http.client
import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from app import core, web


@pytest.fixture
def server(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    core.save_state(
        str(state_file),
        {
            "123": {
                "path": "gdrive:X-Drive/Elon_Musk/2026-10/123",
                "link": "https://drive.google.com/drive/folders/AAA",
                "files": ["Elon_Musk_2026-10-19_01.jpg", "Elon_Musk_2026-10-19_02.mp4"],
                "orig": {
                    "Elon_Musk_2026-10-19_01.jpg": "https://pbs.twimg.com/media/aaa.jpg?name=orig",
                    "Elon_Musk_2026-10-19_02.mp4": "https://video.twimg.com/v/1.mp4",
                },
                "title": "hello <b>world</b>",
                "author": "Elon Musk",
                "source": "https://x.com/u/status/123",
                "at": "2026-10-19T12:00:00",
            },
            "456": {
                "path": "gdrive:X-Drive/a/2026-10/456",
                "link": "",
                "files": ["a_2026-10-18_01.png"],
                "title": "",
                "author": "",
                "source": "https://x.com/u/status/456",
                "at": "2026-10-18T10:00:00",
            },
        },
    )
    monkeypatch.setattr(web, "STATE", str(state_file))
    monkeypatch.setattr(web, "PARSE", "http://127.0.0.1:9")
    monkeypatch.setattr(web, "AUTH", "")
    web._health.update(at=0.0, parse=False)
    httpd = web.serve("127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    return urllib.request.urlopen(req, timeout=10)


def test_index_serves_html(server):
    body = get(server + "/").read().decode()
    assert "<title>x-drive" in body
    assert "/api/tasks" in body


def test_api_tasks(server):
    data = json.loads(get(server + "/api/tasks").read())
    assert data["stats"] == {"tasks": 2, "images": 2, "videos": 1, "last": "2026-10-19T12:00:00"}
    assert data["tasks"][0]["id"] == "123"
    assert data["tasks"][0]["images"] == 1 and data["tasks"][0]["videos"] == 1
    first = data["tasks"][0]
    assert first["handle"] == "u" and first["url"] == "https://x.com/u/status/123"
    media = first["media"]
    assert [m["kind"] for m in media] == ["image", "video"]
    assert media[0]["thumb"].startswith("/thumb?") and "pbs.twimg.com" in media[0]["thumb"]
    assert data["tasks"][1]["link"] == ""
    assert data["health"]["parse"] is False


def test_thumb_rejects_foreign_host(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(server + "/thumb?u=" + urllib.parse.quote("https://evil.example.com/a.jpg", safe=""))
    assert exc.value.code == 400


def test_video_rejects_foreign_host(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(server + "/video?u=" + urllib.parse.quote("https://evil.example.com/a.mp4", safe=""))
    assert exc.value.code == 400


def test_video_item_has_play_url(server):
    data = json.loads(get(server + "/api/tasks").read())
    video = [m for m in data["tasks"][0]["media"] if m["kind"] == "video"][0]
    assert video["play"].startswith("/video?") and "video.twimg.com" in video["play"]
    # 视频直链不是图片，不能塞进 <img>
    assert video["thumb"] == ""
    assert video["raw"].endswith(".mp4")


def test_video_proxy_streams_and_passes_range(server, monkeypatch):
    """代理应透传 Range 并流式转发，不能把整个文件读进内存。"""
    calls = {}

    class FakeUpstream:
        status = 206
        headers = {"Content-Type": "video/mp4", "Content-Length": "8",
                   "Content-Range": "bytes 0-7/1000"}
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n: int = -1):
            if calls.get("done"):
                return b""
            calls["done"] = True
            return b"01234567"

    def fake_urlopen(req, timeout=None):
        calls["range"] = req.headers.get("Range")
        calls["referer"] = req.headers.get("Referer")
        return FakeUpstream()

    monkeypatch.setattr(web.urllib.request, "urlopen", fake_urlopen)
    # 用 http.client：monkeypatch 改的是同一个 urllib.request 模块，不能再用它发测试请求
    base = server.removeprefix("http://")
    host, port = base.split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=10)
    path = "/video?u=" + urllib.parse.quote("https://video.twimg.com/a.mp4", safe="")
    conn.request("GET", path, headers={"Range": "bytes=0-7"})
    resp = conn.getresponse()
    body = resp.read()
    assert resp.status == 206
    assert resp.getheader("Content-Range") == "bytes 0-7/1000"
    assert resp.getheader("Accept-Ranges") == "bytes"
    assert body == b"01234567"
    conn.close()
    assert calls["range"] == "bytes=0-7"
    assert calls["referer"] == "https://x.com/"


def test_thumb_requires_url(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(server + "/thumb")
    assert exc.value.code == 400


def test_api_health(server):
    assert json.loads(get(server + "/api/health").read())["ok"] is True


def test_unknown_path_404(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(server + "/nope")
    assert exc.value.code == 404


def test_health_needs_no_auth(server, monkeypatch):
    monkeypatch.setattr(web, "AUTH", "u:p")
    assert json.loads(get(server + "/api/health").read())["ok"] is True


def test_basic_auth(server, monkeypatch):
    monkeypatch.setattr(web, "AUTH", "u:p")
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(server + "/api/tasks")
    assert exc.value.code == 401
    assert exc.value.headers["WWW-Authenticate"].startswith("Basic")
    # 401 返回可读的登录页而不是纯文本
    page = exc.value.read().decode()
    assert "x-drive 控制台" in page and 'id="u"' in page and 'id="p"' in page

    token = base64.b64encode(b"u:p").decode()
    ok = json.loads(get(server + "/api/tasks", {"Authorization": f"Basic {token}"}).read())
    assert ok["stats"]["tasks"] == 2


def test_basic_auth_rejects_wrong_password(server, monkeypatch):
    monkeypatch.setattr(web, "AUTH", "u:p")
    bad = base64.b64encode(b"u:wrong").decode()
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(server + "/api/tasks", {"Authorization": f"Basic {bad}"})
    assert exc.value.code == 401


def test_basic_auth_password_with_colon(server, monkeypatch):
    monkeypatch.setattr(web, "AUTH", "u:p:a")
    token = base64.b64encode(b"u:p:a").decode()
    ok = json.loads(get(server + "/api/tasks", {"Authorization": f"Basic {token}"}).read())
    assert ok["stats"]["tasks"] == 2


def test_url_credentials_warning_absent():
    """页面脚本应处理 URL 内嵌凭据，避免 fetch 报错（静态断言）。"""
    html = (web.HTML_PATH).read_text(encoding="utf-8")
    assert "AUTH_HEADER" in html
    assert "includes credentials" in html or "Request cannot be constructed" in html


def test_small_thumb_downgrades_orig():
    assert web.small_thumb("https://pbs.twimg.com/media/a.png?name=orig") == "https://pbs.twimg.com/media/a.png?name=small"
    assert web.small_thumb("https://pbs.twimg.com/media/a.png?name=small") == "https://pbs.twimg.com/media/a.png?name=small"
    assert web.small_thumb("https://video.twimg.com/a.mp4") == "https://video.twimg.com/a.mp4"


def test_thumb_hits_small_variant(server):
    data = json.loads(get(server + "/api/tasks").read())
    img = [m for m in data["tasks"][0]["media"] if m["kind"] == "image"][0]
    assert "name%3Dsmall" in img["thumb"]
