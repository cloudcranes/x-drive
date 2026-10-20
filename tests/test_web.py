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
            "SSIS-123": {
                "code": "SSIS-123",
                "path": "gdrive:X-Drive/SSIS-123",
                "link": "https://drive.google.com/drive/folders/AAA",
                "files": ["SSIS-123.mp4"],
                "size": 2 * 1024**3,
                "source": "magnet:?xt=urn:btih:aa",
                "at": "2026-10-20T12:00:00",
            },
            "ABCD-456": {
                "code": "ABCD-456",
                "path": "gdrive:X-Drive/ABCD-456",
                "link": "",
                "files": ["ABCD-456-1.mp4", "ABCD-456-2.mp4"],
                "size": 500 * 1024**2,
                "source": "https://a.com/b.mp4",
                "at": "2026-10-19T10:00:00",
            },
        },
    )
    monkeypatch.setattr(web, "STATE", str(state_file))
    monkeypatch.setattr(web, "AUTH", "")
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
    assert data["stats"]["tasks"] == 2
    assert data["stats"]["files"] == 3
    assert data["stats"]["size_text"] == "2.49GB"
    first = data["tasks"][0]
    assert first["code"] == "SSIS-123"
    assert first["size_text"] == "2.00GB"
    assert first["count"] == 1 and first["link"].startswith("https://drive")
    second = data["tasks"][1]
    assert second["code"] == "ABCD-456" and second["size_text"] == "500MB"
    assert "media" not in first, "下载器模式不再有媒体缩略图字段"


def test_video_rejects_foreign_host(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(server + "/video?u=https://evil.example.com/a.mp4")
    assert exc.value.code == 404, "代理已下线"


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
    assert exc.value.headers["WWW-Authenticate"] is None, "不再触发浏览器原生认证框"
    # 接口未认证返回 401 JSON；页面返回登录页（200，避免浏览器弹原生框）
    body = json.loads(exc.value.read())
    assert body["ok"] is False and body["login"] == "/"

    page_resp = get(server + "/")
    assert page_resp.status == 200
    page = page_resp.read().decode()
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


def test_incremental_render_present():
    """页面必须按 id 增量渲染，且刷新失败不清空列表。"""
    html = web.HTML_PATH.read_text(encoding="utf-8")
    assert "syncFeed" in html, "缺少增量渲染函数"
    assert "mounted" in html, "缺少节点复用表"
    assert "显示的是上次结果" in html, "刷新失败时应保留旧列表"
    assert 'id="refresh"' in html, "缺少手动刷新入口"


def test_metrics_endpoint(server, monkeypatch):
    import tempfile, os
    from pathlib import Path
    d = tempfile.mkdtemp()
    monkeypatch.setattr(web, "METRICS", os.path.join(d, "m.json"))
    monkeypatch.setattr(web, "PENDING_DIR", os.path.join(d, "pending"))
    data = json.loads(get(server + "/api/metrics").read())
    assert data["attempts"] == 0 and data["pending"] == 0
    assert "success_rate" in data


def test_health_reflects_state_readability(server, monkeypatch):
    import tempfile, os
    bad = os.path.join(tempfile.mkdtemp(), "broken.json")
    open(bad, "w").write("{not json")
    monkeypatch.setattr(web, "STATE", bad)
    assert json.loads(get(server + "/api/health").read())["ok"] is False


def test_login_sets_cookie_and_grants_access(server, monkeypatch):
    monkeypatch.setattr(web, "AUTH", "u:p")
    base = server.removeprefix("http://")
    host, port = base.split(":")

    def post(path, body):
        c = http.client.HTTPConnection(host, int(port), timeout=10)
        c.request("POST", path, body=json.dumps(body), headers={"Content-Type": "application/json"})
        r = c.getresponse()
        data, cookie = r.read(), r.getheader("Set-Cookie")
        c.close()
        return r.status, cookie, data

    def get(path, cookie=None):
        c = http.client.HTTPConnection(host, int(port), timeout=10)
        c.request("GET", path, headers={"Cookie": cookie} if cookie else {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    st, cookie, _ = post("/api/login", {"user": "u", "password": "p"})
    assert st == 200 and cookie and "HttpOnly" in cookie
    assert get("/api/tasks")[0] == 401
    assert get("/api/tasks", cookie.split(";")[0])[0] == 200

    st_bad, _, body = post("/api/login", {"user": "u", "password": "x"})
    assert st_bad == 401 and json.loads(body)["ok"] is False

    st_out, cookie_out, _ = post("/api/logout", {})
    assert st_out == 200 and "Max-Age=0" in cookie_out


def test_session_signature_rejects_forgery(monkeypatch):
    monkeypatch.setattr(web, "AUTH", "u:p")
    token = web.make_session()
    assert web.session_ok(token)
    assert not web.session_ok(token[:-1] + ("0" if token[-1] != "0" else "1"))
    assert not web.session_ok("1.deadbeef"), "过期时间必须校验"


def test_health_exposes_counts_only(server):
    d = json.loads(get(server + "/api/health").read())
    assert d["tasks"] >= 1 and d["files"] >= 1
    assert "title" not in json.dumps(d), "健康接口不应泄露内容"


def test_tasks_expose_layout_fields(server):
    """卡片排版依赖 series / kind / cat / month，缺了前端就只能显示一坨文字。"""
    data = json.loads(get(server + "/api/tasks").read())
    first = data["tasks"][0]
    assert first["series"] == "SSIS"
    assert first["month"] == "2026-10"
    assert first["kind"] == "视频" and first["cat"] == "video"
    multi = next(t for t in data["tasks"] if t["code"] == "ABCD-456")
    assert multi["kind"] == "视频 2" and multi["cat"] == "video"


def test_stats_include_series_count(server):
    data = json.loads(get(server + "/api/tasks").read())
    assert data["stats"]["series"] == 2          # SSIS / ABCD
    assert data["stats"]["size_video_text"] == "2.49GB"


def test_series_and_kind_helpers():
    assert web.series_of("SSIS-123") == "SSIS"
    assert web.series_of("FC2-PPV-4422110") == "FC2-PPV"
    assert web.series_of("") == "其他"
    assert web.kind_of(["a.mp4", "b.mkv"]) == "视频 2"
    assert web.kind_of(["a.mp4", "b.jpg"]) == "视频 1 · 图片 1"
    assert web.kind_of(["b.jpg"]) == "图片"
    assert web.kind_of([]) == "空目录"


def test_webui_layout_structure():
    """排版重构的关键结构：侧栏 + 工具条 + 网格 + 队列侧栏，缺一个版面就塌。"""
    html = web.HTML_PATH.read_text(encoding="utf-8")
    for hook in ('id="side"', 'id="nav-series"', 'id="nav-month"', 'id="activefilters"',
                 'id="kindseg"', 'id="sortsel"', 'id="queue"', 'class="shell"',
                 'id="s-series"', 'id="dl-dedupe"'):
        assert hook in html, f"缺少 {hook}"
    assert "grid-template-columns:repeat(auto-fill" in html, "卡片应使用 auto-fill 网格"
    assert "column-count" not in html, "瀑布流应被网格取代"
    # 搜索必须能忽略分隔符（SSIS123 → SSIS-123）
    assert "strip(hay).includes" in html
