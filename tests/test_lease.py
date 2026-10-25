"""执行租约：唯一 token、收紧后的 claim_owner 语义、心跳、失租即停。

为什么要有这些：同一 job 以前用 f"{PROC_ID}:{job_id}" 做租约名，进程内固定，
重试/恢复重新领取时字符串一模一样，旧线程还能继续 beat；而 claim_owner 又允许
同 owner 再领、也允许 STALE_BEAT 超时就接管 —— 发起方还在跑时第二个执行者就能
进来，两边对同一目录各下一份、各删一次。
"""
import threading
import time

import pytest

from app import main as bot
from app import store, web


class _Policy:
    """retry.RetryPolicy 的最小替身：_upload_loop 会往 history 里记一次尝试。"""

    attempts = 0

    def __init__(self):
        self.history = []


def _fresh_store(tmp_path, monkeypatch):
    st = store.Store(str(tmp_path / "jobs.db"))
    monkeypatch.setattr(web, "STORE", st)
    monkeypatch.setattr(web, "JOBS", {})
    monkeypatch.setattr(web, "CANCEL", {})
    monkeypatch.setattr(web, "LEASES", {})
    monkeypatch.setattr(web, "_lease_live", {})
    return st


def _cloud_stub(monkeypatch):
    """把云添加的外部依赖换成假的：存储后端、落地、整理、记账。

    这些用例要验的是**租约**，不是云盘 —— 真实的云盘交互会引入网络与时序噪声。
    """
    from types import SimpleNamespace

    backend = SimpleNamespace(kind="clouddrive2", ensure_dir=lambda rel: rel,
                              list_dirs=lambda rel: [], list_files=lambda rel: [],
                              move_file=lambda *a: None, purge=lambda rel: "已删除",
                              link=lambda rel: "", offline_add=lambda *a: None,
                              real_folder=lambda rel: rel.rsplit("/", 1)[-1])
    monkeypatch.setattr(web.storage, "get", lambda: backend)
    monkeypatch.setattr(web, "cloud_ready", lambda: (True, ""))
    monkeypatch.setattr(web, "get_setting", lambda key, d=None: {
        "CLOUD_INGEST": True, "CLOUD_INBOX": "inbox", "CLOUD_DEST": "dest",
        "CLOUD_POLL": 0.01, "CLOUD_LAND_TIMEOUT": 1, "CLOUD_LAND_STABLE": 1,
    }.get(key, d))

    class _Landing:
        folder = "SEED-1"
        total = 4
        files = [{"name": "v.mp4", "size": 4}]

    async def fake_wait(*a, **kw):
        return [_Landing()]

    monkeypatch.setattr(web.cloudadd, "wait_landing", fake_wait)
    monkeypatch.setattr(web.cloudorganize, "plan",
                        lambda *a, **k: SimpleNamespace(source="SEED-1", target="SSIS-123",
                                                        move=["v.mp4"], drop=[], size=4))
    monkeypatch.setattr(web.cloudorganize, "apply",
                        lambda *a, **k: {"target": "SSIS-123", "files": ["SSIS-123.mp4"],
                                         "size": 4})
    return backend


# ---------------------------------------------------------------- token 唯一化

def test_lease_token_is_unique_per_execution(monkeypatch):
    """同一 job 连续两次真正开始执行，必须拿到不同的 token。

    以前两次都是 "web-<pid>-<ts>:<job_id>"：旧线程释放时会把新线程的租约一起让出去。
    """
    monkeypatch.setattr(web, "LEASES", {})
    monkeypatch.setattr(web, "_lease_live", {})
    # 只验 token 生成，不去碰库：lease_begin 里那次 beat 用假 store 顶掉
    class _NoStore:
        def beat(self, *a, **k):
            return True

        def release(self, *a, **k):
            return None

    monkeypatch.setattr(web, "_store", lambda: _NoStore())
    first = web.lease_begin("j1")
    second = web.lease_begin("j1")
    assert first.startswith(web.PROC_ID) and first.count(":") == 2
    assert first.rsplit(":", 1)[1] and len(first.rsplit(":", 1)[1]) == 12
    assert first != second, "同一 job 两次执行必须是不同 token"
    web.lease_end("j1")


