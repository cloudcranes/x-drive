"""任务持久化（标准库 sqlite3）。

为什么不用原来的 JOBS 字典 + JSON 待办：网页任务一重启就丢，bot 的待办是
一个任务一个文件，两边对同一番号能同时开两份。这里用一张表收口：

  · job_id 唯一；code 在「未结束」状态下唯一（部分唯一索引），并发提交撞不进来
  · 状态落盘，重启后能对账迅雷、继续推进，而不是重下一个
  · 只存路径与阶段，不存凭据

state 取值：queued / downloading / organizing / uploading / done / failed
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

log = logging.getLogger("x-drive.store")

# 占着番号的状态：延期也算（它还要跑，不能放过第二个同番号任务进来）
ACTIVE = ("queued", "downloading", "organizing", "uploading", "cloud", "deferred")
# 正在占着下载槽的状态：延期在等额度，不能算进去；cloud 全程在云盘，
# 不占本地下载并发，但**占番号去重**（曾经漏掉它：云任务挂在这时同番号能重复提交）。
RUNNING = ("queued", "downloading", "organizing", "uploading", "cloud")
# 已结束：清空/删除只认这几个。
FINISHED = ("done", "failed", "cancelled")

# 恢复后「待对账」标记：主标记在 state 表里（跟着库走，备份/覆盖都能带上）；
# 但库本身可能就是刚被覆盖、写不进去的那个，所以另存一份旁路文件。
# 放在任务库同目录（由库路径推导，不新增配置），任何进程只要拿到库路径就能读。
RECONCILE_KEY = "reconcile_required"
RECONCILE_FILE = "reconcile_required.json"


def reconcile_mark_path(db_path: str) -> str:
    """旁路标记文件路径：与任务库同目录，随 JOBS_DB 走。"""
    return os.path.join(os.path.dirname(os.path.abspath(db_path)) or ".", RECONCILE_FILE)


def write_reconcile_mark(mark: dict, db_path: str) -> str:
    """写旁路标记文件，返回路径。**写失败会抛异常**：调用方要据此判定库不可信。

    先写临时文件再 os.replace：另一个进程（bot）随时在轮询这个文件，
    半截 JSON 会被它当成「没有标记」而放行，那就等于没拦。
    """
    target = reconcile_mark_path(db_path)
    data = json.dumps(dict(mark or {}, required=True), ensure_ascii=False)
    tmp = f"{target}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, target)
    return target


def clear_reconcile_mark(db_path: str) -> None:
    """删掉旁路标记文件。文件本来就没有算成功；其他错误照实抛出。"""
    try:
        os.remove(reconcile_mark_path(db_path))
    except FileNotFoundError:
        return


def read_reconcile_mark(db_path: str) -> dict:
    """读「待对账」标记：库内标记与旁路文件任一为真即算待对账，返回合并后的信息。

    **绝不抛异常**：调用方是调度闸门，这里抛异常会把「库不可信」误判成「可以开工」。
    库本身打不开/读不出来时按**待对账**处理 —— 读不出状态的库不能开工。
    """
    out: dict = {"required": False, "db": False, "file": False,
                 "db_path": os.path.abspath(db_path), "errored": []}
    try:
        data = Store(db_path).state_get(RECONCILE_KEY)
        if data.get("required"):
            out.update({k: v for k, v in data.items() if k != "file"})
            out["required"] = True
            out["db"] = True
    except Exception as exc:            # 库覆盖到一半 / 被锁死：读不出=不可信
        out["required"] = True
        out["errored"].append(f"库内标记读取失败：{str(exc)[:120]}")
    try:
        with open(reconcile_mark_path(db_path), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and data.get("required"):
            out.update({k: v for k, v in data.items() if k not in ("db", "file")})
            out["required"] = True
            out["file"] = True
    except FileNotFoundError:
        pass
    except Exception as exc:            # 文件坏了同样是「有人要求停」，不能当成没标记
        out["required"] = True
        out["file"] = True
        out["errored"].append(f"旁路标记读取失败：{str(exc)[:120]}")
    return out

# 迁移版本：存在 user_version 里。每次改表结构/索引都必须 +1 并补一段迁移，
# 因为 CREATE UNIQUE INDEX IF NOT EXISTS 对「已存在的同名旧索引」完全无效 ——
# 旧库升级后 uniquely 条件不会更新，表现就是「测试全绿、线上照旧重复下载」。
# v6：cloud 纳入活跃状态（唯一索引与查询口径都要重建）。
SCHEMA_VERSION = 6

# 执行者心跳超过这么久没更新，就认为那个进程已经不在了，允许别人接手
STALE_BEAT = 120.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    code        TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',
    stage       TEXT NOT NULL DEFAULT 'queued',
    percent     REAL NOT NULL DEFAULT 0,
    speed       REAL NOT NULL DEFAULT 0,
    total       INTEGER NOT NULL DEFAULT 0,
    downloaded  INTEGER NOT NULL DEFAULT 0,
    folder      TEXT NOT NULL DEFAULT '',
    link        TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    retries     INTEGER NOT NULL DEFAULT 0,
    owner       TEXT NOT NULL DEFAULT '',
    xunlei_task TEXT NOT NULL DEFAULT '',
    xunlei_ours INTEGER NOT NULL DEFAULT 1,
    keep_dir    TEXT NOT NULL DEFAULT '',
    resume_from TEXT NOT NULL DEFAULT '',
    exec_owner  TEXT NOT NULL DEFAULT '',
    exec_beat   REAL NOT NULL DEFAULT 0,
    extra       TEXT NOT NULL DEFAULT '{}',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_stage ON jobs(stage);
-- 订阅与发现待办：整份 JSON 按命名空间存放。
-- 放这里是为了和任务表共用同一个库、同一个事务：游标推了而待办没写进去
-- （或反过来）就会漏订，两份文件分两次写根本保证不了原子性。
CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL DEFAULT 0
);
-- 任务事件：每次入队/领取/重试/延期/阶段切换/终态各记一条，前端时间线按它画。
-- 只追加不改写（对账与排查都要看原始顺序）；写入失败绝不影响任务本身。
CREATE TABLE IF NOT EXISTS job_events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    at     REAL NOT NULL,
    kind   TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS job_events_job ON job_events(job_id);
"""

