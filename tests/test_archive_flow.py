"""归档全链路（云添加）：落地 → 远端整理 → 记账 → 补刮 → 校验。

2026-10-25 取消本地下载后，网页与 Bot 走同一条云添加链路
（app/cloudadd.py）：磁力投给迅雷云盘 → 落地 → 远端整理 → 记账。

全部离线：落地/整理/远端列表都是假的，不连 MetaTube、不连云盘。

核心目的没变：**钉住「web 与 bot 写出的归档记录是同一份字段契约」**，
以及补刮不许把清单里的视频项抹掉、校验必须拿**实际远端内容**对照。
"""
import asyncio
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import cloudadd, cloudorganize, core, main, metadata, storage, store, web
from app import meta as meta_mod

CODEC = "SSIS-123"
VIDEO_NAME = "SSIS-123.mp4"
VIDEO_SIZE = 4096

# 归档记录的字段契约：两条路径都必须写全
CONTRACT = {"code", "path", "link", "files", "size", "source", "at",
            "meta", "manifest", "scrape_pending"}

SCRAPED = {"code": CODEC, "title": "测试标题", "original_title": "原题",
           "summary": "简介", "release_date": "2026-01-02", "runtime": 120,
           "actors": ["甲"], "genres": ["剧情"], "maker": "片商",
           "cover_url": "http://x/cover.jpg", "provider": "MetaTube",
           "fetched_at": "2026-01-03T00:00:00"}


class _Landing:
    """一次落地的样子（形状见 app/cloudingest.wait_landing 的返回）。"""
    folder = "SEED-1"
    total = VIDEO_SIZE
    files = [{"name": VIDEO_NAME, "size": VIDEO_SIZE}]


def _cloud_rec(folder=CODEC, files=None, size=VIDEO_SIZE):
    """一条云添加的落地记录（形状见 app/cloudadd.organize_landed 的返回）。"""
    names = files or [f"{folder}.mp4"]
    return {"folder": folder,
            "path": f"clouddrive2:dest/{folder}",
            "backend": "clouddrive2", "root": "dest", "rel": f"dest/{folder}",
            "files": names,
            "files_detail": [{"name": n, "size": size} for n in names],
            "size": size, "inbox": "inbox/task"}


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    """断网：任何真实刮削/元数据查询都只返回空。"""
    async def no_scrape_web(code, provider="", client=None):
        return {}

    async def no_scrape_bot(*a, **k):
        return {}

    async def no_lookup(*a, **k):
        return None

    monkeypatch.setattr(web.metatube, "configured", lambda: False)
    monkeypatch.setattr(web.metatube, "scrape", no_scrape_web)
    monkeypatch.setattr(main.metatube, "configured", lambda: False)
    monkeypatch.setattr(main.metatube, "scrape", no_scrape_bot)
    monkeypatch.setattr(web.meta, "lookup", no_lookup)
    monkeypatch.setattr(main.meta, "lookup", no_lookup)
    monkeypatch.setattr(main, "_STORE", None)
    monkeypatch.setattr(web.notify, "job_done", lambda *a, **k: None)
    monkeypatch.setattr(web, "NFO_ON", True)
    monkeypatch.setattr(web, "POSTER_ON", False)
    monkeypatch.setattr(meta_mod, "MIN_INTERVAL", 0.0)       # 断网时不空等节流
    monkeypatch.setattr(metadata, "fetch_image", lambda url, **k: b"")
    return tmp_path


