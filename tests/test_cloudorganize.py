"""云端整理：选正片、改名、搬运、回滚、路径安全。

这条链路全程在云盘上动，purge 会真删目录 —— 所以「挑哪些文件」和「拼什么路径」
两件事一步都不能错，这里逐条钉住。
"""
from __future__ import annotations

import pytest

from app import cloudorganize
from app.cloudorganize import OrganizeError


def F(name, size):
    return {"name": name, "size": size}


# ─────────────────────────────────────────── 选正片

def test_drops_ads_and_samples():
    """实测那个种子：1 个正片 4.4GB + 3 个推广视频/txt。"""
    files = [F("489155.com@MIMK-290.mp4", 4_408_303_886),
             F("台湾uu美少女直播 20年信誉保证服务全球.mp4", 13_890_047),
             F("社 區 最 新 情 報.mp4", 15_089_802),
             F("最 新 位 址 獲 取：489155.com 收藏不迷路.txt", 136),
             F("聚 合 全 網 H 直 播.html", 145)]
    plan = cloudorganize.plan("MIMK-290", files)
    assert plan.move == ["489155.com@MIMK-290.mp4"], "只留正片"
    assert len(plan.drop) == 4
    assert plan.size == 4_408_303_886


def test_keeps_multi_part_videos():
    """分片很常见：同一部片的多个视频都要留（按番号编号）。"""
    files = [F("a-1.mp4", 1000), F("a-2.mp4", 1000), F("sample.mp4", 5)]
    plan = cloudorganize.plan("ABC-123", files)
    assert sorted(plan.move) == ["a-1.mp4", "a-2.mp4"]


def test_image_only_pack():
    files = [F("p1.jpg", 100), F("p2.jpg", 100), F("readme.txt", 5)]
    plan = cloudorganize.plan("ABC-123", files)
    assert sorted(plan.move) == ["p1.jpg", "p2.jpg"]


def test_no_media_is_an_error():
    files = [F("readme.txt", 5), F("x.url", 3)]
    with pytest.raises(OrganizeError) as err:
        cloudorganize.plan("ABC-123", files)
    assert "没有可归档" in str(err.value)


def test_single_file_kept_even_if_small():
    """只有一个文件时不按体积比丢 —— 它可能就是全部。"""
    files = [F("only.mp4", 100)]
    assert cloudorganize.plan("ABC-123", files).move == ["only.mp4"]


# ─────────────────────────────────────────── 目标名与改名

def test_target_name_uses_code():
    assert cloudorganize.target_name("MIMK-290", [F("x.mp4", 1)]) == "MIMK-290"
    # 目录名认不出、但文件名里有番号
    assert cloudorganize.target_name("seed_name", [F("ABC-123.mp4", 1)]) == "ABC-123"


def test_target_name_falls_back_to_clean_folder():
    name = cloudorganize.target_name("我们 的:资源?*", [F("x.mp4", 1)])
    assert "/" not in name and ":" not in name and name


def test_renamed_single_file():
    plan = cloudorganize.plan("MIMK-290", [F("489155.com@MIMK-290.mp4", 10)])
    assert cloudorganize.renamed_files(plan, "MIMK-290") == [
        ("489155.com@MIMK-290.mp4", "MIMK-290.mp4")]


def test_renamed_multi_files_numbered():
    files = [F("a.mp4", 10), F("b.mp4", 10)]
    plan = cloudorganize.plan("ABC-123", files)
    assert cloudorganize.renamed_files(plan, "ABC-123") == [
        ("a.mp4", "ABC-123-1.mp4"), ("b.mp4", "ABC-123-2.mp4")]


def test_rename_keeps_extension():
    plan = cloudorganize.plan("ABC-123", [F("x.MKV", 10)])
    assert cloudorganize.renamed_files(plan, "ABC-123")[0][1] == "ABC-123.mkv"


# ─────────────────────────────────────────── 路径安全

@pytest.mark.parametrize("bad", ["../evil", "a/b", "a\\b", "..", "sub/../x"])
def test_plan_rejects_unsafe_source(bad):
    with pytest.raises(OrganizeError):
        cloudorganize.plan(bad, [F("x.mp4", 1)])


# ─────────────────────────────────────────── 执行

class FakeCloud:
    """记下所有云端动作；move_file 会真的改内部映射，便于断言最终状态。"""

    def __init__(self, files: dict[str, int]):
        self.files = dict(files)
        self.dirs: set[str] = set()
        self.purged: list[str] = []
        self.fail_on: str | None = None

    def ensure_dir(self, path):
        self.dirs.add(path)
        return path.rsplit("/", 1)[-1]

    def move_file(self, src, dst):
        if self.fail_on and self.fail_on in src:
            raise RuntimeError("云端拒绝")
        if dst in self.files:
            raise RuntimeError("目标已存在")
        self.files[dst] = self.files.pop(src)

    def purge(self, path):
        self.purged.append(path)
        for name in [f for f in self.files if f.startswith(path.rstrip("/") + "/")]:
            self.files.pop(name)