FIELDS = ("code", "source", "stage", "percent", "speed", "total", "downloaded",
          "folder", "link", "error", "retries", "owner", "xunlei_task",
          "xunlei_ours", "keep_dir", "resume_from", "exec_owner", "exec_beat", "extra")

class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # 先建表（老库上是空操作），再按 user_version 迁移。
        # 判断「要不要补」必须看版本号，不能只看表结构：executescript 会先把新表
        # CREATE 出来，若拿结构当唯一依据，老库就会被判成「已经是最新的」，
        # 版本号永远停在旧值（线上表现：升级跑了，版本还是 4）。
        with self._conn() as conn:
            conn.executescript(SCHEMA)
        self.migrate()

    # ------------------------------------------------------------ 迁移
    def migrate(self) -> int:
        """把老库升到当前结构。返回迁移前的版本，便于日志与测试断言。

        整段在一个显式事务里：中途撞唯一索引就整体回滚，库仍是旧版本（下次启动
        还能再试），而不是「列加了一半、版本号已前移、索引没建」的半吊子状态。

        按版本**逐级**推进：v0/v1/v2/v3/v4 都要能一路升到 v5。版本号只是线索、
        不是事实 —— 线上出现过「user_version 写着 4、实际缺 state 表」的库
        （迁移半途失败或手工拷贝所致），所以每级迁移都先用 PRAGMA table_info
        核对实际结构，缺什么补什么，重复执行安全。
        """
        with self._migrate_conn() as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            steps = [(2, self._migrate_v2), (3, self._migrate_v3),
                     (4, self._migrate_v4), (5, self._migrate_v5),
                     (6, self._migrate_v6)]
            if version >= SCHEMA_VERSION and self._structure_ok(conn):
                self._ensure_index(conn)
                return version
            if version >= SCHEMA_VERSION:
                # 版本号说「已是最新」但结构缺件（迁移半途失败 / 手工拷贝 / 从备份
                # 覆盖）：再逐级跑一遍，每步自我校验、幂等。
                log.warning("任务表版本号已是 v%d 但结构缺件，按实际结构补齐", version)
            # 版本号只决定「要不要跑」，不决定「跑到哪」：升到当前版本就把
            # v2→v5 全部走一遍。每步都先查结构再动手，已在的列/表不会重复建。
            log.info("任务表升级：v%d → v%d", version, SCHEMA_VERSION)
            conn.execute("BEGIN IMMEDIATE")
            try:
                for _need, fn in steps:
                    fn(conn)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                conn.commit()
            except Exception:
                conn.rollback()
                log.exception("任务表升级失败，已回滚到 v%d", version)
                raise
        return version

    @contextmanager
    def _migrate_conn(self):
        """迁移专用连接：**不在退出时**再 commit 一次。

        普通 _conn() 在退出时无条件 commit —— 迁移回滚后那次 commit 又把
        「列改了一半」的半截状态提交了，回滚等于白做。这里只在成功路径上
        commit（由 migrate 自己调），异常路径保持回滚后的样子。
        """
        conn = sqlite3.connect(self.path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=15000")
            yield conn
        finally:
            conn.close()

    def _structure_ok(self, conn) -> bool:
        """版本号说「已是最新」时，再按实际结构核对一遍。

        只信 user_version 会漏掉「写着 4 但缺 state 表 / 缺 exec_* 列」的库：
        迁移半途失败、手工拷贝、从备份覆盖都可能造出这种库。
        """
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        return ({"resume_from", "exec_owner", "exec_beat"} <= cols
                and {"state", "job_events"} <= tables)

    def _migrate_v2(self, conn) -> None:
        """v2：补 resume_from 列、唯一索引覆盖 deferred、旧延期记录改判为 deferred。"""
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if "resume_from" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN resume_from TEXT NOT NULL DEFAULT ''")
        marks = ",".join("?" * len(ACTIVE))
        dupes = conn.execute(
            f"SELECT code, COUNT(*) n FROM jobs WHERE code <> '' AND stage IN ({marks})"
            " GROUP BY code HAVING n > 1", ACTIVE).fetchall()
        for row in dupes:
            # 同番号多条活跃记录：保留最新一条，其余转 failed 留痕（不静默删数据）
            keep = conn.execute(
                f"SELECT id FROM jobs WHERE code=? AND stage IN ({marks})"
                " ORDER BY updated_at DESC, created_at DESC LIMIT 1",
                (row["code"], *ACTIVE)).fetchone()["id"]
            conn.execute(
                f"UPDATE jobs SET stage='failed', error=?, updated_at=? WHERE code=?"
                f" AND stage IN ({marks}) AND id <> ?",
                (f"迁移时发现同番号重复活跃任务，保留 {keep}，本条转为失败留痕",
                 time.time(), row["code"], *ACTIVE, keep))
        self._ensure_index(conn, force=True)
        fixed = self._legacy_defers(conn)
        if fixed:
            log.info("迁移：%d 条旧延期记录（failed + defer_until）恢复为 deferred", fixed)

    def _migrate_v3(self, conn) -> None:
        """v3：补执行者列，让 web 与 bot 对同一任务只能有一个执行者。"""
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if "exec_owner" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN exec_owner TEXT NOT NULL DEFAULT ''")
        if "exec_beat" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN exec_beat REAL NOT NULL DEFAULT 0")

    def _migrate_v4(self, conn) -> None:
        """v4：建 state 表（订阅与待办）。SCHEMA 里已 IF NOT EXISTS，这里只需确认存在。"""
        conn.execute(
            "CREATE TABLE IF NOT EXISTS state ("
            " key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '{}',"
            " updated_at REAL NOT NULL DEFAULT 0)")

    def _migrate_v5(self, conn) -> None:
        """v5：建 job_events 表与索引（任务时间线的数据源）。

        被误标成 v5 但缺表的库同样走这里：CREATE ... IF NOT EXISTS 幂等。
        """
        conn.execute(
            "CREATE TABLE IF NOT EXISTS job_events ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " job_id TEXT NOT NULL,"
            " at REAL NOT NULL,"
            " kind TEXT NOT NULL,"
            " detail TEXT NOT NULL DEFAULT '')")
        conn.execute("CREATE INDEX IF NOT EXISTS job_events_job ON job_events(job_id)")

    def _migrate_v6(self, conn) -> None:
        """v6：cloud 纳入活跃状态 —— 重建唯一索引。

        索引条件里的状态列表是内联常量，旧索引不会自己更新，必须 DROP 再建
        （这就是 v2 踩过的同一个坑：IF NOT EXISTS 对旧同名索引无效）。
        """
        self._ensure_index(conn, force=True)

    def _ensure_index(self, conn, force: bool = False) -> None:
        """建/重建活跃番号唯一索引。

        bot 与 web 是两个进程，可能同时初始化同一个库：谁先建好，后到的
        CREATE INDEX 就会报 already exists。这里把它当成正常情况处理 ——
        并发启动不该让程序起不来。
        """
        if force:
            conn.execute("DROP INDEX IF EXISTS jobs_active_code")
        # 部分索引的 WHERE 里不能有绑定参数，只能内联常量
        stages = ",".join(f"'{s}'" for s in ACTIVE)
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS jobs_active_code ON jobs(code)"
                f" WHERE code <> '' AND stage IN ({stages})")
        except sqlite3.OperationalError as exc:
            log.warning("建索引 jobs_active_code 时出错（另一进程可能已在建）：%s", exc)

    def _legacy_defers(self, conn) -> int:
        """把「旧语义的延期」（stage=failed + extra.defer_until）改判为 deferred。"""
        rows = conn.execute(
            "SELECT id, code, extra FROM jobs WHERE stage='failed' AND extra LIKE '%defer_until%'"
        ).fetchall()
        fixed = 0
        for row in rows:
            try:
                extra = json.loads(row["extra"] or "{}")
            except json.JSONDecodeError:
                continue
            until = float(extra.get("defer_until") or 0)
            if not until:
                continue
            # 冲突要看**全部**活跃状态，不能只看 deferred：同番号可能正卡在
            # queued/downloading，改判成 deferred 照样撞唯一索引，整段迁移回滚。
            if row["code"] and self._busy(conn, row["code"], row["id"]):
                continue                     # 已有活跃记录，旧记录保持 failed 留痕
            conn.execute("UPDATE jobs SET stage='deferred', updated_at=? WHERE id=?",
                         (time.time(), row["id"]))
            fixed += 1
        return fixed

    def _busy(self, conn, code: str, exclude_id: str) -> bool:
        marks = ",".join("?" * len(ACTIVE))
        row = conn.execute(
            f"SELECT 1 FROM jobs WHERE code=? AND stage IN ({marks}) AND id<>? LIMIT 1",
            (code, *ACTIVE, exclude_id)).fetchone()
        return row is not None

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=15000")
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------ 写
    def start(self, job_id: str, code: str, source: str, owner: str = "") -> bool:
        """登记新任务。同番号已有未结束任务时返回 False（并发去重）。"""
        now = time.time()
        with self._lock, self._conn() as conn:
            try:
                conn.execute(
                    "INSERT INTO jobs (id, code, source, stage, owner, created_at, updated_at)"
                    " VALUES (?,?,?,'queued',?,?,?)",
                    (job_id, code, source, owner, now, now))
                # 入队事件与登记同一次提交：任务存在就一定有第一条时间线节点。
                # 事件写失败只回滚事件（用 SAVEPOINT），不影响任务登记。
                try:
                    conn.execute("SAVEPOINT ev")
                    conn.execute(
                        "INSERT INTO job_events (job_id, at, kind, detail) VALUES (?,?,?,?)",
                        (job_id, now, "queued",
                         " ".join(x for x in (owner and f"发起方 {owner}", code) if x)))
                    conn.execute("RELEASE ev")
                except Exception as exc:
                    log.warning("记录任务 %s 的入队事件失败：%s", job_id, exc)
                    try:
                        conn.execute("ROLLBACK TO ev")
                        conn.execute("RELEASE ev")
                    except sqlite3.Error:
                        pass
                return True
            except sqlite3.IntegrityError:
                log.info("番号 %s 已存在未结束任务，跳过重复提交", code)
                return False

    def update(self, job_id: str, **fields) -> None:
        clean = {k: v for k, v in fields.items() if k in FIELDS}
        if not clean:
            return
        if "extra" in clean and not isinstance(clean["extra"], str):
            clean["extra"] = json.dumps(clean["extra"], ensure_ascii=False)
        sets = ", ".join(f"{k}=?" for k in clean)
        with self._lock, self._conn() as conn:
            conn.execute(f"UPDATE jobs SET {sets}, updated_at=? WHERE id=?",
                         (*clean.values(), time.time(), job_id))

    def update_cas(self, job_id: str, owner: str, **fields) -> bool:
        """按执行 token 的条件更新：**只有仍持租的执行者写得进去**。

        失租旧线程与用户取消共用一个异常的年代，旧线程退出前会把共享任务写成
        cancelled/failed —— 那时新执行者已接管并正在跑，任务却成了终态，
        「清空已完成」还会顺手把它删掉。现在终态与执行期写入都走这里：
        WHERE exec_owner=本次token，写不进去说明租约已不属于自己，返回 False，
        调用方安静退出即可（不改共享状态、不删外部任务）。
        """
        clean = {k: v for k, v in fields.items() if k in FIELDS}
        if not job_id:
            return False
        if clean:
            if "extra" in clean and not isinstance(clean["extra"], str):
                clean["extra"] = json.dumps(clean["extra"], ensure_ascii=False)
            sets = ", ".join(f"{k}=?" for k in clean)
        else:
            sets = "exec_beat=exec_beat"        # 只想验证归属时的空更新
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                f"UPDATE jobs SET {sets}, updated_at=? WHERE id=? AND exec_owner=?",
                (*clean.values(), time.time(), job_id, owner))
            return bool(cur.rowcount)

    def finish(self, job_id: str, stage: str, error: str = "") -> None:
        self.update(job_id, stage=stage, error=error[:300])

    def finish_cas(self, job_id: str, owner: str, stage: str, error: str = "") -> bool:
        """终态也要按 token 条件写：同 update_cas，见那里的注释。"""
        return self.update_cas(job_id, owner, stage=stage, error=error[:300])

    # ------------------------------------------------------------ 事件（时间线）
    # 事件种类：queued 入队 / claimed 领取 / retry 重试 / deferred 延期 /
    #           resumed 重启恢复 / stage 阶段切换 / done 完成 / failed 失败 /
    #           cancelled 取消。新增种类只追加，前端未知种类按原样显示。
    def event(self, job_id: str, kind: str, detail: str = "",
              owner: str | None = None) -> bool:
        """记一条任务事件。**永不抛异常**：时间线是旁路观测，不能影响任务本身。"""
        if not job_id or not kind:
            return False
        try:
            with self._lock, self._conn() as conn:
                values = (str(job_id), time.time(), str(kind), str(detail or "")[:500])
                if owner is None:
                    cur = conn.execute(
                        "INSERT INTO job_events (job_id, at, kind, detail) VALUES (?,?,?,?)", values)
                else:
                    cur = conn.execute(
                        "INSERT INTO job_events (job_id, at, kind, detail)"
                        " SELECT ?,?,?,? WHERE EXISTS"
                        " (SELECT 1 FROM jobs WHERE id=? AND exec_owner=?)",
                        (*values, job_id, owner))
            return bool(cur.rowcount)
        except Exception as exc:      # 库锁、表缺失、磁盘满……都只记日志
            log.warning("记录任务 %s 的事件 %s 失败：%s", job_id, kind, exc)
            return False

    def events(self, job_id: str, limit: int = 50) -> list[dict]:
        """按时间升序取最近的 limit 条事件（取「最近 N 条」再转成正序）。

        读失败按空表返回：老任务没有事件、库暂时不可读时，时间线退回旧渲染即可。
        """
        if not job_id:
            return []
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT at, kind, detail FROM job_events WHERE job_id=?"
                    " ORDER BY at DESC, id DESC LIMIT ?",
                    (job_id, max(1, int(limit)))).fetchall()
            return [{"at": float(r["at"]), "kind": r["kind"], "detail": r["detail"]}
                    for r in reversed(rows)]
        except Exception as exc:
            log.warning("读取任务 %s 的事件失败：%s", job_id, exc)
            return []

    # ------------------------------------------------------------ 读
    def get(self, job_id: str) -> dict:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._row(row) if row else {}

    def recent(self, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def unfinished(self) -> list[dict]:
        marks = ",".join("?" * len(RUNNING))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM jobs WHERE stage IN ({marks}) ORDER BY created_at",
                RUNNING).fetchall()
        return [self._row(r) for r in rows]

    def unfinished_any(self) -> list[dict]:
        """所有「还要跑」的任务：含 deferred（它没跑完，只是等额度）。

        恢复前判断「有没有活任务」要用这一份：只看 RUNNING 会把延期任务当成
        已结束，覆盖库之后就再也没人唤醒它们了。
        """
        marks = ",".join("?" * len(ACTIVE))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM jobs WHERE stage IN ({marks}) ORDER BY created_at",
                ACTIVE).fetchall()
        return [self._row(r) for r in rows]

    def active_codes(self) -> set[str]:
        marks = ",".join("?" * len(ACTIVE))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT code FROM jobs WHERE stage IN ({marks}) AND code <> ''",
                ACTIVE).fetchall()
        return {r["code"].upper() for r in rows if r["code"]}

    def defer(self, job_id: str, until: float, reason: str = "",
              owner: str | None = None) -> bool:
        """延期：进 deferred 等待，不占并发但**占番号去重**，到点再回队列。

        以前借用 failed 表达延期，而唯一索引只管活跃状态，于是延期期间
        同番号能再提交一份；旧任务到期转 queued 时撞唯一索引，整回复活中断。

        必须带上 until：没有唤醒时间的 deferred 永远不会被 due() 选中，
        表现就是「说好明天自动重试，结果永远不动」（bot 侧就这么丢过任务）。
        """
        until = float(until or 0)
        if not job_id or until <= 0:
            raise ValueError("延期必须给出唤醒时间")
        fields = dict(stage="deferred", error=(reason or "额度用尽，稍后重试")[:300],
                      extra={**self.get(job_id).get("extra", {}), "defer_until": until})
        if owner is not None:
            return self.update_cas(job_id, owner, **fields)
        self.update(job_id, **fields)
        return True

    def cancel_defer(self, job_id: str) -> bool:
        """取消延期：把唤醒时间抹掉并转终态，否则到期会被自动复活。"""
        job = self.get(job_id)
        if not job or job.get("stage") != "deferred":
            return False
        extra = {k: v for k, v in (job.get("extra") or {}).items() if k != "defer_until"}
        self.update(job_id, stage="cancelled", extra=extra,
                    error="已取消延期")
        self.event(job_id, "cancelled", "已取消延期")
        return True

    def due(self, now: float | None = None) -> list[dict]:
        """到点的延期任务。"""
        now = time.time() if now is None else now
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE stage='deferred' ORDER BY updated_at").fetchall()
        out = []
        for row in rows:
            job = self._row(row)
            until = float(job["extra"].get("defer_until") or 0)
            if until and until <= now:
                out.append(job)
        return out

    def claim_due(self, job_id: str, now: float | None = None) -> bool:
        """原子领取一条到期延期任务：deferred → queued 只有一个调用者能成功。"""
        now = time.time() if now is None else now
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT extra FROM jobs WHERE id=? AND stage='deferred'",
                               (job_id,)).fetchone()
            if not row:
                return False
            try:
                extra = json.loads(row["extra"] or "{}")
            except json.JSONDecodeError:
                extra = {}
            until = float(extra.get("defer_until") or 0)
            if not until or until > now:
                return False
            extra.pop("defer_until", None)
            cur = conn.execute(
                "UPDATE jobs SET stage='queued', error='', extra=?, updated_at=?"
                " WHERE id=? AND stage='deferred'", (json.dumps(extra), now, job_id))
            return bool(cur.rowcount)

    # ------------------------------------------------------------ 跨进程取消
    #
    # 取消请求必须落库：web 与 bot 是两个进程，进程内的取消事件互相看不见。
    # Web 的停止按钮停不了 bot 正在跑的任务（曾表现为「任务不存在」/点了没反应）。
    # 执行者在阶段边界与进度回调里消费这个标记，消费到就走正常收尾。

    def request_cancel(self, job_id: str, now: float | None = None) -> bool:
        """记一个取消请求。只对未结束的任务有效；返回 False 表示无事可做。"""
        if not job_id:
            return False
        now = time.time() if now is None else now
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT stage FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["stage"] in FINISHED:
                return False
            conn.execute(
                "UPDATE jobs SET extra=json_set(extra, '$.cancel_requested', ?),"
                " updated_at=? WHERE id=?", (now, now, job_id))
            return True

    def cancel_marked(self, job_id: str) -> bool:
        """这条任务是否已被请求取消（跨进程可见）。"""
        if not job_id:
            return False
        with self._conn() as conn:
            row = conn.execute("SELECT extra FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return False
        try:
            extra = json.loads(row["extra"] or "{}")
        except json.JSONDecodeError:
            return False
        return bool(extra.get("cancel_requested"))

    def clear_cancel(self, job_id: str, owner: str | None = None) -> bool:
        """任务收尾时抹掉取消标记（否则重试会被自己上一次的取消卡住）。"""
        if not job_id:
            return False
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT extra FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return False
            try:
                extra = json.loads(row["extra"] or "{}")
            except json.JSONDecodeError:
                extra = {}
            if extra.pop("cancel_requested", None) is None:
                return False
            cur = conn.execute("UPDATE jobs SET extra=?, updated_at=? WHERE id=?"
                               + (" AND exec_owner=?" if owner is not None else ""),
                               (json.dumps(extra), time.time(), job_id)
                               + ((owner,) if owner is not None else ()))
            return bool(cur.rowcount)

    # ------------------------------------------------------------ 执行者领取
    def claim_owner(self, job_id: str, owner: str, now: float | None = None) -> bool:
        """抢占任务执行权：同一任务只能有一个执行者。

        web 与 bot 是两个进程，重启恢复、延期到期、手动重试三条路径都可能
        同时指向同一条记录。以前只靠内存里的线程状态判断，进程一重启就失效，
        两个执行者会对同一个目录各下一份。

        「同 owner 可以再领」那条分支必须去掉：发起方还在跑（心跳在跳），
        同 owner 再领等于放第二个执行者进来；而 beat 超时只说明心跳停了，
        不代表原执行者已退出，所以只有空租约或心跳过期才允许接管。
        """
        now = time.time() if now is None else now
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "UPDATE jobs SET exec_owner=?, exec_beat=?, updated_at=?"
                " WHERE id=? AND (exec_owner='' OR exec_beat < ?)",
                (owner, now, now, job_id, now - STALE_BEAT))
            return bool(cur.rowcount)

    def beat(self, job_id: str, owner: str, now: float | None = None) -> bool:
        """续租。返回 False 表示已被人接管，执行者应停止。"""
        now = time.time() if now is None else now
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "UPDATE jobs SET exec_beat=? WHERE id=? AND exec_owner=?",
                (now, job_id, owner))
            return bool(cur.rowcount)

    def release(self, job_id: str, owner: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET exec_owner='', exec_beat=0 WHERE id=? AND exec_owner=?",
                (job_id, owner))

    # ------------------------------------------------------------ 键值状态
    def state_get(self, key: str) -> dict:
        """读一份 JSON 状态（订阅表、发现待办）。缺失或损坏都返回空表。"""
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        if not row:
            return {}
        try:
            data = json.loads(row["value"] or "{}")
        except json.JSONDecodeError:
            log.warning("状态 %s 已损坏，按空表处理", key)
            return {}
        return data if isinstance(data, dict) else {}

    def state_put(self, updates: dict[str, dict], conn=None) -> None:
        """写一批状态。给 conn 就复用调用方的事务 —— 订阅游标与待办必须同一次提交。"""
        now = time.time()
        if conn is not None:
            for key, value in updates.items():
                conn.execute(
                    "INSERT INTO state (key, value, updated_at) VALUES (?,?,?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                    " updated_at=excluded.updated_at",
                    (key, json.dumps(value, ensure_ascii=False), now))
            return
        with self._lock, self._conn() as own:
            self.state_put(updates, conn=own)

    @contextmanager
    def transaction(self):
        """一个事务里同时改订阅与待办：中途失败两边都不落，不会出现半截状态。"""
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    # 已结束的状态：可以被「清空」删掉记录的那几种（统一引用模块级定义，
    # 阶段口径只此一份：cloud 一进 ACTIVE，终态与清空就都自动一致）
    FINISHED = ("done", "failed", "cancelled")

    def delete(self, job_id: str) -> bool:
        """删一条任务记录（连带它的事件）。**只删记录，不动文件。**

        只允许删已结束的任务：还在跑的任务删掉记录 = 让调度器失去对它的追踪，
        下载器里那条任务还在跑、本地目录也还在，之后谁也认不出来。
        """
        if not job_id:
            return False
        marks = ",".join("?" * len(self.FINISHED))
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT stage FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["stage"] not in self.FINISHED:
                return False
            conn.execute("DELETE FROM job_events WHERE job_id=?", (job_id,))
            cur = conn.execute(f"DELETE FROM jobs WHERE id=? AND stage IN ({marks})",
                               (job_id, *self.FINISHED))
            return bool(cur.rowcount)

    def clear_finished(self) -> list[str]:
        """清掉所有已结束的任务记录，返回被删的 id（前端要报「清了几条」）。"""
        marks = ",".join("?" * len(self.FINISHED))
        with self._lock, self._conn() as conn:
            rows = conn.execute(f"SELECT id FROM jobs WHERE stage IN ({marks})",
                                self.FINISHED).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                holes = ",".join("?" * len(ids))
                conn.execute(f"DELETE FROM job_events WHERE job_id IN ({holes})", ids)
                conn.execute(f"DELETE FROM jobs WHERE id IN ({holes})", ids)
            return ids

    def prune(self, days: float = 30.0) -> int:
        """删掉很久以前已结束的记录，别让表无限增长。"""
        cutoff = time.time() - max(1.0, days) * 86400
        with self._lock, self._conn() as conn:
            # 事件表跟着任务一起清：「本任务的旧记录」已经删掉，事件留着只会越攒越多
            conn.execute(
                "DELETE FROM job_events WHERE job_id IN ("
                " SELECT id FROM jobs WHERE stage IN ('done','failed','cancelled')"
                " AND updated_at < ?)", (cutoff,))
            cur = conn.execute(
                "DELETE FROM jobs WHERE stage IN ('done','failed','cancelled') AND updated_at < ?",
                (cutoff,))
            return cur.rowcount or 0

    @staticmethod
    def _row(row) -> dict:
        data = dict(row)
        try:
            data["extra"] = json.loads(data.get("extra") or "{}")
        except json.JSONDecodeError:
            data["extra"] = {}
        data["xunlei_ours"] = bool(data.get("xunlei_ours", 1))
        return data

    # ------------------------------------------------------------ 备份与恢复
    def backup(self, target: str) -> str:
        """一致快照备份。用 sqlite 的 backup API，不是拷文件（WAL 下拷文件会缺页）。"""
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        with self._lock, self._conn() as src, sqlite3.connect(target) as dst:
            src.backup(dst)
        return target

    def stats(self) -> dict:
        """备份前给用户看的概览：多少任务、多少订阅与待办。"""
        with self._conn() as conn:
            jobs = conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"]
            active = conn.execute(
                "SELECT COUNT(*) c FROM jobs WHERE stage IN ('queued','downloading',"
                "'organizing','uploading','deferred')").fetchone()["c"]
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            subs = conn.execute("SELECT COUNT(*) c FROM state WHERE key='subs'").fetchone()["c"]
            todo = conn.execute("SELECT COUNT(*) c FROM state WHERE key='backlog'").fetchone()["c"]
        return {"jobs": jobs, "active": active, "schema": version,
                "has_subs": bool(subs), "has_backlog": bool(todo)}