def _stub_cloud(monkeypatch, landed=None, records=None):
    """把云添加的每一步换成假的：提交、等待、整理、记账。"""
    monkeypatch.setattr(cloudadd, "ready", lambda get: (True, "ok"))
    # 记录是 clouddrive2 的，后端也得是它 —— 跨后端会被保护挡住
    monkeypatch.setattr(storage, "get", lambda: SimpleNamespace(
        kind="clouddrive2", ensure_dir=lambda rel: rel, list_dirs=lambda rel: [],
        list_files=lambda rel: [], move_file=lambda *a: None,
        purge=lambda rel: "已删除", link=lambda rel: "",
        offline_add=lambda *a: None, real_folder=lambda rel: rel.rsplit("/", 1)[-1]))
    monkeypatch.setattr(storage, "kind", lambda: "clouddrive2")
    monkeypatch.setattr(cloudadd, "destinations", lambda get: ("inbox", "dest"))
    monkeypatch.setattr(cloudadd, "task_inbox", lambda inbox, key: f"{inbox}/{key}")

    def fake_submit(backend, source, my_inbox):
        return {"before": {}, "inbox": my_inbox}

    monkeypatch.setattr(cloudadd, "submit", fake_submit)
    # 远端整理（move/purge）也是假的：这一步要真连云盘
    monkeypatch.setattr(cloudorganize, "apply",
                        lambda backend, src_root, plan, folder_code, dst_root="": {
                            "target": folder_code, "files": [f"{folder_code}.mp4"],
                            "size": VIDEO_SIZE})
    monkeypatch.setattr(cloudorganize, "plan",
                        lambda source, files, folder_code="": SimpleNamespace(
                            source=source, target=folder_code,
                            move=[f"{folder_code}.mp4"], drop=[], size=VIDEO_SIZE))

    async def fake_wait(backend, my_inbox, before, source, **kw):
        return landed if landed is not None else [_Landing()]

    monkeypatch.setattr(cloudadd, "wait_landing", fake_wait)
    monkeypatch.setattr(cloudadd, "clean_sources", lambda *a, **k: [])
    monkeypatch.setattr(cloudadd, "build_record",
                        lambda rec, scraped=None, **kw: _record_of(rec, scraped, **kw))
    if records is not None:
        monkeypatch.setattr(cloudadd, "organize_landed",
                            lambda *a, **k: (records, [], []))
    return fake_wait


def _record_of(rec, scraped=None, *, source="", infohash=""):
    """按 metadata.archive_record 的契约组装一条记录（与真实实现同源）。"""
    record = metadata.archive_record(
        rec["folder"], rec["path"], "", rec["files"], rec["size"], source or "",
        Path("/nonexistent-cloud"), scraped=scraped or {},
        manifest_files=rec["files"], pending=True, infohash=infohash)
    record.update({k: rec[k] for k in ("backend", "root", "rel") if k in rec})
    by_size = {str(f.get("name")): int(f.get("size") or 0)
               for f in (rec.get("files_detail") or [])}
    for entry in (record.get("manifest") or {}).get("files") or []:
        if entry.get("name") in by_size:
            entry["size"] = by_size[entry["name"]]
    record["manifest"]["total_size"] = sum(
        int(e.get("size") or 0) for e in record["manifest"]["files"])
    return record


# ---------------------------------------------------------------- web 侧

