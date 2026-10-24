"""云添加落地判定：返回值不可信、只认目录出现、稳定才算完。

这台 CD2 + 迅雷云盘不提供任何离线任务状态（实测），所以「下完没有」只能靠
观测云盘目录。这些测试把那套判据钉死：早一秒读是半截、假磁力永远等不到、
批量提交要收全、超时要能报出真实原因。
"""
from __future__ import annotations

import asyncio

import pytest

from app import cloudingest
from app.cloudingest import Landing, LandingTimeout


class FakeBackend:
    """只实现 cloudingest 用到的那几个方法，行为对齐真机。"""

    def __init__(self):
        self.dirs: set[str] = {"/inbox"}
        self.files: dict[str, int] = {}
        self.added: list[tuple[str, str]] = []
        # 每次调用 list_dirs 时推进一个「剧本」：模拟目录先出现、文件随后写
        self.script: list[dict[str, int]] = []
        self.round = 0

    # —— 后端接口
    def list_dirs(self, path: str) -> list[str]:
        self._advance()
        prefix = "/" + path.strip("/") + "/"
        return [d[len(prefix):] for d in sorted(self.dirs)
                if d.startswith(prefix) and "/" not in d[len(prefix):]]

    def list_files(self, path: str):
        class E:
            def __init__(self, name, size):
                self.name = name
                self.size = size
        prefix = "/" + path.strip("/") + "/"
        return [E(f[len(prefix):], n) for f, n in sorted(self.files.items())
                if f.startswith(prefix) and "/" not in f[len(prefix):]]

    def ensure_dir(self, path: str) -> str:
        self.dirs.add("/" + path.strip("/"))
        return path.rsplit("/", 1)[-1]

    def offline_add(self, url: str, folder: str) -> None:
        self.added.append((url, folder))

    # —— 测试控制
    def _advance(self):
        if self.round < len(self.script):
            step = self.script[self.round]
            self.round += 1
            for name, files in step.items():
                self.dirs.add("/inbox/" + name)
                for fname, size in files.items():
                    self.files[f"/inbox/{name}/{fname}"] = size


def run(backend, before, **kw):
    kw.setdefault("poll", 0.01)
    kw.setdefault("timeout", 2.0)
    kw.setdefault("stable", 2)
    return asyncio.run(cloudingest.wait_landing(backend, "inbox", before, **kw))


# ─────────────────────────────────────────── 观察

def test_snapshot_marks_dirs_and_sizes_files():
    b = FakeBackend()
    b.dirs.add("/inbox/sub")
    b.files["/inbox/a.mp4"] = 10
    snap = cloudingest.snapshot(b, "inbox")
    assert snap["sub"] == -1 and snap["a.mp4"] == 10


def test_snapshot_survives_read_errors():
    class Boom(FakeBackend):
        def list_dirs(self, path):
            raise RuntimeError("boom")

        def list_files(self, path):
            raise RuntimeError("boom")
    assert cloudingest.snapshot(Boom(), "inbox") == {}


def test_measure_counts_inside_directories():
    """迅雷把种子放同名目录里：体量要往里数一层，否则永远看到空目录。"""
    b = FakeBackend()
    b.dirs.add("/inbox/MIMK-290")
    b.files["/inbox/MIMK-290/a.mp4"] = 100
    b.files["/inbox/MIMK-290/b.nfo"] = 5
    measured = cloudingest.measure(b, "inbox")
    assert measured["MIMK-290"]["count"] == 2
    assert measured["MIMK-290"]["bytes"] == 105


# ─────────────────────────────────────────── 落地判定

