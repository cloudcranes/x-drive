"""番号下载流程：识别、归档路径、去重、失败保留，全部离线 stub。"""
import asyncio
import shutil
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import codes, core, fetcher, main, pipeline


@pytest.fixture(autouse=True)
def fresh_task_store(monkeypatch, tmp_path):
    """每个用例一份独立任务表：bot 侧现在也写统一任务表，不能落到真实 /data。"""
    monkeypatch.setattr(main, "_STORE", None)


def make_cfg(tmp_path, **over):
    return main.Config(
        token="x", allowed={1}, remote="gdrive", drive_root="X-Drive", max_mb=10, concurrency=1,
        state_path=str(tmp_path / "state.json"), tmp_root=str(tmp_path / "dl"),
        rclone_config="/dev/null",
        pending_dir=str(tmp_path / "pending"), metrics_path=str(tmp_path / "metrics.json"),
        state_dir=str(tmp_path / "work"),
        jobs_db=str(tmp_path / "jobs.db"),
        daily_upload_gb=0, **over,
    )


class Msg:
    chat_id = 7
    def __init__(self): self.text = ""
    async def edit_text(self, t): self.text = t


@pytest.fixture(autouse=True)
def no_whatslink(monkeypatch):
    """单测不联网：元信息查询直接返回 None。"""
    async def none(client, source):
        return None
    monkeypatch.setattr(pipeline, "probe", none)


@pytest.fixture
def no_meta(monkeypatch):
    """单测不联网：bot 记账时的兜底元数据查询直接返回 None。

    bot 现在会调与网页同一套 meta.lookup（不 monkeypatch 就会去 javbus 抓真站），
    这里只把它固定为「查不到」，记录里 meta 就来自刮削结果或留空。
    """
    async def none(*a, **k):
        return None
    monkeypatch.setattr(main.meta, "lookup", none)


@pytest.fixture
def stub(monkeypatch, tmp_path, no_meta):
    """把下载与 rclone 都换成假的：写一个 2MB 的假视频。"""
    calls = {"copy": [], "body": b"x" * (2 << 20)}

    async def fake_rclone(cfg, *args, timeout=180.0):
        if args[0] == "lsf":
            return 0, "SSIS-123.mp4\n" if calls.get("uploaded") else "", ""
        if args[0] == "copy":
            calls["copy"].append(args[2])
            calls["uploaded"] = True
        return 0, "", ""

    async def fake_link(cfg, target):
        return "https://drive/folder/" + target.rsplit("/", 1)[-1]

    async def fake_download(xl, client, source, dest, progress=None, **opts):
        dest.mkdir(parents=True, exist_ok=True)
        # 用来源链接区分不同任务，避免两条任务互相覆盖同一个文件名
        tag = source.rsplit("btih=", 1)[-1] or "x"
        (dest / f"{tag}.mp4").write_bytes(calls["body"])
        return {"task": "T", "dir": dest, "waited": 0.0, "reused": False}

    def fake_stage(source_dir, work, key):
        work.mkdir(parents=True, exist_ok=True)
        for p in source_dir.glob("*"):
            if p.is_file():
                shutil.copy2(p, work / p.name)
        return list(work.glob("*"))

    monkeypatch.setattr(pipeline, "download", fake_download)
    monkeypatch.setattr(pipeline, "stage_copy", fake_stage)
    monkeypatch.setattr(main, "rclone", fake_rclone)
    monkeypatch.setattr(main, "folder_link", fake_link)
    return calls


def run(tmp_path, state, text, cfg=None, force=False):
    cfg = cfg or make_cfg(tmp_path)
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data={
        "cfg": cfg, "state": state, "sem": asyncio.Semaphore(1), "http": object()}))
    msg = Msg()
    reply = asyncio.run(main.deliver(ctx, text, msg, force=force))
    return reply, msg, cfg, state