def test_lease_owner_is_stable_within_one_execution(store_env):
    web_leases, _ = store_env
    a = web.lease_owner("j1")
    b = web.lease_owner("j1")
    assert a == b, "同一次执行内反复取名必须一致，否则 beat 打空"
    assert a in web_leases["LEASES"].values()


@pytest.fixture
def store_env(tmp_path, monkeypatch):
    st = _fresh_store(tmp_path, monkeypatch)

    class _Env(dict):
        pass

    env = _Env(LEASES=web.LEASES, STORE=st)
    st.start("j1", "SSIS-1", "magnet:a")
    yield env, st
    web.LEASES.clear()
    web._lease_live.clear()


# ---------------------------------------------------------------- claim_owner 收紧

def test_claim_owner_rejects_same_owner_reclaim(tmp_path):
    """同一 owner 重复领取返回 False：发起方还在跑，同 owner 再领等于放第二个执行者进来。"""
    st = store.Store(str(tmp_path / "j.db"))
    st.start("j1", "C-1", "magnet:a")
    assert st.claim_owner("j1", "own-a") is True
    assert st.claim_owner("j1", "own-a") is False, "同 owner 不能重复领取"
    assert st.claim_owner("j1", "own-b") is False, "心跳还新鲜时别人也领不到"
    assert st.get("j1")["exec_owner"] == "own-a", "失败的领取不能改归属"


def test_claim_owner_takes_over_only_after_stale_beat(tmp_path):
    """心跳停过 STALE_BEAT 才允许接管（进程已经不在的那种）。"""
    st = store.Store(str(tmp_path / "j.db"))
    st.start("j1", "C-1", "magnet:a")
    now = time.time()
    assert st.claim_owner("j1", "old", now=now) is True
    assert st.claim_owner("j1", "new", now=now + store.STALE_BEAT - 1) is False
    assert st.claim_owner("j1", "new", now=now + store.STALE_BEAT + 1) is True
    assert st.get("j1")["exec_owner"] == "new"


def test_beat_and_release_only_affect_own_token(tmp_path):
    """旧 token 既 beat 不动也释放不掉，新执行者的租约不会被顺手清掉。"""
    st = store.Store(str(tmp_path / "j.db"))
    st.start("j1", "C-1", "magnet:a")
    st.claim_owner("j1", "old")
    st.release("j1", "old")
    assert st.claim_owner("j1", "new") is True
    assert st.beat("j1", "old") is False, "旧 token 不能再续租"
    st.release("j1", "old")
    assert st.get("j1")["exec_owner"] == "new", "旧 token 释放不了新租约"
    assert st.beat("j1", "new") is True


# ---------------------------------------------------------------- 心跳

def test_heartbeat_refreshes_beat_and_stops_on_takeover(tmp_path, monkeypatch):
    """心跳线程周期性续租；一旦被接管就置失租标志，lease_alive 立刻为假。"""
    st = store.Store(str(tmp_path / "jobs.db"))
    monkeypatch.setattr(web, "STORE", st)
    monkeypatch.setattr(web, "LEASES", {})
    monkeypatch.setattr(web, "_lease_live", {})
    monkeypatch.setattr(web, "HEARTBEAT_INTERVAL", 0.05)
    st.start("j1", "SSIS-1", "magnet:a")
    token = web.lease_owner("j1")
    assert st.claim_owner("j1", token) is True

    owner = web.lease_begin("j1", token)
    assert owner == token, "领取与心跳必须用同一个 token"
    first_beat = st.get("j1")["exec_beat"]
    assert web.lease_alive("j1") is True
    time.sleep(0.18)
    assert st.get("j1")["exec_beat"] > first_beat, "心跳没续租"

    # 模拟别人接管（心跳已过期才会被接管，这里直接改库代表那个结果）
    st.claim_owner("j1", "other", now=time.time() + store.STALE_BEAT + 1)
    deadline = time.time() + 2.0
    while web._lease_live.get("j1", True) and time.time() < deadline:
        time.sleep(0.05)
    assert web._lease_live["j1"] is False, "心跳失败要置失租标志"
    assert web.lease_alive("j1") is False
    web.lease_end("j1")


