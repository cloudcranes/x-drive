"""归档记账：云添加落地的记录要带齐 manifest/scrape_pending 与定位三元组。

2026-10-25 取消本地下载后，本地链路那个 `_finish` 没有了（它带工作区、
带附件生成），云添加走的是 `_finish_cloud`：文件本来就在云盘里，没有工作区，
清单大小直接用落地时读到的那份。
"""
import json
from pathlib import Path

from app import metadata


def _env(tmp_path, monkeypatch):
    from app import core, store, web
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    monkeypatch.setattr(web, "JOBS", {})
    monkeypatch.setattr(web, "LEASES", {})
    monkeypatch.setattr(web, "_lease_live", {})
    core.save_state(web.STATE, {})
    monkeypatch.setattr(web.notify, "job_done", lambda *a, **k: None)
    return core, web


def _hold(web, job_id):
    """直接调 _finish 的场景：这两是执行者的节点，得先持有租约。
    走 lease_begin 而不是自己拼 token，是为了和真实路径用同一套 LEASES。"""
    owner = web.lease_begin(job_id)
    web.STORE.claim_owner(job_id, owner)
    return owner


def _no_lookup(web, monkeypatch):
    async def none(code):
        return {}

    monkeypatch.setattr(web.meta, "lookup", none)


def _record(folder, files, size=100, detail=None):
    """一条云添加的落地记录（形状见 app/cloudadd.organize_landed 的返回）。

    files 是**整理后**的文件名（按番号重命名过），files_detail 是落地时
    读到的原始文件名与大小 —— 记账靠它把大小回填到清单上。
    """
    names = [f.name if hasattr(f, "name") else str(f) for f in files]
    return {"folder": folder,
            "path": f"clouddrive2:dest/{folder}",
            "backend": "clouddrive2", "root": "dest", "rel": f"dest/{folder}",
            "files": names,
            "files_detail": detail or [{"name": n, "size": size} for n in names],
            "size": size, "inbox": "inbox/task"}


def _finish(web, job_id, folder, files, size=100, detail=None):
    """走云添加的记账入口。"""
    web._finish_cloud(job_id, "magnet:a",
                      {"records": [_record(folder, files, size, detail)]})


def test_finish_writes_scrape_fields_from_extra(tmp_path, monkeypatch):
    """上传前刮好的结果、infohash、locked、待补刮标记都要落进归档记录。"""
    core, web = _env(tmp_path, monkeypatch)
    _no_lookup(web, monkeypatch)
    web.STORE.start("j1", "SSIS-123", "magnet:a")
    _hold(web, "j1")

    _finish(web, "j1", "SSIS-123", ["SSIS-123.mp4"], size=100)

    rec = core.load_state(web.STATE)["SSIS-123"]
    assert rec["scrape_pending"] is True, "云添加暂不推附件，标成待补"
    names = [e["name"] for e in rec["manifest"]["files"]]
    assert "SSIS-123.mp4" in names, "清单要含视频（云端整理按番号重命名过）"
    assert rec["manifest"]["files"][0]["size"] == 100, "大小取自落地时读到的那份"
    assert rec["manifest"]["code"] == "SSIS-123"
    assert "manifest.json" not in names, "清单不自引用"
    # 定位三元组：之后的核验/补刮/改名/删除都按它找目录
    assert rec["backend"] == "clouddrive2"
    assert rec["rel"] == "dest/SSIS-123"
    assert rec["root"] == "dest"


def test_finish_falls_back_to_extra_files_when_workspace_gone(tmp_path, monkeypatch):
    """工作区已被清空也不能抛异常：用 extra 里的文件名兜底，大小留 0（不编）。"""
    core, web = _env(tmp_path, monkeypatch)
    _no_lookup(web, monkeypatch)
    web.STORE.start("j2", "SSIS-456", "magnet:a")
    _hold(web, "j2")

    # 落地清单里有个 0 字节的文件（迅雷侧还没写完）—— 大小如实记 0，不编造
    _finish(web, "j2", "SSIS-456", ["SSIS456.mp4"],
            size=0, detail=[{"name": "SSIS456.mp4", "size": 0}])

    rec = core.load_state(web.STATE)["SSIS-456"]
    assert rec["files"] == ["SSIS456.mp4"]
    entries = {e["name"]: e["size"] for e in rec["manifest"]["files"]}
    assert entries == {"SSIS456.mp4": 0}, "没读到的字节数就记 0"
    assert rec["scrape_pending"] is True
    assert "infohash" not in rec and "locked" not in rec, "缺数据就不写，不编造"


