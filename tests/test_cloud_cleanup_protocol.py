"""云端整理与清理：对着**真协议**（进程内假 gRPC 服务）跑完整链路。

前面的 test_cloud_cleanup_guards.py 用内存 Fake 验证「只删已知项、保留未知项」；
本文件验证的是**真适配器 + 真协议**：move、purge、目录回读在 gRPC 层的实际行为。

最要紧的一条：落地目录里常有「种子目录 + 里面的文件」两层结构，
观测只看第一层 —— 清理时若整目录 purge，**没被观测到的嵌套媒体就没了**。
本文件用真协议复现这个场景，断言嵌套文件活着。
"""
from __future__ import annotations

import pytest

from app import cloudingest, cloudorganize
from app.storage import clouddrive2 as cd2mod
from app.storage.base import StorageError
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


def _seed_tree(fake, root: str) -> None:
    """落地形态：收件目录下有一个种子目录，里面是正片 + 嵌套的花絮目录。"""
    fake.add_file(f"/{root}/SEED-1/main.mp4", 4096)        # 正片（观测得到）
    fake.add_file(f"/{root}/SEED-1/ad.txt", 12)            # 广告（会被丢弃）
    fake.add_file(f"/{root}/SEED-1/extras/second.mp4", 2048)  # 嵌套媒体（观测不到）


def test_nested_media_survives_cleanup_over_real_protocol(fake, cd2):
    """嵌套媒体不能被清理掉 —— 这是整目录 purge 最致命的后果。"""
    root = "迅雷云盘/我的资源/TASK1"
    _seed_tree(fake, root)

    # 观测：只读一层
    items = cloudingest.measure(cd2, root)
    assert "SEED-1" in items
    assert {f["name"] for f in items["SEED-1"]["files"]} == {"main.mp4", "ad.txt"}

    # 整理：把正片搬到归档目录
    plan = cloudorganize.plan("SEED-1", items["SEED-1"]["files"], folder_code="DEMO-1")
    assert plan.move == ["main.mp4"]
    assert plan.drop == ["ad.txt"]
    moved = cloudorganize.apply(cd2, root, plan, "DEMO-1", dst_root="迅雷云盘/X-Drive")
    assert moved["files"] == ["DEMO-1.mp4"]
    assert fake.files["/迅雷云盘/X-Drive/DEMO-1/DEMO-1.mp4"] == 4096

    # 清理：源目录里还有嵌套目录 → 必须保留现场并报 pending
    with pytest.raises(cloudorganize.OrganizeError) as err:
        cloudorganize.cleanup_source(cd2, root, plan, "迅雷云盘/X-Drive")
    assert "extras" in str(err.value) or "未知" in str(err.value)

    # 关键断言：嵌套媒体与广告都还在（广告是该删的，但清理被中止了，所以还在）
    assert "/迅雷云盘/我的资源/TASK1/SEED-1/extras/second.mp4" in fake.files
    assert fake.files["/迅雷云盘/我的资源/TASK1/SEED-1/extras/second.mp4"] == 2048
    assert "/迅雷云盘/我的资源/TASK1/SEED-1/ad.txt" in fake.files


def test_cleanup_deletes_only_known_drops_over_real_protocol(fake, cd2):
    """没有未知内容时：只删广告，正片已搬走，源目录壳留着。"""
    root = "迅雷云盘/我的资源/TASK2"
    fake.add_file(f"/{root}/SEED-2/main.mp4", 4096)
    fake.add_file(f"/{root}/SEED-2/ad.txt", 12)

    items = cloudingest.measure(cd2, root)
    plan = cloudorganize.plan("SEED-2", items["SEED-2"]["files"], folder_code="DEMO-2")
    cloudorganize.apply(cd2, root, plan, "DEMO-2", dst_root="迅雷云盘/X-Drive")

    receipt = cloudorganize.cleanup_source(cd2, root, plan, "迅雷云盘/X-Drive")
    assert "保留源目录" in receipt
    assert "/迅雷云盘/我的资源/TASK2/SEED-2/ad.txt" not in fake.files
    # 源目录壳还在（没有非递归 rmdir 契约，不硬删）
    assert "/迅雷云盘/我的资源/TASK2/SEED-2" in fake.dirs