def test_lease_begin_without_token_would_break_claim(tmp_path, monkeypatch):
    """回归：领取用的 token 不传给 lease_begin 时，首跳必然打空 —— 所以入口必须传。

    这个用例坐实「领取 token 与心跳 token 必须是同一个字符串」这条契约。
    """
    st = store.Store(str(tmp_path / "jobs.db"))
    monkeypatch.setattr(web, "STORE", st)
    monkeypatch.setattr(web, "LEASES", {})
    monkeypatch.setattr(web, "_lease_live", {})
    st.start("j1", "SSIS-1", "magnet:a")
    claimed = web.lease_owner("j1")
    st.claim_owner("j1", claimed)
    web.lease_begin("j1")                      # 故意不传：铸了新 token
    assert web.lease_alive("j1") is False, "新 token 不属于自己，必须判失租"
    assert st.get("j1")["exec_owner"] == claimed
    web.lease_end("j1")


# ---------------------------------------------------------------- 关键节点真正调用 require_lease

def test_require_lease_raises_when_lost(tmp_path, monkeypatch):
    st = store.Store(str(tmp_path / "jobs.db"))
    monkeypatch.setattr(web, "STORE", st)
    monkeypatch.setattr(web, "LEASES", {})
    monkeypatch.setattr(web, "_lease_live", {})
    st.start("j1", "SSIS-1", "magnet:a")
    token = web.lease_owner("j1")
    st.claim_owner("j1", token)
    web_lease = web.lease_begin("j1", token)
    assert web_lease == token
    web.require_lease("j1")                    # 持租时放行
    st.claim_owner("j1", "other", now=time.time() + store.STALE_BEAT + 1)
    with pytest.raises(web.LossOfLease) as err:
        web.require_lease("j1", "SSIS-1")
    assert "已由另一个执行者接手" in str(err.value)
    web.lease_end("j1")


def test_finish_refuses_to_archive_without_lease(tmp_path, monkeypatch):
    """_finish 写归档前也要验租：否则被别人接手的任务会把记录写重。"""
    from app import core

    st = _fresh_store(tmp_path, monkeypatch)
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(web.notify, "job_done", lambda *a, **k: None)
    core.save_state(web.STATE, {})
    st.start("j1", "SSIS-1", "magnet:a")
    token = web.lease_owner("j1")
    st.claim_owner("j1", token)
    st.claim_owner("j1", "other", now=time.time() + store.STALE_BEAT + 1)
    with pytest.raises(web.LossOfLease):
        web._finish_cloud("j1", "magnet:a", {"records": [{
            "folder": "SSIS-1", "path": "clouddrive2:dest/SSIS-1",
            "backend": "clouddrive2", "root": "dest", "rel": "dest/SSIS-1",
            "files": ["SSIS-1.mp4"], "files_detail": [], "size": 0,
            "inbox": "inbox/task"}]})
    assert core.load_state(web.STATE) == {}, "失租时不许写归档"


def test_cloud_organize_checks_lease_before_moving(tmp_path, monkeypatch):
    """整理（远端 move）前失租就不许动云盘。"""
    st = _fresh_store(tmp_path, monkeypatch)
    st.start("j1", "SSIS-1", "magnet:a")
    token = web.lease_owner("j1")
    st.claim_owner("j1", token)
    st.claim_owner("j1", "other", now=time.time() + store.STALE_BEAT + 1)
    moved = []
    monkeypatch.setattr(web.cloudorganize, "apply",
                        lambda *a, **k: moved.append(1) or {"target": "SSIS-1",
                                                           "files": [], "size": 0})
    with pytest.raises(web.LossOfLease):
        web._cloud_run("j1", "magnet:a", "SSIS-1", "SSIS1")
    assert moved == [], "失租后不能继续整理远端目录"


def test_archive_is_not_written_when_lease_lost(tmp_path, monkeypatch):
    """归档记账前失租：不许写归档，也不许写共享终态。

    接管者正持租在跑，旧线程写 done 等于替别人宣布完成 ——
    「清空已完成」还会顺手把它删掉。
    """
    st = _fresh_store(tmp_path, monkeypatch)
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(web, "METRICS", str(tmp_path / "m.json"))
    monkeypatch.setattr(web.meta, "lookup", _no_lookup)
    _cloud_stub(monkeypatch)

    def lose_before_finish(landed, backend, my_inbox, dest, code, **kw):
        st.claim_owner("j1", "other", now=time.time() + store.STALE_BEAT + 1)
        return ([], [], [])

    monkeypatch.setattr(web.cloudadd, "organize_landed", lose_before_finish)
    from app import core
    core.save_state(web.STATE, {})
    st.start("j1", "SSIS-123", "magnet:a")
    token = web.lease_owner("j1")
    st.claim_owner("j1", token)
    web.lease_begin("j1", token)

    web.run_web_job("j1", "magnet:a", "SSIS-123")

    job = st.get("j1")
    assert job["stage"] not in ("done", "failed", "cancelled"), \
        "失租只退出，不许写共享终态（接管者还在跑，写 cancelled 等于替别人取消）"
    assert core.load_state(web.STATE) == {}, "失租时不写归档"
    assert job["exec_owner"] == "other", "失租时不能把别人的租约释放掉"