def test_waits_until_stable():
    """目录先出现、文件随后写：只出现目录不算完（早读就是半截）。"""
    b = FakeBackend()
    b.script = [
        {"MIMK-290": {}},                                  # 第 1 轮：空目录
        {"MIMK-290": {"a.mp4": 100}},                      # 第 2 轮：写了一个
        {"MIMK-290": {"a.mp4": 100, "b.nfo": 5}},          # 第 3 轮：写完
        {"MIMK-290": {"a.mp4": 100, "b.nfo": 5}},          # 第 4 轮：稳定
        {"MIMK-290": {"a.mp4": 100, "b.nfo": 5}},
        {"MIMK-290": {"a.mp4": 100, "b.nfo": 5}},
    ]
    landed = run(b, {}, stable=2)
    assert len(landed) == 1
    assert landed[0].folder == "MIMK-290"
    assert landed[0].total == 105
    assert sorted(landed[0].names()) == ["a.mp4", "b.nfo"]


def test_fake_magnet_never_lands():
    """接口回 success 但迅雷不接收：必须超时报错，不能当成功。"""
    b = FakeBackend()                                     # script 空 = 永远不出现
    with pytest.raises(LandingTimeout) as err:
        run(b, {}, timeout=0.3, poll=0.05)
    assert "没有出现新内容" in str(err.value)


def test_batch_collects_every_landed_dir():
    """批量提交多个种子：一次来两个目录都要收下。"""
    b = FakeBackend()
    b.script = [{"A-1": {"a.mp4": 10}, "B-2": {"b.mp4": 20}}] * 6
    landed = run(b, {}, stable=2)
    assert sorted(x.folder for x in landed) == ["A-1", "B-2"]
    assert {x.folder: x.total for x in landed} == {"A-1": 10, "B-2": 20}


def test_existing_entries_are_not_treated_as_new():
    """提交前就在 inbox 里的同名目录，不算这次落地（否则会认错片子）。"""
    b = FakeBackend()
    b.dirs.add("/inbox/OLD-1")
    b.files["/inbox/OLD-1/old.mp4"] = 99
    before = cloudingest.snapshot(b, "inbox")
    b.script = [{"OLD-1": {"old.mp4": 99}}, {"OLD-1": {"old.mp4": 99}}] * 3
    with pytest.raises(LandingTimeout):
        run(b, before, timeout=0.3, poll=0.05)


def test_submit_snapshots_before_and_adds():
    b = FakeBackend()
    out = cloudingest.submit(b, "magnet:?xt=urn:btih:" + "a" * 40, "inbox")
    assert out["inbox"] == "inbox" and out["before"] == {}
    assert b.added == [("magnet:?xt=urn:btih:" + "a" * 40, "inbox")]
    assert "/inbox" in b.dirs


def test_progress_is_reported():
    b = FakeBackend()
    b.script = [{"X-1": {"a.mp4": 10}}] * 6
    seen = []
    run(b, {}, progress=lambda snap, rounds: seen.append((dict(snap), rounds)), stable=2)
    assert seen, "至少要回调一次（队列要显示「已出现 N 个文件」）"
    assert seen[-1][0]["X-1"]["bytes"] == 10


def test_cancel_stops_waiting():
    b = FakeBackend()
    with pytest.raises(LandingTimeout) as err:
        asyncio.run(cloudingest.wait_landing(
            b, "inbox", {}, poll=0.01, timeout=5, stable=2,
            cancel_check=lambda: True))
    assert "已停止" in str(err.value)


def test_folder_key_is_stable():
    a = cloudingest.folder_key("magnet:x", Landing(folder="MIMK-290"))
    b = cloudingest.folder_key("magnet:x", Landing(folder="MIMK-290"))
    assert a == b and "MIMK-290" in a


# ─────────────────────────────────────────── 与 web 的接缝

def test_cloud_ready_explains_why_not(monkeypatch):
    """没启用 / 没配目录 / 后端不对 —— 每种都要说清是哪一项，面板直接显示。"""
    from app import web
    monkeypatch.setattr(web, "get_setting", lambda k, d=None: False if k == "CLOUD_INGEST" else d)
    ok, why = web.cloud_ready()
    assert not ok and "未启用" in why

    values = {"CLOUD_INGEST": True, "CLOUD_INBOX": ""}
    monkeypatch.setattr(web, "get_setting",
                        lambda k, d=None: values.get(k, d))
    ok, why = web.cloud_ready()
    assert not ok and "落地目录" in why