def test_code_detection():
    assert codes.detect("SSIS-123 magnet:?xt=urn:btih:aa") == "SSIS-123"
    assert codes.detect("FC2-PPV-1234567") == "FC2-PPV-1234567"
    assert codes.detect("heyzo_1234.mp4") == "HEYZO-1234"
    assert codes.detect("ABCD-456 1080p x264") == "ABCD-456"
    assert codes.detect("WEB-DL 1080p") is None


def test_sources_extracts_magnet_and_http():
    got = fetcher.sources("看这个 magnet:?xt=urn:btih:aa 还有 https://a.com/b.mp4")
    assert got[0].startswith("magnet:") and got[1].startswith("https://")
    assert len(fetcher.sources("magnet:?xt=urn:btih:aa magnet:?xt=urn:btih:aa")) == 1


def test_download_and_archive_by_code(tmp_path, stub):
    reply, msg, cfg, state = run(tmp_path, {}, "SSIS-123 magnet:?xt=urn:btih:aa")
    assert "✅ SSIS-123" in reply
    assert stub["copy"] == ["gdrive:X-Drive/SSIS-123"], "按番号建一级目录"
    assert state["SSIS-123"]["code"] == "SSIS-123"
    # bot 现在与网页走同一份附件生成：视频之外还有 NFO/JSON/清单，都一起上传
    assert "SSIS-123.mp4" in state["SSIS-123"]["files"], "单文件重命名为番号"
    assert state["SSIS-123"]["files"] == sorted(state["SSIS-123"]["files"])
    assert state["SSIS-123"]["size"] == 2 << 20


def test_unknown_code_falls_back_to_unclassified(tmp_path, stub, monkeypatch):
    async def fake_download(xl, client, source, dest, progress=None, **opts):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "video_only.mp4").write_bytes(b"x" * (2 << 20))
        return {"task": "T", "dir": dest, "waited": 0.0, "reused": False}
    monkeypatch.setattr(pipeline, "download", fake_download)

    reply, _, cfg, state = run(tmp_path, {}, "magnet:?xt=urn:btih:bb")
    key = next(iter(state))
    assert key.startswith("unclassified-"), "识别不到番号时用时间戳目录"
    assert "✅" in reply


def test_duplicate_code_skipped_unless_force(tmp_path, stub):
    state = {"SSIS-123": {"code": "SSIS-123", "path": "gdrive:X-Drive/SSIS-123",
                          "link": "https://drive/old", "files": ["SSIS-123.mp4"]}}
    reply, _, _, _ = run(tmp_path, state, "SSIS-123 magnet:?xt=urn:btih:aa")
    assert "♻️" in reply and "https://drive/old" in reply
    assert stub["copy"] == [], "已存在不应重复上传"

    reply2, _, _, _ = run(tmp_path, state, "SSIS-123 magnet:?xt=urn:btih:aa", force=True)
    assert stub["copy"], "force 时必须重新下载上传"


def test_no_source_message(tmp_path, stub):
    reply, _, _, _ = run(tmp_path, {}, "SSIS-123 帮我下")
    assert "没找到可以下载的链接" in reply


def test_failure_is_recorded_in_shared_store(tmp_path, stub, monkeypatch):
    """失败要落到统一任务表（不再是 pending 文件），这样重启与网页端都看得到。"""
    from app import store

    async def boom_download(aria, client, source, work, progress=None, **opts):
        raise RuntimeError("下载中断")
    monkeypatch.setattr(pipeline, "download", boom_download)
    reply, _, cfg, _ = run(tmp_path, {}, "SSIS-123 magnet:?xt=urn:btih:aa")
    assert "失败" in reply
    rows = store.Store(cfg.jobs_db).recent(10)
    assert [r["stage"] for r in rows] == ["failed"], "失败要留痕"
    assert rows[0]["code"] == "SSIS-123"
    assert not list(Path(cfg.pending_dir).glob("*.json")), "旧待办文件不再使用"


