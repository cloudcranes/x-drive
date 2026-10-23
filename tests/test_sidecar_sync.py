"""补刮同步的两条纪律：清单以旧完整清单为基底、待补刮按实际推送结果判定。

对应两个已确证缺陷：
  · E6 旧的 sync_attachments 把所有附件项从 keep 里剔掉，补一次刮削就把
    poster.jpg 从云端清单里抹掉；同步成功也不回写站内清单，站内大小永远是旧的；
    only=["poster.jpg"] 在没有生成海报时返回 ok=True/synced=[]/failed=[]。
  · E7 scrape_pending 在同步**之前**按「有没有标题」清掉，于是三个附件全推失败
    也照样退出待补刮清单。

全部离线：rclone 打桩，只把 copyto 的真实载荷记下来当作「云端」。
"""
from __future__ import annotations

import json

import pytest

from app import core, metadata


def _env(tmp_path, monkeypatch):
    from app import store, web
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    monkeypatch.setattr(web, "JOBS", {})
    monkeypatch.setattr(web, "LEASES", {})
    monkeypatch.setattr(web, "_lease_live", {})
    monkeypatch.setattr(web, "RCLONE_REMOTE", "gdrive")
    monkeypatch.setattr(web, "DRIVE_ROOT", "X-Drive")
    monkeypatch.setattr(web, "NFO_ON", True)
    core.save_state(web.STATE, {})
    return core, web


VIDEO = "SSIS-123.mp4"
OLD_MANIFEST = {"files": [
    {"name": VIDEO, "size": 4096, "ext": ".mp4"},
    {"name": "poster.jpg", "size": 98765, "ext": ".jpg"},
    {"name": "movie.nfo", "size": 8, "ext": ".nfo"},
    {"name": "metadata.json", "size": 12, "ext": ".json"},
], "total_size": 4096 + 98765 + 8 + 12}


def _record(pending=False):
    return {"code": "SSIS-123", "path": "gdrive:X-Drive/SSIS-123",
            "files": [VIDEO],
            "meta": {"code": "SSIS-123", "title": "测试标题", "cover_url": ""},
            "locked": [], "manifest": json.loads(json.dumps(OLD_MANIFEST)),
            "scrape_pending": pending}


def _fake_rclone(monkeypatch, web, fail=()):
    """假 rclone：记下每个 copyto 的目标名与真实载荷，size 就是用它的字节数。"""
    sent = []

    def fake(cmd, src, dst, **kw):
        name = dst.rsplit("/", 1)[-1]
        payload = open(src, "rb").read() if src and "/" in str(src) else b""
        sent.append({"name": name, "data": payload})
        return (1, "", "boom") if name in fail else (0, "", "")

    monkeypatch.setattr(web, "rclone", fake)
    return sent


def _cloud(sent):
    """云端实际有什么 = 真正 copyto 成功的那些字节。"""
    return {e["name"]: len(e["data"]) for e in sent}


# ─────────────────────────────────────────── E6 清单以旧完整清单为基底

def test_rescrape_keeps_poster_entry(tmp_path, monkeypatch):
    """本次没生成海报（开关关着）时，旧 poster 条目必须留在新清单里。"""
    core, web = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(web, "POSTER_ON", False)
    sent = _fake_rclone(monkeypatch, web)

    web.sync_attachments("SSIS-123", _record())

    blob = json.loads([e for e in sent if e["name"] == "manifest.json"][0]["data"])
    names = {e["name"] for e in blob["files"]}
    assert "poster.jpg" in names, f"海报从清单里蒸发了：{sorted(names)}"
    assert VIDEO in names, "视频项不许丢"


