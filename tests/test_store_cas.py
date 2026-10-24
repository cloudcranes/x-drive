"""v6 状态机补丁：cloud 进活跃集合、唯一索引重建、租约条件写入（CAS）。

三个真实缺陷钉死在这里：
  · cloud 没进 ACTIVE：同番号重复提交、重启恢复漏任务、订阅对账回流（曾实测）；
  · 旧索引的 WHERE 常量不会自己更新：IF NOT EXISTS 对同名旧索引无效（v2 踩过）；
  · 失租旧线程能把共享任务写成终态：新执行者持租在跑，任务却 failed/cancelled，
    「清空已完成」还会把它删掉。
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from app import store


# ---------------------------------------------------------------- cloud 进活跃集合

def test_cloud_counts_as_active_and_unfinished(tmp_path):
    s = store.Store(str(tmp_path / "jobs.db"))
    s.start("j1", "DEMO-1", "magnet:a")
    s.update("j1", stage="cloud")
    assert [j["id"] for j in s.unfinished()] == ["j1"], "云落地中也是未完成，重启要恢复"
    assert s.active_codes() == {"DEMO-1"}, "云任务占番号去重"


def test_cloud_blocks_duplicate_start(tmp_path):
    """同番号在 cloud 期间再提交必须被唯一索引挡住（曾经放进来了）。"""
    s = store.Store(str(tmp_path / "jobs.db"))
    assert s.start("j1", "DEMO-1", "magnet:first")
    s.update("j1", stage="cloud")
    assert not s.start("j2", "DEMO-1", "magnet:second"), \
        "cloud 没进活跃集合时这里会返回 True —— 重复下载同一部片"


def test_cloud_blocked_by_unique_index_after_reopen(tmp_path):
    """唯一索引条件里要有 cloud：重开库（另一进程）后依然去重。"""
    path = str(tmp_path / "jobs.db")
    s = store.Store(path)
    s.start("j1", "DEMO-1", "magnet:first")
    s.update("j1", stage="cloud")
    s2 = store.Store(path)                      # 模拟 bot 进程打开同一个库
    assert not s2.start("j2", "DEMO-1", "magnet:second")


# ---------------------------------------------------------------- 旧库迁移重建索引

LEGACY_V5_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS jobs_active_code
    ON jobs(code) WHERE code <> '' AND stage IN
        ('queued','downloading','organizing','uploading','deferred');
"""

def legacy_v5_db(tmp_path) -> str:
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    # 一张 v5 时代的 jobs 表（v6 没有加列，结构与现表一致）＋ 不含 cloud 的旧索引：
    # 模拟一个升级前的真实旧库。INSERT 一个 stage=cloud 的任务，
    # v6 之前它在索引条件外 —— 重复提交拦不住，这正是要修的。
    conn.executescript("""
CREATE TABLE jobs (
    id TEXT PRIMARY KEY, code TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
    stage TEXT NOT NULL DEFAULT 'queued', percent REAL NOT NULL DEFAULT 0,
    speed REAL NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
    downloaded INTEGER NOT NULL DEFAULT 0, folder TEXT NOT NULL DEFAULT '',
    link TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
    retries INTEGER NOT NULL DEFAULT 0, owner TEXT NOT NULL DEFAULT '',
    xunlei_task TEXT NOT NULL DEFAULT '', xunlei_ours INTEGER NOT NULL DEFAULT 1,
    keep_dir TEXT NOT NULL DEFAULT '', resume_from TEXT NOT NULL DEFAULT '',
    exec_owner TEXT NOT NULL DEFAULT '', exec_beat REAL NOT NULL DEFAULT 0,
    extra TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
""" + LEGACY_V5_INDEX)
    conn.execute(
        "INSERT INTO jobs (id, code, source, stage, extra, created_at, updated_at)"
        " VALUES ('j1','DEMO-1','magnet:a','cloud','{}',?,?)", (time.time(), time.time()))
    conn.commit()
    conn.close()
    return path


def test_migration_v6_rebuilds_index_to_include_cloud(tmp_path):
    path = legacy_v5_db(tmp_path)
    s = store.Store(path)
    assert s.migrate() == store.SCHEMA_VERSION
    conn = sqlite3.connect(path)
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='jobs_active_code'").fetchone()[0]
    conn.close()
    assert "'cloud'" in sql, "唯一索引还是旧条件，cloud 期间重复提交拦不住"
    assert not s.start("j2", "DEMO-1", "magnet:second")


# ---------------------------------------------------------------- CAS 写入（租约条件）

def test_update_cas_only_when_lease_held(tmp_path):
    s = store.Store(str(tmp_path / "jobs.db"))
    s.start("j1", "DEMO-1", "magnet:a")
    s.claim_owner("j1", "tok-old")
    assert s.update_cas("j1", "tok-old", stage="downloading")
    # 被接管后，旧 token 的写入必须被拒绝
    s.claim_owner("j1", "tok-new", now=time.time() + store.STALE_BEAT + 1)
    assert not s.update_cas("j1", "tok-old", stage="cancelled"), \
        "失租线程不能覆盖共享状态"


def test_finish_cas_refuses_after_takeover(tmp_path):
    """新执行者接管后，旧执行者写 failed 必须失败 —— 否则清空会把活任务删掉。"""
    s = store.Store(str(tmp_path / "jobs.db"))
    s.start("j1", "DEMO-1", "magnet:a")
    s.claim_owner("j1", "old")
    s.claim_owner("j1", "new", now=time.time() + store.STALE_BEAT + 1)
    assert not s.finish_cas("j1", "old", "failed", "旧线程的收尾")
    job = s.get("j1")
    assert job["stage"] == "queued", "接管者的任务状态不许被旧线程改掉"
    assert job["exec_owner"] == "new", "接管者的租约不许被动"
    # 接管者自己写终态才是合法的
    assert s.finish_cas("j1", "new", "done")


def test_clear_finished_keeps_leased_job_after_cas(tmp_path):
    """回归「终态覆盖 → 清空误删」全链：CAS 挡住旧线程后，清空不丢活任务。"""
    s = store.Store(str(tmp_path / "jobs.db"))
    s.start("j1", "DEMO-1", "magnet:a")
    s.claim_owner("j1", "old")
    s.claim_owner("j1", "new", now=time.time() + store.STALE_BEAT + 1)
    s.finish_cas("j1", "old", "failed")          # 旧线程想收尾
    assert s.clear_finished() == [], "活任务不许被清掉"
    assert s.get("j1"), "任务记录还在"