def test_cleanup_runs_when_lease_held(tmp_path, monkeypatch):
    """对照组：同一流程在持租时正常清理，证明上一条的「没删」是租约挡的。"""
    st = _fresh_store(tmp_path, monkeypatch)
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(web, "METRICS", str(tmp_path / "m.json"))
    monkeypatch.setattr(web.meta, "lookup", _no_lookup)
    _cloud_stub(monkeypatch)
    cleaned = []
    monkeypatch.setattr(web.cloudadd, "clean_sources",
                        lambda backend, pending, **kw: cleaned.append(pending) or [])
    from app import core
    core.save_state(web.STATE, {})
    st.start("j1", "SSIS-123", "magnet:a")
    token = web.lease_owner("j1")
    st.claim_owner("j1", token)
    web.lease_begin("j1", token)

    web.run_web_job("j1", "magnet:a", "SSIS-123")

    assert st.get("j1")["stage"] == "done"
    assert cleaned, "持租成功后才清源目录"
    assert st.get("j1")["exec_owner"] == "", "终态要释放租约"




async def _no_lookup(code, path=None, client=None):
    return {}


# ---------------------------------------------------------------- bot 侧

def _bot_env(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "_STORE", store.Store(str(tmp_path / "jobs.db")))
    monkeypatch.setattr(bot, "LEASES", {})
    monkeypatch.setattr(bot, "_lease_live", {})

    class _Cfg:
        jobs_db = str(tmp_path / "jobs.db")

    return _Cfg()


def test_bot_lease_begin_claims_and_heartbeats(tmp_path, monkeypatch):
    """bot 侧：领取与心跳同一 token，第二次领取拿到新 token 且旧租约已释放。"""
    cfg = _bot_env(tmp_path, monkeypatch)
    monkeypatch.setattr(bot, "HEARTBEAT_INTERVAL", 0.05)
    st = bot.task_store(cfg)
    st.start("b1", "SSIS-1", "magnet:a")
    assert bot.lease_begin(cfg, "b1") is True
    first = st.get("b1")["exec_owner"]
    assert first == bot.LEASES["b1"], "领取与心跳必须是同一个 token"
    assert bot.lease_alive(cfg, "b1") is True
    bot.lease_end(cfg, "b1")
    assert st.get("b1")["exec_owner"] == "", "结束后要让出租约"
    assert bot.lease_begin(cfg, "b1") is True
    assert st.get("b1")["exec_owner"] != first, "再次执行必须换新 token"
    bot.lease_end(cfg, "b1")


def test_bot_lease_begin_fails_when_someone_else_holds(tmp_path, monkeypatch):
    cfg = _bot_env(tmp_path, monkeypatch)
    st = bot.task_store(cfg)
    st.start("b1", "SSIS-1", "magnet:a")
    assert st.claim_owner("b1", "other") is True
    assert bot.lease_begin(cfg, "b1") is False, "别人持租时不能再领"
    assert "b1" not in bot.LEASES, "领取失败不能留下半截 token"


def test_bot_require_lease_stops_after_takeover(tmp_path, monkeypatch):
    cfg = _bot_env(tmp_path, monkeypatch)
    st = bot.task_store(cfg)
    st.start("b1", "SSIS-1", "magnet:a")
    assert bot.lease_begin(cfg, "b1") is True
    bot.require_lease(cfg, "b1")               # 持租时放行
    st.claim_owner("b1", "other", now=time.time() + store.STALE_BEAT + 1)
    with pytest.raises(RuntimeError) as err:
        bot.require_lease(cfg, "b1")
    assert "已由另一个执行者接手" in str(err.value)
    bot.lease_end(cfg, "b1")