def test_rescrape_keeps_media_and_old_poster_size(tmp_path, monkeypatch):
    """没重写的条目沿用旧大小，不能被本次生成的附件大小顶掉。"""
    core, web = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(web, "POSTER_ON", False)
    sent = _fake_rclone(monkeypatch, web)

    web.sync_attachments("SSIS-123", _record())

    blob = json.loads([e for e in sent if e["name"] == "manifest.json"][0]["data"])
    by = {e["name"]: e["size"] for e in blob["files"]}
    assert by[VIDEO] == 4096, "视频大小不许被改"
    assert by["poster.jpg"] == 98765, "没重写的海报要沿用旧大小"


def test_manifest_size_matches_what_was_actually_pushed(tmp_path, monkeypatch):
    """清单里 movie.nfo 的大小必须等于真正推上去的字节数，不能是旧值。"""
    core, web = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(web, "POSTER_ON", False)
    sent = _fake_rclone(monkeypatch, web)

    web.sync_attachments("SSIS-123", _record())

    cloud = _cloud(sent)
    blob = json.loads([e for e in sent if e["name"] == "manifest.json"][0]["data"])
    by = {e["name"]: e["size"] for e in blob["files"]}
    assert by["movie.nfo"] == cloud["movie.nfo"]
    assert by["movie.nfo"] != 8, "旧大小是 8，说明根本没回写实际内容"
    assert by["metadata.json"] == cloud["metadata.json"]


def test_manifest_published_last(tmp_path, monkeypatch):
    """清单必须最后推：先让内容上线，再发布描述内容的清单。"""
    core, web = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(web, "POSTER_ON", False)
    sent = _fake_rclone(monkeypatch, web)

    web.sync_attachments("SSIS-123", _record())

    assert sent[-1]["name"] == "manifest.json", \
        f"清单不是最后一个：{[e['name'] for e in sent]}"


# ─────────────────────────────────────────── E6c only 不许假成功

def test_only_poster_reports_missing_instead_of_fake_success(tmp_path, monkeypatch):
    """开关关着时点名重推海报：必须明说没生成，不能返回一个空的成功。"""
    core, web = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(web, "POSTER_ON", False)
    sent = _fake_rclone(monkeypatch, web)

    out = web.sync_attachments("SSIS-123", _record(), only=["poster.jpg"])

    assert out["synced"] == [] and out["sync_failed"] == []
    assert out.get("missing") == ["poster.jpg"], f"没报告未生成：{out}"
    assert "poster.jpg" in out.get("error", ""), "要说清哪个附件没生成"
    assert sent == [], "什么都没生成，就一个字节都不该推"


def test_only_nfo_pushes_exactly_one_and_keeps_others(tmp_path, monkeypatch):
    """单推 nfo：只推它，别的附件不动，清单里其它条目原样保留。"""
    core, web = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(web, "POSTER_ON", False)
    sent = _fake_rclone(monkeypatch, web)

    out = web.sync_attachments("SSIS-123", _record(), only=["movie.nfo"])

    assert [e["name"] for e in sent] == ["movie.nfo"]
    assert out["synced"] == ["movie.nfo"] and not out.get("missing")
    assert "metadata.json" in out["skipped"]


def test_only_with_poster_on_really_pushes_poster(tmp_path, monkeypatch):
    """开关打开且能取到图时，点名重推海报要真的推上去。"""
    core, web = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(web, "POSTER_ON", True)
    monkeypatch.setattr(metadata, "fetch_image", lambda url, **k: b"\xff\xd8jpeg")
    rec = _record()
    rec["meta"]["cover_url"] = "https://example.invalid/c.jpg"
    sent = _fake_rclone(monkeypatch, web)

    out = web.sync_attachments("SSIS-123", rec, only=["poster.jpg"])

    assert [e["name"] for e in sent] == ["poster.jpg"]
    assert out["synced"] == ["poster.jpg"]


def test_only_with_all_invalid_names_pushes_nothing(tmp_path, monkeypatch):
    """全非法名字一个都不推，也不许退化成「全部重推」。"""
    core, web = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(web, "POSTER_ON", False)
    sent = _fake_rclone(monkeypatch, web)

    out = web.sync_attachments("SSIS-123", _record(),
                               only=["../../etc/passwd", VIDEO, "x.nfo"])

    assert sent == []
    assert out["synced"] == []


