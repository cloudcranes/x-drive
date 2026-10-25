"""faststart 接口：路由位置、重排的边界（不碰归档内容）。

真机实测（2026-10-25）：线上五个 MIMK 归档的 MP4 **全部**是
`ftyp + mdat + moov` —— moov 索引在文件最末尾。浏览器只认
`ftyp + moov + mdat`，找不到就等（readyState 停在 0），于是播放没反应。
"""
from __future__ import annotations

import pytest

from app import faststart, web


def test_faststart_route_is_post():
    """前端用 POST 调 —— 路由必须注册在 do_POST 上。

    （test_webui_routes 有一道守卫，但这里再钉一次：这个接口要做重排，
    语义上就是「动东西」，不能是 GET。）
    """
    from app import store as store_mod
    import tempfile
    tmp = tempfile.mkdtemp()
    web.STATE = str(tmp + "/state.json")
    web.STORE = store_mod.Store(str(tmp + "/jobs.db"))
    # 路由表：POST 必须在
    import inspect
    src = inspect.getsource(web.Handler.do_POST)
    assert '"/api/cloud/faststart"' in src
    assert '"/api/cloud/faststart"' not in inspect.getsource(web.Handler.do_GET)


def test_faststart_archive_reports_missing_record(tmp_path, monkeypatch):
    """番号不存在：如实报错，不做什么。"""
    from app import core, store as store_mod
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(web, "STORE", store_mod.Store(str(tmp_path / "jobs.db")))
    core.save_state(web.STATE, {})
    monkeypatch.setattr(faststart, "ffmpeg_available", lambda: (True, "可用"))

    out = web.faststart_archive("NOPE-1")
    assert out["ok"] is False
    assert "NOPE-1" in out["error"]


def test_faststart_archive_reports_when_ffmpeg_missing(tmp_path, monkeypatch):
    """ffmpeg 不可用：说清原因，不动手。"""
    from app import core, store as store_mod
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(web, "STORE", store_mod.Store(str(tmp_path / "jobs.db")))
    core.save_state(web.STATE, {"ABC-1": {"code": "ABC-1", "files": ["ABC-1.mp4"]}})
    monkeypatch.setattr(faststart, "ffmpeg_available", lambda: (False, "找不到 ffmpeg"))

    out = web.faststart_archive("ABC-1")
    assert out["ok"] is False
    assert "ffmpeg" in out["error"]


def test_archive_media_path_picks_the_video(tmp_path, monkeypatch):
    """归档目录里取媒体文件：挑视频，不是随便第一个。"""
    from app import core, store as store_mod
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(web, "STORE", store_mod.Store(str(tmp_path / "jobs.db")))
    core.save_state(web.STATE, {"ABC-1": {"code": "ABC-1", "files": ["ABC-1.mp4"],
                                           "backend": "clouddrive2", "rel": "dest/ABC-1"}})

    class Entry:
        def __init__(self, name, size=1):
            self.name = name
            self.size = size

    class Backend:
        kind = "clouddrive2"

        def list_files(self, rel):
            return [Entry("cover.jpg"), Entry("ABC-1.mp4", 100)]

    path = web.archive_media_path({"backend": "clouddrive2", "rel": "dest/ABC-1"}, Backend())
    assert path == "dest/ABC-1/ABC-1.mp4"


def test_archive_media_path_reports_when_no_media(tmp_path, monkeypatch):
    """目录里没有媒体：报错，不瞎猜路径。"""
    from app import storage

    class Entry:
        def __init__(self, name):
            self.name = name
            self.size = 1

    class Backend:
        kind = "clouddrive2"

        def list_files(self, rel):
            return [Entry("cover.jpg")]

    with pytest.raises(storage.StorageError, match="没有媒体文件"):
        web.archive_media_path({"backend": "clouddrive2", "rel": "dest/ABC-1"}, Backend())


def test_faststart_starts_background_job(tmp_path, monkeypatch):
    """重排是后台任务：接口立刻返回 job_id，不同步等十几分钟。"""
    from app import core, store as store_mod
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(web, "STORE", store_mod.Store(str(tmp_path / "jobs.db")))
    core.save_state(web.STATE, {"ABC-1": {"code": "ABC-1", "files": ["ABC-1.mp4"],
                                           "backend": "clouddrive2", "rel": "dest/ABC-1"}})
    monkeypatch.setattr(faststart, "ffmpeg_available", lambda: (True, "可用"))

    class Entry:
        name = "ABC-1.mp4"
        size = 100

    class Backend:
        kind = "clouddrive2"

        def list_files(self, rel):
            return [Entry()]

        def download_url(self, path):
            return "http://cdn.local/ABC-1.mp4"

    monkeypatch.setattr(web, "storage_backend", lambda: Backend())
    # 不真的起线程 —— 只验接口立刻返回
    started = []
    monkeypatch.setattr(web.threading, "Thread",
                        lambda **kw: started.append(kw) or type("T", (), {
                            "start": lambda self: None})())

    out = web.faststart_archive("ABC-1")
    assert out["ok"] is True, out
    assert out.get("job_id"), "应当返回 job_id"
    assert started, "应当起一个后台线程"
    row = web._store().get(out["job_id"])
    assert row is not None, "任务要登记进任务表"
    assert row["stage"] in ("downloading", "uploading", "queued", "done", "failed")