def test_finish_keeps_record_when_extra_unreadable(tmp_path, monkeypatch):
    """extra 读不出来（序列化损坏）也要把已上传的记录写下去。"""
    core, web = _env(tmp_path, monkeypatch)
    _no_lookup(web, monkeypatch)
    web.STORE.start("j3", "AB-1", "magnet:a")
    _hold(web, "j3")
    real = web._store()

    class _BadExtra:
        """只有 get 返回损坏的 extra，其余照旧落库。"""

        def __getattr__(self, name):
            return getattr(real, name)

        def get(self, job_id):
            row = real.get(job_id)
            return {**row, "extra": "{不是 json"} if row else row

    monkeypatch.setattr(web, "_store", lambda: _BadExtra())

    _finish(web, "j3", "AB-1", ["AB-1.mp4"], size=7)

    rec = core.load_state(web.STATE)["AB-1"]
    assert rec["size"] == 7 and rec["files"] == ["AB-1.mp4"]
    assert rec["manifest"]["files"] == [{"name": "AB-1.mp4", "size": 7, "ext": ".mp4"}]


def test_finish_lookup_failure_keeps_scraped_record(tmp_path, monkeypatch):
    """元数据查询抛错不影响归档，记录里仍有刮削结果与清单。"""
    core, web = _env(tmp_path, monkeypatch)

    async def boom(code):
        raise RuntimeError("JavBus 不可达")

    monkeypatch.setattr(web.meta, "lookup", boom)
    web.STORE.start("j4", "SSIS-789", "magnet:a")
    _hold(web, "j4")

    _finish(web, "j4", "SSIS-789", ["SSIS-789.mp4"], size=3)

    rec = core.load_state(web.STATE)["SSIS-789"]
    assert rec["meta"] == {}, "查不到元数据就留空，不编造"
    assert rec["link"] == "", "云添加没有分享链接这一说"


# ---------------------------------------------------------------- 补刮不丢视频项

def test_sync_attachments_keeps_manifest_video(tmp_path, monkeypatch):
    """给定含视频项的旧清单，补刮后新清单仍含视频项，附件项按最新结果。"""
    core, web = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(web, "NFO_ON", True)
    monkeypatch.setattr(web, "POSTER_ON", False)
    sent = {}

    def fake_rclone(cmd, src, dst, **kw):
        sent[dst.rsplit("/", 1)[-1]] = Path(src).read_text(encoding="utf-8")
        return 0, "", ""

    monkeypatch.setattr(web, "rclone", fake_rclone)
    record = {"code": "SSIS-123",
              "meta": {"code": "SSIS-123", "title": "新标题", "provider": "JavBus"},
              "manifest": {"files": [{"name": "SSIS123.mp4", "size": 100, "ext": ".mp4"},
                                     {"name": "movie.nfo", "size": 10, "ext": ".nfo"}]},
              "files": ["SSIS123.mp4"]}

    out = web.sync_attachments("SSIS-123", record)

    assert out["sync_failed"] == [] and "manifest.json" in out["synced"]
    blob = json.loads(sent["manifest.json"])
    by = {e["name"]: e for e in blob["files"]}
    assert "SSIS123.mp4" in by, "补刮不许把清单里的视频项抹掉"
    assert by["SSIS123.mp4"]["size"] == 100
    assert by["movie.nfo"]["size"] == len(sent["movie.nfo"].encode("utf-8")), \
        "附件项按本次实际生成的大小"
    assert "metadata.json" in by and "manifest.json" not in by


def test_sync_attachments_falls_back_to_record_files(tmp_path, monkeypatch):
    """没有 manifest 时用记录里的 files 兜底，视频项照样保留。"""
    core, web = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(web, "NFO_ON", True)
    sent = {}

    def fake_rclone(cmd, src, dst, **kw):
        sent[dst.rsplit("/", 1)[-1]] = Path(src).read_text(encoding="utf-8")
        return 0, "", ""

    monkeypatch.setattr(web, "rclone", fake_rclone)
    web.sync_attachments("ABC-9", {"meta": {"title": "t"},
                                   "files": ["ABC9.mp4", "movie.nfo"]})
    blob = json.loads(sent["manifest.json"])
    names = [e["name"] for e in blob["files"]]
    assert "ABC9.mp4" in names and names.count("movie.nfo") == 1
    assert blob["files"] == sorted(blob["files"], key=lambda e: e["name"])


def test_write_sidecar_keep_preserves_video_entry(tmp_path):
    """metadata.write_sidecar 的 keep：工作区没有视频，清单里也要留着视频项。"""
    work = tmp_path / "w"
    work.mkdir()
    keep = [{"name": "SSIS123.mp4", "size": 100, "ext": ".mp4"},
            {"name": "movie.nfo", "size": 1, "ext": ".nfo"}]   # 附件项交给实际生成覆盖
    out = metadata.write_sidecar(work, {"code": "SSIS-123", "title": "标题"}, [], 
                                 code="SSIS-123", keep=keep)
    assert "manifest.json" in out["written"]
    blob = json.loads((work / metadata.MANIFEST_NAME).read_text(encoding="utf-8"))
    by = {e["name"]: e for e in blob["files"]}
    assert by["SSIS123.mp4"]["size"] == 100, "视频项要保留"
    assert by["movie.nfo"]["size"] == (work / metadata.NFO_NAME).stat().st_size, \
        "附件项按最新生成的实际大小"
    assert "metadata.json" in by
    assert blob["total_size"] == sum(e["size"] for e in blob["files"])
