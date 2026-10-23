"""死种换源重试：手动发磁力下到死种，不去反复撞同一条，而是换候选池里的下一条。

对应「换源重试」：retry.classify 判定为 dead（死种）时，download 抛出的
Stalled（"没有任何进展…可能是死种"）不再是重试同一条，而是去 sources.search
换一条 infohash 不同的候选重下，直到候选耗尽才按死种收尾。

全部离线：下载/rclone/搜索一律打桩。
"""
from __future__ import annotations

import asyncio

import pytest

from app import main, pipeline, retry, sources, xunlei


def _magnet(btih: str) -> str:
    return f"magnet:?xt=urn:btih:{btih}"


# ─────────────────────────────────────────── next_candidate_magnet

def test_next_candidate_skips_tried_infohash(monkeypatch):
    """换源要按 infohash 跳已试过的，而不是按磁力字符串。"""
    tried = {"a" * 40}
    async def fake_search(code, client=None, limit=30):
        return [
            {"magnet": _magnet("a" * 40), "has_subtitle": True, "is_hd": True,
             "seeders": 10, "size_bytes": 1},          # 已试过
            {"magnet": _magnet("b" * 40), "has_subtitle": False, "is_hd": False,
             "seeders": 1, "size_bytes": 1},           # 新候选
        ]
    monkeypatch.setattr(sources, "search", fake_search)

    got = asyncio.run(main.next_candidate_magnet("SSIS-123", tried))

    assert got == _magnet("b" * 40)


def test_next_candidate_returns_empty_when_all_tried(monkeypatch):
    tried = {"a" * 40}
    async def fake_search(code, client=None, limit=30):
        return [{"magnet": _magnet("a" * 40), "has_subtitle": True, "is_hd": True,
                 "seeders": 1, "size_bytes": 1}]
    monkeypatch.setattr(sources, "search", fake_search)

    assert asyncio.run(main.next_candidate_magnet("SSIS-123", tried)) == ""


def test_next_candidate_empty_for_no_code(monkeypatch):
    assert asyncio.run(main.next_candidate_magnet("", set())) == ""


# ─────────────────────────────────────────── run_task 死种换源

def _mk_cfg(tmp_path):
    return main.Config(
        token="x", allowed={1}, remote="gdrive", drive_root="X", max_mb=10,
        concurrency=1, state_path=str(tmp_path / "s.json"), tmp_root=str(tmp_path / "dl"),
        rclone_config="/dev/null", pending_dir=str(tmp_path / "p"),
        metrics_path=str(tmp_path / "m.json"), state_dir=str(tmp_path / "work"),
        jobs_db=str(tmp_path / "jobs.db"),
    )


def _run(tmp_path, monkeypatch, downloaded, download_side_effect):
    """驱动 run_task，记录每次 download 收到的 magnet。"""
    cfg = _mk_cfg(tmp_path)
    monkeypatch.setattr(main, "_STORE", None)
    monkeypatch.setattr(main.metatube, "configured", lambda: False)   # 不刮削

    async def fake_download(xl, client, source, dest, progress=None, **opts):
        downloaded.append(source)
        if callable(download_side_effect):
            eff = download_side_effect()
        else:
            eff = download_side_effect
        if isinstance(eff, Exception):
            raise eff
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "x.mp4").write_bytes(b"x" * (2 << 20))
        return {"task": "T", "dir": dest, "waited": 0.0, "reused": False}

    monkeypatch.setattr(pipeline, "download", fake_download)

    async def fake_probe(client, source):
        return None
    async def fake_link(cfg, target):
        return "https://d/x"
    async def fake_lookup(*a, **k):
        return None
    monkeypatch.setattr(pipeline, "probe", fake_probe)
    monkeypatch.setattr(main, "folder_link", fake_link)
    monkeypatch.setattr(main.meta, "lookup", fake_lookup)

    async def fake_rclone(cfg, *args, timeout=180.0):
        return 0, "", ""
    monkeypatch.setattr(main, "rclone", fake_rclone)

    return cfg


def _async(v):
    async def run(*a, **k):
        return v
    return run


def test_dead_torrent_swaps_source_then_succeeds(tmp_path, monkeypatch):
    """第一次下死种，换源后第二次成功，且两次磁力不同。"""
    downloaded = []
    dead = xunlei.Stalled("900 秒没有任何进展（停在 0.0%），可能是死种")
    calls = {"n": 0}

    def side_effect():
        calls["n"] += 1
        return dead if calls["n"] == 1 else None   # 第一次死种，之后成功

    cfg = _run(tmp_path, monkeypatch, downloaded, side_effect)
    # 候选池：第一条是用户发的（死种），第二条是换源目标
    async def fake_search(code, client=None, limit=30):
        return [
            {"magnet": _magnet("a" * 40), "has_subtitle": True, "is_hd": True,
             "seeders": 1, "size_bytes": 1},
            {"magnet": _magnet("b" * 40), "has_subtitle": False, "is_hd": False,
             "seeders": 0, "size_bytes": 1},
        ]
    monkeypatch.setattr(sources, "search", fake_search)

    reply = asyncio.run(main.run_task(
        cfg, None, {}, "SSIS-123 " + _magnet("a" * 40), _async_progress()))

    assert "✅" in reply, reply
    assert len(downloaded) == 2, f"应该先下死种再换源：{downloaded}"
    assert downloaded[0] == _magnet("a" * 40)
    assert downloaded[1] == _magnet("b" * 40), "换源后应下新候选，不是重试同一条"


def test_dead_torrent_gives_up_when_no_other_candidate(tmp_path, monkeypatch):
    """候选池里只有那一条死种时，换不到就按死种收尾，不死循环。"""
    downloaded = []
    dead = xunlei.Stalled("900 秒没有任何进展（停在 0.0%），可能是死种")

    def side_effect():
        return dead

    cfg = _run(tmp_path, monkeypatch, downloaded, side_effect)
    async def fake_search(code, client=None, limit=30):
        return [{"magnet": _magnet("a" * 40), "has_subtitle": True, "is_hd": True,
                 "seeders": 1, "size_bytes": 1}]
    monkeypatch.setattr(sources, "search", fake_search)

    reply = asyncio.run(main.run_task(
        cfg, None, {}, "SSIS-123 " + _magnet("a" * 40), _async_progress()))

    assert "✅" not in reply
    # 只下了一次（死种），换源发现没有别的候选就放弃，不会反复下载
    assert len(downloaded) == 1


def _async_progress():
    async def p(text):
        pass
    return p
