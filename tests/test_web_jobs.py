"""网页后台的下载任务接口：提交、进度、鉴权。"""
import base64
import http.client
import json
import threading

import pytest

from app import core, web


@pytest.fixture
def server(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    core.save_state(str(state_file), {})
    monkeypatch.setattr(web, "STATE", str(state_file))
    monkeypatch.setattr(web, "AUTH", "")
    monkeypatch.setattr(web, "TMP_ROOT", str(tmp_path / "dl"))
    monkeypatch.setattr(web, "JOBS", {})
    started = []
    monkeypatch.setattr(web, "start_job",
                        lambda src, code, info=None: started.append((src, code)) or "j1")
    monkeypatch.setattr(web, "peek", lambda magnet: {"name": "SSIS-123", "size": 100,
                                                     "count": 1, "screenshots": []})
    httpd = web.serve("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", started
    httpd.shutdown()
    httpd.server_close()


def post(base, path, body, auth=None):
    host, port = base.removeprefix("http://").split(":")
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = "Basic " + base64.b64encode(auth.encode()).decode()
    c = http.client.HTTPConnection(host, int(port), timeout=10)
    c.request("POST", path, body=json.dumps(body), headers=headers)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, json.loads(data or b"{}")


def test_submit_download_creates_job(server):
    base, started = server
    status, data = post(base, "/api/download", {"text": "SSIS-123 magnet:?xt=urn:btih:aa"})
    assert status == 200 and data["ok"] is True
    assert data["code"] == "SSIS-123"
    assert started and started[0][1] == "SSIS-123", "番号要带进任务"


def test_submit_multiple_sources(server):
    base, started = server
    status, data = post(base, "/api/download", {
        "text": "ABCD-1 magnet:?xt=urn:btih:aa https://a.com/b.mkv"})
    assert status == 200 and data["count"] == 2
    assert len(started) == 2


def test_submit_without_source_rejected(server):
    base, _ = server
    status, data = post(base, "/api/download", {"text": "SSIS-123 帮我下"})
    assert status == 400 and data["ok"] is False


def test_download_requires_auth(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    core.save_state(str(state_file), {})
    monkeypatch.setattr(web, "STATE", str(state_file))
    monkeypatch.setattr(web, "AUTH", "u:p")
    monkeypatch.setattr(web, "JOBS", {})
    monkeypatch.setattr(web, "start_job", lambda src, code, info=None: "j1")
    httpd = web.serve("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        status, _ = post(base, "/api/download", {"text": "magnet:?xt=urn:btih:aa"})
        assert status == 401
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_jobs_endpoint_shape(server, monkeypatch):
    base, _ = server
    web.job_update("j9", stage="downloading", percent=42.5, speed=1048576, code="SSIS-9")
    host, port = base.removeprefix("http://").split(":")
    c = http.client.HTTPConnection(host, int(port), timeout=10)
    c.request("GET", "/api/jobs")
    r = c.getresponse()
    data = json.loads(r.read())
    c.close()
    job = data["jobs"][0]
    assert job["id"] == "j9" and job["percent"] == 42.5 and job["speed"] == 1048576


def test_aria2_tasks_survives_rpc_failure(monkeypatch):
    monkeypatch.setattr(web, "ARIA2_RPC", "http://127.0.0.1:1")
    data = web.aria_tasks()
    assert "error" in data and data["active"] == []
