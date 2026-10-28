"""「云盘已添加」索引：搜磁力时据此标出哪些重复了。

判断依据两条互补的证据：
  · 归档账本（state 表）里的 infohash —— 资源身份；
  · 任务表里的 infohash —— 提交过就算（含失败的：多半已落地）。

注意两类标记含义不同，别混：
  · added_exact —— **这条磁力本身**下过（infohash 相同），纯重复；
  · added_code —— 同番号别的版本，可能不重复，只作提示。
"""
from __future__ import annotations

import json

import pytest

from app import added, store

HASH_A = "52c829d6564d0cbd530085ea95e92094857e0136"
HASH_B = "0130a8a6b0bcf2f26aab2ba3c56853740e7453f6"
MAG_A = f"magnet:?xt=urn:btih:{HASH_A.upper()}&dn=MIMK-267ch"


# ─────────────────────────────────────────── 归档账本

def test_index_from_state_reads_infohash_field():
    idx = added.from_state({"MIMK-267": {"code": "MIMK-267", "infohash": HASH_A}})
    assert idx.match_hash(MAG_A) is not None
    assert idx.match_code("MIMK-267") is not None


def test_index_from_state_falls_back_to_source_magnet():
    """归档记录里 infohash 常常是空的（只有 source 是 magnet 时才填）——
    所以要从 source 里把 btih 抠出来。"""
    idx = added.from_state({"MIMK-267": {"code": "MIMK-267", "source": MAG_A}})
    assert idx.match_hash(MAG_A) is not None


def test_index_from_state_handles_empty_and_garbage():
    assert len(added.from_state({})) == 0
    assert len(added.from_state({"x": "不是字典"})) == 0
    assert len(added.from_state({"x": {"code": "MIMK-1"}})) == 0


# ─────────────────────────────────────────── 任务表

def test_index_from_jobs_includes_failed_tasks(tmp_path):
    """失败的任务也算「已添加」：资源多半已经落地在云盘了。"""
    db = str(tmp_path / "jobs.db")
    st = store.Store(db)
    st.start("j1", "MIMK-188", MAG_A)
    st.update("j1", stage="failed")

    idx = added.from_jobs(st)

    assert idx.match_hash(MAG_A) is not None
    assert idx.match_code("MIMK-188") is not None


def test_index_from_jobs_survives_broken_store():
    class Boom:
        def recent(self, limit=20):
            raise RuntimeError("db locked")

    assert len(added.from_jobs(Boom())) == 0


# ─────────────────────────────────────────── 标注

def test_annotate_marks_exact_match():
    idx = added.from_state({"MIMK-267": {"code": "MIMK-267", "infohash": HASH_A}})
    items = [{"magnet": MAG_A, "size_text": "4.9GB"}]

    added.annotate(items, idx, "MIMK-267")

    assert items[0]["added_exact"] is True


def test_annotate_marks_same_code_different_version():
    """同番号但 infohash 不同 —— 不是同一个资源，只作提示。"""
    idx = added.from_state({"MIMK-267": {"code": "MIMK-267", "infohash": HASH_A}})
    items = [{"magnet": f"magnet:?xt=urn:btih:{HASH_B}&dn=MIMK-267"}]

    added.annotate(items, idx, "MIMK-267")

    assert "added_exact" not in items[0]
    assert items[0]["added_code"] is True


def test_annotate_leaves_unrelated_items_alone():
    idx = added.from_state({"MIMK-267": {"code": "MIMK-267", "infohash": HASH_A}})
    items = [{"magnet": f"magnet:?xt=urn:btih:{HASH_B}&dn=SSIS-999"}]

    added.annotate(items, idx, "SSIS-999")

    assert "added_exact" not in items[0]
    assert "added_code" not in items[0]


def test_annotate_exact_wins_over_code():
    """同一个资源命中时只报 exact，别又补一个 code（那是同一条的两个标签）。"""
    idx = added.from_state({"MIMK-267": {"code": "MIMK-267", "infohash": HASH_A}})
    items = [{"magnet": MAG_A}]

    added.annotate(items, idx, "MIMK-267")

    assert items[0]["added_exact"] is True
    assert "added_code" not in items[0]


def test_annotate_with_empty_index_is_noop():
    items = [{"magnet": MAG_A}]
    added.annotate(items, added.AddedIndex(), "MIMK-267")
    assert "added_exact" not in items[0]


def test_annotate_handles_broken_magnet():
    idx = added.from_state({"MIMK-267": {"code": "MIMK-267", "infohash": HASH_A}})
    items = [{"magnet": "不是磁力"}, {"no_magnet": 1}]

    added.annotate(items, idx, "MIMK-267")

    assert "added_exact" not in items[0]


# ─────────────────────────────────────────── 合并

def test_build_merges_state_and_jobs(tmp_path):
    db = str(tmp_path / "jobs.db")
    st = store.Store(db)
    st.start("j1", "MIMK-189", f"magnet:?xt=urn:btih:{HASH_B}")
    st.update("j1", stage="done")

    idx = added.build({"MIMK-267": {"code": "MIMK-267", "infohash": HASH_A}}, st,
                      include_cloud_tasks=False)

    assert idx.match_hash(MAG_A) is not None
    assert idx.match_hash(f"magnet:?xt=urn:btih:{HASH_B}") is not None


def test_build_for_reads_files(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(
        {"MIMK-267": {"code": "MIMK-267", "infohash": HASH_A}}), encoding="utf-8")

    idx = added.build_for(str(state_path), str(tmp_path / "jobs.db"),
                          include_cloud_tasks=False)

    assert idx.match_hash(MAG_A) is not None


def test_build_for_tolerates_missing_state(tmp_path):
    idx = added.build_for(str(tmp_path / "没有这个文件.json"),
                          str(tmp_path / "jobs.db"), include_cloud_tasks=False)
    assert len(idx) == 0


def test_code_of_extracts_from_folder_name():
    assert added._code_of("MIMK-188") == "MIMK-188"
    assert added._code_of("mimk188") == "MIMK-188"
    assert added._code_of("随便什么") == ""
