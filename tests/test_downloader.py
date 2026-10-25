"""Bot 的云添加流程：识别、归档、去重、失败留痕、任务表共用，全离线。

2026-10-25 取消本地下载后，Bot 与网页走同一条云添加链路（app/cloudadd.py），
所以这里测的是「Bot 侧怎么用这条链路」，而不是「怎么下到本地再上传」。

原来这个文件里测「整理工作区」「广告丢弃」「预告片剔除」「失败目录占空间」
的用例都随本地下载一起删掉了 —— 那些逻辑在 app/pipeline.py 与 app/fetcher.py 里，
模块已不存在。剩下的断言（识别、去重、失败留痕、两端共用任务表）与链路无关，保留。
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import cloudadd, codes, core, main, store


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
        **over,
    )


class Msg:
    chat_id = 7
    def __init__(self): self.text = ""
    async def edit_text(self, t): self.text = t


class _Landing:
    def __init__(self, folder="SEED-1", total=2 << 20):
        self.folder = folder
        self.total = total
        self.files = [{"name": "v.mp4", "size": total}]


@pytest.fixture
def stub(monkeypatch, tmp_path):
    """把云添加的每一步换成假的：提交、等待、整理、记账、清理。"""
    calls = {"submitted": [], "cleaned": []}

    monkeypatch.setattr(cloudadd, "ready", lambda get: (True, "ok"))
    monkeypatch.setattr(cloudadd, "destinations", lambda get: ("inbox", "dest"))
    monkeypatch.setattr(cloudadd, "task_inbox", lambda inbox, key: f"{inbox}/{key}")

    def fake_submit(backend, source, my_inbox):
        calls["submitted"].append(source)
        return {"before": {}, "inbox": my_inbox}

    monkeypatch.setattr(cloudadd, "submit", fake_submit)

    async def fake_wait(backend, my_inbox, before, source, **kw):
        return [_Landing()]

    monkeypatch.setattr(cloudadd, "wait_landing", fake_wait)

    def fake_organize(landed, backend, my_inbox, dest, code, **kw):
        return ([{"folder": code or "UNCLASSIFIED",
                  "path": f"{backend.kind}:{dest}/{code or 'UNCLASSIFIED'}",
                  "backend": backend.kind, "root": dest,
                  "rel": f"{dest}/{code or 'UNCLASSIFIED'}",
                  "files": [f"{code or 'UNCLASSIFIED'}.mp4"],
                  "files_detail": [{"name": "v.mp4", "size": 2 << 20}],
                  "size": 2 << 20, "inbox": my_inbox}], [])

    monkeypatch.setattr(cloudadd, "organize_landed", fake_organize)

    def fake_clean(backend, pending, **kw):
        calls["cleaned"].append(pending)
        return []

    monkeypatch.setattr(cloudadd, "clean_sources", fake_clean)
    monkeypatch.setattr(cloudadd, "build_record", lambda rec, scraped=None: {
        "code": rec["folder"], "path": rec["path"], "files": rec["files"],
        "size": rec["size"], "backend": rec.get("backend"), "rel": rec.get("rel")})
    return calls


def run(tmp_path, state, text, cfg=None, force=False):
    cfg = cfg or make_cfg(tmp_path)
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data={
        "cfg": cfg, "state": state, "sem": asyncio.Semaphore(1), "http": object()}))
    msg = Msg()
    reply = asyncio.run(main.deliver(ctx, text, msg, force=force))
    return reply, msg, cfg, state


# ─────────────────────────────────────────── 识别

def test_code_detection():
    assert codes.detect("SSIS-123 magnet:?xt=urn:btih:aa") == "SSIS-123"
    assert codes.detect("FC2-PPV-1234567") == "FC2-PPV-1234567"
    assert codes.detect("heyzo_1234.mp4") == "HEYZO-1234"
    assert codes.detect("ABCD-456 1080p x264") == "ABCD-456"
    assert codes.detect("WEB-DL 1080p") is None


def test_sources_extracts_magnet_and_http():
    got = core.extract_urls("看这个 magnet:?xt=urn:btih:aa 还有 https://a.com/b.mp4")
    assert got[0].startswith("magnet:") and got[1].startswith("https://")
    assert len(core.extract_urls("magnet:?xt=urn:btih:aa magnet:?xt=urn:btih:aa")) == 1


def test_human_size():
    assert core.human_size(0) == "—"
    assert core.human_size(512) == "512B"
    assert core.human_size(1536) == "2KB"
    assert core.human_size(5 * 1024**2) == "5MB"
    assert core.human_size(2 * 1024**3) == "2.00GB"


def test_extract_urls_dedup():
    text = "SSIS-123 magnet:?xt=urn:btih:aa https://a.com/b.mp4 magnet:?xt=urn:btih:aa"
    got = core.extract_urls(text)
    assert len(got) == 2 and got[0].startswith("magnet:")


# ─────────────────────────────────────────── 云添加

def test_cloud_add_and_archive_by_code(tmp_path, stub):
    reply, msg, cfg, state = run(tmp_path, {}, "SSIS-123 magnet:?xt=urn:btih:aa")
    assert "✅ SSIS-123" in reply
    assert stub["submitted"] == ["magnet:?xt=urn:btih:aa"], "磁力要投给云盘"
    assert state["SSIS-123"]["code"] == "SSIS-123"
    assert state["SSIS-123"]["files"] == ["SSIS-123.mp4"], "单文件重命名为番号"
    assert state["SSIS-123"]["size"] == 2 << 20


def test_unknown_code_falls_back_to_unclassified(tmp_path, stub):
    reply, _, cfg, state = run(tmp_path, {}, "magnet:?xt=urn:btih:bb")
    key = next(iter(state))
    assert key.startswith("unclassified-"), "识别不到番号时用时间戳目录"
    assert "✅" in reply


def test_duplicate_code_skipped_unless_force(tmp_path, stub):
    state = {"SSIS-123": {"code": "SSIS-123", "path": "clouddrive2:dest/SSIS-123",
                          "link": "https://drive/old", "files": ["SSIS-123.mp4"]}}
    reply, _, _, _ = run(tmp_path, state, "SSIS-123 magnet:?xt=urn:btih:aa")
    assert "♻️" in reply and "https://drive/old" in reply
    assert stub["submitted"] == [], "已存在不应重复提交"

    run(tmp_path, state, "SSIS-123 magnet:?xt=urn:btih:aa", force=True)
    assert stub["submitted"], "force 时必须重新提交"


def test_no_source_message(tmp_path, stub):
    reply, _, _, _ = run(tmp_path, {}, "SSIS-123 帮我下")
    assert "没找到可以下载的链接" in reply


def test_cloud_unavailable_gives_clear_error(tmp_path, monkeypatch):
    """云添加没启用时要明确说为什么，别让人干等。"""
    monkeypatch.setattr(cloudadd, "ready", lambda get: (False, "未启用「云添加」"))
    reply, _, _, _ = run(tmp_path, {}, "SSIS-123 magnet:?xt=urn:btih:aa")
    assert "云添加不可用" in reply
    assert "未启用" in reply


def test_failure_is_recorded_in_shared_store(tmp_path, stub, monkeypatch):
    """失败要落到统一任务表（不再是 pending 文件），这样重启与网页端都看得到。"""

    async def boom(backend, my_inbox, before, source, **kw):
        raise RuntimeError("云端落地中：意外中断")

    monkeypatch.setattr(cloudadd, "wait_landing", boom)
    reply, _, cfg, _ = run(tmp_path, {}, "SSIS-123 magnet:?xt=urn:btih:aa")
    assert "失败" in reply
    rows = store.Store(cfg.jobs_db).recent(10)
    assert [r["stage"] for r in rows] == ["failed"], "失败要留痕"
    assert rows[0]["code"] == "SSIS-123"
    assert not list(Path(cfg.pending_dir).glob("*.json")), "旧待办文件不再使用"


# ─────────────────────────────────────────── 归档状态

def test_update_state_merges_not_overwrites(tmp_path):
    """bot 与 web 同时归档时必须合并，而不是后写的整份覆盖掉先写的。"""
    path = str(tmp_path / "state.json")
    core.save_state(path, {"SSIS-001": {"code": "SSIS-001"}})
    core.update_state(path, {"ABP-002": {"code": "ABP-002"}})
    state = core.load_state(path)
    assert set(state) == {"SSIS-001", "ABP-002"}, "旧记录被覆盖了"


def test_concurrent_updates_do_not_lose_entries(tmp_path):
    """20 个线程同时写不同番号，最终必须一条不少。"""
    import threading
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


def test_bot_has_no_local_download_modules():
    """本地下载已取消：bot 不该再引用 pipeline / fetcher（模块已删）。"""
    import inspect
    from app import main as bot

    src = inspect.getsource(bot)
    assert "pipeline." not in src, "bot 不该再走本地下载链路"
    assert "fetcher." not in src, "bot 不该再走本地下载链路"
    assert "cloudadd." in src, "bot 应当走云添加链路"


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
    cfg = make_cfg(tmp_path)
    return SimpleNamespace(
        args=args,
        application=SimpleNamespace(bot_data={"cfg": cfg, "state": state}),
    )


def _update():
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


# ---------------------------------------------------------------- 两端共用任务表

def test_bot_and_web_share_one_task_table(tmp_path, monkeypatch):
    """bot 与 web 是两个进程：同一张表（两个 Store 实例）里同番号只能有一个执行者。"""
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
