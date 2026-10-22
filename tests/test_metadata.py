"""随资源上传的附件：字段不编造、不泄漏鉴权、清单不自引用。"""
import json
import time

from app import metadata


REC = {"code": "SSIS-123", "title": "标题 & <特殊>", "original_title": "原题",
       "summary": "简介", "release_date": "2026-01-02", "runtime": 120,
       "director": "导演", "maker": "片商", "label": "厂牌", "series": "系列",
       "actors": ["甲", "乙"], "genres": ["剧情", "恋爱"], "score": 8.1,
       "cover_url": "http://x/c.jpg", "provider": "JavBus",
       "fetched_at": "2026-01-03T00:00:00"}


def test_nfo_escapes_xml_and_skips_missing():
    xml = metadata.to_nfo(REC)
    assert "<title>标题 &amp; &lt;特殊&gt;</title>" in xml
    assert "<actor><name>甲</name></actor>" in xml
    assert "<genre>恋爱</genre>" in xml
    assert 'type="num"' in xml and "SSIS-123" in xml
    assert "<plot>简介</plot>" in xml
    # 缺字段不要造
    empty = metadata.to_nfo({"code": "X-1"})
    assert "<plot>" not in empty and "<title>" not in empty


def test_metadata_file_has_no_token():
    rec = {**REC, "token": "SECRET"}
    blob = json.dumps(metadata.to_metadata(rec, code="SSIS-123"), ensure_ascii=False)
    assert "SECRET" not in blob
    out = metadata.to_metadata(rec, code="SSIS-123", locks={"title"})
    assert out["locked"] == ["title"]
    assert out["code"] == "SSIS-123"
    assert out["fields"]["actors"] == ["甲", "乙"]


def test_merge_respects_locks_and_empty_values():
    old = {"title": "手改的", "summary": "旧简介", "actors": ["甲"]}
    fresh = {"title": "自动的", "summary": "", "actors": []}
    out = metadata.merge(old, fresh, locks={"title"})
    assert out["title"] == "手改的", "锁定字段不许被覆盖"
    assert out["summary"] == "旧简介" and out["actors"] == ["甲"], "空值不覆盖"
    out2 = metadata.merge(old, {"title": "自动的"})
    assert out2["title"] == "自动的", "没锁就可以更新"


def test_manifest_lists_files_without_self_reference(tmp_path):
    (tmp_path / "SSIS123.mp4").write_bytes(b"x" * 100)
    (tmp_path / "movie.nfo").write_text("<movie/>", encoding="utf-8")
    files = [tmp_path / "SSIS123.mp4", tmp_path / "movie.nfo"]
    out = metadata.manifest(files, REC, code="SSIS-123", media={"duration": 7200})
    names = [e["name"] for e in out["files"]]
    assert names == ["SSIS123.mp4", "movie.nfo"]
    assert "manifest.json" not in names, "清单不写自身"
    assert out["total_size"] == 100 + len("<movie/>")
    assert out["media"]["duration"] == 7200