def test_quota_blocks_upload(tmp_path, stub, monkeypatch):
    import dataclasses

    async def fake_download(xl, client, source, dest, progress=None, **opts):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "SSIS-123.mp4").write_bytes(b"x" * (2 << 20))
        return {"task": "T", "dir": dest, "waited": 0.0, "reused": False}
    monkeypatch.setattr(pipeline, "download", fake_download)

    cfg = dataclasses.replace(make_cfg(tmp_path), daily_upload_gb=1)
    main.add_usage(cfg, 1023 * 1024**2)   # 距 1GB 上限只剩 1MB
    reply, _, _, state = run(tmp_path, {}, "SSIS-999 magnet:?xt=urn:btih:cc", cfg=cfg)
    assert "上限" in reply
    assert stub["copy"] == [], "超限不应上传"


def test_human_size():
    from app.core import human_size
    assert human_size(0) == "—"
    assert human_size(512) == "512B"
    assert human_size(1536) == "2KB"
    assert human_size(5 * 1024**2) == "5MB"
    assert human_size(2 * 1024**3) == "2.00GB"


def test_extract_urls_dedup():
    from app.core import extract_urls
    text = "SSIS-123 magnet:?xt=urn:btih:aa https://a.com/b.mp4 magnet:?xt=urn:btih:aa"
    got = extract_urls(text)
    assert len(got) == 2 and got[0].startswith("magnet:")


def test_organize_reports_what_is_actually_in_the_dir(tmp_path):
    """找不到视频时要把目录内容带进错误里，否则线上只看到一句无从排查的提示。"""
    from app import pipeline
    work = tmp_path / "job"
    work.mkdir()
    (work / "广告.txt").write_text("x")
    (work / "小样本.mp4").write_bytes(b"0" * 100)      # 小于 1MB，被当作广告丢弃

    with pytest.raises(RuntimeError) as exc:
        pipeline.organize(work, "SSIS-123")
    msg = str(exc.value)
    assert "没有找到可归档的媒体文件" in msg
    assert "广告.txt" in msg, f"错误信息应列出目录内容：{msg}"


def test_pipeline_cleanup_removes_tree(tmp_path):
    from app import pipeline
    work = tmp_path / "job"
    (work / "sub").mkdir(parents=True)
    (work / "sub" / "a.mp4").write_bytes(b"x")
    pipeline.cleanup(work)
    assert not work.exists()


def test_organize_drops_ads_and_keeps_only_videos(tmp_path):
    """BT 种子夹带的 .url 广告、小样本不能跟着传到 Drive。"""
    from app import pipeline
    work = tmp_path / "job"
    sub = work / "mimk-288ch"
    sub.mkdir(parents=True)
    (sub / "big.mp4").write_bytes(b"1" * (2 << 20))       # 2MB 视频
    (sub / "extra.mp4").write_bytes(b"2" * (2 << 20))
    (sub / "2026 官网.url").write_text("http://ad")
    (sub / "推荐.txt").write_text("ad")
    (sub / "sample.mp4").write_bytes(b"x" * 1024)          # 太小，视为样本

    folder, files = pipeline.organize(work, "MIMK-288")
    assert folder == "MIMK-288"
    names = sorted(p.name for p in files)
    assert names == ["MIMK-288-1.mp4", "MIMK-288-2.mp4"]
    left = sorted(p.name for p in work.rglob("*") if p.is_file())
    assert left == ["MIMK-288-1.mp4", "MIMK-288-2.mp4"], f"广告文件必须删掉：{left}"


def test_bot_cleanup_is_per_source_not_global():
    """旧逻辑用「累计 failures」决定是否删目录：前面失败过一次，
    后面成功任务的下载目录就永远留在磁盘上（4.6G 级别的泄漏）。"""
    import inspect
    from app import main as bot
    src = inspect.getsource(bot.run_task)
    assert "if not failures:" not in src, (
        "清理判断不能看累计的 failures，必须看当前这个任务自己是否成功")


