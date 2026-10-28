"""空源目录壳的清理：只删「递归读过、确实空了」的目录，且用非递归删除。

为什么需要它：云添加每次留下一个收件目录壳（实测每次云添加留一个），
不清就会越积越多。但**整目录 purge 是递归的** —— 删除前若有别的任务刚好
往里落了文件，那份数据会跟着消失。所以必须：

  1. 先递归读一遍，确认真的空；
  2. 用**非递归**的删除（backend.remove_dir）。

真机实测（2026-10-25，CD2 1.0.18 + 迅雷云盘）：`DeleteFile` 对空目录删得掉；
对**非空**目录它会连子目录一起删（递归语义）—— 所以适配器自己那道
「确认是空」的检查才是真正的保护。

全部离线：对着进程内假 gRPC 服务跑真适配器。
"""
from __future__ import annotations

import pytest

from app import cloudadd, cloudorganize
from app.storage import clouddrive2 as cd2mod
from app.storage.base import NotFoundError, StorageError
from tests.cd2_fake import FakeCd2


@pytest.fixture()
def fake():
    server = FakeCd2().start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture()
def cd2(fake):
    return cd2mod.CloudDrive2(fake.url, fake.user, fake.password,
                              chunk_mb=1, task_timeout=10, call_timeout=10)


# ─────────────────────────────────────────── 适配器层

def test_remove_dir_deletes_empty_directory(fake, cd2):
    cd2.ensure_dir("inbox/task")
    assert "task" in cd2.list_dirs("inbox")

    receipt = cd2.remove_dir("inbox/task")

    assert "已删除" in receipt
    assert "task" not in cd2.list_dirs("inbox")


def test_remove_dir_refuses_non_empty_directory(fake, cd2):
    """非空目录必须拒绝 —— 适配器这道检查才是真正的保护。"""
    cd2.ensure_dir("inbox/task")
    fake.add_file("/inbox/task/v.mp4", 10)

    with pytest.raises(StorageError, match="非空"):
        cd2.remove_dir("inbox/task")

    assert "/inbox/task/v.mp4" in fake.files, "文件不能被删"
    assert "task" in cd2.list_dirs("inbox")


def test_remove_dir_refuses_directory_with_subdirectory(fake, cd2):
    """有子目录也算非空 —— 哪怕子目录里是空的。

    真机的 DeleteFile 是递归的：这里要是放过去，整个子树都会没。
    """
    cd2.ensure_dir("inbox/task")
    cd2.ensure_dir("inbox/task/sub")

    with pytest.raises(StorageError, match="非空"):
        cd2.remove_dir("inbox/task")

    assert "sub" in cd2.list_dirs("inbox/task")


# ─────────────────────────────────────────── 共享模块层

def test_is_empty_dir_walks_recursively(fake, cd2):
    """空壳判定要递归看：最深一层有文件，整个树就不算空。"""
    cd2.ensure_dir("a/b/c")
    assert cloudadd._is_empty_dir(cd2, "a") is True

    fake.add_file("/a/b/c/v.mp4", 10)
    assert cloudadd._is_empty_dir(cd2, "a") is False
    assert cloudadd._is_empty_dir(cd2, "a/b") is False


def test_remove_empty_shells_removes_only_empty_ones(fake, cd2):
    """空的删掉，不空的保留并记录原因。"""
    cd2.ensure_dir("inbox/empty")
    cd2.ensure_dir("inbox/full")
    fake.add_file("/inbox/full/v.mp4", 10)

    leftover = cloudadd.remove_empty_shells(cd2, ["inbox/empty", "inbox/full"])

    assert "empty" not in cd2.list_dirs("inbox"), "空壳应当被删"
    assert "full" in cd2.list_dirs("inbox"), "不空的必须保留"
    assert "/inbox/full/v.mp4" in fake.files
    assert len(leftover) == 1
    assert leftover[0]["source"] == "inbox/full"