def test_loose_single_file_landing_over_real_protocol(fake, cd2):
    """单文件种子：文件直接躺在收件目录根下，清理只删文件、留目录。"""
    root = "迅雷云盘/我的资源/TASK3"
    fake.add_file(f"/{root}/DEMO-3.mp4", 4096)

    items = cloudingest.measure(cd2, root)
    assert cloudingest.LOOSE_KEY in items
    plan = cloudorganize.plan("", items[cloudingest.LOOSE_KEY]["files"], folder_code="DEMO-3")
    assert plan.source == "" and plan.target == "DEMO-3"

    moved = cloudorganize.apply(cd2, root, plan, "DEMO-3", dst_root="迅雷云盘/X-Drive")
    assert moved["files"] == ["DEMO-3.mp4"]
    assert fake.files["/迅雷云盘/X-Drive/DEMO-3/DEMO-3.mp4"] == 4096

    receipt = cloudorganize.cleanup_source(cd2, root, plan, "迅雷云盘/X-Drive")
    assert receipt
    # 收件目录本身留着（不能连目录一起删）
    assert "/迅雷云盘/我的资源/TASK3" in fake.dirs
    assert "/迅雷云盘/我的资源/TASK3/DEMO-3.mp4" not in fake.files


def test_existing_empty_inbox_accepts_single_file_landing(fake, cd2):
    """已存在的空收件目录：提交前快照是空的，单文件落地能被认出来。"""
    root = "迅雷云盘/我的资源/TASK4"
    fake._mkdirs(root)

    before = cloudingest.snapshot_strict(cd2, root)
    assert before == {}, f"空收件目录的快照不该带聚合键：{before}"

    fake.add_file(f"/{root}/DEMO-4.mp4", 4096)
    items = cloudingest.measure(cd2, root)
    fresh = cloudingest.newcomers(before, items)
    assert fresh == [cloudingest.LOOSE_KEY], f"单文件落地应当被认出来：{fresh}"


def test_existing_inbox_with_history_is_not_claimed(fake, cd2):
    """收件目录里已有历史文件：不认领（不把别人的东西当成本次落地）。"""
    root = "迅雷云盘/我的资源/TASK5"
    fake.add_file(f"/{root}/OLD.mp4", 100)

    before = cloudingest.snapshot_strict(cd2, root)
    assert before, "有历史文件时快照不能是空的"

    items = cloudingest.measure(cd2, root)
    fresh = cloudingest.newcomers(before, items)
    assert fresh == [], f"历史文件不该被当成本次落地：{fresh}"


def test_apply_verifies_remote_after_move_over_real_protocol(fake, cd2):
    """搬完要回读核验：远端没有就是失败，不能记账。"""
    root = "迅雷云盘/我的资源/TASK6"
    fake.add_file(f"/{root}/SEED-6/main.mp4", 4096)
    items = cloudingest.measure(cd2, root)
    plan = cloudorganize.plan("SEED-6", items["SEED-6"]["files"], folder_code="DEMO-6")

    # 目标目录里有同名不同大小的文件 → 核验必须发现
    fake.add_file("/迅雷云盘/X-Drive/DEMO-6/DEMO-6.mp4", 1)
    with pytest.raises(cloudorganize.OrganizeError):
        cloudorganize.apply(cd2, root, plan, "DEMO-6", dst_root="迅雷云盘/X-Drive")


def test_cleanup_never_purges_source_directory_tree(fake, cd2):
    """任何情况下都不该整目录 purge 源 —— 断言 purge 的调用目标逐项检查。"""
    root = "迅雷云盘/我的资源/TASK7"
    _seed_tree(fake, root)
    items = cloudingest.measure(cd2, root)
    plan = cloudorganize.plan("SEED-1", items["SEED-1"]["files"], folder_code="DEMO-7")
    cloudorganize.apply(cd2, root, plan, "DEMO-7", dst_root="迅雷云盘/X-Drive")

    purged: list[str] = []
    original = cd2.purge

    def spy(path):
        purged.append(path)
        return original(path)

    cd2.purge = spy
    with pytest.raises(cloudorganize.OrganizeError):
        cloudorganize.cleanup_source(cd2, root, plan, "迅雷云盘/X-Drive")
    for path in purged:
        assert path != f"{root}/SEED-1", "绝不能整目录 purge 源目录"
        assert not path.startswith(f"{root}/SEED-1/extras"), "绝不能碰嵌套目录"