def test_apply_moves_and_purges():
    """目标目录与落地目录不同名：搬到新目录，原目录（含广告）整个清掉。"""
    cloud = FakeCloud({"inbox/seed_xxx/489155.com@MIMK-290.mp4": 100,
                       "inbox/seed_xxx/广告.txt": 1})
    plan = cloudorganize.plan("seed_xxx", cloudorganize_measure(cloud, "seed_xxx"))
    plan.target = "MIMK-290"                    # 目录名认不出番号，但文件里有
    out = cloudorganize.apply(cloud, "inbox", plan, "MIMK-290")
    assert out["files"] == ["MIMK-290.mp4"]
    assert "inbox/MIMK-290/MIMK-290.mp4" in cloud.files
    assert cloud.purged == ["inbox/seed_xxx"], "原目录连带广告一起清掉"


def test_apply_renames_in_place_without_purge():
    """目标与源同名（最常见的「目录名本身就是番号」）：只改名，绝不 purge。"""
    cloud = FakeCloud({"inbox/MIMK-290/489155.com@MIMK-290.mp4": 100,
                       "inbox/MIMK-290/广告.txt": 1})
    plan = cloudorganize.plan("MIMK-290", cloudorganize_measure(cloud))
    assert plan.target == "MIMK-290"
    out = cloudorganize.apply(cloud, "inbox", plan, "MIMK-290")
    assert out["files"] == ["MIMK-290.mp4"]
    assert "inbox/MIMK-290/MIMK-290.mp4" in cloud.files, "文件必须还在"
    assert cloud.purged == [], "同名时 purge 会把刚搬好的文件删掉"


def cloudorganize_measure(cloud, folder="MIMK-290"):
    from pathlib import PurePosixPath
    prefix = f"inbox/{folder}/"
    return [{"name": PurePosixPath(p).name, "size": n}
            for p, n in cloud.files.items() if p.startswith(prefix)]


def test_apply_rolls_back_on_move_failure():
    """搬到一半失败：尽力搬回去，别留一地半成品。"""
    cloud = FakeCloud({"inbox/A-1/a.mp4": 10, "inbox/A-1/b.mp4": 10})
    cloud.fail_on = "b.mp4"
    files = cloudorganize_measure(cloud, "A-1")
    plan = cloudorganize.plan("A-1", files)
    with pytest.raises(OrganizeError):
        cloudorganize.apply(cloud, "inbox", plan, "A-1")
    assert "inbox/A-1/a.mp4" in cloud.files, "已经搬走的那个要搬回来"
    assert not cloud.purged, "失败时不许删原目录"


def test_apply_rejects_unsafe_target():
    cloud = FakeCloud({})
    plan = cloudorganize.plan("A-1", [F("x.mp4", 1)])
    plan.target = "../escape"
    with pytest.raises(OrganizeError):
        cloudorganize.apply(cloud, "inbox", plan, "A-1")


def test_duplicate_source_names_is_an_error():
    """落地目录里出现同名文件：云端同名没法区分，计划阶段就拒绝。"""
    with pytest.raises(OrganizeError) as err:
        cloudorganize.plan("A-1", [F("x.mp4", 10), F("x.mp4", 10)])
    assert "同名" in str(err.value)


def test_multi_file_names_are_always_unique():
    """多文件一律按序号命名，天然不重名（这是保证，不是巧合）。"""
    plan = cloudorganize.plan("A-1", [F("a.mp4", 10), F("b.mp4", 10)])
    names = [new for _old, new in cloudorganize.renamed_files(plan, "A-1")]
    assert names == ["A-1-1.mp4", "A-1-2.mp4"]
    assert len(set(names)) == len(names)


def test_apply_guards_against_duplicate_targets(monkeypatch):
    """万一改名规则出 bug 造出重名，apply 必须拦下来（宁可不做，不覆盖）。"""
    plan = cloudorganize.plan("A-1", [F("a.mp4", 10), F("b.mp4", 10)])
    monkeypatch.setattr(cloudorganize, "renamed_files",
                        lambda p, code: [("a.mp4", "same.mp4"), ("b.mp4", "same.mp4")])
    with pytest.raises(OrganizeError) as err:
        cloudorganize.apply(FakeCloud({}), "inbox", plan, "A-1")
    assert "重名" in str(err.value)