# ─────────────────────────────────────────── E7 待补刮按结果持久化

def test_all_sync_failures_keep_scrape_pending(tmp_path, monkeypatch):
    """三个附件全推失败：不能退出待补刮清单，要留下失败痕迹。"""
    core, web = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(web, "POSTER_ON", False)
    core.save_state(web.STATE, {"SSIS-123": _record(pending=True)})
    _fake_rclone(monkeypatch, web, fail={"movie.nfo", "metadata.json", "manifest.json"})

    out = web.scrape_sync("SSIS-123", confirm=True)

    saved = core.load_state(web.STATE)["SSIS-123"]
    assert saved["scrape_pending"] is True, "全失败了还退出待补读清单，等于假成功"
    assert out["pending"] is True
    assert out.get("partial") is True
    assert saved.get("sync_failed"), "要留下失败附件名，便于逐条重试"


def test_successful_sync_clears_scrape_pending(tmp_path, monkeypatch):
    core, web = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(web, "POSTER_ON", False)
    core.save_state(web.STATE, {"SSIS-123": _record(pending=True)})
    _fake_rclone(monkeypatch, web)

    out = web.scrape_sync("SSIS-123", confirm=True)

    saved = core.load_state(web.STATE)["SSIS-123"]
    assert saved["scrape_pending"] is False
    assert out["pending"] is False
    assert not out.get("partial")
    assert "sync_failed" not in saved


def test_without_confirm_still_updates_pending_by_title(tmp_path, monkeypatch):
    """只改站内记录（不推 Drive）时，待补刮仍按有没有标题判定。"""
    core, web = _env(tmp_path, monkeypatch)
    core.save_state(web.STATE, {"SSIS-123": _record(pending=True)})
    sent = _fake_rclone(monkeypatch, web)

    out = web.scrape_sync("SSIS-123")           # 无 confirm

    assert sent == [], "没有 confirm 不许动 Drive"
    assert out["pending"] is False
    assert core.load_state(web.STATE)["SSIS-123"]["scrape_pending"] is False


def test_record_manifest_written_back_after_sync(tmp_path, monkeypatch):
    """同步成功后站内清单按实际推送字节回写，健康校验才不会误报。"""
    core, web = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(web, "POSTER_ON", False)
    core.save_state(web.STATE, {"SSIS-123": _record()})
    sent = _fake_rclone(monkeypatch, web)

    web.scrape_sync("SSIS-123", confirm=True)

    saved = core.load_state(web.STATE)["SSIS-123"]
    by = {e["name"]: e["size"] for e in saved["manifest"]["files"]}
    cloud = _cloud(sent)
    assert by["movie.nfo"] == cloud["movie.nfo"], "站内清单没按实际字节更新"
    assert "poster.jpg" in by and by["poster.jpg"] == 98765, "旧海报条目被抹掉了"


def test_verify_passes_after_sync_using_real_cloud(tmp_path, monkeypatch):
    """端到端：同步后按**真实云端内容**校验，必须通过（不再靠清单自证）。"""
    core, web = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(web, "POSTER_ON", False)
    core.save_state(web.STATE, {"SSIS-123": _record()})
    sent = _fake_rclone(monkeypatch, web)

    web.scrape_sync("SSIS-123", confirm=True)

    cloud = _cloud(sent)
    cloud[VIDEO] = 4096            # 视频是更早上传的，本次不重推
    cloud["poster.jpg"] = 98765    # 海报同理，本次没重生成
    monkeypatch.setattr(web, "remote_ls",
                        lambda code: [{"name": n, "size": s} for n, s in cloud.items()])
    verdict = web.verify_archive("SSIS-123")
    assert verdict["state"] == "校验通过", verdict
