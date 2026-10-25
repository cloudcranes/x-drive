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
    # 报错要说清是「格式不认识」而不是「只有广告」——
    # 后者会让人以为是种子的错，其实是程序没认出这个容器
    assert "格式都不认识" in str(err.value), str(err.value)
    assert "readme.txt" in str(err.value), "要把文件名列出来"


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

    def list_dirs(self, path):
        prefix = path.rstrip("/") + "/"
        return sorted({p[len(prefix):].split("/", 1)[0] for p in self.files
                       if p.startswith(prefix) and "/" in p[len(prefix):]})

    def list_files(self, path):
        """与真实后端一致，只列直接子文件，不将嵌套媒体伪装成顶层文件。"""
        from pathlib import PurePosixPath
        prefix = path.rstrip("/") + "/"
        return [type("E", (), {"name": PurePosixPath(p).name, "size": n})()
                for p, n in self.files.items()
                if p.startswith(prefix) and "/" not in p[len(prefix):]]

    def move_file(self, src, dst):
        if self.fail_on and self.fail_on in src:
            raise RuntimeError("云端拒绝")
        if dst in self.files:
            raise RuntimeError("目标已存在")
        self.files[dst] = self.files.pop(src)

    def purge(self, path):
        self.purged.append(path)
        for name in [f for f in self.files
                     if f == path or f.startswith(path.rstrip("/") + "/")]:
            self.files.pop(name)
        return "已删除"


def test_apply_moves_and_verifies_without_purge():
    """目标目录与落地目录不同名：搬到新目录。apply 只搬+核验，**不再删源**。"""
    cloud = FakeCloud({"inbox/seed_xxx/489155.com@MIMK-290.mp4": 100,
                       "inbox/seed_xxx/广告.txt": 1})
    plan = cloudorganize.plan("seed_xxx", cloudorganize_measure(cloud, "seed_xxx"))
    plan.target = "MIMK-290"                    # 目录名认不出番号，但文件里有
    out = cloudorganize.apply(cloud, "inbox", plan, "MIMK-290", dst_root="archive")
    assert out["files"] == ["MIMK-290.mp4"]
    assert "archive/MIMK-290/MIMK-290.mp4" in cloud.files, "要真的搬到归档根"
    assert cloud.purged == [], "清理挪到 cleanup_source：归档落盘之后才允许删"


def test_cleanup_source_removes_source_after_archive():
    """归档落盘后单独清理：源目录（含广告）删掉，目标目录完好。"""
    cloud = FakeCloud({"inbox/seed_xxx/489155.com@MIMK-290.mp4": 100,
                       "inbox/seed_xxx/广告.txt": 1})
    plan = cloudorganize.plan("seed_xxx", cloudorganize_measure(cloud, "seed_xxx"))
    plan.target = "MIMK-290"
    cloudorganize.apply(cloud, "inbox", plan, "MIMK-290", dst_root="archive")
    receipt = cloudorganize.cleanup_source(cloud, "inbox", plan, "archive")
    assert receipt and cloud.purged == ["inbox/seed_xxx/广告.txt"]
    assert "archive/MIMK-290/MIMK-290.mp4" in cloud.files, "目标目录不能被误删"
    assert not [f for f in cloud.files if f.startswith("inbox/seed_xxx")], \
        "广告文件跟着源目录一起消失"


def test_cleanup_source_refuses_same_name():
    """源与目标同名（原地整理）绝不能清：会把刚搬好的文件删掉。"""
    cloud = FakeCloud({"inbox/MIMK-290/MIMK-290.mp4": 100})
    plan = cloudorganize.plan("MIMK-290", cloudorganize_measure(cloud))
    with pytest.raises(OrganizeError):
        cloudorganize.cleanup_source(cloud, "inbox", plan)      # dst 缺省 = src


def test_apply_verifies_target_or_raises():
    """搬完必须回读核验：目标目录里没有那个文件就不许返回成功。"""
    cloud = FakeCloud({"inbox/A-1/a.mp4": 10})
    plan = cloudorganize.plan("A-1", cloudorganize_measure(cloud, "A-1"))
    # 搬完偷偷把文件从目标目录里抹掉（模拟远端异常），核验必须抓住
    orig = cloud.move_file

    def move_then_vanish(src, dst):
        orig(src, dst)
        cloud.files.pop(dst, None)
    cloud.move_file = move_then_vanish
    with pytest.raises(OrganizeError) as err:
        cloudorganize.apply(cloud, "inbox", plan, "A-1", dst_root="archive")
    assert "核验失败" in str(err.value)


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


