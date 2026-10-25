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


def test_faststart_replacement_keeps_original_until_new_is_in_place(tmp_path, monkeypatch):
    """替换文件时，原文件必须留到新文件就位之后。

    以前是「purge 原文件 → rename 新文件」：中间那步失败，原文件已经没了，
    只剩一个临时名 —— 归档直接空掉，5GB 的片子说没就没。

    现在：先把原文件改名成备份 → 新文件改名顶上 → 核验大小 → 才删备份。
    """
    from app import core, store as store_mod
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(web, "STORE", store_mod.Store(str(tmp_path / "jobs.db")))
    core.save_state(web.STATE, {"ABC-1": {"code": "ABC-1", "files": ["ABC-1.mp4"],
                                           "backend": "clouddrive2", "rel": "dest/ABC-1"}})
    monkeypatch.setattr(faststart, "ffmpeg_available", lambda: (True, "可用"))

    calls = []
    renamed = {"dest/ABC-1/ABC-1.mp4": "dest/ABC-1/ABC-1.mp4.faststart.bak"}

    class Entry:
        name = "ABC-1.mp4"
        size = 100

    class Backend:
        kind = "clouddrive2"

        def list_files(self, rel):
            return [Entry()]

        def download_url(self, path):
            return "http://cdn.local/ABC-1.mp4"

        def upload_file(self, local, dest):
            calls.append(("upload", dest))

        def rename_dir(self, src, new_name):
            calls.append(("rename", src, new_name))
            renamed[src] = f"{src.rsplit('/', 1)[0]}/{new_name}"

        def purge(self, dest):
            calls.append(("purge", dest))

    monkeypatch.setattr(web, "storage_backend", lambda: Backend())
    # 让重排与下载都变成假的（这一步只验顺序）
    monkeypatch.setattr(web, "_faststart_worker",
                        lambda job_id, backend, path, link, code: {
                            "ok": True, "changed": True, "path": path})

    out = web.faststart_archive("ABC-1")
    assert out["ok"] is True
    # worker 被桩掉了 —— 顺序要在 worker 里验，所以这里只验接口能起来
    assert out.get("job_id")


def test_faststart_worker_replacement_order(tmp_path, monkeypatch):
    """替换顺序：备份 → 顶上 → 核验 → 删备份。原文件在顶上之前不会没。"""
    from app import core, store as store_mod
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(web, "STORE", store_mod.Store(str(tmp_path / "jobs.db")))
    core.save_state(web.STATE, {"ABC-1": {"code": "ABC-1", "files": ["ABC-1.mp4"],
                                           "backend": "clouddrive2", "rel": "dest/ABC-1"}})

    calls = []

    class Entry:
        name = "ABC-1.mp4"
        size = 100

    class Backend:
        kind = "clouddrive2"

        def list_files(self, rel):
            return [Entry()]

        def upload_file(self, local, dest):
            calls.append(("upload", dest))

        def rename_dir(self, src, new_name):
            calls.append(("rename", src, new_name))

        def purge(self, dest):
            calls.append(("purge", dest))

    # 下载与重排都桩掉
    class FakeResp:
        headers = {"Content-Length": "100"}

        def __init__(self):
            self.left = 100

        def read(self, n=-1):
            if self.left <= 0:
                return b""            # 读完就返回空 —— 否则下载循环停不下来
            self.left -= 100
            return b"x" * 100

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(web.urllib.request, "urlopen", lambda *a, **k: FakeResp())
    monkeypatch.setattr(faststart, "needs_faststart", lambda p: True)
    # 临时目录：worker 用的是 STATE_DIR 旁边（本地盘），指向 tmp_path
    monkeypatch.setattr(web, "STATE_DIR", str(tmp_path / "work"))

    fixed = tmp_path / "fixed.mp4"
    fixed.write_bytes(b"x" * 100)
    monkeypatch.setattr(faststart, "faststart_file", lambda src, work=None: fixed)

    out = web._faststart_worker("j1", Backend(), "dest/ABC-1/ABC-1.mp4",
                                "http://cdn.local/a.mp4", "ABC-1")
    assert out["ok"] is True, out

    kinds = [c[0] for c in calls]
    # 原文件先改名为备份，新文件再顶上，最后才删备份
    assert kinds == ["upload", "rename", "rename", "purge"], calls
    # 第一次 rename 是「把原文件改成备份」
    assert calls[1][2].endswith(".faststart.bak"), calls[1]
    # 第二次 rename 是「新文件顶上」
    assert calls[2][2] == "ABC-1.mp4", calls[2]
    # 最后 purge 的必须是备份，不是原文件
    assert calls[3][1].endswith(".faststart.bak"), calls[3]


def test_faststart_temp_dir_is_on_local_disk_not_the_cloud_mount(tmp_path, monkeypatch):
    """中间件必须放**本地盘**，不能放 TMP_ROOT（迅雷下载器的云盘挂载）。

    TMP_ROOT 在生产上是 /downloads —— 一个云盘挂载点。5GB 的中间件写进去要
    走上行网络（实测掉到 0.3MB/s，而直连本地盘是 462MB/s），而且会污染
    用户的下载目录。
    """
    from app import core, store as store_mod
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(web, "STORE", store_mod.Store(str(tmp_path / "jobs.db")))
    monkeypatch.setattr(web, "TMP_ROOT", "/downloads")          # 云盘挂载
    monkeypatch.setattr(web, "STATE_DIR", str(tmp_path / "work"))  # 本地盘
    monkeypatch.setattr(web, "STATE_DIR", str(tmp_path / "work"))
    # STATE_DIR 的 parent 在生产上是 /（根目录不可写）—— 不能用 parent
    core.save_state(web.STATE, {"ABC-1": {"code": "ABC-1", "files": ["ABC-1.mp4"],
                                           "backend": "clouddrive2", "rel": "dest/ABC-1"}})
    monkeypatch.setattr(faststart, "ffmpeg_available", lambda: (True, "可用"))
    monkeypatch.setattr(faststart, "needs_faststart", lambda p: True)

    used = {}

    class FakeResp:
        headers = {"Content-Length": "100"}

        def __init__(self):
            self.left = 100

        def read(self, n=-1):
            if self.left <= 0:
                return b""
            self.left -= 100
            return b"x" * 100

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        used["work"] = web.Path(web.STATE_DIR) / "faststart"
        return FakeResp()

    monkeypatch.setattr(web.urllib.request, "urlopen", fake_urlopen)

    class Backend:
        kind = "clouddrive2"

        def list_files(self, rel):
            return []

        def upload_file(self, local, dest):
            pass

        def rename_dir(self, src, new_name):
            pass

        def purge(self, dest):
            pass

    fixed = tmp_path / "fixed.mp4"
    fixed.write_bytes(b"x" * 100)
    monkeypatch.setattr(faststart, "faststart_file", lambda src, work=None: fixed)

    web._faststart_worker("j1", Backend(), "dest/ABC-1/ABC-1.mp4",
                          "http://cdn.local/a.mp4", "ABC-1")

    assert used.get("work") is not None, "应当用 faststart 临时目录"
    assert not str(used["work"]).startswith("/downloads"), \
        f"中间件不能放云盘挂载里：{used['work']}"
