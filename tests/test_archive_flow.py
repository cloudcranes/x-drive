"""归档全链路：生成附件 → 上传（假 rclone）→ 记账 → 补刮 → 校验。

全部离线：媒体是现造的几 KB 假文件，rclone/上传/远端列表都是假的，
不连 MetaTube、不连迅雷、不碰真实 Drive。
核心目的是钉住「web 与 bot 写出的归档记录是同一份字段契约」。
"""
import asyncio
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import core, main, metadata, store, web
from app import meta as meta_mod

CODEC = "SSIS-123"
# web 侧的假视频名（不经 organize 改名）；bot 侧经 pipeline.organize 会改成规范名
VIDEO_NAME = "SSIS123.mp4"


def make_video(work: Path, name: str = VIDEO_NAME, size: int = 4096) -> Path:
    """几 KB 的假 mp4：只要求「是文件、够大、能改名」，不探测时长。"""
    work.mkdir(parents=True, exist_ok=True)
    path = work / name
    path.write_bytes(b"\x00" * size)
    return path


SCRAPED = {"code": CODEC, "title": "测试标题", "original_title": "原题",
           "summary": "简介", "release_date": "2026-01-02", "runtime": 120,
           "actors": ["甲"], "genres": ["剧情"], "maker": "片商",
           "cover_url": "http://x/cover.jpg", "provider": "MetaTube",
           "fetched_at": "2026-01-03T00:00:00"}


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    """断网：任何真实刮削/元数据查询都只返回空，或由用例自己替换。"""
    async def no_scrape_web(code, provider="", client=None):
        return {}

    async def no_scrape_bot(*a, **k):
        return {}

    async def no_lookup(*a, **k):
        return None

    async def no_probe(client, source):
        return None

    monkeypatch.setattr(web.metatube, "configured", lambda: False)
    monkeypatch.setattr(web.metatube, "scrape", no_scrape_web)
    # bot 侧的刮削结果是空的：即「没刮到」，不发请求
    monkeypatch.setattr(main.metatube, "configured", lambda: False)
    monkeypatch.setattr(main.metatube, "scrape", no_scrape_bot)
    monkeypatch.setattr(web.meta, "lookup", no_lookup)
    monkeypatch.setattr(main.meta, "lookup", no_lookup)
    monkeypatch.setattr(main.pipeline, "probe", no_probe)   # bot 投递前查磁力信息
    monkeypatch.setattr(main, "_STORE", None)
    monkeypatch.setattr(web.notify, "job_done", lambda *a, **k: None)
    monkeypatch.setattr(web, "SCRAPE_ON", True)
    monkeypatch.setattr(web, "NFO_ON", True)
    monkeypatch.setattr(web, "POSTER_ON", False)
    monkeypatch.setattr(meta_mod, "MIN_INTERVAL", 0.0)       # 断网时不空等节流
    # 封面：假的取图函数，绝不发请求
    monkeypatch.setattr(metadata, "fetch_image", lambda url, **k: b"")
    return tmp_path


# ---------------------------------------------------------------- web 侧