def test_remove_empty_shells_keeps_directory_when_listing_fails(fake, cd2, monkeypatch):
    """读不出来 = 不确定 = 当作非空，保留。

    「不确定就删」是这里最危险的错误：一次读失败不该换来数据丢失。
    """
    cd2.ensure_dir("inbox/uncertain")

    def boom(path):
        raise StorageError("列表读取失败")
    monkeypatch.setattr(cd2, "list_dirs", boom)
    monkeypatch.setattr(cd2, "list_files", boom)

    leftover = cloudadd.remove_empty_shells(cd2, ["inbox/uncertain"])

    # 目录还在（断言绕开被打桩的 list_dirs，直接看假服务的目录表）
    assert "/inbox/uncertain" in fake.dirs
    assert len(leftover) == 1
    assert "非空" in leftover[0]["error"] or "内容" in leftover[0]["error"]


def test_remove_empty_shells_reports_backend_without_support(fake, cd2, monkeypatch):
    """后端没有非递归删除时：保留目录壳，只记原因。"""
    cd2.ensure_dir("inbox/task")

    class _NoRemove:
        """没有 remove_dir 的后端（老适配器就是这样）。"""
        def __init__(self, inner):
            self._inner = inner

        def list_dirs(self, path):
            return self._inner.list_dirs(path)

        def list_files(self, path):
            return self._inner.list_files(path)

    leftover = cloudadd.remove_empty_shells(_NoRemove(cd2), ["inbox/task"])

    assert "task" in cd2.list_dirs("inbox")
    assert len(leftover) == 1
    assert "不支持" in leftover[0]["error"]


# ─────────────────────────────────────────── 端到端：清理 → 空壳收尾

def test_clean_sources_removes_the_shell_after_cleanup(fake, cd2):
    """整理成功后，源目录壳与任务专属收件目录都被收掉（不越积越多）。

    注意「收件目录」有两层含义：
      · 任务专属的 `inbox/task` —— 设计上**要删**（每个任务留一个空文件夹，
        一个月就是几百个，这正是本模块存在的原因）；
      · 共享的 `inbox` 本身 —— 绝不能动，别的任务还要往里落。
    """
    root = "inbox/task"
    fake.add_file(f"/{root}/SEED-1/main.mp4", 4096)
    fake.add_file(f"/{root}/SEED-1/ad.txt", 12)

    items = cloudingest_items(cd2, root)
    plan = cloudorganize.plan("SEED-1", items["SEED-1"]["files"], folder_code="DEMO-1")
    moved = cloudorganize.apply(cd2, root, plan, "DEMO-1", dst_root="dest")
    assert moved["files"] == ["DEMO-1.mp4"]

    leftover = cloudadd.clean_sources(cd2, [{"plan": plan, "src_root": root, "dst_root": "dest"}])

    assert leftover == [], f"不该有残留：{leftover}"
    # 壳删干净了 —— 再列那个目录就是「路径不存在」（契约：目录不存在应抛）
    with pytest.raises(NotFoundError):
        cd2.list_dirs(f"{root}/SEED-1")
    # 任务专属收件目录也收掉了（避免每次云添加留一个空文件夹）
    with pytest.raises(NotFoundError):
        cd2.list_dirs(root)
    # 但共享的 inbox 根必须还在
    assert "inbox" in cd2.list_dirs(""), "共享收件根不能被连带删掉"


def test_clean_sources_keeps_shell_when_unknown_content_remains(fake, cd2):
    """有未知内容时不删壳 —— 整目录 purge 会把嵌套媒体连带删掉。"""
    root = "inbox/task"
    fake.add_file(f"/{root}/SEED-1/main.mp4", 4096)
    fake.add_file(f"/{root}/SEED-1/extras/second.mp4", 2048)

    items = cloudingest_items(cd2, root)
    plan = cloudorganize.plan("SEED-1", items["SEED-1"]["files"], folder_code="DEMO-1")
    cloudorganize.apply(cd2, root, plan, "DEMO-1", dst_root="dest")

    leftover = cloudadd.clean_sources(cd2, [{"plan": plan, "src_root": root, "dst_root": "dest"}])

    assert leftover, "有未知内容时必须有残留记录"
    assert "SEED-1" in cd2.list_dirs(root), "壳必须留着"
    assert f"/{root}/SEED-1/extras/second.mp4" in fake.files, "嵌套媒体不能被删"


def cloudingest_items(cd2, root):
    from app import cloudingest
    return cloudingest.measure(cd2, root)
