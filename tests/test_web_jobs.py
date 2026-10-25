"""网页后台的下载任务接口：提交、进度、鉴权。"""
import base64
import contextlib
import http.client
import io
import json
import threading
import time
from pathlib import Path

import pytest

from app import core, meta, pipeline, store, web, workspace

_INJECTED = {}


def _inject(monkeypatch, **overrides):
    """注入可热更新参数：这些参数已迁到 get_setting，monkeypatch 模块常量已失效。

    用模块级 dict 累加：连续多次 _inject 不会互相覆盖。
    """
    real = web.get_setting
    _INJECTED.update(overrides)
    monkeypatch.setattr(web, "get_setting",
                       lambda key, d=None: _INJECTED[key] if key in _INJECTED else real(key))


@pytest.fixture(autouse=True)
def clean_leases():
    """每个用例清空进程级的 LEASES / 心跳标志。

    这里只做清理，不预置租约：租约该由 claim_owner 真正领，测试不许绕过去。
    """
    _INJECTED.clear()
    web.LEASES.clear()
    web._lease_live.clear()
    yield
    _INJECTED.clear()
    web.LEASES.clear()
    web._lease_live.clear()


@pytest.fixture
def server(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    core.save_state(str(state_file), {})
    monkeypatch.setattr(web, "STATE", str(state_file))
    monkeypatch.setattr(web, "AUTH", "")
    monkeypatch.setattr(web, "TMP_ROOT", str(tmp_path / "dl"))
    monkeypatch.setattr(web, "JOBS", {})
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
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


def test_bodyless_post_routes_drain_keep_alive(tmp_path, monkeypatch):
    """带 body 的 POST 打到「不读请求体」的路由（如 /api/scrape/status），
    复用同一连接再发 GET 必须正常解析，不能残留成 `{}GET` 报 501/400。"""
    state_file = tmp_path / "state.json"
    core.save_state(str(state_file), {})
    monkeypatch.setattr(web, "STATE", str(state_file))
    monkeypatch.setattr(web, "AUTH", "")
    monkeypatch.setattr(web, "JOBS", {})
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    httpd = web.serve("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host, port = "127.0.0.1", httpd.server_address[1]
    try:
        c = http.client.HTTPConnection(host, port, timeout=10)
        # 这 5 条路由通过鉴权后都不读请求体，曾导致 keep-alive 残留
        for path in ("/api/scrape/status", "/api/jobs/clear", "/api/digest",
                     "/api/restore/status", "/api/logout"):
            c.request("POST", path, body=json.dumps({"code": "ABC-001"}),
                      headers={"Content-Type": "application/json"})
            r = c.getresponse()
            r.read()
        # 复用同一连接：后续 GET 必须正常返回 JSON，不能是 501/400
        c.request("GET", "/api/jobs")
        r = c.getresponse()
        body = r.read()
        assert r.status == 200, f"复用连接后 GET /api/jobs 应 200，实际 {r.status}: {body[:120]!r}"
        json.loads(body)
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
    monkeypatch.setattr(web, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(web, "METRICS", str(tmp_path / "m.json"))
    monkeypatch.setattr(web, "JOBS", {})
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    # 任务表要有这条记录，延期/去重才有对象；调度器放行时会一并授予执行租约
    web.STORE.start("j1", "SSIS-1", "magnet:?xt=urn:btih:aa")
    web.STORE.claim_owner("j1", web.lease_owner("j1"))
    monkeypatch.setattr(web, "CANCEL", {})
    monkeypatch.setattr(web, "RETRIES", 2)
    monkeypatch.setattr(web, "RETRY_BASE_DELAY", 0)
    monkeypatch.setattr(pipeline, "job_dir", lambda root, key: work)

    async def fake_download(xl, client, source, dest, progress=None, **opts):
        calls["download"] += 1
        if calls["download"] <= download_fails:
            raise RuntimeError(fatal or "连接超时")
        return {"task": "T1", "dir": dest, "waited": 0.0, "reused": False}

    async def fake_client(*a, **kw):
        return None

    monkeypatch.setattr(pipeline, "download", fake_download)

    def fake_stage(source_dir, target_dir, key):
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "SSIS-1.mp4").write_bytes(b"x" * (2 << 20))
        return [target_dir / "SSIS-1.mp4"]

    monkeypatch.setattr(pipeline, "stage_copy", fake_stage)

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
                        lambda policy, cancel_check=None, sleep=None, **kw: calls.__setitem__(
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
    job = web.JOBS["j1"]
    assert job["stage"] == "deferred", "配额类是等待，不是失败"
    # 定到次日自动重跑，且延期中仍占着这个番号
    deferred = web.STORE.get("j1")["extra"].get("defer_until")
    assert deferred and deferred > time.time(), "配额类要延期而不是丢弃"
    assert "SSIS-1" in web.active_codes()


def test_cancel_interrupts_retry_wait(monkeypatch, tmp_path):
    """重试等待期间点停止，必须立刻收尾而不是等满退避时间。"""
    with run_job_env(monkeypatch, tmp_path, download_fails=99) as (calls, _):
        monkeypatch.setattr(web.retry, "backoff_sleep",
                            lambda policy, cancel_check=None, sleep=None, **kw: False)
        web.run_web_job("j1", "magnet:?xt=urn:btih:aa", "SSIS-1")
    assert calls["download"] == 1
    assert web.JOBS["j1"]["error"] == "已停止"


# ---------------------------------------------------------------- 归档回收

def test_cleanup_preview_is_read_only(server, monkeypatch):
    """预览接口绝不能删东西。"""
    base, _ = server
    core.save_state(web.STATE, {
        f"CODE-{i}": {"code": f"CODE-{i}", "size": 1024**3,
                      "at": f"2026-01-{i+1:02d}T00:00:00"} for i in range(5)
    })
    _inject(monkeypatch, CLEANUP_MAX_GB=2.0)
    _inject(monkeypatch, CLEANUP_KEEP_RECENT=1)
    called = []
    monkeypatch.setattr(web, "purge_archives", lambda codes: called.append(codes) or [])

    host, port = base.removeprefix("http://").split(":")
    c = http.client.HTTPConnection(host, int(port), timeout=10)
    c.request("GET", "/api/cleanup/preview")
    r = c.getresponse(); data = json.loads(r.read()); c.close()

    assert data["dry_run"] is True
    assert data["delete_count"] > 0, "应该算出有东西可删"
    assert not called, "预览不能执行删除"
    assert len(core.load_state(web.STATE)) == 5, "state 不能被改动"


def test_cleanup_run_without_confirm_is_dry_run(server, monkeypatch):
    """不带 confirm 的调用一律按 dry-run 处理，防止误删。"""
    base, _ = server
    core.save_state(web.STATE, {
        f"C-{i}": {"code": f"C-{i}", "size": 1024**3, "at": f"2026-01-{i+1:02d}T00:00:00"}
        for i in range(5)})
    _inject(monkeypatch, CLEANUP_MAX_GB=1.0)
    _inject(monkeypatch, CLEANUP_KEEP_RECENT=1)
    called = []
    monkeypatch.setattr(web, "purge_archives", lambda codes: called.append(codes) or [])

    status, data = post(base, "/api/cleanup/run", {})
    assert status == 200 and data["dry_run"] is True
    assert not called


def test_cleanup_run_with_confirm_executes(server, monkeypatch):
    base, _ = server
    core.save_state(web.STATE, {
        f"D-{i}": {"code": f"D-{i}", "size": 1024**3, "at": f"2026-01-{i+1:02d}T00:00:00"}
        for i in range(5)})
    _inject(monkeypatch, CLEANUP_MAX_GB=1.0)
    _inject(monkeypatch, CLEANUP_KEEP_RECENT=1)
    called = []
    monkeypatch.setattr(web, "purge_archives",
                        lambda codes: called.append(codes) or [{"code": c, "ok": True} for c in codes])

    status, data = post(base, "/api/cleanup/run", {"confirm": True})
    assert status == 200 and data["dry_run"] is False
    assert called and called[0], "确认后应真正执行"


def test_cleanup_never_touches_active_job_codes(server, monkeypatch):
    """正在下载的番号即使在删除清单候选里也不能被清掉。"""
    base, _ = server
    core.save_state(web.STATE, {
        "BUSY-001": {"code": "BUSY-001", "size": 10 * 1024**3, "at": "2026-01-01T00:00:00"},
        "OLD-002": {"code": "OLD-002", "size": 10 * 1024**3, "at": "2026-01-02T00:00:00"},
    })
    web.job_update("jx", stage="downloading", code="BUSY-001", percent=50.0)
    _inject(monkeypatch, CLEANUP_MAX_GB=1.0)
    _inject(monkeypatch, CLEANUP_KEEP_RECENT=0)

    plan = web.build_cleanup_plan(dry_run=True)
    assert "BUSY-001" not in [i["code"] for i in plan["delete"]]


def test_purge_archives_keeps_state_on_failure(server, monkeypatch):
    """Drive 删不掉时不能把 state 记录也删了，否则归档会「消失」但文件还在。"""
    base, _ = server
    core.save_state(web.STATE, {"KEEP-1": {"code": "KEEP-1", "size": 1}})
    monkeypatch.setattr(web, "rclone", lambda *a, **kw: (1, "", "purge 失败"))
    removed = web.purge_archives(["KEEP-1"])
    assert removed == []
    assert "KEEP-1" in core.load_state(web.STATE)

def test_panel_tasks_hides_finished(monkeypatch):
    """看板只列未结束的迅雷任务：历史完成记录几十条，塞满侧栏没意义。"""
    class FakeXl:
        def client(self, *a, **kw):
            raise AssertionError("不该真连")

    tasks = [
        {"id": "A", "name": "跑着", "phase": "PHASE_TYPE_RUNNING", "progress": 42,
         "file_size": 100, "params": {"speed": "1024", "real_path": "/downloads/A"}},
        {"id": "B", "name": "旧的", "phase": "PHASE_TYPE_COMPLETE", "progress": 100,
         "file_size": 100, "params": {}},
        {"id": "C", "name": "死了", "phase": "PHASE_TYPE_ERROR", "progress": 3,
         "file_size": 100, "params": {"error": "任务超时"}},
    ]

    class Xl(FakeXl):
        async def tasks(self, client):
            return tasks
        async def __aenter__(self):  # pragma: no cover
            return self

    monkeypatch.setattr(web, "xunlei_client", lambda: Xl())
    Xl.client = lambda self, *a, **kw: _NullCtx()
    data = web.panel_tasks()
    assert [t["id"] for t in data["tasks"]] == ["A", "C"]
    assert data["tasks"][1]["error"] == "任务超时"

class _NullCtx:
    async def __aenter__(self): return None
    async def __aexit__(self, *exc): return False

def test_xunlei_control_routes_actions(monkeypatch):
    """看板上的暂停/继续/删除走同一个入口，按 action 分派到客户端方法。"""
    calls = []

    class Xl:
        def client(self, *a, **kw): return _NullCtx()
        async def pause(self, client, tid): calls.append(("pause", tid))
        async def remove(self, client, tid): calls.append(("remove", tid))
        async def control(self, client, tid, phase): calls.append(("control", tid, phase))

    monkeypatch.setattr(web, "xunlei_client", lambda: Xl())
    for action, want in (("pause", ("pause", "T1")),
                         ("delete", ("remove", "T1")),
                         ("resume", ("control", "T1", "running"))):
        calls.clear()
        monkeypatch.setattr(web, "asyncio", __import__("asyncio"))
        code, data = post_free(("POST", "/api/xunlei/control", {"id": "T1", "action": action}))
        assert code == 200 and data["ok"], (action, data)
        assert calls == [want], (action, calls)

def test_xunlei_control_rejects_unknown_action():
    code, data = post_free(("POST", "/api/xunlei/control", {"id": "T1", "action": "selfdestruct"}))
    assert code == 400 and not data["ok"]

def post_free(spec):
    """直接调 Handler.do_POST：不启服务器，鉴权留空时自动放行。"""
    method, path, payload = spec
    body = json.dumps(payload).encode()
    h = web.Handler.__new__(web.Handler)
    h.path = path
    h.headers = {"Content-Length": str(len(body))}
    h.rfile = io.BytesIO(body)
    out = {}
    def send_response(code, *a): out["code"] = code
    def send_header(*a): pass
    def end_headers(): pass
    h.send_response = send_response
    h.send_header = send_header
    h.end_headers = end_headers
    h.wfile = io.BytesIO()
    h._authorized = lambda: True
    h.do_POST()
    return out["code"], json.loads(h.wfile.getvalue().decode() or "{}")

# ---------------------------------------------------------------- 归档改名

def _archive(tmp_path, monkeypatch, state: dict, drive_dirs=()):
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(web, "RCLONE_CONFIG", "/dev/null")
    monkeypatch.setattr(web, "DRIVE_ROOT", "X-Drive")
    monkeypatch.setattr(web, "RCLONE_REMOTE", "gdrive")
    core.save_state(web.STATE, state)
    calls = []

    def fake_rclone(*args, timeout=0):
        calls.append(args)
        if args[0] == "lsf":
            return 0, "\n".join(drive_dirs), ""
        if args[0] == "link":
            return 0, "https://drive/folder/" + args[1].rsplit("/", 1)[-1], ""
        return 0, "", ""

    monkeypatch.setattr(web, "rclone", fake_rclone)
    return calls

def test_rename_archive_moves_drive_and_state(tmp_path, monkeypatch):
    calls = _archive(tmp_path, monkeypatch, {
        "SSIS-123": {"code": "SSIS-123", "path": "gdrive:X-Drive/SSIS-123",
                     "files": ["SSIS-123.mp4"], "size": 1, "at": "2026-10-20T00:00:00"},
    })
    out = web.rename_archive("SSIS-123", "SSIS-124")
    assert out["ok"] and out["code"] == "SSIS-124"
    state = core.load_state(web.STATE)
    assert list(state) == ["SSIS-124"], "旧键要删掉，不能留两条指向同一个 Drive 目录"
    assert state["SSIS-124"]["code"] == "SSIS-124"
    assert state["SSIS-124"]["path"] == "gdrive:X-Drive/SSIS-124"
    assert state["SSIS-124"]["link"].endswith("SSIS-124")
    assert ("move", "gdrive:X-Drive/SSIS-123", "gdrive:X-Drive/SSIS-124") in calls
    assert ("rmdir", "gdrive:X-Drive/SSIS-123") in calls, "搬完把空目录清掉"

def test_rename_archive_sanitises_name(tmp_path, monkeypatch):
    _archive(tmp_path, monkeypatch, {
        "SSIS-123": {"code": "SSIS-123", "path": "x", "files": [], "size": 0, "at": ""},
    })
    out = web.rename_archive("SSIS-123", "  abp 456  ")
    assert out["ok"] and out["code"] == "ABP-456"

def test_rename_archive_rejects_unknown_and_duplicate(tmp_path, monkeypatch):
    _archive(tmp_path, monkeypatch, {
        "SSIS-123": {"code": "SSIS-123", "path": "x", "files": [], "size": 0, "at": ""},
        "ABP-456": {"code": "ABP-456", "path": "y", "files": [], "size": 0, "at": ""},
    })
    assert web.rename_archive("NOPE-1", "SSIS-999")["ok"] is False
    dup = web.rename_archive("SSIS-123", "ABP-456")
    assert dup["ok"] is False and "已经存在" in dup["error"]

def test_rename_archive_rejects_existing_drive_dir(tmp_path, monkeypatch):
    """state 里没记录但 Drive 里已经有同名目录时不能盲搬，否则会混在一起。"""
    _archive(tmp_path, monkeypatch, {
        "SSIS-123": {"code": "SSIS-123", "path": "x", "files": [], "size": 0, "at": ""},
    }, drive_dirs=["SSIS-123", "SSIS-999"])
    out = web.rename_archive("SSIS-123", "SSIS-999")
    assert out["ok"] is False and "Drive" in out["error"]

def test_rename_archive_rejects_bad_name(tmp_path, monkeypatch):
    _archive(tmp_path, monkeypatch, {
        "SSIS-123": {"code": "SSIS-123", "path": "x", "files": [], "size": 0, "at": ""},
    })
    assert web.rename_archive("SSIS-123", "../etc")["ok"] is False, "路径穿越要挡掉"

def test_rename_archive_keeps_record_when_move_fails(tmp_path, monkeypatch):
    _archive(tmp_path, monkeypatch, {
        "SSIS-123": {"code": "SSIS-123", "path": "x", "files": [], "size": 0, "at": ""},
    })
    monkeypatch.setattr(web, "rclone",
                        lambda *a, timeout=0: (1, "", "permission denied"))
    out = web.rename_archive("SSIS-123", "SSIS-124")
    assert out["ok"] is False
    assert list(core.load_state(web.STATE)) == ["SSIS-123"], "搬失败要原样留着，重试安全"

# ---------------------------------------------------------------- 规范名

def test_normalize_plan_only_touches_detectable_codes(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    core.save_state(web.STATE, {
        "MIMK-286-中文字幕": {"code": "MIMK-286-中文字幕", "files": [], "size": 0, "at": ""},
        "第一會所新片@SIS001@MIMK-285-U": {"code": "第一會所新片@SIS001@MIMK-285-U", "files": [], "size": 0, "at": ""},
        "SSIS-123": {"code": "SSIS-123", "files": [], "size": 0, "at": ""},
        "unclassified-20261022-010203": {"code": "", "files": [], "size": 0, "at": ""},
    })
    plan = web.normalize_plan()
    got = {i["code"]: i["to"] for i in plan["rename"]}
    assert got == {"MIMK-286-中文字幕": "MIMK-286",
                   "第一會所新片@SIS001@MIMK-285-U": "MIMK-285"}
    assert plan["total"] == 4

def test_normalize_plan_skips_collisions(tmp_path, monkeypatch):
    """两个旧名指向同一新名时整组跳过：自动改名不能把两条记录合成一条。"""
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    core.save_state(web.STATE, {
        "MIMK-286-中文字幕": {"code": "MIMK-286-中文字幕", "files": [], "size": 0, "at": ""},
        "MIMK-286-FHD": {"code": "MIMK-286-FHD", "files": [], "size": 0, "at": ""},
        "SSIS-123": {"code": "SSIS-123", "files": [], "size": 0, "at": ""},
        "abp 456": {"code": "abp 456", "files": [], "size": 0, "at": ""},
        "ABP-456": {"code": "ABP-456", "files": [], "size": 0, "at": ""},
    })
    plan = web.normalize_plan()
    assert plan["rename"] == []
    assert {i["to"] for i in plan["skipped"]} == {"MIMK-286", "ABP-456"}

def test_normalize_plan_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    core.save_state(web.STATE, {"SSIS-123": {"code": "SSIS-123", "files": [], "size": 0, "at": ""}})
    assert web.normalize_plan()["rename"] == [], "已经规范的不该反复搬"

# ---------------------------------------------------------------- 自动改名

def test_auto_name_uses_release_date_and_title(tmp_path, monkeypatch):
    """有元数据就用规范名：`YYYY-MM-DD 番号 标题`，去掉文件名非法字符。"""
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    core.save_state(web.STATE, {"SSIS-123": {
        "code": "SSIS-123", "files": ["a.mp4"], "size": 1, "at": "2026-10-20T00:00:00",
        "meta": {"release_date": "2026-09-12", "title": "絶頂/中出し:特集"}}})
    plan = web.normalize_plan()
    assert plan["rename"] == [{"code": "SSIS-123",
                               "to": "2026-09-12 SSIS-123 絶頂-中出し-特集"}], plan

def test_auto_name_without_meta_falls_back_to_code(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    core.save_state(web.STATE, {"ssis_123": {"code": "ssis_123", "files": [], "size": 0, "at": ""}})
    assert web.normalize_plan()["rename"] == [{"code": "ssis_123", "to": "SSIS-123"}]

def test_auto_name_truncates_long_titles(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    core.save_state(web.STATE, {"SSIS-123": {
        "code": "SSIS-123", "files": [], "size": 0, "at": "",
        "meta": {"title": "很" * 300}}})
    got = web.normalize_plan()["rename"][0]["to"]
    assert got.startswith("SSIS-123 ") and len(got) <= 100

# ---------------------------------------------------------------- 未分类自愈

def test_adopt_plan_reads_code_from_file_names(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    core.save_state(web.STATE, {
        "unclassified-20261022-010203": {"code": "", "files": ["hhd800.com@FC2-PPV-4519894.mp4"],
                                         "size": 1, "at": ""},
        "unclassified-20261022-020304": {"code": "", "files": ["random.mp4"], "size": 1, "at": ""},
    })
    plan = web.adopt_plan()
    assert plan["adopt"] == [{"code": "unclassified-20261022-010203",
                              "to": "FC2-PPV-4519894"}]
    assert plan["skipped"][0]["reason"].startswith("文件名里认不出")

def test_adopt_plan_skips_code_already_archived(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    core.save_state(web.STATE, {
        "SSIS-123": {"code": "SSIS-123", "files": [], "size": 1, "at": ""},
        "unclassified-20261022-010203": {"code": "", "files": ["SSIS-123.mp4"], "size": 1, "at": ""},
    })
    assert web.adopt_plan()["adopt"] == []
    assert "已在归档里" in web.adopt_plan()["skipped"][0]["reason"]

# ---------------------------------------------------------------- 体积估算

def test_series_median_gives_unknown_size_a_budget(tmp_path, monkeypatch):
    state = {"SSIS-100": {"size": 2 << 30}, "SSIS-101": {"size": 4 << 30},
             "ABP-1": {"size": 9 << 30}}
    assert web.series_median("SSIS-999", state) == 3 << 30
    assert web.series_median("XXX-1", state) == 0, "没有同系列历史就返回 0，交给调用方"

def test_start_job_estimates_total_when_size_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "JOBS", {})
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    monkeypatch.setattr(web, "STATE", "/dev/null")
    monkeypatch.setattr(web, "series_median", lambda code, state: 5 << 30)
    monkeypatch.setattr(web, "admit", lambda job_id: True)
    job_id = web.start_job("magnet:?xt=urn:btih:aa", "SSIS-999")
    assert web.JOBS[job_id]["total"] == 5 << 30
    assert web.JOBS[job_id]["estimated"] is True

# ---------------------------------------------------------------- 僵尸任务清理

def _panel(monkeypatch, tasks):
    class Xl:
        def client(self, *a, **kw): return _NullCtx()
    monkeypatch.setattr(web, "panel_tasks", lambda: {"tasks": tasks})
    monkeypatch.setattr(web, "xunlei_client", lambda: Xl())
    return Xl()

def test_clean_panel_errors_only_old_failures(monkeypatch):
    old = "2026-10-01T00:00:00+08:00"
    fresh = "2026-10-22T00:00:00+08:00"
    _panel(monkeypatch, [
        {"id": "A", "name": "old-err", "phase": "PHASE_TYPE_ERROR", "updated": old},
        {"id": "B", "name": "new-err", "phase": "PHASE_TYPE_ERROR", "updated": fresh},
        {"id": "C", "name": "running", "phase": "PHASE_TYPE_RUNNING", "updated": old},
    ])
    plan = web.clean_panel_errors(7)
    assert [i["id"] for i in plan["delete"]] == ["A"]
    assert "confirm" in plan["hint"]

def test_clean_panel_errors_confirm_deletes(monkeypatch):
    removed = []
    class Xl:
        def client(self, *a, **kw): return _NullCtx()
        async def remove(self, client, task_id): removed.append(task_id)
    monkeypatch.setattr(web, "xunlei_client", lambda: Xl())
    monkeypatch.setattr(web, "panel_tasks", lambda: {"tasks": [
        {"id": "A", "name": "old-err", "phase": "PHASE_TYPE_ERROR",
         "updated": "2026-10-01T00:00:00+08:00"}]})
    out = web.clean_panel_errors(7, confirm=True)
    assert out["deleted"] == ["A"] and removed == ["A"]

# ---------------------------------------------------------------- 订阅建议

def test_subscribe_suggestions_count_and_attach_cached_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(meta, "IDS_PATH", str(tmp_path / "ids.json"))
    core.save_state(web.STATE, {
        "SSIS-1": {"meta": {"actors": ["三上悠亜"], "series": "SSIS"}},
        "SSIS-2": {"meta": {"actors": ["三上悠亜", "別人"], "series": "SSIS"}},
        "ABP-1": {"meta": {"actors": ["別人"], "series": "ABP"}},
    })
    meta._save_id("三上悠亜", "actor", "Av2e")
    out = web.subscribe_suggestions()
    top = {a["name"]: a for a in out["actors"]}
    assert top["三上悠亜"]["count"] == 2 and top["三上悠亜"]["id"] == "Av2e"
    assert top["別人"]["id"] == "", "没缓存 ID 就留空，由前端禁用按钮"
    assert [s["name"] for s in out["series"]][0] in ("SSIS", "ABP")

def test_subscribe_from_suggestion_uses_cached_id(monkeypatch, tmp_path):
    monkeypatch.setattr(meta, "IDS_PATH", str(tmp_path / "ids.json"))
    monkeypatch.setattr(web, "SUBS", str(tmp_path / "subs.json"))
    # 订阅与待办现在存在任务库里，需要独立的库，否则会写进真实 /data
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    meta._save_id("三上悠亜", "actor", "Av2e")
    out = web.subscribe_from_suggestion("actor", "三上悠亜")
    assert out["ok"] and out["sub"]["id"] == "Av2e"

def test_subscribe_from_suggestion_reports_missing_id(monkeypatch, tmp_path):
    monkeypatch.setattr(meta, "IDS_PATH", str(tmp_path / "ids.json"))
    monkeypatch.setattr(web, "SUBS", str(tmp_path / "subs.json"))
    # 订阅与待办现在存在任务库里，需要独立的库，否则会写进真实 /data
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))

    async def none(name, kind="actor", client=None, timeout=0):
        return None

    monkeypatch.setattr(meta, "resolve_id", none)
    out = web.subscribe_from_suggestion("actor", "查无此人")
    assert out["ok"] is False and "javdb ID" in out["error"]

def test_subscribe_from_suggestion_accepts_typed_id(monkeypatch, tmp_path):
    """自动解析不到时允许手填；填过就进缓存，下次不用再问。"""
    monkeypatch.setattr(meta, "IDS_PATH", str(tmp_path / "ids.json"))
    monkeypatch.setattr(web, "SUBS", str(tmp_path / "subs.json"))
    # 订阅与待办现在存在任务库里，需要独立的库，否则会写进真实 /data
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    out = web.subscribe_from_suggestion("series", "某系列", sub_id="https://javdb.com/series/AbC12")
    assert out["ok"] and out["sub"]["id"] == "AbC12"
    assert meta.cached_id("某系列", "series") == "AbC12"

def test_subscribe_from_suggestion_rejects_garbage_typed_id(monkeypatch, tmp_path):
    monkeypatch.setattr(meta, "IDS_PATH", str(tmp_path / "ids.json"))
    monkeypatch.setattr(web, "SUBS", str(tmp_path / "subs.json"))
    # 订阅与待办现在存在任务库里，需要独立的库，否则会写进真实 /data
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    out = web.subscribe_from_suggestion("actor", "某人", sub_id="???")
    assert out["ok"] is False and "看不懂" in out["error"]

def test_magnet_search_returns_sorted_candidates(monkeypatch):
    async def fake(code, client=None, limit=8):
        assert code == "SSIS-123"
        return [{"magnet": "magnet:?xt=urn:btih:aa", "size_text": "4.2GB", "has_subtitle": True}]

    monkeypatch.setattr(web.sources, "search", fake)
    out = web.magnet_search("ssis_123")
    assert out["ok"] and out["code"] == "SSIS-123"
    assert out["items"][0]["has_subtitle"] is True

def test_magnet_search_requires_code():
    assert web.magnet_search("")["ok"] is False

def test_magnet_search_soft_fails(monkeypatch):
    async def boom(code, client=None, limit=8):
        raise RuntimeError("javbus 挂了")
    monkeypatch.setattr(web.sources, "search", boom)
    out = web.magnet_search("SSIS-123")
    assert out["ok"] is False and "javbus" in out["error"]

def test_magnet_search_includes_show_info(monkeypatch):
    """搜磁力只给体积/标签不够，必须带上封面、标题、日期、演员，才知道选的是哪部。"""
    async def fake_search(code, client=None, limit=8):
        return [{"magnet": "magnet:?xt=urn:btih:aa", "size_text": "6.68GB",
                 "has_subtitle": True, "size_bytes": 1, "is_hd": True,
                 "seeders": 0, "source": "javbus", "title": ""}]

    async def fake_lookup(code, path=None, client=None):
        return {"code": code, "title": "むすんで、つないで。", "date": "2021-07-14",
                "duration": "150 分钟", "maker": "エスワン", "actors": ["乙白さやか"],
                "cover": "https://www.javbus.com/pics/cover/8cd9_b.jpg"}

    monkeypatch.setattr(web.sources, "search", fake_search)
    monkeypatch.setattr(web.meta, "lookup", fake_lookup)
    out = web.magnet_search("ssis_123")
    assert out["ok"] and out["code"] == "SSIS-123"
    assert out["info"]["title"] == "むすんで、つないで。"
    assert out["info"]["actors"] == ["乙白さやか"]
    assert out["info"]["cover"].startswith("https://")
    assert len(out["items"]) == 1

def test_magnet_search_survives_meta_failure(monkeypatch):
    """元数据查不到不能连磁力一起丢。"""
    async def fake_search(code, client=None, limit=8):
        return [{"magnet": "magnet:?xt=urn:btih:aa", "size_text": "1GB"}]

    async def boom(code, path=None, client=None):
        raise RuntimeError("javbus 超时")

    monkeypatch.setattr(web.sources, "search", fake_search)
    monkeypatch.setattr(web.meta, "lookup", boom)
    monkeypatch.setattr(web.meta, "cached", lambda code, path=None: None)
    out = web.magnet_search("SSIS-123")
    assert out["ok"] and out["info"] == {} and len(out["items"]) == 1

def test_duplicate_submit_hits_unique_index(tmp_path, monkeypatch):
    """并发提交同一番号：第二次必须被唯一索引挡下，返回 409。"""
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    monkeypatch.setattr(web, "JOBS", {})
    monkeypatch.setattr(web, "WAITING", {})
    monkeypatch.setattr(web, "_queued_at", {})
    monkeypatch.setattr(web, "admit", lambda job_id: True)
    first = web.start_job("magnet:?xt=urn:btih:aa", "SSIS-404", {"size": 1})
    second = web.start_job("magnet:?xt=urn:btih:bb", "SSIS-404", {"size": 1})
    assert first and second == ""
    assert len(web.jobs_snapshot()) == 1

def test_recover_jobs_returns_unfinished_with_task_id(tmp_path, monkeypatch):
    db = str(tmp_path / "jobs.db")
    st = store.Store(db)
    monkeypatch.setattr(web, "STORE", st)
    st.start("j1", "SSIS-1", "magnet:a")
    st.update("j1", stage="downloading", xunlei_task="T77")
    rows = web.recover_jobs()
    assert rows == [{"id": "j1", "code": "SSIS-1", "source": "magnet:a",
                     "stage": "downloading", "xunlei_task": "T77"}]

# ---------------------------------------------------------------- 端到端（离线）

def _e2e_env(tmp_path, monkeypatch, upload_fails=False):
    from app import workspace as ws
    monkeypatch.setenv("TMP_ROOT", str(tmp_path / "dl"))
    monkeypatch.setattr(web, "TMP_ROOT", str(tmp_path / "dl"))
    monkeypatch.setattr(web, "STATE_DIR", str(tmp_path / "work"))
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(web, "METRICS", str(tmp_path / "m.json"))
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    monkeypatch.setattr(web, "JOBS", {})
    monkeypatch.setattr(web, "CANCEL", {})
    monkeypatch.setattr(web, "RETRIES", 0)
    core.save_state(web.STATE, {})

    async def fake_download(xl, client, source, dest, progress=None, on_task=None, **opts):
        real = Path(str(dest) + "-1")        # 迅雷因重名改了目录名
        real.mkdir(parents=True, exist_ok=True)
        (real / "v.mp4").write_bytes(b"x" * (2 << 20))
        (real / "ad.txt").write_bytes(b"ad")
        # 真实 fetcher 在这一步校验路径并补标记（带任务证据）；桩要复现同一契约
        workspace.claim(dest, workspace.key_of(dest.name), source)
        workspace.adopt(real, workspace.key_of(dest.name), source, task_id="T1")
        if on_task:
            on_task("T1", {"reused": False})
        return {"task": "T1", "dir": real, "waited": 0.0, "reused": False}

    async def fake_lookup(code, path=None, client=None):
        return {"code": code, "title": "标题"}

    monkeypatch.setattr(pipeline, "download", fake_download)
    monkeypatch.setattr(meta, "lookup", fake_lookup)
    if upload_fails:
        monkeypatch.setattr(web, "upload_folder",
                            lambda p, f: (_ for _ in ()).throw(RuntimeError("上传失败")))
    else:
        monkeypatch.setattr(web, "upload_folder", lambda p, f: ("https://d/" + f, 0))
    monkeypatch.setattr(web, "series_median", lambda code, state: 2 << 20)

    def fake_admit(job_id):
        # 与真实 admit() 一致：放行时用 lease_owner() 铸的 token 真正领取
        web.STORE.claim_owner(job_id, web.lease_owner(job_id))
        return True

    monkeypatch.setattr(web, "admit", fake_admit)
    return ws

def test_end_to_end_success_cleans_up(tmp_path, monkeypatch):
    ws = _e2e_env(tmp_path, monkeypatch)
    # 相邻有一个名字近似的外部目录：清理不许碰它
    neighbour = tmp_path / "dl" / "SSIS999OTHER"
    neighbour.mkdir(parents=True)
    (neighbour / "keep.mp4").write_bytes(b"keep")
    job_id = web.start_job("magnet:?xt=urn:btih:e1", "SSIS-999", {"size": 2 << 20})
    web.run_web_job(job_id, "magnet:?xt=urn:btih:e1", "SSIS-999")
    job = web.STORE.get(job_id)
    state = core.load_state(web.STATE)
    assert job["stage"] == "done" and job["folder"] == "SSIS-999"
    # 取的是迅雷上报的 -1 目录里的视频；刮削附件（nfo/manifest 等）一并进归档清单
    assert "SSIS-999.mp4" in state["SSIS-999"]["files"], "取的是迅雷上报的 -1 目录"
    assert state["SSIS-999"]["files"][0] == "SSIS-999.mp4", "视频排在清单首位"
    assert state["SSIS-999"]["meta"]["title"] == "标题"
    assert not (tmp_path / "dl" / "SSIS999").exists(), "成功后清下载原件"
    assert not (tmp_path / "dl" / "SSIS999-1").exists(), "真实落盘目录（-1）也要清掉"
    assert not (tmp_path / "work" / "state" / "SSIS999").exists(), "成功后清工作区"
    assert (neighbour / "keep.mp4").exists(), "名字近似的外部目录不能删"

def test_end_to_end_failure_keeps_files(tmp_path, monkeypatch):
    """上传失败必须保留原件与工作区，否则续传和排查都没了。"""
    _e2e_env(tmp_path, monkeypatch, upload_fails=True)
    job_id = web.start_job("magnet:?xt=urn:btih:e2", "ABP-777", {"size": 2 << 20})
    web.run_web_job(job_id, "magnet:?xt=urn:btih:e2", "ABP-777")
    assert web.STORE.get(job_id)["stage"] == "failed"
    # 迅雷把目录改成了 ABP777-1，原件在那里
    assert (tmp_path / "dl" / "ABP777-1" / "v.mp4").exists()
    assert (tmp_path / "work" / "state" / "ABP777").exists()
    assert web.STORE.get(job_id)["keep_dir"].endswith("ABP777-1"), "保留路径指向真实目录"
    assert "ABP-777" not in core.load_state(web.STATE)

# ---------------------------------------------------------------- 统一入队（B 包）

def test_requeue_goes_through_disk_gate(tmp_path, monkeypatch):
    """恢复与延期都要经过 admit()，不能直接起线程把盘写满。"""
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    monkeypatch.setattr(web, "JOBS", {})
    monkeypatch.setattr(web, "WAITING", {})
    monkeypatch.setattr(web, "_queued_at", {})
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(web, "series_median", lambda code, state: 0)
    started = []
    monkeypatch.setattr(web, "admit", lambda job_id: started.append(job_id) or False)
    web.STORE.start("j1", "SSIS-1", "magnet:a")
    web.STORE.update("j1", stage="downloading")

    assert web.requeue("j1", "magnet:a", "SSIS-1") is True
    assert started == ["j1"], "必须走闸门"
    assert web.STORE.get("j1")["stage"] == "queued"
    assert "j1" in web.WAITING, "没放行就留在队列里等空间"

def test_requeue_unknown_job_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    assert web.requeue("nope", "magnet:a", "SSIS-1") is False

def test_due_job_claim_prevents_double_enqueue(tmp_path, monkeypatch):
    """到期任务只被领取一次：并发循环不会重复投递。"""
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    monkeypatch.setattr(web, "JOBS", {})
    monkeypatch.setattr(web, "WAITING", {})
    monkeypatch.setattr(web, "_queued_at", {})
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(web, "series_median", lambda code, state: 0)
    monkeypatch.setattr(web, "admit", lambda job_id: True)
    web.STORE.start("j1", "SSIS-1", "magnet:a")
    web.STORE.defer("j1", time.time() - 1)
    assert web.STORE.claim_due("j1") is True
    assert web.STORE.claim_due("j1") is False

def test_resume_upload_only_reuses_work_dir(tmp_path, monkeypatch):
    """整理已完成、只剩上传：不该再下载一次。"""
    calls = {"download": 0, "upload": 0}
    monkeypatch.setattr(web, "STATE_DIR", str(tmp_path / "work"))
    monkeypatch.setattr(web, "TMP_ROOT", str(tmp_path / "dl"))
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(web, "METRICS", str(tmp_path / "m.json"))
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    monkeypatch.setattr(web, "JOBS", {})
    monkeypatch.setattr(web, "CANCEL", {})
    core.save_state(web.STATE, {})

    work = tmp_path / "work" / "state" / "SSIS555"
    work.mkdir(parents=True)
    (work / "SSIS555.mp4").write_bytes(b"x" * (2 << 20))

    async def fake_download(*a, **kw):
        calls["download"] += 1
        raise AssertionError("不该重新下载")

    def fake_upload(path, folder):
        calls["upload"] += 1
        return f"https://d/{folder}", 0

    monkeypatch.setattr(pipeline, "download", fake_download)
    monkeypatch.setattr(web, "upload_folder", fake_upload)
    monkeypatch.setattr(web.meta, "lookup", lambda code, path=None, client=None: {})

    web.STORE.start("j1", "SSIS-555", "magnet:a")
    # 续跑判据是 resume_from + 落盘清单，不是 stage（入队会把 stage 改回 queued）
    web.STORE.update("j1", stage="queued", folder="SSIS-555", total=2 << 20,
                     resume_from="upload", extra={"files": ["SSIS555.mp4"]})
    web.STORE.claim_owner("j1", web.lease_owner("j1"))     # 放行时授予租约
    web.run_web_job("j1", "magnet:a", "SSIS-555")

    job = web.STORE.get("j1")
    assert calls == {"download": 0, "upload": 1}, "只补上传"
    assert job["stage"] == "done" and job["link"] == "https://d/SSIS-555"
    assert "SSIS-555" in core.load_state(web.STATE)


def test_resume_ignores_unorganised_work_dir(tmp_path, monkeypatch):
    """工作区里还是原始文件名：说明整理没做完，必须重跑而不是当作已完成。"""
    work = tmp_path / "work" / "state" / "SSIS556"
    work.mkdir(parents=True)
    (work / "random_name.mp4").write_bytes(b"x" * (2 << 20))
    job = {"stage": "organizing", "folder": "SSIS-556"}
    assert web._resume_stage(job, work, "SSIS556") is None, "没写 resume_from 就不续跑"
    job = {**job, "resume_from": "upload"}
    assert web._resume_stage(job, work, "SSIS556") is None, "文件名不是规范名也不续跑"

# ---------------------------------------------------------------- 重启续跑闭环（批次3）

def _restart_env(tmp_path, monkeypatch):
    """模拟进程重启：任务表里的记录保留，内存镜像为空。"""
    monkeypatch.setattr(web, "STATE_DIR", str(tmp_path / "work"))
    monkeypatch.setattr(web, "TMP_ROOT", str(tmp_path / "dl"))
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(web, "METRICS", str(tmp_path / "m.json"))
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    monkeypatch.setattr(web, "JOBS", {})
    monkeypatch.setattr(web, "WAITING", {})
    monkeypatch.setattr(web, "_queued_at", {})
    monkeypatch.setattr(web, "CANCEL", {})
    monkeypatch.setattr(web, "series_median", lambda code, state: 4 << 20)
    # 重启恢复同样先抢执行租约，否则重跑会被自己的租约挡住
    for jid in ("b1", "b2", "j1"):
        web.STORE.claim_owner(jid, web.lease_owner(jid))
    core.save_state(web.STATE, {})
    work = tmp_path / "work" / "state" / "SSIS600"
    work.mkdir(parents=True)
    (work / "SSIS600.mp4").write_bytes(b"x" * (2 << 20))
    web.STORE.start("j1", "SSIS-600", "magnet:a")
    web.STORE.update("j1", stage="uploading", folder="SSIS-600", size=2 << 20,
                     total=2 << 20, resume_from="upload",
                     extra={"files": ["SSIS600.mp4"]})
    return work


def test_restart_requeue_keeps_resume_stage(tmp_path, monkeypatch):
    """重启入队会把 stage 改回 queued，但 resume_from 与体积必须还在，闸门才看得见。"""
    _restart_env(tmp_path, monkeypatch)
    monkeypatch.setattr(web, "admit", lambda job_id: False)   # 空间不够，先停在队列
    assert web.requeue("j1", "magnet:a", "SSIS-600") is True
    job = web.STORE.get("j1")
    assert job["stage"] == "queued"
    assert job["resume_from"] == "upload", "续跑证据不能被入队抹掉"
    assert job["total"] == 2 << 20, "体积要给闸门用"

def test_restart_then_upload_only_without_redownload(tmp_path, monkeypatch):
    """完整闭环：重启 → 入队 → 放行 → 只补上传。"""
    work = _restart_env(tmp_path, monkeypatch)
    calls = {"download": 0, "upload": 0}

    async def fake_download(*a, **kw):
        calls["download"] += 1
        raise AssertionError("不该重新下载")

    def fake_upload(path, folder):
        calls["upload"] += 1
        assert path == work
        return f"https://d/{folder}", 0

    monkeypatch.setattr(pipeline, "download", fake_download)
    monkeypatch.setattr(web, "upload_folder", fake_upload)
    monkeypatch.setattr(web.meta, "lookup", lambda code, path=None, client=None: {})
    started = []

    def fake_admit(job_id):
        # 真实 admit() 放行时会一并抢执行租约，这里照做
        web.STORE.claim_owner(job_id, web.lease_owner(job_id))
        started.append(job_id)
        return True

    monkeypatch.setattr(web, "admit", fake_admit)

    assert web.requeue("j1", "magnet:a", "SSIS-600") is True
    web.run_web_job("j1", "magnet:a", "SSIS-600")
    assert started == ["j1"], "恢复也要过空间闸门"
    assert calls == {"download": 0, "upload": 1}
    job = web.STORE.get("j1")
    assert job["stage"] == "done" and job["resume_from"] == "", "完成后清掉续跑标记"

def test_resume_refuses_when_manifest_file_missing(tmp_path, monkeypatch):
    work = _restart_env(tmp_path, monkeypatch)
    (work / "SSIS600.mp4").unlink()
    assert web._resume_stage(web.STORE.get("j1"), work, "SSIS600") is None

# ---------------------------------------------------------------- 订阅调度闭环（批次4）

def _subs_env(tmp_path, monkeypatch):
    from app import subscribe

    monkeypatch.setattr(web, "SUBS", str(tmp_path / "subs.json"))
    # 订阅与待办现在存在任务库里，需要独立的库，否则会写进真实 /data
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    monkeypatch.setattr(web, "BACKLOG", str(tmp_path / "backlog.json"))
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    monkeypatch.setattr(web, "JOBS", {})
    monkeypatch.setattr(web, "WAITING", {})
    monkeypatch.setattr(web, "_queued_at", {})
    monkeypatch.setattr(web, "admit", lambda job_id: True)
    monkeypatch.setattr(web, "diskspace_room_ok", lambda: True)
    core.save_state(web.STATE, {})
    sub = subscribe.new_sub("w5a", "测试演员", "2020-01-01", max_per_run=1,
                            kind="actor", source="javbus")
    sub["key"] = "javbus:actor:w5a"
    subscribe.save_subs(web.SUBS, {"javbus:actor:w5a": sub})
    return subscribe


def test_subscription_schedule_is_not_blocked_by_missing_magnet(tmp_path, monkeypatch):
    """第一部永远没磁力：它退避，第二部照样提交 —— 以前队头会把它堵死。"""
    from app import subscribe

    _subs_env(tmp_path, monkeypatch)

    async def fake_discover(sub_id, cursor, client=None, kind="actor", pages=2):
        return {"items": [{"code": "AAA-1", "date": "2020-01-01", "title": "一"},
                          {"code": "AAA-2", "date": "2020-01-02", "title": "二"}],
                "cursor": "2020-01-02", "error": "", "seen": 2, "pages_done": 1}

    calls = {"n": 0}

    async def fake_best(code, max_size_gb=0, client=None, tried=None):
        if code == "AAA-1":
            return None, "还没有磁力"
        return {"code": code, "magnet": "magnet:?xt=urn:btih:zz", "size_mb": 100,
                "tags": [], "has_subtitle": False, "is_hd": False}, ""

    monkeypatch.setattr(subscribe, "discover_javbus", fake_discover)
    monkeypatch.setattr(subscribe, "fetch_best", fake_best)

    first = web.run_subscriptions()
    assert [s["code"] for s in first["results"][0]["started"]] == ["AAA-2"], "不被队头挡住"
    second = web.run_subscriptions()
    assert second["results"][0]["started"] == [], "已提交的不会重复提交"
    assert second["results"][0]["pending"] == 1, "没磁力的仍留在待办里"


def test_subscription_discovers_all_but_submits_by_budget(tmp_path, monkeypatch):
    """一页 3 部、每轮上限 1：三部都要被记住，分三轮下完，一部都不能漏。"""
    from app import subscribe

    _subs_env(tmp_path, monkeypatch)

    async def fake_discover(sub_id, cursor, client=None, kind="actor", pages=2):
        return {"items": [{"code": "BBB-1", "date": "2020-01-01"},
                          {"code": "BBB-2", "date": "2020-01-02"},
                          {"code": "BBB-3", "date": "2020-01-03"}],
                "cursor": "2020-01-03", "error": "", "seen": 3, "pages_done": 1}

    async def fake_best(code, max_size_gb=0, client=None, tried=None):
        return {"code": code, "magnet": f"magnet:?xt=urn:btih:{code.lower()}",
                "size_mb": 100, "tags": [], "has_subtitle": False, "is_hd": False}, ""

    monkeypatch.setattr(subscribe, "discover_javbus", fake_discover)
    monkeypatch.setattr(subscribe, "fetch_best", fake_best)

    got = []
    for _ in range(4):
        out = web.run_subscriptions()
        got += [s["code"] for s in out["results"][0]["started"]]
    assert got == ["BBB-1", "BBB-2", "BBB-3"], f"不能漏，实际 {got}"


def test_job_snapshot_exposes_name_position_and_defer(tmp_path, monkeypatch):
    """接口要能读到「内部确实写了」的字段：名字/截图在 extra，位置在内存镜像。"""
    monkeypatch.setattr(web, "run_web_job", lambda *a: None)   # 别真的去连迅雷
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    monkeypatch.setattr(web, "JOBS", {})
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    core.save_state(web.STATE, {})
    # 用完整 40 位 infohash：短串解析不出来，也不该被当证据
    btih = "0102030405060708090a0b0c0d0e0f1011121314"
    jid = web.start_job(f"magnet:?xt=urn:btih:{btih}", "SSIS-424",
                        {"name": "标题", "size": 1 << 20})
    with web._jobs_lock:
        web.JOBS.setdefault(jid, {}).update({"queued_position": 3})

    row = next(j for j in web.jobs_snapshot() if j["id"] == jid)
    assert row["name"] == "标题"
    assert row["queued_position"] == 3
    assert row["infohash"] == btih, "infohash 要能持久化下来，重启后才能续接自己的任务"
    for key in web.JOB_FIELDS:
        assert key in row, f"接口字段契约缺 {key}"


def test_cancelling_a_queued_job_removes_it_from_queue(tmp_path, monkeypatch):
    """排队中的任务没有线程：以前点停止毫无反应，空间够了照样开下。"""
    monkeypatch.setattr(web.diskspace, "free_bytes", lambda _path: 0)  # 真实经过磁盘闸门排队
    monkeypatch.setattr(web, "run_web_job", lambda *a: None)   # 别真的去连迅雷
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    monkeypatch.setattr(web, "JOBS", {})
    monkeypatch.setattr(web, "WAITING", {})
    monkeypatch.setattr(web, "_queued_at", {})
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    core.save_state(web.STATE, {})
    jid = web.start_job("magnet:?xt=urn:btih:cd", "SSIS-500", {"size": 1 << 20})
    with web._waiting_lock:
        web.WAITING[jid] = ("magnet:?xt=urn:btih:cd", "SSIS-500")
    assert web.stop_job(jid) is True
    assert jid not in web.WAITING
    assert web.STORE.get(jid)["stage"] == "cancelled"
    assert web.stop_job(jid) is False, "重复停止不再算成功"


# ---------------------------------------------------------------- 清理任务记录

def test_clear_really_deletes_from_db(server):
    """列表读的是库：只清内存镜像等于没清（曾经的 bug，失败的记录永远清不掉）。"""
    base, _ = server
    web.job_update("j-done", stage="done")
    web.job_update("j-bad", stage="failed", error="boom")
    web.job_update("j-run", stage="downloading", percent=10.0)

    status, data = post(base, "/api/jobs/clear", {})
    assert status == 200 and data["ok"] is True
    assert set(data["deleted"]) == {"j-done", "j-bad"}
    left = {j["id"] for j in web.jobs_snapshot()}
    assert left == {"j-run"}, "进行中的任务不能一起清掉"


def test_clear_failed_only(server):
    base, _ = server
    web.job_update("j-done", stage="done")
    web.job_update("j-bad", stage="failed", error="boom")
    status, data = post(base, "/api/jobs/clear", {"stage": "failed"})
    assert status == 200 and data["deleted"] == ["j-bad"]
    assert {j["id"] for j in web.jobs_snapshot()} == {"j-done"}


def test_clear_rejects_running_stage(server):
    base, _ = server
    web.job_update("j-run", stage="downloading")
    status, data = post(base, "/api/jobs/clear", {"stage": "downloading"})
    assert status == 200 and data["ok"] is False
    assert "downloading" in data["allowed"] or data["allowed"]


def test_clear_removes_events_too(server, tmp_path):
    """记录删了，它的事件也别留着，否则时间线表只增不减。"""
    base, _ = server
    web.job_update("j-bad", stage="failed", error="boom")
    web.job_event("j-bad", "failed", "boom")
    assert web._store().events("j-bad")
    post(base, "/api/jobs/clear", {})
    assert web._store().events("j-bad") == []


def test_delete_single_job_needs_confirm(server):
    base, _ = server
    web.job_update("j-bad", stage="failed", error="boom")
    status, data = post(base, "/api/job/delete", {"id": "j-bad"})
    assert status == 200 and "confirm" in data["hint"]
    assert web._store().get("j-bad"), "没 confirm 不许删"


def test_delete_single_job(server):
    base, _ = server
    web.job_update("j-bad", stage="failed", error="boom")
    status, data = post(base, "/api/job/delete", {"id": "j-bad", "confirm": True})
    assert status == 200 and data["deleted"] is True
    assert not web._store().get("j-bad")


def test_delete_refuses_running_job(server):
    """还在跑的任务删记录 = 让调度器失去追踪，下载器里那条还在跑。"""
    base, _ = server
    web.job_update("j-run", stage="downloading", percent=30.0)
    status, data = post(base, "/api/job/delete", {"id": "j-run", "confirm": True})
    assert status == 200 and data["ok"] is False
    assert "进行中" in data["error"]
    assert web._store().get("j-run")


def test_delete_unknown_job(server):
    base, _ = server
    status, data = post(base, "/api/job/delete", {"id": "ghost", "confirm": True})
    assert status == 200 and data["ok"] is False and "没有" in data["error"]


def test_panel_cleanup_days_zero_means_today_too(tmp_path, monkeypatch):
    """days=0 必须真的按 0 天算：以前 `or` 把它当没填，刚失败的永远清不掉。"""
    calls = []
    monkeypatch.setattr(web, "clean_panel_errors",
                        lambda days, confirm: calls.append((days, confirm)) or {"ok": True})
    state_file = tmp_path / "state.json"
    core.save_state(str(state_file), {})
    monkeypatch.setattr(web, "STATE", str(state_file))
    monkeypatch.setattr(web, "AUTH", "")
    httpd = web.serve("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        host, port = "127.0.0.1", httpd.server_address[1]
        c = http.client.HTTPConnection(host, port, timeout=10)
        c.request("POST", "/api/xunlei/cleanup",
                  body=json.dumps({"days": 0, "confirm": True}),
                  headers={"Content-Type": "application/json"})
        r = c.getresponse(); r.read()
        assert r.status == 200
        c.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert calls == [(0.0, True)], f"days 被吃掉了：{calls}"
