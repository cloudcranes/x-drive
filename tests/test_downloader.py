"""番号下载流程：识别、归档路径、去重、失败保留，全部离线 stub。"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import codes, core, fetcher, main, pipeline


def make_cfg(tmp_path, **over):
    return main.Config(
        token="x", allowed={1}, parse_base="http://p", domains=["x.com"],
        remote="gdrive", drive_root="X-Drive", max_mb=10, concurrency=1,
        state_path=str(tmp_path / "state.json"), tmp_root=str(tmp_path / "dl"),
        rclone_config="/dev/null",
        pending_dir=str(tmp_path / "pending"), metrics_path=str(tmp_path / "metrics.json"),
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
def stub(monkeypatch, tmp_path):
    """把下载与 rclone 都换成假的：写一个 2MB 的假视频。"""
    calls = {"copy": [], "body": b"x" * (2 << 20)}

    async def fake_http(client, url, dest_dir, max_bytes):
        dest_dir.mkdir(parents=True, exist_ok=True)
        p = dest_dir / "SSIS-123.mp4"
        p.write_bytes(calls["body"])
        return p

    async def fake_rclone(cfg, *args, timeout=180.0):
        if args[0] == "lsf":
            return 0, "SSIS-123.mp4\n" if calls.get("uploaded") else "", ""
        if args[0] == "copy":
            calls["copy"].append(args[2])
            calls["uploaded"] = True
        return 0, "", ""

    async def fake_link(cfg, target):
        return "https://drive/folder/" + target.rsplit("/", 1)[-1]

    async def fake_download(aria, client, source, work, progress=None):
        await fake_http(None, source, work, 1 << 30)

    monkeypatch.setattr(fetcher, "download_http", fake_http)
    monkeypatch.setattr(pipeline, "download", fake_download)
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
    assert state["SSIS-123"]["files"] == ["SSIS-123.mp4"], "单文件重命名为番号"
    assert state["SSIS-123"]["size"] == 2 << 20


def test_unknown_code_falls_back_to_unclassified(tmp_path, stub, monkeypatch):
    async def fake_http(client, url, dest_dir, max_bytes):
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / "video_only.mp4").write_bytes(b"x" * (2 << 20))
    monkeypatch.setattr(fetcher, "download_http", fake_http)

    async def fake_download(aria, client, source, work, progress=None):
        await fake_http(None, source, work, 1 << 30)
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


def test_failure_keeps_pending(tmp_path, stub, monkeypatch):
    async def boom(client, url, dest_dir, max_bytes):
        raise RuntimeError("下载中断")

    monkeypatch.setattr(fetcher, "download_http", boom)
    async def boom_download(aria, client, source, work, progress=None):
        raise RuntimeError("下载中断")
    monkeypatch.setattr(pipeline, "download", boom_download)
    reply, _, cfg, _ = run(tmp_path, {}, "SSIS-123 magnet:?xt=urn:btih:aa")
    assert "失败" in reply
    assert list(Path(cfg.pending_dir).glob("*.json")), "失败要留待办"


def test_quota_blocks_upload(tmp_path, stub, monkeypatch):
    import dataclasses

    async def fake_http(client, url, dest_dir, max_bytes):
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / "SSIS-123.mp4").write_bytes(b"x" * (2 << 20))
    monkeypatch.setattr(fetcher, "download_http", fake_http)

    async def fake_download(aria, client, source, work, progress=None):
        await fake_http(None, source, work, 1 << 30)
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
    assert "没有找到视频文件" in msg
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
