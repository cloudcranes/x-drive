"""2.1/2.2 云提交幂等续接：重试不重复投，而是沿用提交前快照继续观察。

背景：AddOfflineFiles 对假磁力也回 success（实测），重投只可能制造重复
落地或撞同名目录；提交成功后重启/超时重试，必须能认出「这个链接已经投过」。
"""
from __future__ import annotations

import pytest

from app import store, web


class FakeBackend:
    def __init__(self):
        self.dirs: set[str] = {"/inbox"}
        self.files: dict[str, int] = {}
        self.added: list[str] = []

    def list_dirs(self, path):
        prefix = "/" + path.strip("/") + "/"
        return [d[len(prefix):] for d in sorted(self.dirs)
                if d.startswith(prefix) and "/" not in d[len(prefix):]]

    def list_files(self, path):
        class E:
            name, size = "", 0

            def __init__(self, n, s):
                self.name, self.size = n, s
        prefix = "/" + path.strip("/") + "/"
        return [E(f[len(prefix):], n) for f, n in sorted(self.files.items())
                if f.startswith(prefix) and "/" not in f[len(prefix):]]

    def ensure_dir(self, path):
        self.dirs.add("/" + path.strip("/"))
        return path.rsplit("/", 1)[-1]

    def offline_add(self, url, folder):
        self.added.append(url)


def _env(tmp_path, monkeypatch):
    st = store.Store(str(tmp_path / "jobs.db"))
    monkeypatch.setattr(web, "STORE", st)
    monkeypatch.setattr(web, "JOBS", {})
    monkeypatch.setattr(web, "CANCEL", {})
    return st


def test_first_submit_really_submits_and_records(tmp_path, monkeypatch):
    st = _env(tmp_path, monkeypatch)
    st.start("j1", "MIMK-290", "magnet:one")
    b = FakeBackend()
    out = web._cloud_submit(b, "j1", "magnet:one", "inbox/task")
    assert b.added == ["magnet:one"], "第一次要真的投"
    extra = st.get("j1")["extra"]
    assert extra["cloud_submit"]["url"] == "magnet:one"
    assert extra["cloud_submit"]["before"] == {}, out


def test_retry_reuses_submission_instead_of_resubmitting(tmp_path, monkeypatch):
    """重试（同链接）不重复投：提交意图已持久化。"""
    st = _env(tmp_path, monkeypatch)
    st.start("j1", "MIMK-290", "magnet:one")
    b = FakeBackend()
    web._cloud_submit(b, "j1", "magnet:one", "inbox/task")
    out2 = web._cloud_submit(b, "j1", "magnet:one", "inbox/task")
    assert b.added == ["magnet:one"], f"重投了：{b.added}"
    assert out2["before"] == {}, "续接沿用提交前快照"


def test_different_source_submits_again(tmp_path, monkeypatch):
    st = _env(tmp_path, monkeypatch)
    st.start("j1", "MIMK-290", "magnet:one")
    b = FakeBackend()
    web._cloud_submit(b, "j1", "magnet:one", "inbox/task")
    web._cloud_submit(b, "j1", "magnet:two", "inbox/task")
    assert b.added == ["magnet:one", "magnet:two"]


def test_resume_after_restart_sees_existing_landing(tmp_path, monkeypatch):
    """提交后落地完成、重启重试：沿用 before 就能立刻收下（新目录是 newcomer）。"""
    from app import cloudingest
    st = _env(tmp_path, monkeypatch)
    st.start("j1", "MIMK-290", "magnet:one")
    b = FakeBackend()
    web._cloud_submit(b, "j1", "magnet:one", "inbox/task")
    # 落地已发生（模拟下载完成）
    b.dirs.add("/inbox/task/MIMK-290")
    b.files["/inbox/task/MIMK-290/a.mp4"] = 5
    before = web._cloud_submit(b, "j1", "magnet:one", "inbox/task")["before"]
    items = cloudingest.measure(b, "inbox/task")
    assert cloudingest.newcomers(before, items) == ["MIMK-290"], \
        "续接快照下，已落地目录要被认出（而不是继续傻等）"