def setup_web(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    monkeypatch.setattr(web, "JOBS", {})
    monkeypatch.setattr(web, "LEASES", {})
    monkeypatch.setattr(web, "_lease_live", {})
    core.save_state(web.STATE, {})
    return web.STORE


def hold_web(job_id: str) -> None:
    owner = web.lease_begin(job_id)
    web.STORE.claim_owner(job_id, owner)


def test_web_cloud_flow_record_rescrape_verify(tmp_path, monkeypatch):
    """网页云添加：落地 → 远端整理 → 记账 → 补刮 → 校验（通过 / 大小异常）。"""
    setup_web(tmp_path, monkeypatch)
    _stub_cloud(monkeypatch)
    web.STORE.start("j1", CODEC, "magnet:a")
    hold_web("j1")
    web.job_update("j1", extra={"infohash": "ab" * 20, "locked": ["title"]})

    # ① 记账：字段契约完整（云添加没有本地工作区，清单按远端回填）
    web._finish_cloud("j1", "magnet:a", {"records": [_cloud_rec()]})

    rec = core.load_state(web.STATE)[CODEC]
    for key in CONTRACT:
        assert key in rec, f"归档记录缺字段 {key}"
    assert rec["path"] == "clouddrive2:dest/SSIS-123"
    assert rec["link"] == "", "云添加没有分享链接这一说"
    assert rec["scrape_pending"] is True, "附件没推，标成待补"
    assert rec["backend"] == "clouddrive2" and rec["rel"] == "dest/SSIS-123"
    by = {e["name"]: e for e in rec["manifest"]["files"]}
    assert VIDEO_NAME in by and by[VIDEO_NAME]["size"] == VIDEO_SIZE
    assert "manifest.json" not in by, "清单不自引用"

    # ② 补刮：清单里的视频项不许丢
    posted = {}

    def sync_rclone(cmd, src, dst, **kw):
        posted[dst.rsplit("/", 1)[-1]] = Path(src).read_text(encoding="utf-8")
        return 0, "", ""

    monkeypatch.setattr(web, "rclone", sync_rclone)
    # 补刮把附件推到云盘：假的 upload_file 记下内容
    def fake_upload_file(local, remote):
        posted[Path(local).name] = Path(local).read_text(encoding="utf-8")
        return remote
    monkeypatch.setattr(web.storage, "get", lambda: SimpleNamespace(
        kind="clouddrive2", ensure_dir=lambda rel: rel, list_dirs=lambda rel: [],
        list_files=lambda rel: [], move_file=lambda *a: None,
        purge=lambda rel: "已删除", link=lambda rel: "",
        upload_file=fake_upload_file, real_folder=lambda rel: rel.rsplit("/", 1)[-1]))
    record = {**core.load_state(web.STATE)[CODEC],
              "meta": dict(SCRAPED), "scrape_pending": False}
    core.update_state(web.STATE, {CODEC: record})
    synced = web.sync_attachments(CODEC, record)
    assert synced["sync_failed"] == [] and "manifest.json" in synced["synced"]
    blob = json.loads(posted["manifest.json"])
    by2 = {e["name"]: e for e in blob["files"]}
    assert by2[VIDEO_NAME]["size"] == VIDEO_SIZE, "补刮不许把视频项抹掉"
    assert by2["movie.nfo"]["size"] == len(posted["movie.nfo"].encode("utf-8"))
    assert "测试标题" in posted["movie.nfo"], "补刮按最新元数据重写 NFO"
    assert json.loads(posted["metadata.json"])["fields"]["title"] == "测试标题"

    # ③ 校验：远端与清单一致 → 校验通过
    #
    # 关键：remote 必须由**实际推送上去的内容**构造，不能拿站内清单自证。
    # 以前这里用 local 清单直接造 remote，于是「清单说有、云端其实没有」这种
    # 分叉永远测不出来（补刮把 poster 剔除、附件大小不更新都溜了过去）。
    remote_ok = [{"name": VIDEO_NAME, "size": VIDEO_SIZE}]
    remote_ok += [{"name": n, "size": len(blob.encode("utf-8"))}
                  for n, blob in posted.items()]
    monkeypatch.setattr(web, "remote_ls", lambda code, **_: remote_ok)
    verdict = web.verify_archive(CODEC)
    assert verdict["state"] == "校验通过", verdict
    assert verdict["missing"] == [] and verdict["size_mismatch"] == []

    # ③' 远端某个文件 0 字节 → 大小异常
    remote_zero = [{"name": e["name"], "size": (0 if e["name"] == VIDEO_NAME else e["size"])}
                   for e in remote_ok]
    monkeypatch.setattr(web, "remote_ls", lambda code, **_: remote_zero)
    bad = web.verify_archive(CODEC)
    assert bad["state"] == "大小异常", bad
    assert bad["size_mismatch"] == [{"name": VIDEO_NAME, "local": VIDEO_SIZE, "remote": 0}]


# ---------------------------------------------------------------- bot 侧

def bot_cfg(tmp_path, **over):
    return main.Config(
        token="x", allowed={1}, remote="gdrive", drive_root="X-Drive", max_mb=10,
        concurrency=1, state_path=str(tmp_path / "state.json"),
        tmp_root=str(tmp_path / "dl"), rclone_config="/dev/null",
        pending_dir=str(tmp_path / "pending"),
        metrics_path=str(tmp_path / "metrics.json"),
        state_dir=str(tmp_path / "work"), jobs_db=str(tmp_path / "jobs.db"),
        **over,
    )


def stub_bot(monkeypatch, calls, scraped=SCRAPED):
    """假云添加：落地/整理/记账都是假的，刮削结果可注入。"""
    _stub_cloud(monkeypatch)
    monkeypatch.setattr(main.metatube, "configured", lambda: True)

    async def fake_scrape(code, provider="", client=None):
        if isinstance(scraped, Exception):
            raise scraped
        return dict(scraped)

    monkeypatch.setattr(main.metatube, "scrape", fake_scrape)
    return calls


BOT_MAGNET = "SSIS-123 magnet:?xt=urn:btih:" + "ab" * 20


def run_bot(tmp_path, state, text=BOT_MAGNET, **cfg_over):
    cfg = bot_cfg(tmp_path, **cfg_over)
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data={
        "cfg": cfg, "state": state, "sem": asyncio.Semaphore(1), "http": object()}))
    msg = SimpleNamespace(chat_id=7, text="", edit_text=None)

    async def edit(t):
        msg.text = t
    msg.edit_text = edit
    reply = asyncio.run(main.deliver(ctx, text, msg))
    return reply, cfg, state