def test_update_state_merges_not_overwrites(tmp_path):
    """bot 与 web 同时归档时必须合并，而不是后写的整份覆盖掉先写的。"""
    from app import core
    path = str(tmp_path / "state.json")
    core.save_state(path, {"SSIS-001": {"code": "SSIS-001"}})
    core.update_state(path, {"ABP-002": {"code": "ABP-002"}})
    state = core.load_state(path)
    assert set(state) == {"SSIS-001", "ABP-002"}, "旧记录被覆盖了"


def test_concurrent_updates_do_not_lose_entries(tmp_path):
    """20 个线程同时写不同番号，最终必须一条不少。"""
    import threading
    from app import core
    path = str(tmp_path / "state.json")
    core.save_state(path, {})
    errors = []

    def worker(n: int) -> None:
        try:
            for i in range(10):
                core.update_state(path, {f"CODE-{n}-{i}": {"code": f"CODE-{n}-{i}"}})
        except Exception as exc:            # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    state = core.load_state(path)
    assert len(state) == 200, f"丢了 {200 - len(state)} 条"


def test_trim_failed_caps_disk_usage(tmp_path):
    """失败目录先留着续传，但总容量要封顶，从最旧的删。"""
    from app import pipeline
    root = tmp_path / "dl"
    for name in ("old", "mid", "new"):
        d = root / name
        d.mkdir(parents=True)
        (d / "big.mp4").write_bytes(b"x" * (2 << 20))     # 每个 2MB

    removed = pipeline.trim_failed(str(root), keep=root / "new", limit_bytes=4 << 20)
    assert "old" in removed
    assert (root / "new").exists(), "当前任务的目录不能删"
    assert (root / "mid").exists()

# ---------------------------------------------------------------- 回归测试

def test_handlers_are_defined_once():
    """main.py 曾经把整段命令处理函数复制了两遍，后一份覆盖前一份，
    导致 on_message 退化成只处理第一个链接。"""
    import inspect
    from app import main as bot

    src = inspect.getsource(bot)
    for name in ("pick_url", "mentioned_key", "on_retry", "on_list",
                 "on_delete", "on_help", "on_message"):
        assert src.count(f"async def {name}(") + src.count(f"\ndef {name}(") == 1, (
            f"{name} 被定义了多次，后一份会覆盖前一份")

def test_no_reference_to_removed_core_helpers():
    """core 里没有 tweet_id_from_url（遗留自推文版本），不能在 bot 里调用。"""
    import inspect
    from app import core, main as bot

    assert not hasattr(core, "tweet_id_from_url")
    assert "tweet_id_from_url" not in inspect.getsource(bot)

class ReplyMsg:
    """只支持 reply_text / edit_text 的假消息。"""
    chat_id = 7
    message_id = 1

    def __init__(self):
        self.sent = []

    async def reply_text(self, text, *a, **k):
        self.sent.append(text)
        return self

    async def edit_text(self, text, *a, **k):
        self.sent.append(text)

def _ctx(tmp_path, state, args):
    from types import SimpleNamespace
    cfg = make_cfg(tmp_path)
    return SimpleNamespace(
        args=args,
        application=SimpleNamespace(bot_data={"cfg": cfg, "state": state}),
    )

def _update():
    from types import SimpleNamespace
    msg = ReplyMsg()
    return SimpleNamespace(effective_message=msg, effective_user=SimpleNamespace(id=1)), msg