def test_write_sidecar_reports_failures_without_raising(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    (work / "SSIS123.mp4").write_bytes(b"x" * 10)
    out = metadata.write_sidecar(work, REC, [work / "SSIS123.mp4"], code="SSIS-123",
                                 poster=b"jpg")
    assert set(out["written"]) >= {"movie.nfo", "metadata.json", "poster.jpg"}
    assert out["failed"] == []
    assert json.loads((work / "metadata.json").read_text())["format"] == metadata.FORMAT_VERSION
    assert (work / "poster.jpg").read_bytes() == b"jpg"


def test_write_sidecar_skips_nfo_when_disabled(tmp_path):
    work = tmp_path / "w2"
    work.mkdir()
    out = metadata.write_sidecar(work, REC, [], code="SSIS-1", want_nfo=False)
    assert "movie.nfo" not in out["written"]
    assert (work / "metadata.json").exists()


# ---------------------------------------------------------------- 刮削接入（批次3）

def _scrape_env(tmp_path, monkeypatch):
    from app import core, store, web
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    monkeypatch.setattr(web, "JOBS", {})
    monkeypatch.setattr(web, "SCRAPE_ON", True)
    monkeypatch.setattr(web, "NFO_ON", True)
    monkeypatch.setattr(web, "POSTER_ON", False)
    core.save_state(web.STATE, {})
    return core, web


def test_scrape_attachments_written_into_upload_list(tmp_path, monkeypatch):
    """附件要和视频一起进上传清单，否则归档里只有视频。"""
    core, web = _scrape_env(tmp_path, monkeypatch)
    work = tmp_path / "work" / "SSIS123"
    work.mkdir(parents=True)
    video = work / "SSIS123.mp4"
    video.write_bytes(b"x" * 32)
    monkeypatch.setattr(web.metatube, "configured", lambda: True)

    async def fake_scrape(code, provider="", client=None):
        return {"code": code, "title": "标题", "actors": ["甲"], "provider": "JavBus"}

    monkeypatch.setattr(web.metatube, "scrape", fake_scrape)
    web.STORE.start("j1", "SSIS-123", "magnet:a")
    out = web.scrape_attachments(work, "SSIS-123", [video], "j1")
    names = {f.name for f in out["files"]}
    assert {"SSIS123.mp4", "movie.nfo", "metadata.json", "manifest.json"} <= names
    assert out["pending"] is False
    extra = web.STORE.get("j1")["extra"]
    assert extra["scraped"]["title"] == "标题" and extra["scrape_pending"] is False


def test_scrape_failure_marks_pending_but_keeps_video(tmp_path, monkeypatch):
    """刮不到就登记待补刮：视频照样能传，附件不全要能看见。"""
    core, web = _scrape_env(tmp_path, monkeypatch)
    work = tmp_path / "work" / "SSIS999"
    work.mkdir(parents=True)
    video = work / "SSIS999.mp4"
    video.write_bytes(b"x")
    monkeypatch.setattr(web.metatube, "configured", lambda: True)

    async def boom(code, provider="", client=None):
        raise RuntimeError("服务不可达")

    monkeypatch.setattr(web.metatube, "scrape", boom)
    web.STORE.start("j2", "SSIS-999", "magnet:a")
    out = web.scrape_attachments(work, "SSIS-999", [video], "j2")
    assert out["pending"] is True
    assert video in out["files"], "视频不能被丢掉"
    assert web.STORE.get("j2")["extra"]["scrape_pending"] is True


def test_scrape_rejects_mismatched_number(tmp_path, monkeypatch):
    """服务返回别的番号：宁可空着，也不把别人的元数据写进归档。"""
    core, web = _scrape_env(tmp_path, monkeypatch)
    work = tmp_path / "work" / "ABC123"
    work.mkdir(parents=True)
    monkeypatch.setattr(web.metatube, "configured", lambda: True)

    async def other(code, provider="", client=None):
        return {"code": "XYZ-999", "title": "别人的"}

    monkeypatch.setattr(web.metatube, "scrape", other)
    web.STORE.start("j3", "ABC-123", "magnet:a")
    out = web.scrape_attachments(work, "ABC-123", [], "j3")
    assert out["record"] == {}
    assert "别人的" not in (work / "metadata.json").read_text(encoding="utf-8")


def test_manual_fields_get_locked_and_survive_resync(tmp_path, monkeypatch):
    """手工改过的字段要自动锁定，之后重新刮削不得覆盖。"""
    core, web = _scrape_env(tmp_path, monkeypatch)
    core.save_state(web.STATE, {"SSIS-1": {"code": "SSIS-1",
                                          "meta": {"title": "原题", "summary": "旧"}}})
    monkeypatch.setattr(web.metatube, "configured", lambda: True)

    async def fake_scrape(code, provider="", client=None):
        return {"code": code, "title": "自动标题", "summary": "新简介"}

    monkeypatch.setattr(web.metatube, "scrape", fake_scrape)
    out = web.scrape_sync("SSIS-1", fields={"title": "我改的"})
    assert out["locked"] == ["title"]
    out2 = web.scrape_sync("SSIS-1", resync=True, confirm=False)
    rec = core.load_state(web.STATE)["SSIS-1"]
    assert rec["meta"]["title"] == "我改的", "锁定字段不能被自动刮削覆盖"
    assert rec["meta"]["summary"] == "新简介", "没锁的字段照常更新"
    assert "title" not in out2["changed"]


def test_scrape_preview_only_reports_changes(tmp_path, monkeypatch):
    """差异预览不能动文件、不能改记录。"""
    core, web = _scrape_env(tmp_path, monkeypatch)
    core.save_state(web.STATE, {"SSIS-2": {"code": "SSIS-2", "meta": {"title": "旧"}}})
    monkeypatch.setattr(web.metatube, "configured", lambda: True)

    async def fake_scrape(code, provider="", client=None):
        return {"code": code, "title": "新", "summary": "补上的"}

    monkeypatch.setattr(web.metatube, "scrape", fake_scrape)
    out = web.scrape_preview("SSIS-2")
    assert out["changes"]["title"] == {"from": "旧", "to": "新"}
    assert "summary" in out["changes"]
    assert core.load_state(web.STATE)["SSIS-2"]["meta"]["title"] == "旧", "预览不许改记录"


def test_scrape_preview_flags_mismatched_number(tmp_path, monkeypatch):
    core, web = _scrape_env(tmp_path, monkeypatch)
    core.save_state(web.STATE, {"SSIS-3": {"code": "SSIS-3", "meta": {}}})
    monkeypatch.setattr(web.metatube, "configured", lambda: True)

    async def fake_scrape(code, provider="", client=None):
        return {"code": "OTHER-1", "title": "别的"}

    monkeypatch.setattr(web.metatube, "scrape", fake_scrape)
    out = web.scrape_preview("SSIS-3")
    assert out["ok"] is False and "OTHER-1" in out["error"]


def test_status_lists_pending_without_leaking_token(tmp_path, monkeypatch):
    core, web = _scrape_env(tmp_path, monkeypatch)
    core.save_state(web.STATE, {"SSIS-4": {"code": "SSIS-4", "meta": {}},
                                "SSIS-5": {"code": "SSIS-5", "meta": {"title": "有"}}})
    monkeypatch.setattr(web.metatube, "configured", lambda: False)
    monkeypatch.setattr(web.metatube, "TOKEN", "SECRET-XYZ")
    st = web.scrape_status()
    assert st["pending"] == ["SSIS-4"]
    assert st["configured"] is False
    assert "SECRET-XYZ" not in json.dumps(st, ensure_ascii=False)


# ---------------------------------------------------------------- 归档校验与汇总（批次4）

def test_verify_archive_reports_missing_and_mismatch(tmp_path, monkeypatch):
    """默认只报告：缺文件、大小不对都要说清楚，不自动补传。"""
    core, web = _scrape_env(tmp_path, monkeypatch)
    core.save_state(web.STATE, {"SSIS-7": {
        "code": "SSIS-7", "files": ["a.mp4", "b.nfo"],
        "manifest": {"files": [{"name": "a.mp4", "size": 10},
                               {"name": "b.nfo", "size": 5}]}}})
    monkeypatch.setattr(web, "remote_ls", lambda code, timeout=180.0: [
        {"name": "a.mp4", "size": 10}, {"name": "c.json", "size": 3}])
    out = web.verify_archive("SSIS-7")
    assert out["state"] == "缺文件" and out["missing"] == ["b.nfo"]
    assert out["extra"] == ["c.json"]
    assert out["confirmed"] is False


def test_verify_archive_size_mismatch(tmp_path, monkeypatch):
    core, web = _scrape_env(tmp_path, monkeypatch)
    core.save_state(web.STATE, {"SSIS-8": {
        "code": "SSIS-8", "manifest": {"files": [{"name": "a.mp4", "size": 10}]}}})
    monkeypatch.setattr(web, "remote_ls", lambda code, timeout=180.0: [
        {"name": "a.mp4", "size": 9}])
    out = web.verify_archive("SSIS-8")
    assert out["state"] == "大小异常"
    assert out["size_mismatch"][0] == {"name": "a.mp4", "local": 10, "remote": 9}


def test_verify_archive_remote_zero_byte_is_mismatch(tmp_path, monkeypatch):
    """远端 0 字节而本地有大小：上传残缺，必须判异常，不能算通过。"""
    core, web = _scrape_env(tmp_path, monkeypatch)
    core.save_state(web.STATE, {"SSIS-11": {
        "code": "SSIS-11", "manifest": {"files": [{"name": "a.mp4", "size": 100}]}}})
    monkeypatch.setattr(web, "remote_ls", lambda code, timeout=180.0: [
        {"name": "a.mp4", "size": 0}])
    out = web.verify_archive("SSIS-11")
    assert out["state"] == "大小异常"
    assert out["size_mismatch"] == [{"name": "a.mp4", "local": 100, "remote": 0}]
    assert out.get("hint") is None, "默认不补传、不删除"


def test_verify_archive_unknown_local_size_is_not_pass(tmp_path, monkeypatch):
    """本地大小不明（0 或没记）：不做大小断言，但要说「大小未知」，不能算通过。"""
    core, web = _scrape_env(tmp_path, monkeypatch)
    core.save_state(web.STATE, {"SSIS-12": {
        "code": "SSIS-12", "manifest": {"files": [{"name": "a.mp4", "size": 0},
                                                  {"name": "b.nfo"}]}}})
    monkeypatch.setattr(web, "remote_ls", lambda code, timeout=180.0: [
        {"name": "a.mp4", "size": 100}, {"name": "b.nfo", "size": 5}])
    out = web.verify_archive("SSIS-12")
    assert out["state"] == "大小未知"
    assert out["size_unknown"] == ["a.mp4", "b.nfo"]
    assert out["size_mismatch"] == [], "大小未知不做断言"
    assert "大小未知" in out["note"]


def test_verify_archive_records_without_sizes_still_ok(tmp_path, monkeypatch):
    """记录里只有文件名（无清单）、远端非空：大小未知但不许谎报通过。"""
    core, web = _scrape_env(tmp_path, monkeypatch)
    core.save_state(web.STATE, {"SSIS-13": {"code": "SSIS-13", "files": ["a.mp4"]}})
    monkeypatch.setattr(web, "remote_ls", lambda code, timeout=180.0: [
        {"name": "a.mp4", "size": 100}])
    out = web.verify_archive("SSIS-13")
    assert out["state"] == "大小未知" and out["size_unknown"] == ["a.mp4"]


def test_verify_archive_ls_failure_is_reported_not_raised(tmp_path, monkeypatch):
    core, web = _scrape_env(tmp_path, monkeypatch)
    core.save_state(web.STATE, {"SSIS-9": {"code": "SSIS-9", "files": ["a.mp4"]}})

    def boom(code, timeout=180.0):
        raise RuntimeError("rclone 不存在")

    monkeypatch.setattr(web, "remote_ls", boom)
    out = web.verify_archive("SSIS-9")
    assert out["ok"] is False and out["state"] == "检查失败"


def test_dup_check_finds_same_infohash(tmp_path, monkeypatch):
    core, web = _scrape_env(tmp_path, monkeypatch)
    core.save_state(web.STATE, {
        "A-1": {"code": "A-1", "infohash": "aa" * 20},
        "B-2": {"code": "B-2", "infohash": "aa" * 20},
    })
    out = web.dup_check()
    kinds = [d["kind"] for d in out["duplicates"]]
    assert "同 infohash 多份归档" in kinds


def test_daily_digest_sends_once(tmp_path, monkeypatch):
    core, web = _scrape_env(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(web.notify, "daily_summary",
                        lambda text, day: sent.append(day) or True)
    web.STORE.start("d1", "SSIS-10", "magnet:a")
    web.STORE.finish("d1", "done", "")
    first = web.daily_digest()
    assert first["sent"] is True and "完成 1 部" in first["text"]
    assert len(sent) == 1


def test_long_pending_only_reports_old_enough(tmp_path, monkeypatch):
    core, web = _scrape_env(tmp_path, monkeypatch)
    now = time.time()
    web.subs_save({}, {"k": {
        "OLD-1": {"state": "pending", "since": now - 20 * 86400, "note": "还没有磁力"},
        "NEW-1": {"state": "pending", "since": now - 3600},
    }})
    hits = []
    monkeypatch.setattr(web.notify, "long_pending",
                        lambda code, days, note="": hits.append(code) or True)
    out = web.notify_long_pending(days=14, now=now)
    assert [o["code"] for o in out] == ["OLD-1"]
    assert hits == ["OLD-1"]