def test_bot_cloud_flow_writes_full_record(tmp_path, monkeypatch):
    """bot 链路：云添加落地后写出的记录与网页同一份契约。"""
    calls = {}
    stub_bot(monkeypatch, calls)

    reply, cfg, state = run_bot(tmp_path, {})

    assert "✅" in reply, reply
    rec = core.load_state(tmp_path / "state.json")[CODEC]
    for key in CONTRACT:
        assert key in rec, f"归档记录缺字段 {key}"
    assert rec["code"] == CODEC
    assert rec["files"] == [VIDEO_NAME]
    assert rec["size"] == VIDEO_SIZE
    assert rec["backend"] == "clouddrive2"
    assert rec["rel"] == "dest/SSIS-123", "按实际归档位置写，不跟全局根走"


def test_bot_scrape_failure_does_not_block_archive(tmp_path, monkeypatch):
    """刮削失败只记待补刮，不能挡住归档。"""
    calls = {}
    stub_bot(monkeypatch, calls, scraped=RuntimeError("MetaTube 不可达"))

    reply, cfg, state = run_bot(tmp_path, {})

    assert "✅" in reply, reply
    rec = core.load_state(tmp_path / "state.json")[CODEC]
    assert rec["scrape_pending"] is True
    assert rec["files"] == [VIDEO_NAME], "视频照样归档"


def test_web_and_bot_record_field_sets_agree(tmp_path, monkeypatch):
    """两条路径写出的归档记录字段集合一致 —— 就是同一个契约。"""
    web_root = tmp_path / "web"
    web_root.mkdir()
    setup_web(web_root, monkeypatch)
    _stub_cloud(monkeypatch)
    web.STORE.start("jc", CODEC, "magnet:a")
    hold_web("jc")
    # 两端都要带上 infohash（查重靠它）—— 这是字段契约的一部分
    web.job_update("jc", extra={"infohash": "ab" * 20})
    web._finish_cloud("jc", "magnet:a", {"records": [_cloud_rec()]})
    web_rec = core.load_state(web.STATE)[CODEC]

    bot_root = tmp_path / "bot"
    bot_root.mkdir()
    stub_bot(monkeypatch, {})
    _, _, _ = run_bot(bot_root, {})
    bot_rec = core.load_state(bot_root / "state.json")[CODEC]
    # 两端都要带上 source（磁力）与 infohash —— 缺了就查重、自愈、来源显示全失灵
    for rec, who in ((web_rec, "web"), (bot_rec, "bot")):
        assert rec.get("infohash"), f"{who} 侧没落 infohash"
        assert rec.get("source"), f"{who} 侧没落 source"

    assert set(web_rec) == set(bot_rec), (
        f"字段集合不一致：只在 web={set(web_rec) - set(bot_rec)}，"
        f"只在 bot={set(bot_rec) - set(web_rec)}")
    assert CONTRACT <= set(web_rec), f"契约缺字段：{CONTRACT - set(web_rec)}"
    # 键一致之外，值的形状也要一致：元数据、清单结构、清单里的名字
    for rec in (web_rec, bot_rec):
        assert isinstance(rec["meta"], dict) and isinstance(rec["manifest"], dict)
        assert isinstance(rec["files"], list) and isinstance(rec["size"], int)
        assert isinstance(rec["scrape_pending"], bool)
        assert "files" in rec["manifest"] and "format" in rec["manifest"]
        # 清单 = files 去掉自引用的 manifest.json
        assert sorted(f["name"] for f in rec["manifest"]["files"]) == \
            sorted(n for n in rec["files"] if n != "manifest.json")
        assert rec["path"] == "clouddrive2:dest/SSIS-123"


# ---------------------------------------------------------------- 共用实现

def test_web_and_bot_share_the_same_helpers():
    """不是两份复制粘贴：两端都走 app/cloudadd.py 这一条云添加链路。"""
    for fn in (web._cloud_run, main.run_task):
        assert "cloudadd." in inspect.getsource(fn), \
            f"{fn.__name__} 没有走共享的云添加模块"
    # 归档记录的组装也只在一处（cloudadd.build_record → metadata.archive_record）
    assert "metadata.archive_record" in inspect.getsource(cloudadd.build_record)
    for fn in (web._finish_cloud, main.run_task):
        assert "cloudadd.build_record" in inspect.getsource(fn), \
            f"{fn.__name__} 没有复用共享的记录组装"
