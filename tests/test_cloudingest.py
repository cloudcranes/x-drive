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


def test_measure_collects_loose_files_in_inbox():
    """inbox 根下的裸文件**要**算落地：单文件种子就落成这样（实测 MIMK-267）。

    早先「只认目录」是为共享 inbox 设防：那里躺着用户的 zip/iso/mp4。
    现在收件目录是**任务专属**的（`<inbox>/<任务键>`），别的任务和用户手动
    放的东西都进不来 —— 这个风险不存在了，而不看裸文件就会让单文件种子
    永远等不到落地（MIMK-267 就这么卡住的）。
    """
    b = FakeBackend()
    b.files["/inbox/MIMK-267ch.mp4"] = 5_282_620_174
    items = cloudingest.measure(b, "inbox")
    assert cloudingest.LOOSE_KEY in items, items
    assert items[cloudingest.LOOSE_KEY]["count"] == 1
    assert items[cloudingest.LOOSE_KEY]["bytes"] == 5_282_620_174
    assert items[cloudingest.LOOSE_KEY]["files"][0]["name"] == "MIMK-267ch.mp4"


def test_measure_still_counts_subdirectories():
    """多文件种子那种「建个种子同名目录再往里写」的形态照旧。"""
    b = FakeBackend()
    b.script = [{"kss55.cc@MAAN-1199": {"MAAN-1199.mp4": 911_376_996}}] * 3
    items = cloudingest.measure(b, "inbox")
    assert "kss55.cc@MAAN-1199" in items
    assert items["kss55.cc@MAAN-1199"]["bytes"] == 911_376_996


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
    assert out["inbox"] == "inbox"
    assert out["before"] == {}, "空目录应允许后续单文件成为 newcomer"
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


# ─────────────────────────────────────────── 归属隔离与重验（v6 补丁）

def test_task_inbox_isolates_per_job():
    """每个任务一个专属子目录：共享 inbox 曾让两个任务互相认领对方的落地。"""
    assert cloudingest.task_inbox("迅雷云盘/我的资源", "MIMK290") == \
        "迅雷云盘/我的资源/MIMK290"
    assert cloudingest.task_inbox("inbox", "") == "inbox/task", "空键退到占位，不拼出裸 inbox"
    assert "/" not in cloudingest.task_inbox("i", "a/b").rsplit("/", 1)[1]


def test_two_tasks_same_inbox_do_not_steal_each_other():
    """两个任务各看各的子目录：A 的落地不能被 B 的观察收走（曾经全收）。"""
    b = FakeBackend()
    b.script = [{"AUDIT-A": {"a.mp4": 10}}] * 6
    landed_a = run(b, {}, stable=2)          # 任务 A 的观察点 = inbox
    # 任务 B 的观察点在另一个子目录：A 的目录对 B 不可见，B 什么也收不到，
    # 等不到自己的落地就超时 —— 而不是把 A 的目录当成自己的
    with pytest.raises(LandingTimeout):
        asyncio.run(cloudingest.wait_landing(
            b, "inbox/task-B", {}, poll=0.01, timeout=0.3, stable=2))
    assert [x.folder for x in landed_a] == ["AUDIT-A"]


def test_strict_snapshot_refuses_to_degrade_to_empty():
    """提交前拍不到底片必须抛错：空快照会把既存目录全当成新落地。"""
    class Boom(FakeBackend):
        def list_dirs(self, path):
            raise RuntimeError("boom")
    with pytest.raises(RuntimeError):
        cloudingest.snapshot_strict(Boom(), "inbox")
    # 对照：旧 snapshot 仍允许降级（等待期间的观察用它，读不到当空）
    assert cloudingest.snapshot(Boom(), "inbox") == {}


