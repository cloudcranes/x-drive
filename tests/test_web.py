"""展示页服务测试：真实起 http.server，验证 HTML 与 /api/tasks。"""
import base64
import json
import threading
import urllib.error
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
    assert data["tasks"][1]["link"] == ""
    assert data["health"]["parse"] is False


def test_api_health(server):
    assert json.loads(get(server + "/api/health").read())["ok"] is True


def test_unknown_path_404(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(server + "/nope")
    assert exc.value.code == 404


def test_basic_auth(server, monkeypatch):
    monkeypatch.setattr(web, "AUTH", "u:p")
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(server + "/api/tasks")
    assert exc.value.code == 401
    assert exc.value.headers["WWW-Authenticate"].startswith("Basic")
    # 401 返回可读的登录页而不是纯文本
    page = exc.value.read().decode()
    assert "需要登录" in page and "x-drive" in page

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
