"""网页后台的下载任务接口：提交、进度、鉴权。"""
import base64
import contextlib
import http.client
import json
import threading

import pytest

from app import core, pipeline, web


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


def test_unauthorized_keeps_connection_clean(tmp_path, monkeypatch):
    """鉴权失败也要读掉请求体，否则 keep-alive 复用连接时残留字节会被当成新请求。"""
    state_file = tmp_path / "state.json"
    core.save_state(str(state_file), {})
    monkeypatch.setattr(web, "STATE", str(state_file))
    monkeypatch.setattr(web, "AUTH", "u:p")
    monkeypatch.setattr(web, "JOBS", {})
    httpd = web.serve("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host, port = "127.0.0.1", httpd.server_address[1]
    try:
        c = http.client.HTTPConnection(host, port, timeout=10)
        for path in ("/api/download", "/api/job/stop"):
            c.request("POST", path, body=json.dumps({"text": "magnet:?xt=urn:btih:aa", "id": "x"}),
                      headers={"Content-Type": "application/json"})
            r = c.getresponse()
            assert r.status == 401
            r.read()
        # 同一个连接继续用：请求必须被正常解析（不是 400 Bad request syntax）
        c.request("POST", "/api/jobs/clear", body=json.dumps({}),
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        assert r.status == 401
        r.read()
        c.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_job_records_phase_timing(monkeypatch, tmp_path):
    """任务结束后要把阶段耗时写进 metrics，否则永远不知道慢在哪一步。"""
    from app import metrics
    mpath = tmp_path / "m.json"
    monkeypatch.setattr(web, "METRICS", str(mpath))

    web.record_timing("j1", "SSIS-123", 100 * 1048576,
                      {"download": 10.0, "organize": 0.5, "upload": 5.0}, "ok", started=0.0)
    snap = metrics.timing_snapshot(str(mpath))
    assert snap["phases"]["download"]["avg"] == 10.0
    assert snap["phases"]["upload"]["avg"] == 5.0
    assert snap["upload_mbps"] == 20.0


def test_job_timing_failure_does_not_break_job(monkeypatch):
    """统计写不进去也不能让任务本身失败。"""
    monkeypatch.setattr(web, "METRICS", "/nonexistent-root-dir/blocked/m.json")
    web.record_timing("j1", "X-1", 1, {"download": 1.0}, "ok", started=0.0)


def test_metrics_api_exposes_timing(server, monkeypatch, tmp_path):
    import tempfile, os
    d = tempfile.mkdtemp()
    monkeypatch.setattr(web, "METRICS", os.path.join(d, "m.json"))
    monkeypatch.setattr(web, "PENDING_DIR", os.path.join(d, "pending"))
    web.record_timing("j", "A-1", 50 * 1048576, {"download": 2.0, "upload": 5.0}, "ok", 0.0)
    base, _ = server
    host, port = base.removeprefix("http://").split(":")
    c = http.client.HTTPConnection(host, int(port), timeout=10)
    c.request("GET", "/api/metrics")
    r = c.getresponse()
    data = json.loads(r.read())
    c.close()
    assert "timing" in data
    assert data["timing"]["phases"]["download"]["count"] >= 1


# ---------------------------------------------------------------- 失败自动重试

@contextlib.contextmanager
def run_job_env(monkeypatch, tmp_path, *, download_fails=0, upload_fails=0, fatal=""):
    """把 run_web_job 的外部依赖全部替换掉，只测重试编排逻辑。"""
    work = tmp_path / "work"
    (work / "sub").mkdir(parents=True)
    (work / "sub" / "SSIS-1.mp4").write_bytes(b"x" * (2 << 20))

    calls = {"download": 0, "upload": 0, "sleep": 0}

    monkeypatch.setattr(web, "TMP_ROOT", str(tmp_path))
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(web, "METRICS", str(tmp_path / "m.json"))
    monkeypatch.setattr(web, "JOBS", {})
    monkeypatch.setattr(web, "CANCEL", {})
    monkeypatch.setattr(web, "RETRIES", 2)
    monkeypatch.setattr(web, "RETRY_BASE_DELAY", 0)
    monkeypatch.setattr(pipeline, "job_dir", lambda root, key: work)

    async def fake_download(aria, client, source, dest, progress=None):
        calls["download"] += 1
        if calls["download"] <= download_fails:
            raise RuntimeError(fatal or "连接超时")

    async def fake_client(*a, **kw):
        return None

    monkeypatch.setattr(pipeline, "download", fake_download)

    class FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(web.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(web.pipeline, "cleanup", lambda p: None)

    def fake_upload(path, folder):
        calls["upload"] += 1
        if calls["upload"] <= upload_fails:
            raise RuntimeError(fatal or "rclone 上传失败：connection reset")
        return "https://drive.google.com/x", 0

    monkeypatch.setattr(web, "upload_folder", fake_upload)
    # 退避不真等
    monkeypatch.setattr(web.retry, "backoff_sleep",
                        lambda policy, cancel_check=None, sleep=None: calls.__setitem__(
                            "sleep", calls["sleep"] + 1) or True)
    yield calls, work


def test_download_retries_then_succeeds(monkeypatch, tmp_path):
    with run_job_env(monkeypatch, tmp_path, download_fails=2) as (calls, _):
        web.run_web_job("j1", "magnet:?xt=urn:btih:aa", "SSIS-1")
    assert calls["download"] == 3, "失败 2 次后第 3 次应成功"
    assert web.JOBS["j1"]["stage"] == "done"
    assert web.JOBS["j1"]["retries"] == 2


def test_download_gives_up_after_limit(monkeypatch, tmp_path):
    with run_job_env(monkeypatch, tmp_path, download_fails=99) as (calls, _):
        web.run_web_job("j1", "magnet:?xt=urn:btih:aa", "SSIS-1")
    assert calls["download"] == 3, "RETRIES=2 表示总共 3 次尝试"
    assert web.JOBS["j1"]["stage"] == "failed"


def test_fatal_download_error_is_not_retried(monkeypatch, tmp_path):
    with run_job_env(monkeypatch, tmp_path, download_fails=99,
                     fatal="下载完成但没有找到视频文件") as (calls, _):
        web.run_web_job("j1", "magnet:?xt=urn:btih:aa", "SSIS-1")
    assert calls["download"] == 1, "决定性错误不该重试"
    assert "没有找到视频文件" in web.JOBS["j1"]["error"]


def test_upload_retries_then_succeeds(monkeypatch, tmp_path):
    with run_job_env(monkeypatch, tmp_path, upload_fails=1) as (calls, _):
        web.run_web_job("j1", "magnet:?xt=urn:btih:aa", "SSIS-1")
    assert calls["upload"] == 2
    assert web.JOBS["j1"]["stage"] == "done"


def test_quota_error_on_upload_is_not_retried(monkeypatch, tmp_path):
    with run_job_env(monkeypatch, tmp_path, upload_fails=99,
                     fatal="rclone 上传失败：uploadLimitExceeded") as (calls, _):
        web.run_web_job("j1", "magnet:?xt=urn:btih:aa", "SSIS-1")
    assert calls["upload"] == 1, "配额用完重试没有意义"
    assert web.JOBS["j1"]["stage"] == "failed"


def test_cancel_interrupts_retry_wait(monkeypatch, tmp_path):
    """重试等待期间点停止，必须立刻收尾而不是等满退避时间。"""
    with run_job_env(monkeypatch, tmp_path, download_fails=99) as (calls, _):
        monkeypatch.setattr(web.retry, "backoff_sleep",
                            lambda policy, cancel_check=None, sleep=None: False)
        web.run_web_job("j1", "magnet:?xt=urn:btih:aa", "SSIS-1")
    assert calls["download"] == 1
    assert web.JOBS["j1"]["error"] == "已停止"