def test_apply_moves_to_separate_dst_root():
    """落地目录（inbox）与归档根必须分开：混成一个就会「原地改名」，账实不符。"""
    cloud = FakeCloud({"inbox/seed_1/BIG-999.mp4": 900,
                       "inbox/seed_1/广告.mp4": 10})
    plan = cloudorganize.plan("seed_1", cloudorganize_measure(cloud, "seed_1"))
    out = cloudorganize.apply(cloud, "inbox", plan, "BIG-999", dst_root="archive")
    assert out["folder"] == "archive/BIG-999"
    assert "archive/BIG-999/BIG-999.mp4" in cloud.files
    assert cloud.purged == [], "apply 不再删源；清理归 cleanup_source"


def test_cleanup_source_removes_source_after_archive():
    """归档落盘后单独清理：源目录（含广告）删掉，目标目录完好。"""
    cloud = FakeCloud({"inbox/seed_xxx/489155.com@MIMK-290.mp4": 100,
                       "inbox/seed_xxx/广告.txt": 1})
    plan = cloudorganize.plan("seed_xxx", cloudorganize_measure(cloud, "seed_xxx"))
    plan.target = "MIMK-290"
    cloudorganize.apply(cloud, "inbox", plan, "MIMK-290", dst_root="archive")
    receipt = cloudorganize.cleanup_source(cloud, "inbox", plan, "archive")
    assert receipt and cloud.purged == ["inbox/seed_xxx/广告.txt"]
    assert "archive/MIMK-290/MIMK-290.mp4" in cloud.files, "目标目录不能被误删"
    assert not [f for f in cloud.files if f.startswith("inbox/seed_xxx")], \
        "广告文件跟着源目录一起消失"


def test_apply_rejects_empty_roots():
    cloud = FakeCloud({})
    plan = cloudorganize.plan("A-1", [F("x.mp4", 1)])
    with pytest.raises(OrganizeError):
        cloudorganize.apply(cloud, "", plan, "A-1", dst_root="archive")


# ─────────────────────────────────────────── 多集/多图：不能按体积误杀

def test_keeps_all_episodes_of_a_multi_episode_release():
    """多集发行：一集 4K + 若干 1080p，**每一集都要留**。

    体积比例规则（本意是「正片 4.4GB vs 推广视频 14MB，差三个数量级」）
    在多集上会误杀：1080p 一集的体积约 4K 的 1/5，正好落在 0.25 线上 ——
    于是三集正片被当成样片**永久删除**，站内只剩一集的记录，用户无从察觉。

    判据不是体积，是**结构**：同扩展名的多个文件 = 多集/分卷，全留。
    """
    files = [F("ep1.mkv", 1_500_000_000), F("ep2.mkv", 1_500_000_000),
             F("ep3.mkv", 1_500_000_000), F("ep4.mkv", 8_000_000_000),
             F("ad.txt", 136)]
    plan = cloudorganize.plan("DEMO-1", files)
    assert sorted(plan.move) == ["ep1.mkv", "ep2.mkv", "ep3.mkv", "ep4.mkv"], \
        "多集的每一集都要留"
    assert plan.drop == ["ad.txt"], "只有广告该丢"


def test_keeps_all_photos_of_an_image_pack():
    """图包同理：100 张照片，一���都不能丢。

    同一条逻辑在图包上炸过：最大 5MB、其余 800KB → 只留 1 张，其余 99 张
    连同封面一起被 purge。
    """
    files = [F("PHOTO-1.jpg", 5_000_000)]
    files += [F(f"PHOTO-{i}.jpg", 800_000) for i in range(2, 101)]
    plan = cloudorganize.plan("PIC-1", files)
    assert len(plan.move) == 100, f"图包要全留，实得 {len(plan.move)}"


def test_keeps_sidecar_files_with_the_main_video():
    """字幕 / 封面是**正片的附属**，跟着一起留，不当广告丢。"""
    files = [F("main.mp4", 4_000_000_000), F("main.srt", 45_000),
             F("cover.jpg", 120_000)]
    plan = cloudorganize.plan("DEMO-2", files)
    assert "main.mp4" in plan.move
    assert "main.srt" in plan.move, "字幕要跟着正片留"
    assert "cover.jpg" in plan.move, "封面要跟着正片留"
    assert plan.drop == []