def test_del_deletes_by_code(tmp_path, monkeypatch):
    """/del 的 state 键是番号（不是推文 ID）。"""
    from app import main as bot

    state = {"SSIS-123": {"code": "SSIS-123", "path": "gdrive:X-Drive/SSIS-123",
                          "link": "https://drive/x", "files": ["SSIS-123.mp4"]}}
    purged = []

    async def fake_rclone(cfg, *args, timeout=180.0):
        purged.append(args)
        return 0, "", ""

    monkeypatch.setattr(bot, "rclone", fake_rclone)

    update, msg = _update()
    asyncio.run(bot.on_delete(update, _ctx(tmp_path, state, ["ssis_123"])))
    assert not purged, "没带 --yes 不能删"
    assert "将删除 SSIS-123" in msg.sent[-1]

    update, msg = _update()
    asyncio.run(bot.on_delete(update, _ctx(tmp_path, state, ["ssis_123", "--yes"])))
    assert purged and purged[0][0] == "purge"
    assert purged[0][1] == "gdrive:X-Drive/SSIS-123"
    assert "SSIS-123" not in state

def test_del_rejects_path_outside_drive_root(tmp_path, monkeypatch):
    """路径校验：不在 Drive 根目录下的记录一律拒删。"""
    from app import main as bot

    state = {"SSIS-123": {"code": "SSIS-123", "path": "/etc", "files": []}}

    async def fake_rclone(cfg, *args, timeout=180.0):
        raise AssertionError("不该执行 rclone")

    monkeypatch.setattr(bot, "rclone", fake_rclone)
    update, msg = _update()
    asyncio.run(bot.on_delete(update, _ctx(tmp_path, state, ["SSIS-123", "--yes"])))
    assert "路径校验未通过" in msg.sent[-1]
    assert "SSIS-123" in state

def test_on_message_accepts_multiple_links(tmp_path, stub):
    """一条消息里多个链接要逐个处理，不能只取第一个。"""
    from types import SimpleNamespace
    from app import main as bot

    cfg = make_cfg(tmp_path)

    from datetime import datetime, timezone

    class Msg(ReplyMsg):
        text = "SSIS-123 magnet:?xt=urn:btih:aa ABP-456 magnet:?xt=urn:btih:bb"
        caption = None
        date = datetime.now(timezone.utc)

    msg = Msg()
    update = SimpleNamespace(effective_message=msg,
                             effective_user=SimpleNamespace(id=1))
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data={
        "cfg": cfg, "state": {}, "sem": asyncio.Semaphore(1), "http": object()}))

    asyncio.run(bot.on_message(update, ctx))
    last = msg.sent[-1]
    assert "SSIS-123" in last and "ABP-456" in last, f"两个链接都该处理：{last}"

def test_pair_matches_each_link_to_its_own_code():
    """一条消息多个链接时，番号必须各归各的链接。

    曾经的 bug：整条消息只 detect 一次，拿到第一个番号就套给所有链接，
    第二个链接被判定「已存在」而根本不下载。
    """
    text = "SSIS-123 magnet:?xt=urn:btih:aa ABP-456 magnet:?xt=urn:btih:bb"
    got = codes.pair(text)
    assert got["magnet:?xt=urn:btih:aa"] == "SSIS-123"
    assert got["magnet:?xt=urn:btih:bb"] == "ABP-456"

def test_pair_handles_code_after_link_and_single_link():
    assert codes.pair("magnet:?xt=urn:btih:aa SSIS-123") == {"magnet:?xt=urn:btih:aa": "SSIS-123"}
    assert codes.pair("SSIS-123 magnet:?xt=urn:btih:aa") == {"magnet:?xt=urn:btih:aa": "SSIS-123"}
    # 从文件名/URL 里识别
    assert codes.pair("https://a.com/ABP-456.mp4")["https://a.com/ABP-456.mp4"] == "ABP-456"

def test_pair_leaves_unknown_links_unassigned():
    """两条链接只有一个番号时，不能把番号硬套给第二条（交给文件名识别）。"""
    got = codes.pair("SSIS-123 magnet:?xt=urn:btih:aa magnet:?xt=urn:btih:bb")
    assert got == {"magnet:?xt=urn:btih:aa": "SSIS-123"}
    assert codes.pair("magnet:?xt=urn:btih:aa magnet:?xt=urn:btih:bb") == {}