def setup_web(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(web, "STORE", store.Store(str(tmp_path / "jobs.db")))
    monkeypatch.setattr(web, "JOBS", {})
    monkeypatch.setattr(web, "LEASES", {})
    monkeypatch.setattr(web, "_lease_live", {})
    monkeypatch.setattr(web, "RCLONE_REMOTE", "gdrive")
    monkeypatch.setattr(web, "DRIVE_ROOT", "X-Drive")
    core.save_state(web.STATE, {})
    return web.STORE


def hold_web(job_id: str) -> None:
    owner = web.lease_begin(job_id)
    web.STORE.claim_owner(job_id, owner)


def fake_upload(monkeypatch):
    """假上传：只把工作区文件名记下来，不碰 Drive。"""
    sent = {"files": [], "targets": []}

    def upload_folder(work, folder):
        sent["files"] = sorted(p.name for p in Path(work).iterdir() if p.is_file())
        sent["targets"].append(f"gdrive:X-Drive/{folder}")
        return "https://drive.example/f/1", 0

    monkeypatch.setattr(web, "upload_folder", upload_folder)
    return sent


def test_web_flow_attachments_upload_record_rescrape_verify(tmp_path, monkeypatch):
    """网页链路：附件生成 → 假上传 → 记账 → 补刮 → 校验（通过 / 大小异常）。"""
    setup_web(tmp_path, monkeypatch)
    web.STORE.start("j1", CODEC, "magnet:a")
    hold_web("j1")
    work = tmp_path / "work" / "SSIS123"
    video = make_video(work)
    web.job_update("j1", extra={"infohash": "ab" * 20, "locked": ["title"]})

    # ① 生成附件（与 bot 共用 metadata.build_attachments）
    async def scraped_web(code, provider="", client=None):
        return dict(SCRAPED)

    monkeypatch.setattr(web.metatube, "configured", lambda: True)
    monkeypatch.setattr(web.metatube, "scrape", scraped_web)
    out = web.scrape_attachments(work, CODEC, [video], "j1")
    assert out["pending"] is False
    names = {p.name for p in out["files"]}
    assert {VIDEO_NAME, "movie.nfo", "metadata.json", "manifest.json"} <= names
    assert "poster.jpg" not in names, "封面开关关着就不该有海报"

    # ② 假上传：附件随视频一起进上传清单
    sent = fake_upload(monkeypatch)
    link, _ = web.upload_folder(work, CODEC)
    assert VIDEO_NAME in sent["files"] and "manifest.json" in sent["files"], \
        "附件必须跟着 rclone copy 一起上去"

    # ③ 记账：字段契约完整
    web._finish("j1", work, CODEC, out["files"], video.stat().st_size, link, "magnet:a",
                {"download": 1.0, "organize": 1.0, "upload": 1.0},
                SimpleNamespace(attempts=0))
    rec = core.load_state(web.STATE)[CODEC]
    for key in ("code", "path", "link", "files", "size", "source", "at",
                "meta", "manifest", "infohash", "locked", "scrape_pending"):
        assert key in rec, f"归档记录缺字段 {key}"
    assert rec["path"] == "gdrive:X-Drive/SSIS-123"
    assert rec["link"] == "https://drive.example/f/1"
    assert rec["locked"] == ["title"] and rec["infohash"] == "ab" * 20
    assert rec["scrape_pending"] is False, "刮到了标题就不该再挂着待补刮"
    assert rec["meta"]["title"] == "测试标题"
    by = {e["name"]: e for e in rec["manifest"]["files"]}
    assert VIDEO_NAME in by and by[VIDEO_NAME]["size"] == 4096
    assert "manifest.json" not in by, "清单不自引用"

    # ④ 补刮：清单里的视频项不许丢
    posted = {}

    def sync_rclone(cmd, src, dst, **kw):
        posted[dst.rsplit("/", 1)[-1]] = Path(src).read_text(encoding="utf-8")
        return 0, "", ""

    monkeypatch.setattr(web, "rclone", sync_rclone)
    record = {**core.load_state(web.STATE)[CODEC],
              "meta": dict(SCRAPED), "scrape_pending": False}
    core.update_state(web.STATE, {CODEC: record})
    synced = web.sync_attachments(CODEC, record)
    assert synced["sync_failed"] == [] and "manifest.json" in synced["synced"]
    blob = json.loads(posted["manifest.json"])
    by2 = {e["name"]: e for e in blob["files"]}
    assert by2[VIDEO_NAME]["size"] == 4096, "补刮不许把视频项抹掉"
    assert by2["movie.nfo"]["size"] == len(posted["movie.nfo"].encode("utf-8"))
    assert "测试标题" in posted["movie.nfo"], "补刮按最新元数据重写 NFO"
    assert json.loads(posted["metadata.json"])["fields"]["title"] == "测试标题"

    # ⑤ 校验：远端与清单一致 → 校验通过
    record = core.load_state(web.STATE)[CODEC]
    local = {f["name"]: f["size"] for f in record["manifest"]["files"]}
    remote_ok = [{"name": n, "size": s} for n, s in local.items()]
    monkeypatch.setattr(web, "remote_ls", lambda code: remote_ok)
    verdict = web.verify_archive(CODEC)
    assert verdict["state"] == "校验通过", verdict
    assert verdict["missing"] == [] and verdict["size_mismatch"] == []

    # ⑤' 远端某个文件 0 字节 → 大小异常
    remote_zero = [{"name": n, "size": (0 if n == VIDEO_NAME else s)}
                   for n, s in local.items()]
    monkeypatch.setattr(web, "remote_ls", lambda code: remote_zero)
    bad = web.verify_archive(CODEC)
    assert bad["state"] == "大小异常", bad
    assert bad["size_mismatch"] == [{"name": VIDEO_NAME, "local": 4096, "remote": 0}]


# ---------------------------------------------------------------- bot 侧

def bot_cfg(tmp_path, **over):
    return main.Config(
        token="x", allowed={1}, remote="gdrive", drive_root="X-Drive", max_mb=10,
        concurrency=1, state_path=str(tmp_path / "state.json"),
        tmp_root=str(tmp_path / "dl"), rclone_config="/dev/null",
        pending_dir=str(tmp_path / "pending"),
        metrics_path=str(tmp_path / "metrics.json"),
        state_dir=str(tmp_path / "work"), jobs_db=str(tmp_path / "jobs.db"),
        daily_upload_gb=0, **over,
    )


BOT_VIDEO_BYTES = 1 << 20          # 1MB：满足 media_files 的最小体积，又不拖慢测试


def stub_bot(monkeypatch, calls, scraped=SCRAPED):
    """假下载 / 假整理 / 假 rclone / 假分享链接，外加可注入的刮削结果。"""

    async def fake_download(xl, client, source, dest, progress=None, **opts):
        dest.mkdir(parents=True, exist_ok=True)
        # 与真实下载一致地用一个够大的媒体文件：organize/media_files 默认丢 1MB 以下的小文件
        (dest / "raw.mp4").write_bytes(b"\x00" * BOT_VIDEO_BYTES)
        return {"task": "T", "dir": dest, "waited": 0.0, "reused": False}

    def fake_stage(source_dir, work, key):
        work.mkdir(parents=True, exist_ok=True)
        for p in Path(source_dir).glob("*"):
            if p.is_file():
                (work / p.name).write_bytes(p.read_bytes())
        return list(work.glob("*"))

    async def fake_rclone(cfg, *args, timeout=180.0):
        if args[0] == "copy":
            calls["copied"] = sorted(p.name for p in Path(args[1]).iterdir()
                                     if p.is_file())
            calls["target"] = args[2]
        return 0, "", ""

    async def fake_link(cfg, target):
        return "https://drive.example/bot"

    async def fake_scrape(code, provider="", client=None):
        if isinstance(scraped, Exception):
            raise scraped
        return dict(scraped)

    monkeypatch.setattr(main.pipeline, "download", fake_download)
    monkeypatch.setattr(main.pipeline, "stage_copy", fake_stage)
    monkeypatch.setattr(main, "rclone", fake_rclone)
    monkeypatch.setattr(main, "folder_link", fake_link)
    monkeypatch.setattr(main.metatube, "configured", lambda: True)
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


def test_bot_flow_generates_attachments_and_full_record(tmp_path, monkeypatch):
    """bot 链路：附件跟着 rclone copy 上去，记账字段与网页同一份契约。"""
    calls = {"copied": [], "target": ""}
    stub_bot(monkeypatch, calls)

    reply, cfg, state = run_bot(tmp_path, {})

    assert "✅ SSIS-123" in reply
    assert calls["target"] == "gdrive:X-Drive/SSIS-123"
    # 整理会把视频改成规范名 SSIS-123.mp4；附件则必须一起进 rclone copy
    bot_video = "SSIS-123.mp4"
    assert {bot_video, "movie.nfo", "metadata.json", "manifest.json"} <= set(calls["copied"]), \
        "bot 上传的 rclone copy 必须包含生成的附件"
    rec = state["SSIS-123"]
    assert core.load_state(cfg.state_path)["SSIS-123"] == rec, "内存镜像与落盘记录要一致"
    for key in ("code", "path", "link", "files", "size", "source", "at",
                "meta", "manifest", "infohash", "scrape_pending"):
        assert key in rec, f"bot 归档记录缺字段 {key}"
    assert rec["meta"]["title"] == "测试标题"
    assert rec["scrape_pending"] is False
    assert rec["infohash"] == "ab" * 20, "infohash 来自投递任务时记的证据"
    by = {e["name"]: e for e in rec["manifest"]["files"]}
    assert by[bot_video]["size"] == BOT_VIDEO_BYTES
    assert "manifest.json" not in by


def test_bot_scrape_failure_does_not_block_upload(tmp_path, monkeypatch):
    """刮削炸了也照传：记录里登记待补刮，视频仍在清单里。"""
    calls = {"copied": [], "target": ""}
    stub_bot(monkeypatch, calls, scraped=RuntimeError("MetaTube 不可达"))

    reply, cfg, state = run_bot(tmp_path, {})

    assert "✅ SSIS-123" in reply
    assert "SSIS-123.mp4" in calls["copied"], "刮削失败不能拖累上传"
    rec = core.load_state(cfg.state_path)["SSIS-123"]
    assert rec["scrape_pending"] is True
    assert rec["meta"] == {}, "没刮到就留空，不编造"
    assert "SSIS-123.mp4" in {e["name"] for e in rec["manifest"]["files"]}


# ---------------------------------------------------------------- 同一契约

CONTRACT = {"code", "path", "link", "files", "size", "source", "at",
            "meta", "manifest", "scrape_pending"}


def test_web_and_bot_record_field_sets_agree(tmp_path, monkeypatch):
    """两条路径写出的归档记录字段集合一致 —— 就是同一个契约。"""
    web_root = tmp_path / "web"
    web_root.mkdir()
    setup_web(web_root, monkeypatch)
    web.STORE.start("jc", CODEC, "magnet:a")
    hold_web("jc")
    work = make_video(web_root / "work" / "SSIS123").parent
    video = work / VIDEO_NAME

    async def scraped_web(code, provider="", client=None):
        return dict(SCRAPED)

    monkeypatch.setattr(web.metatube, "configured", lambda: True)
    monkeypatch.setattr(web.metatube, "scrape", scraped_web)
    web.job_update("jc", extra={"infohash": "cd" * 20, "locked": ["title"]})
    out = web.scrape_attachments(work, CODEC, [video], "jc")
    web._finish("jc", work, CODEC, out["files"], video.stat().st_size,
                "https://drive.example/w", "magnet:a",
                {"download": 0, "organize": 0, "upload": 1}, SimpleNamespace(attempts=0))
    web_rec = core.load_state(web.STATE)[CODEC]

    bot_root = tmp_path / "bot"
    bot_root.mkdir()
    calls = {"copied": [], "target": ""}
    stub_bot(monkeypatch, calls)

    async def with_extra(cfg, work_dir, folder, files, job_id=""):
        """在 bot 记账前给任务补上 infohash/locked —— 模拟真实任务记录里已有的证据。"""
        main.task_store(cfg).update(job_id, extra={
            **(main.task_store(cfg).get(job_id).get("extra") or {}),
            "infohash": "cd" * 20, "locked": ["title"]})
        return await real_scrape_bot(cfg, work_dir, folder, files, job_id)

    real_scrape_bot = main.scrape_bot_attachments
    monkeypatch.setattr(main, "scrape_bot_attachments", with_extra)
    _, _, state = run_bot(bot_root, {})
    bot_rec = core.load_state(bot_root / "state.json")[CODEC]

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
        assert rec["path"] == "gdrive:X-Drive/SSIS-123"


# ---------------------------------------------------------------- 共用实现

def test_web_and_bot_share_the_same_helpers():
    """不是两份复制粘贴：附件生成与记账都落在 metadata 的同一份实现上。"""
    builders = (web.scrape_attachments, main.scrape_bot_attachments)
    for fn in builders:
        src = inspect.getsource(fn)
        assert "metadata.build_attachments" in src, f"{fn.__name__} 没有复用附件生成核心"
        assert "write_sidecar" not in src, f"{fn.__name__} 又写了一份 sidecar 逻辑"
    for fn in (web._finish, main.run_task):
        assert "metadata.archive_record" in inspect.getsource(fn), \
            f"{fn.__name__} 没有复用归档记录组装"