def test_settled_dir_is_revoked_when_it_grows_again():
    """「稳定」不是一锤定音：已判稳定的目录体量又变，必须撤销重新等。

    迅雷中途卡一下再继续，旧版锁定第一次平稳的体量就去整理，少收文件，
    随后整目录清理把后到的内容删掉。
    """
    b = FakeBackend()
    b.script = [
        {"MIMK-290": {"a.mp4": 10}},
        {"MIMK-290": {"a.mp4": 10}},         # 稳 1
        {"MIMK-290": {"a.mp4": 100}},        # 放行条件未齐时又变了：必须撤销
        {"MIMK-290": {"a.mp4": 100}},         # 重新稳 1
        {"MIMK-290": {"a.mp4": 100}},         # 重新稳 2
        {"MIMK-290": {"a.mp4": 100}},
    ]
    landed = run(b, {}, stable=2)
    assert len(landed) == 1
    assert landed[0].total == 100, "拿旧体量（10）去整理就是漏收文件"


def test_returns_only_after_every_known_dir_is_settled():
    """A 已稳、B 还在涨：不许只返回 A（B 会被漏掉并随后被整目录清理波及）。"""
    class Scripted(FakeBackend):
        """A 一直稳定，B 每轮增长。"""
        def measure_hook(self, inbox):
            return None
    b = FakeBackend()
    # FakeBackend 的剧本机制是「每次 list_dirs 推进一格」；这里手写轮次：
    # 直接构造 measure 的返回序列最直观 —— 用 monkeypatch 式子类。
    seq = [
        {"A-1": {"a.mp4": 10}, "B-1": {"b.mp4": 1}},
        {"A-1": {"a.mp4": 10}, "B-1": {"b.mp4": 2}},
        {"A-1": {"a.mp4": 10}, "B-1": {"b.mp4": 3}},
        {"A-1": {"a.mp4": 10}, "B-1": {"b.mp4": 9}},   # B 最终稳定
        {"A-1": {"a.mp4": 10}, "B-1": {"b.mp4": 9}},
        {"A-1": {"a.mp4": 10}, "B-1": {"b.mp4": 9}},
    ]
    it = iter(seq)

    class SeqFake(FakeBackend):
        def list_dirs(self, path):
            return sorted(next(it, seq[-1]).keys())

        def list_files(self, path):
            step = seq[-1] if it.count is None else None
            return []

    # 更简单：直接给 FakeBackend 换 measure 序列
    steps = list(seq)
    calls = {"n": 0}

    def fake_measure(backend, inbox):
        idx = min(calls["n"], len(steps) - 1)
        calls["n"] += 1
        return {k: {"files": [{"name": n, "size": s} for n, s in v.items()],
                    "count": len(v), "bytes": sum(v.values())}
                for k, v in steps[idx].items()}

    import app.cloudingest as ci
    orig = ci.measure
    ci.measure = fake_measure
    try:
        landed = asyncio.run(ci.wait_landing(
            b, "inbox", {}, poll=0.01, timeout=3, stable=2))
    finally:
        ci.measure = orig
    got = {x.folder: x.total for x in landed}
    assert got == {"A-1": 10, "B-1": 9}, f"B 在涨时被丢弃了：{got}"


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


def test_fast_release_when_offline_complete(monkeypatch):
    """迅雷 API 明确回 COMPLETE 时，网盘检测到新文件只需 1 轮指纹即放行，免除 30s 盲等。"""
    b = FakeBackend()
    b.script = [{"MIMK-288": {"a.mp4": 100}}] * 2
    state = cloudingest.OfflineState(found=True, phase="PHASE_TYPE_COMPLETE", progress=100)
    monkeypatch.setattr(cloudingest, "inspect_offline_task", lambda url, client=None: state)

    landed = asyncio.run(cloudingest.wait_landing(
        b, "inbox", {}, poll=0.01, timeout=3, stable=5, source="magnet:?xt=urn:btih:abc"))
    assert len(landed) == 1
    assert landed[0].folder == "MIMK-288"
    assert landed[0].rounds == 1, "迅雷已完成时第 1 轮即放行，不必等满 stable=5 轮"