def test_pick_media_drops_trailer_when_feature_present(tmp_path):
    """有正片时丢掉 5 分钟以下的预告：4K 预告能比正片大，靠体积猜会错。"""
    from app import fetcher
    feature = tmp_path / "feature.mp4"
    trailer = tmp_path / "trailer.mp4"
    feature.write_bytes(b"x" * (2 << 20))
    trailer.write_bytes(b"x" * (3 << 20))       # 比正片还大
    durations = {feature: 5400.0, trailer: 120.0}
    import app.fetcher as f
    orig = f.probe_seconds
    f.probe_seconds = lambda p, timeout=20.0: durations.get(p, 0.0)
    try:
        keep, drop = fetcher.pick_media([trailer, feature])
    finally:
        f.probe_seconds = orig
    assert keep == [feature] and drop == [trailer]

def test_pick_media_keeps_everything_when_short_or_unknown(tmp_path):
    from app import fetcher
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    a.write_bytes(b"x" * (2 << 20)); b.write_bytes(b"x" * (2 << 20))
    import app.fetcher as f
    orig = f.probe_seconds
    f.probe_seconds = lambda p, timeout=20.0: 0.0        # ffprobe 拿不到
    try:
        assert fetcher.pick_media([a, b])[1] == []
        f.probe_seconds = lambda p, timeout=20.0: 600.0  # 都是短片（集数多的图集）
        assert fetcher.pick_media([a, b])[1] == [], "都是短片时不能乱删"
    finally:
        f.probe_seconds = orig

# ---------------------------------------------------------------- 两端共用任务表

def test_bot_and_web_share_one_task_table(tmp_path, monkeypatch):
    """bot 与 web 是两个进程：同一张表（两个 Store 实例）里同番号只能有一个执行者。"""
    from app import store

    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(main, "_STORE", None)
    bot_job = main.claim_task(cfg, "SSIS-777", "magnet:?xt=urn:btih:aa", chat_id=9)
    assert bot_job, "第一次登记应成功"

    # 模拟网页进程：独立 Store 实例指向同一文件
    web_store = store.Store(cfg.jobs_db)
    assert web_store.start("web1", "SSIS-777", "magnet:?xt=urn:btih:bb") is False
    assert "SSIS-777" in web_store.active_codes()

    main.finish_task(cfg, bot_job, "done")
    assert store.Store(cfg.jobs_db).start("web2", "SSIS-777", "magnet:?xt=urn:btih:cc") is True


def test_bot_deferral_keeps_code_reserved(tmp_path, monkeypatch):
    """bot 的配额类等待要写成 deferred，延期期间同番号不能被另一端抢进来。"""
    from app import store

    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(main, "_STORE", None)
    job = main.claim_task(cfg, "SSIS-888", "magnet:?xt=urn:btih:dd", chat_id=9)
    main.finish_task(cfg, job, "deferred", "今日额度用尽")
    rows = store.Store(cfg.jobs_db).recent(5)
    assert rows[0]["stage"] == "deferred"
    assert store.Store(cfg.jobs_db).start("web3", "SSIS-888", "magnet:x") is False


def test_bot_deferral_writes_a_wake_up_time(tmp_path, monkeypatch):
    """光把状态改成 deferred 不算延期：没有 defer_until 就永远等不到唤醒。

    这是真实丢过任务的路径 —— bot 说明天补跑，调度器却选不中它。
    """
    from app import store

    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(main, "_STORE", None)
    job = main.claim_task(cfg, "SSIS-889", "magnet:?xt=urn:btih:ee", chat_id=9)
    until = main.defer_task(cfg, job, "今日额度用尽")

    st = store.Store(cfg.jobs_db)
    row = st.get(job)
    assert row["stage"] == "deferred"
    assert float(row["extra"]["defer_until"]) == until
    assert st.due(now=until + 1) and st.due(now=until + 1)[0]["id"] == job
    assert st.due(now=until - 60) == [], "没到点不该被唤醒"
    assert st.claim_due(job, now=until + 1) is True
