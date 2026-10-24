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
                 'id="s-series"', 'id="q"', 'id="submit-slot"', 'id="resource-slot"'):
        assert hook in html, f"缺少 {hook}"
    assert "grid-template-columns:repeat(auto-fill" in html, "卡片应使用 auto-fill 网格"
    assert "column-count" not in html, "瀑布流应被网格取代"
    # 搜索必须能忽略分隔符（SSIS123 → SSIS-123）
    assert "strip(hay).includes" in html


# ---------------------------------------------------------------- 删除归档

def _post_json(url, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_archive_delete_requires_confirm(server, monkeypatch):
    """没 confirm 只能看到将删什么，绝不能真删。"""
    called = []
    monkeypatch.setattr(web, "purge_archives", lambda codes: called.append(codes) or [])
    status, body = _post_json(server + "/api/archive/delete", {"code": "SSIS-123"})
    assert status == 200
    assert body["ok"] and body["code"] == "SSIS-123"
    assert body["size"] == 2 * 1024**3
    assert "confirm" in body["hint"]
    assert called == [], "没有 confirm 就不许删"


def test_archive_delete_with_confirm(server, monkeypatch):
    monkeypatch.setattr(web, "purge_archives",
                        lambda codes: [{"code": c, "ok": True} for c in codes])
    status, body = _post_json(server + "/api/archive/delete",
                              {"code": "SSIS-123", "confirm": True})
    assert status == 200
    assert body["deleted"][0]["code"] == "SSIS-123"


def test_archive_delete_unknown_code(server):
    status, body = _post_json(server + "/api/archive/delete",
                              {"code": "NOPE-1", "confirm": True})
    assert status == 404
    assert not body["ok"]


def test_archive_delete_needs_code(server):
    status, body = _post_json(server + "/api/archive/delete", {"confirm": True})
    assert status == 400


def test_archive_delete_keeps_record_when_purge_fails(server, monkeypatch):
    """Drive 删失败时记录必须留着，否则档案还在盘上却查不到了。"""
    monkeypatch.setattr(web, "purge_archives", lambda codes: [])
    status, body = _post_json(server + "/api/archive/delete",
                              {"code": "SSIS-123", "confirm": True})
    assert status == 502 and not body["ok"]
    assert "SSIS-123" in core.load_state(web.STATE), "失败不能丢记录"


# ---------------------------------------------------------------- 存储后端接口

def test_api_storage_shape(server):
    data = json.loads(get(server + "/api/storage").read())
    assert data["ok"] is True
    assert data["kind"] in ("rclone", "openlist", "clouddrive2")
    assert data["root"]
    assert "PASSWORD" not in json.dumps(data), "接口不能回显任何密钥字段"


def test_api_storage_test_failure_still_200(server, monkeypatch):
    """连不通也要 200 + ok:false：面板要能把原因直接显示出来。"""
    monkeypatch.setattr(web, "storage_test",
                        lambda: {"ok": False, "kind": "openlist", "root": "X-Drive",
                                 "latency_ms": 3, "error": "openlist：没配 OPENLIST_URL"})
    status, body = _post_json(server + "/api/storage/test", {})
    assert status == 200 and body["ok"] is False
    assert "OPENLIST_URL" in body["error"]


def test_api_storage_test_success(server, monkeypatch):
    monkeypatch.setattr(web, "storage_test",
                        lambda: {"ok": True, "kind": "clouddrive2", "root": "X-Drive",
                                 "latency_ms": 12, "detail": "clouddrive2 x: 可读写"})
    status, body = _post_json(server + "/api/storage/test", {})
    assert status == 200 and body["ok"] is True and "可读写" in body["detail"]


def test_storage_endpoints_need_auth(server, monkeypatch):
    monkeypatch.setattr(web, "AUTH", "u:p")
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(server + "/api/storage")
    assert exc.value.code == 401
    status, body = _post_json(server + "/api/storage/test", {})
    assert status == 401 and body["ok"] is False


def test_saving_settings_resets_storage_cache(server, tmp_path, monkeypatch):
    """存完设置必须清掉后端实例：否则「保存成功但下一次上传还走旧配置」。"""
    from app import settings as settings_mod, storage, store
    inst = settings_mod.Settings(store.Store(str(tmp_path / "jobs.db")))
    monkeypatch.setattr(web, "_settings", inst)
    monkeypatch.setattr(settings_mod, "bind", lambda _i: None)
    calls = []
    monkeypatch.setattr(storage, "reset", lambda: calls.append(1))
    status, body = _post_json(server + "/api/settings",
                              {"updates": {"CD2_URL": "http://127.0.0.1:19798"}})
    assert status == 200 and body["ok"] is True
    assert calls, "保存设置后必须让存储实例缓存失效"


# ---------------------------------------------------------------- 首页媒体墙

def test_api_wall_shape(server, monkeypatch):
    monkeypatch.setattr(web, "wall_feed",
                        lambda page=1, which="": {"ok": True, "source": "javbus",
                                                  "page": page, "more": True, "rows": 2,
                                                  "items": [{"code": "A-1", "cover": "u"}]})
    data = json.loads(get(server + "/api/wall?page=2").read())
    assert data["ok"] is True and data["page"] == 2 and data["items"][0]["code"] == "A-1"


def test_api_wall_hides_when_disabled(server, monkeypatch):
    """设置里关掉就明确回 off，前端据此整块不显示（不是报错）。"""
    monkeypatch.setattr(web, "get_setting",
                        lambda key, d=None: False if key == "WALL_ON" else d)
    data = json.loads(get(server + "/api/wall").read())
    assert data["ok"] is False and data["off"] is True


def test_api_wall_never_breaks_the_page(server, monkeypatch):
    """媒体墙是锦上添花：上游炸了也只回 ok:false，首页其它部分照常。"""
    def boom(page=1, which=""):
        raise RuntimeError("上游 500")
    monkeypatch.setattr(web.wall, "feed", boom)
    data = json.loads(get(server + "/api/wall").read())
    assert data["ok"] is False and "上游 500" in data["error"]
    # 首页本身照样能开
    assert "<title>x-drive" in get(server + "/").read().decode()


# ---------------------------------------------------------------- 封面缓存

def test_proxy_rejects_unknown_host(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(server + "/proxy?u=https%3A%2F%2Fevil.example.com%2Fa.jpg")
    assert exc.value.code == 400, "代理只放行已知图床，不做任意转发"


def test_proxy_serves_from_cache(server, tmp_path, monkeypatch):
    """命中缓存就不再打上游：图床握手不稳，一张图只该成功取一次。"""
    monkeypatch.setenv("COVER_CACHE", str(tmp_path / "covers"))
    url = "https://www.javbus.com/pics/thumb/cached.jpg"
    web.imgcache.put(url, b"\xff\xd8JPEG", "image/jpeg")
    real = urllib.request.urlopen
    called = []

    def guard(req, *a, **k):
        # 只拦「去取封面」，测试自己的 HTTP 请求照常发
        target = req.full_url if hasattr(req, "full_url") else str(req)
        if "javbus.com/pics" in target:
            called.append(target)
            raise RuntimeError("不该联网")
        return real(req, *a, **k)

    monkeypatch.setattr(web.urllib.request, "urlopen", guard)
    resp = get(server + "/proxy?u=" + urllib.parse.quote(url, safe=""))
    assert resp.status == 200 and resp.read() == b"\xff\xd8JPEG"
    assert called == []


# ---------------------------------------------------------------- 图片缓存接口

def test_api_images_reports_usage(server, tmp_path, monkeypatch):
    monkeypatch.setenv("COVER_CACHE", str(tmp_path / "covers"))
    web.imgcache.put("https://www.javbus.com/pics/thumb/a.jpg", b"z" * 2048, "image/jpeg")
    data = json.loads(get(server + "/api/images").read())
    assert data["ok"] is True and data["files"] == 1
    assert data["bytes"] == 2048 and data["dir"].endswith("covers")
    assert "limit_mb" in data


def test_api_images_clear_needs_confirm(server, tmp_path, monkeypatch):
    monkeypatch.setenv("COVER_CACHE", str(tmp_path / "covers"))
    web.imgcache.put("https://www.javbus.com/pics/thumb/b.jpg", b"z" * 10, "image/jpeg")
    status, body = _post_json(server + "/api/images/clear", {})
    assert status == 200 and "confirm" in body["hint"]
    assert web.imgcache.stats()["files"] == 1, "没 confirm 不许清"


def test_api_images_clear(server, tmp_path, monkeypatch):
    monkeypatch.setenv("COVER_CACHE", str(tmp_path / "covers"))
    web.imgcache.put("https://www.javbus.com/pics/thumb/c.jpg", b"z" * 10, "image/jpeg")
    status, body = _post_json(server + "/api/images/clear", {"confirm": True})
    assert status == 200 and body["removed"] == 1
    assert body["after"]["files"] == 0


def test_images_endpoints_need_auth(server, monkeypatch):
    monkeypatch.setattr(web, "AUTH", "u:p")
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(server + "/api/images")
    assert exc.value.code == 401
    status, body = _post_json(server + "/api/images/clear", {"confirm": True})
    assert status == 401 and body["ok"] is False
