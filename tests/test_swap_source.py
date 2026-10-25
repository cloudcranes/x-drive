"""死种换源：云添加时迅雷解析失败，就换候选池里的下一条，而不是反复撞同一条。

2026-10-25 取消本地下载后，云添加是唯一的下载链路，但死种换源的能力要保住：
迅雷云盘对 0 个做种的种子会回「解析失败」，这时换一条候选重投才有意义。

按 **infohash** 判同源 —— 同一种子换不同 tracker 参数不该被当成「新候选」
反复撞。全部离线：提交/落地一律打桩。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app import cloudadd, main, sources, xunlei


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


# ─────────────────────────────────────────── 云添加的死种换源

class _Landing:
    def __init__(self, folder="SEED-1", total=1024):
        self.folder = folder
        self.total = total
        self.files = [{"name": "v.mp4", "size": total}]


def _mk_cfg(tmp_path):
    return main.Config(
        token="x", allowed={1}, remote="gdrive", drive_root="X", max_mb=10,
        concurrency=1, state_path=str(tmp_path / "s.json"), tmp_root=str(tmp_path / "dl"),
        rclone_config="/dev/null", pending_dir=str(tmp_path / "p"),
        metrics_path=str(tmp_path / "m.json"), state_dir=str(tmp_path / "work"),
        jobs_db=str(tmp_path / "jobs.db"),
    )


def _install(monkeypatch, tmp_path, wait_side_effect):
    """把云添加的每一步换成假的：提交、等待、整理、记账。"""
    cfg = _mk_cfg(tmp_path)
    monkeypatch.setattr(main, "_STORE", None)
    monkeypatch.setattr(cloudadd, "ready", lambda get: (True, "ok"))
    monkeypatch.setattr(cloudadd, "destinations", lambda get: ("inbox", "dest"))
    monkeypatch.setattr(cloudadd, "task_inbox", lambda inbox, key: f"{inbox}/{key}")

    submitted: list[str] = []

    def fake_submit(backend, source, my_inbox):
        submitted.append(source)
        return {"before": {}, "inbox": my_inbox}

    monkeypatch.setattr(cloudadd, "submit", fake_submit)

    async def fake_wait(backend, my_inbox, before, source, **kw):
        eff = wait_side_effect() if callable(wait_side_effect) else wait_side_effect
        if isinstance(eff, Exception):
            raise eff
        return [_Landing()]

    monkeypatch.setattr(cloudadd, "wait_landing", fake_wait)
    monkeypatch.setattr(cloudadd, "organize_landed", lambda *a, **k: (
        [{"folder": "DEMO-1", "path": "clouddrive2:dest/DEMO-1", "backend": "clouddrive2",
          "root": "dest", "rel": "dest/DEMO-1", "files": ["DEMO-1.mp4"],
          "files_detail": [{"name": "DEMO-1.mp4", "size": 1024}], "size": 1024,
          "inbox": "inbox/task"}], [], []))
    monkeypatch.setattr(cloudadd, "clean_sources", lambda *a, **k: [])
    monkeypatch.setattr(cloudadd, "build_record", lambda rec, scraped=None, **kw: {
        "code": rec["folder"], "path": rec["path"], "files": rec["files"],
        "size": rec["size"], "source": kw.get("source", ""),
        "infohash": kw.get("infohash", "")})
    return cfg, submitted


def _async_progress():
    async def p(text):
        pass
    return p


def test_dead_seed_swaps_source_then_succeeds(tmp_path, monkeypatch):
    """第一次死种，换源后第二次成功，且两次磁力不同。"""
    dead = RuntimeError("迅雷侧拒绝了这次下载：解析失败，请重试或更换链接")
    calls = {"n": 0}

    def side_effect():
        calls["n"] += 1
        return dead if calls["n"] == 1 else None   # 第一次死种，之后成功

    cfg, submitted = _install(monkeypatch, tmp_path, side_effect)

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
    assert len(submitted) == 2, f"应该先投死种再换源：{submitted}"
    assert submitted[0] == _magnet("a" * 40)
    assert submitted[1] == _magnet("b" * 40), "换源后应投新候选，不是重试同一条"


def test_dead_seed_gives_up_when_no_other_candidate(tmp_path, monkeypatch):
    """候选池里只有那一条死种时，换不到就按死种收尾，不死循环。"""
    dead = RuntimeError("迅雷侧拒绝了这次下载：解析失败，请重试或更换链接")
    cfg, submitted = _install(monkeypatch, tmp_path, dead)

    async def fake_search(code, client=None, limit=30):
        return [{"magnet": _magnet("a" * 40), "has_subtitle": True, "is_hd": True,
                 "seeders": 1, "size_bytes": 1}]
    monkeypatch.setattr(sources, "search", fake_search)

    reply = asyncio.run(main.run_task(
        cfg, None, {}, "SSIS-123 " + _magnet("a" * 40), _async_progress()))

    assert "✅" not in reply
    # 只投了一次（死种），换源发现没有别的候选就放弃，不会反复投
    assert len(submitted) == 1
