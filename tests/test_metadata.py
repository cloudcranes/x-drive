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


def test_scrape_rejects_mismatched_number(tmp_path, monkeypatch):
    """服务返回别的番号：宁可空着，也不把别人的元数据写进归档。

    云添加链路不生成附件（附件由「补刮」推），所以这条守的是补刮入口：
    番号对不上时必须明确失败，不能把别人的标题写进记录。
    """
    core, web = _scrape_env(tmp_path, monkeypatch)
    core.save_state(web.STATE, {"ABC-123": {"code": "ABC-123", "files": []}})
    monkeypatch.setattr(web.metatube, "configured", lambda: True)

    async def other(code, provider="", client=None):
        return {"code": "XYZ-999", "title": "别人的"}

    monkeypatch.setattr(web.metatube, "scrape", other)
    out = web.scrape_sync("ABC-123", resync=True, confirm=True)

    assert out["ok"] is False
    assert "XYZ-999" in out["error"]
    rec = core.load_state(web.STATE)["ABC-123"]
    assert "别人的" not in json.dumps(rec, ensure_ascii=False)


def test_scrape_failure_keeps_record_and_marks_pending(tmp_path, monkeypatch):
    """两个源都挂了就报错：归档记录照旧，不被清空。

    手动刮削走两个源（javbus 字段全、metatube 覆盖 provider 多）——
    **一个源失败不算失败**（另一个还能给出东西），两个都失败才是真失败。
    """
    core, web = _scrape_env(tmp_path, monkeypatch)
    core.save_state(web.STATE, {"SSIS-999": {"code": "SSIS-999", "files": ["SSIS999.mp4"],
                                           "scrape_pending": True}})
    monkeypatch.setattr(web.metatube, "configured", lambda: True)

    async def boom(code, provider="", client=None):
        raise RuntimeError("服务不可达")

    monkeypatch.setattr(web.metatube, "scrape", boom)

    async def boom_javbus(code, path=None, client=None, timeout=0, refresh=False):
        raise RuntimeError("javbus 不可达")

    monkeypatch.setattr(web.meta, "lookup", boom_javbus)
    out = web.scrape_sync("SSIS-999", resync=True, confirm=True)

    assert out["ok"] is False
    rec = core.load_state(web.STATE)["SSIS-999"]
    assert rec["files"] == ["SSIS999.mp4"], "视频条目不能被丢掉"
    assert rec["scrape_pending"] is True, "附件不全要能看见"


def test_one_source_failing_still_uses_the_other(tmp_path, monkeypatch):
    """一个源挂掉，另一个照样能用 —— 不因为一个源不可用就整条放弃。"""
    core, web = _scrape_env(tmp_path, monkeypatch)
    core.save_state(web.STATE, {"SSIS-7": {"code": "SSIS-7", "meta": {}}})
    monkeypatch.setattr(web.metatube, "configured", lambda: True)

    async def boom(code, provider="", client=None):
        raise RuntimeError("服务不可达")

    monkeypatch.setattr(web.metatube, "scrape", boom)

    async def fake_javbus(code, path=None, client=None, timeout=0, refresh=False):
        return {"code": "SSIS-7", "title": "javbus 给的标题", "director": "导演甲"}

    monkeypatch.setattr(web.meta, "lookup", fake_javbus)
    out = web.scrape_sync("SSIS-7", resync=True, confirm=False)

    assert out["ok"] is True, out
    rec = core.load_state(web.STATE)["SSIS-7"]
    assert rec["meta"]["director"] == "导演甲", "另一个源的数据要留下"
    assert rec["meta"]["title"] == "SSIS-7 javbus 给的标题"


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
    # 新标题补上番号前缀：站内惯例是「番号 + 标题」，metatube 的标题不带前缀
    assert out["changes"]["title"] == {"from": "旧", "to": "SSIS-2 新"}
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
    monkeypatch.setattr(web, "remote_ls", lambda code, timeout=180.0, rel_path="", **_: [
        {"name": "a.mp4", "size": 10}, {"name": "c.json", "size": 3}])
    out = web.verify_archive("SSIS-7")
    assert out["state"] == "缺文件" and out["missing"] == ["b.nfo"]
    assert out["extra"] == ["c.json"]
    assert out["confirmed"] is False