def test_still_drops_a_real_ad_next_to_a_single_video():
    """护栏不能把广告也留下 —— 单个正片 + 明显小的推广视频，仍然只留正片。"""
    files = [F("movie.mp4", 4_400_000_000),
             F("台湾uu美少女直播 20年信誉保证.mp4", 13_890_000),
             F("社区最新情报.mp4", 15_089_802)]
    plan = cloudorganize.plan("DEMO-3", files)
    assert plan.move == ["movie.mp4"], "单个正片场景仍要认得出广告"
    assert len(plan.drop) == 2


# ─────────────────────────────────────────── 重复下载 / 多处落地

class _FakeBackend:
    """够用的假后端：能列、能搬、能删。"""

    def __init__(self, files=None, dirs=None):
        self.files = dict(files or {})
        self.dirs = set(dirs or ())

    def list_dirs(self, path):
        prefix = (path.rstrip("/") + "/") if path else ""
        out = set()
        for d in self.dirs:
            if d.startswith(prefix) and "/" not in d[len(prefix):]:
                out.add(d[len(prefix):])
        return sorted(out)

    def list_files(self, path):
        prefix = (path.rstrip("/") + "/") if path else ""
        out = []
        for name, size in self.files.items():
            if name.startswith(prefix) and "/" not in name[len(prefix):]:
                out.append(type("E", (), {"name": name[len(prefix):], "size": size})())
        return out

    def ensure_dir(self, path):
        self.dirs.add(path)
        return path

    def move_file(self, src, dst):
        # 真后端是「先查目标在不在，不在才搬」—— 失败时源文件原封不动
        if dst in self.files:
            raise RuntimeError(f"目标已存在：{dst}")
        if src not in self.files:
            raise RuntimeError(f"源文件不存在：{src}")
        self.files[dst] = self.files.pop(src)

    def purge(self, path):
        self.files.pop(path, None)
        for f in [f for f in self.files if f.startswith(path + "/")]:
            self.files.pop(f)
        return "已删除"


def test_already_archived_file_is_treated_as_redundant_not_fatal():
    """目标里已有同名同大小的文件 → 源文件是冗余，不当致命错误。

    以前这种情况直接抛错 → 任务 failed → 源文件留在原地 → 重跑再撞同一个
    已存在的目录再失败 —— **死循环**。
    """
    be = _FakeBackend(files={"dest/DEMO-1/DEMO-1.mp4": 4000,
                             "inbox/seed/movie.mp4": 4000},
                      dirs={"dest/DEMO-1", "inbox/seed"})
    plan = cloudorganize.plan("seed", [F("movie.mp4", 4000)], folder_code="DEMO-1")

    moved = cloudorganize.apply(be, "inbox", plan, "DEMO-1", dst_root="dest")

    assert moved["repeated"] == ["movie.mp4"], moved
    assert moved["files"] == ["DEMO-1.mp4"], "目标里已有的文件要算进清单"
    assert "inbox/seed/movie.mp4" in be.files, "冗余的源文件先留着，交给清理"


def test_same_name_different_size_is_not_treated_as_archived():
    """同名**不同大小** = 换版重发，不是重复下载 —— 不能当冗余丢掉。"""
    be = _FakeBackend(files={"dest/DEMO-1/DEMO-1.mp4": 9999,
                             "inbox/seed/movie.mp4": 4000},
                      dirs={"dest/DEMO-1", "inbox/seed"})
    plan = cloudorganize.plan("seed", [F("movie.mp4", 4000)], folder_code="DEMO-1")

    with pytest.raises(OrganizeError):
        cloudorganize.apply(be, "inbox", plan, "DEMO-1", dst_root="dest")

    assert "inbox/seed/movie.mp4" in be.files, "源文件不能被丢"


def test_plan_rejects_illegal_folder_code():
    """番号不合法要在**计划阶段**就拒 —— 推到执行阶段报错会让人以为是云端的问题。"""
    with pytest.raises(OrganizeError, match="番号不合法"):
        cloudorganize.plan("", [F("movie.mp4", 100)], folder_code="../x")


def test_suffix_ignores_trailing_space():
    """文件名带尾随空格时扩展名仍要认出来 —— 否则那个文件被静默当广告丢掉。"""
    assert cloudorganize._suffix("movie.mp4 ") == ".mp4"
    assert cloudorganize._suffix("movie.MP4") == ".mp4"


def test_m2ts_is_recognized_as_video():
    """BD 原盘的 .m2ts 要认得 —— 不认得的话整个任务失败且报「只有广告？」。"""
    plan = cloudorganize.plan("seed", [F("movie.m2ts", 8_000_000_000)],
                              folder_code="X-3")
    assert plan.move == ["movie.m2ts"]
