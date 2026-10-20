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


def test_duplicate_archived_code_rejected(server, monkeypatch):
    """已归档的番号默认挡下，避免重复占配额；勾掉去重可强制提交。"""
    base, started = server
    core.save_state(web.STATE, {"SSIS-123": {"code": "SSIS-123", "files": ["a.mp4"], "size": 1,
                                             "at": "2026-10-20T00:00:00"}})
    status, data = post(base, "/api/download", {"text": "SSIS-123 magnet:?xt=urn:btih:aa"})
    assert status == 409 and data["duplicate"] == "archived"
    assert not started, "被挡下的不应该起任务"

    status, data = post(base, "/api/download",
                        {"text": "SSIS-123 magnet:?xt=urn:btih:aa", "dedupe": False})
    assert status == 200 and data["ok"] is True and len(started) == 1


def test_duplicate_active_code_rejected(server):
    base, started = server
    web.job_update("jx", stage="downloading", code="ABP-888", percent=10.0)
    status, data = post(base, "/api/download", {"text": "ABP-888 magnet:?xt=urn:btih:bb"})
    assert status == 409 and data["duplicate"] == "active"
    assert not started


def test_stop_job_marks_cancel(server):
    base, _ = server
    web.job_update("jy", stage="downloading", code="X-1", percent=5.0)
    host, port = base.removeprefix("http://").split(":")
    c = http.client.HTTPConnection(host, int(port), timeout=10)
    c.request("POST", "/api/job/stop", body=json.dumps({"id": "jy"}),
              headers={"Content-Type": "application/json"})
    r = c.getresponse(); body = json.loads(r.read()); c.close()
    assert r.status == 200 and body["ok"] is True
    assert web.cancel_requested("jy") is True
    assert web.cancel_requested("nope") is False


def test_stop_unknown_job_404(server):
    base, _ = server
    status, data = post(base, "/api/job/stop", {"id": "ghost"})
    assert status == 404 and data["ok"] is False


def test_clear_finished_jobs_only(server):
    base, _ = server
    web.job_update("j-done", stage="done")
    web.job_update("j-run", stage="downloading", percent=50.0)
    status, data = post(base, "/api/jobs/clear", {})
    assert status == 200
    assert "j-done" not in web.JOBS and "j-run" in web.JOBS