def test_verify_archive_size_mismatch(tmp_path, monkeypatch):
    core, web = _scrape_env(tmp_path, monkeypatch)
    core.save_state(web.STATE, {"SSIS-8": {
        "code": "SSIS-8", "manifest": {"files": [{"name": "a.mp4", "size": 10}]}}})
    monkeypatch.setattr(web, "remote_ls", lambda code, timeout=180.0, rel_path="", **_: [
        {"name": "a.mp4", "size": 9}])
    out = web.verify_archive("SSIS-8")
    assert out["state"] == "大小异常"
    assert out["size_mismatch"][0] == {"name": "a.mp4", "local": 10, "remote": 9}


def test_verify_archive_remote_zero_byte_is_mismatch(tmp_path, monkeypatch):
    """远端 0 字节而本地有大小：上传残缺，必须判异常，不能算通过。"""
    core, web = _scrape_env(tmp_path, monkeypatch)
    core.save_state(web.STATE, {"SSIS-11": {
        "code": "SSIS-11", "manifest": {"files": [{"name": "a.mp4", "size": 100}]}}})
    monkeypatch.setattr(web, "remote_ls", lambda code, timeout=180.0, rel_path="", **_: [
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
    monkeypatch.setattr(web, "remote_ls", lambda code, timeout=180.0, rel_path="", **_: [
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
    monkeypatch.setattr(web, "remote_ls", lambda code, timeout=180.0, rel_path="", **_: [
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


def test_normalize_meta_maps_javbus_keys_to_lockable_names():
    """javbus 那套键名要映射到 LOCKABLE 那套，否则补刮看不见站内的值。

    实测踩过：站内有 date=2024-11-29（javbus 的键名），补刮预览却说
    release_date「从 null 变成 2024-11-29」—— 站内明明有值，它却当成空的。
    """
    from app import metadata
    out = metadata.normalize_meta({
        "title": "标题", "date": "2024-11-29", "duration": "120 分钟",
        "rating": "8.5", "cover": "http://x/c.jpg", "categories": ["剧情"],
        "review": "简介",
    })
    assert out["release_date"] == "2024-11-29"
    assert out["runtime"] == "120 分钟"
    assert out["score"] == "8.5"
    assert out["cover_url"] == "http://x/c.jpg"
    assert out["genres"] == ["剧情"]
    assert out["summary"] == "简介"
    # 老键留着：不丢东西
    assert out["date"] == "2024-11-29"
    assert out["title"] == "标题"


def test_normalize_meta_keeps_existing_new_names():
    """已经有归一后的键名时，以它为准 —— 不许被老键覆盖。"""
    from app import metadata
    out = metadata.normalize_meta({"date": "2020-01-01", "release_date": "2024-11-29"})
    assert out["release_date"] == "2024-11-29"


def test_normalize_meta_ignores_blank_values():
    """空值不占位：空的 date 不该把 release_date 写成空串。"""
    from app import metadata
    out = metadata.normalize_meta({"date": "", "duration": 0})
    assert "release_date" not in out
    assert "runtime" not in out


def test_archive_record_stores_normalized_meta():
    """新写的归档记录，meta 的键名就是站内统一那套。"""
    from app import metadata
    from pathlib import Path
    rec = metadata.archive_record(
        "MIMK-186", "clouddrive2:dest/MIMK-186", "", ["MIMK-186.mp4"], 100, "",
        Path("/nonexistent"), scraped={"date": "2024-11-29", "title": "标题"},
        manifest_files=["MIMK-186.mp4"])
    assert rec["meta"]["release_date"] == "2024-11-29"
    assert rec["meta"]["title"] == "标题"


def test_with_code_prefix_adds_missing_prefix():
    """metatube 的标题不带番号前缀 —— 补上去，番号才不从标题里消失。"""
    from app import metadata
    assert metadata.with_code_prefix("入り浸りギャル", "MIMK-186") == "MIMK-186 入り浸りギャル"


def test_with_code_prefix_does_not_duplicate():
    """已有前缀就不重复加。"""
    from app import metadata
    assert metadata.with_code_prefix("MIMK-186 入り浸りギャル", "MIMK-186") == "MIMK-186 入り浸りギャル"
    assert metadata.with_code_prefix("MIMK-186", "MIMK-186") == "MIMK-186"


def test_with_code_prefix_handles_empty():
    from app import metadata
    assert metadata.with_code_prefix("", "MIMK-186") == ""
    assert metadata.with_code_prefix("标题", "") == "标题"


def test_scrape_preview_matches_what_sync_writes(tmp_path, monkeypatch):
    """预览说会变成什么，写入就得变成什么 —— 两处判据不许分叉。

    以前 preview 与 sync 各写一套跳过逻辑，分叉时用户看到的是假话。
    """
    core, web = _scrape_env(tmp_path, monkeypatch)
    core.save_state(web.STATE, {"SSIS-9": {"code": "SSIS-9", "meta": {"title": "旧标题"}}})
    monkeypatch.setattr(web.metatube, "configured", lambda: True)

    async def fake_scrape(code, provider="", client=None):
        return {"code": code, "title": "新标题", "release_date": "2024-11-29",
                "runtime": "120 分钟"}

    monkeypatch.setattr(web.metatube, "scrape", fake_scrape)
    preview = web.scrape_preview("SSIS-9")

    web.scrape_sync("SSIS-9", resync=True, confirm=False)
    rec = core.load_state(web.STATE)["SSIS-9"]

    for key, change in preview["changes"].items():
        assert rec["meta"].get(key) == change["to"], \
            f"{key}：预览说变成 {change['to']!r}，实际写成了 {rec['meta'].get(key)!r}"
    assert rec["meta"]["title"] == "SSIS-9 新标题", "写入的标题要带番号前缀"


def test_merge_ignores_empty_to_empty_changes():
    """空值不互相覆盖 —— 不产出「（空）→（空）」这种噪音变化。

    实测：手动刮削的确认框列出一堆「原题：（空）→（空）」「简介：（空）→（空）」，
    用户看了什么也没多知道。旧值空、新值也空时，保持原样。
    """
    from app import metadata
    out = metadata.merge({"original_title": None, "summary": ""},
                         {"original_title": "", "summary": None})
    assert out["original_title"] is None
    assert out["summary"] == ""
    # 旧值有内容时，空的新值也不许覆盖
    out2 = metadata.merge({"summary": "有简介"}, {"summary": ""})
    assert out2["summary"] == "有简介"
    # 有内容的新值照常写入
    out3 = metadata.merge({"summary": ""}, {"summary": "新简介"})
    assert out3["summary"] == "新简介"


def test_scrape_preview_reports_fields_beyond_lockable(tmp_path, monkeypatch):
    """预览要报**所有**会变的字段，不只是 LOCKABLE 那 12 个。

    手动刮削的价值就在补全字段（演员/封面/预览图/发行商/时长…），
    只报 LOCKABLE 字段的话，用户根本看不到补到了什么 —— 实测同一番号
    javbus 给 22 个字段，metatube 只给 2 个（都在 LOCKABLE 里），
    差别全在 LOCKABLE 之外。
    """
    core, web = _scrape_env(tmp_path, monkeypatch)
    core.save_state(web.STATE, {"SSIS-8": {"code": "SSIS-8", "meta": {}}})
    monkeypatch.setattr(web.metatube, "configured", lambda: False)

    async def fake_javbus(code, path=None, client=None, timeout=0, refresh=False):
        return {"code": "SSIS-8", "title": "SSIS-8 标题", "director": "导演甲",
                "publisher": "发行商乙", "actors": ["演员丙"], "cover": "http://x/c.jpg",
                "previews": ["http://x/1.jpg", "http://x/2.jpg"], "duration": "120 分钟"}

    monkeypatch.setattr(web.meta, "lookup", fake_javbus)
    out = web.scrape_preview("SSIS-8")

    assert out["ok"] is True
    for key in ("director", "publisher", "actors", "cover", "previews", "duration"):
        assert key in out["changes"], f"预览漏报了 {key}"
    assert out["changes"]["actors"]["to"] == ["演员丙"]
    assert out["changes"]["previews"]["to"] == ["http://x/1.jpg", "http://x/2.jpg"]
