"""云添加落地：提交磁力到云盘，然后等它「真的出现在目录里」。

为什么不能用返回值和任务状态（都是实测出来的）：
  · `AddOfflineFiles` 对**假磁力**也回 `success=True` —— 返回值完全不可信；
  · 这台 CD2（1.0.18）+ 迅雷云盘**不支持** `ListAllOfflineFiles` /
    `GetOfflineQuotaInfo` / `ListOfflineFilesByPath`，也没有任务记录
    （`GetDownloadFileList` 恒为 0）—— 没有进度、没有状态、不能暂停。

所以完成判定只能看云盘目录：**新条目出现 → 文件数与总字节连续 N 次不变**。
实测真实磁力 15 秒后出现目录、5 个文件、4.4GB，之后大小不再变化。

纪律：
  · 提交前先给 inbox 拍快照，之后只认「快照里没有的条目」；同名目录已存在时
    它就在快照里，不会被误当成这次的落地；
  · 稳定判据是「连续 N 次采样（文件数 + 总字节 + 名字集合）完全一致」，
    不能只看「目录存在」—— 迅雷是先建目录再往里写，早一秒读就是半截；
  · 超时就抛，并且明说「接口接受了但迅雷侧没落地」，因为返回值骗人。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .storage.base import NotFoundError, StorageError

log = logging.getLogger("x-drive.cloudingest")

DEFAULT_POLL = 10.0
DEFAULT_TIMEOUT = 7200.0
DEFAULT_STABLE = 3
# 空目录也要给足稳定次数：种子刚解析完时目录先建出来、文件随后才写
MIN_STABLE = 2

# 「文件直接躺在收件目录根下」这种落地的键（单文件种子实测如此）。
# 用一个不会跟真实目录名撞的固定键 —— 目录名不会以 "=" 开头。
LOOSE_KEY = "=loose"


class LandingTimeout(RuntimeError):
    """等不到落地：要么迅雷侧没接收，要么太慢。"""


@dataclass
class Landing:
    """一次落地的结果。folder 是 inbox 下的目录/文件名。"""

    folder: str
    files: list[dict] = field(default_factory=list)   # [{name, size}]
    total: int = 0
    rounds: int = 0

    @property
    def path(self) -> str:
        return self.folder

    def names(self) -> list[str]:
        return [str(f.get("name") or "") for f in self.files if f.get("name")]


# ─────────────────────────────────────────── 观察

def snapshot(backend, inbox: str) -> dict[str, int]:
    """inbox 这一层有什么：目录名 → -1，文件名 → 字节数。读不到就当空。"""
    out: dict[str, int] = {}
    try:
        for name in backend.list_dirs(inbox):
            out[str(name)] = -1                 # 目录只看「有没有」
    except Exception as exc:                    # noqa: BLE001 读不到不该阻断整条链路
        log.info("列 %s 目录失败（当作空）：%s", inbox, exc)
    try:
        for entry in backend.list_files(inbox):
            out[str(entry.name)] = int(entry.size or 0)
    except Exception as exc:                    # noqa: BLE001
        log.info("列 %s 文件失败（当作空）：%s", inbox, exc)
    return out


def snapshot_strict(backend, inbox: str) -> dict[str, int]:
    """提交前快照：**读不到就抛**，不许降级成空目录。

    曾经的降级在这里出过事：列目录失败 → before={} → 之后 inbox 里既有的目录
    全被当成「新落地」，被搬走、被清掉。提交前拍不到底片就不提交 ——
    宁可这次任务失败，也不能拿着别人的目录开工。

    但「目录**压根不存在**」不是读失败：任务专属收件目录
    `<inbox>/<键>` 是**提交时才会建**的，拍快照那一刻它就是没有的
    （MIMK-182 就是这么挂的：CD2 回 "not found under …"，被当成读失败，
    任务在提交前就失败了）。所以：
      · NotFoundError → 这个目录还没有，快照就是空的（真的空，不是降级）
      · 别的读失败     → 照旧抛，不许降级
    """
    out: dict[str, int] = {}
    try:
        for name in backend.list_dirs(inbox):   # 列目录失败：原样抛给调用方
            out[str(name)] = -1
    except NotFoundError:
        # 这个目录还没有建 —— 快照就是空的（等提交时建出来再往里落地）。
        # 但**只**在「目录不存在」时这么办；别的读失败照旧抛。
        return out
    try:
        loose_files = list(backend.list_files(inbox))
        # 空目录不占用 loose 键，否则后来落下的单文件永远不算新增。
        # 用文件数量而非字节数判空：历史零字节文件也属于既有内容，
        # 保守地排除整个 loose 分组，避免将历史文件混入本次归档。
        if loose_files:
            out[LOOSE_KEY] = sum(int(entry.size or 0) for entry in loose_files)
    except Exception as exc:                    # noqa: BLE001
        raise StorageError(f"列 {inbox} 文件失败：{exc}") from exc
    return out


def task_inbox(inbox: str, job_key: str) -> str:
    """任务专属收件目录：`<inbox>/<键>`。共享 inbox 曾经让两个任务互相认领
    对方的落地目录（各自把「新增目录」当自己的），这个键把观察范围圈死在一个
    任务自己的地盘里；键不含路径分隔符（workspace.key_of 已归一）。"""
    leaf = str(job_key or "").strip("/").replace("/", "-") or "task"
    return f"{inbox.rstrip('/')}/{leaf}"


def measure(backend, inbox: str) -> dict[str, dict]:
    """inbox 里的落地内容。

    两种落地形态都要认（2026-10-24 实测两种都出现过）：

      A. 迅雷建一个「种子同名目录」再往里写 —— 绝大多数多文件种子是这样，
         统计的是**子目录里的文件**（往里数一层）。
      B. 单文件种子直接躺在收件目录下（实测 MIMK-267：`MIMK-267ch.mp4`
         直接在 `我的资源/MIMK267/` 下，没有中间那层）—— 这时统计的是
         收件目录**根下的文件**。

    怎么区分「本次落地」和「用户自己放在这儿的文件」？靠 **inbox 是任务专属
    目录**这件事：`my_inbox = <inbox>/<任务键>`，观察范围圈死在一个任务
    自己的地盘里，别的任务和用户手动放的东西都进不来（共享 inbox 曾让两个
    任务互相认领对方的落地目录）。

    早先的「只认目录」是为了防另一件事：落地目录里躺着用户自己的文件时，
    拿一个**普通文件**当目录名去整理会报「是文件，不是目录」。现在收件目录
    是任务专属的，这个风险不存在了 —— 但仍要保证「只统计收件目录这一层」，
    不要再往深层数（深层是种子目录内部，归档时要整体搬）。

    根下的文件单独收在一个固定的键下（""），整理时作为「一个整体」处理。
    """
    out: dict[str, dict] = {}
    try:
        names = backend.list_dirs(inbox)
    except Exception as exc:                     # noqa: BLE001 读不到就当没有
        log.info("列 %s 目录失败（当作空）：%s", inbox, exc)
        names = []
    for name in names:
        name = str(name)
        if not name or name.startswith("."):
            continue
        inner = f"{inbox.rstrip('/')}/{name}"
        files: list[dict] = []
        try:
            for entry in backend.list_files(inner):
                files.append({"name": str(entry.name), "size": int(entry.size or 0)})
        except Exception:                        # noqa: BLE001 空目录/读不到都算空
            files = []
        out[name] = {"files": files, "count": len(files),
                     "bytes": sum(f["size"] for f in files)}
    # B 形态：文件直接躺在收件目录根下（单文件种子）
    try:
        loose: list[dict] = []
        for entry in backend.list_files(inbox):
            loose.append({"name": str(entry.name), "size": int(entry.size or 0)})
    except Exception:                            # noqa: BLE001
        loose = []
    if loose:
        out[LOOSE_KEY] = {"files": loose, "count": len(loose),
                          "bytes": sum(f["size"] for f in loose),
                          "loose": True}
    return out


def _fingerprint(item: dict) -> tuple:
    names = tuple(sorted(f["name"] for f in item.get("files") or []))
    return (int(item.get("count") or 0), int(item.get("bytes") or 0), names)


def newcomers(before: dict, now: dict) -> list[str]:
    """快照里没有、现在有的条目。"""
    return sorted(name for name in now if name not in before)


# ─────────────────────────────────────────── 等落地

async def wait_landing(backend, inbox: str, before: dict, *,
                       poll: float = DEFAULT_POLL, timeout: float = DEFAULT_TIMEOUT,
                       stable: int = DEFAULT_STABLE,
                       progress=None, cancel_check=None,
                       source: str = "") -> list[Landing]:
    """等 inbox 里出现新条目并稳定下来，返回所有已稳定的新条目。

    批量提交多个种子时会一次来好几个目录，全部收下；只要「连续 stable 轮没有
    新名字出现」且已有稳定的条目，就返回。一个都没来且超时 → LandingTimeout。

    「稳定」必须每轮重新验证：目录一旦进 settled 就不再看它，曾是老版本的
    缺陷 —— 迅雷中途卡一下再继续，第一次平稳读到的体量就被永久锁定，
    拿着旧清单去整理（少收文件），随后整目录清理还把后到的文件删掉。
    现在每轮对**所有**已知条目重新算指纹，变化即撤销稳定；全部已绑定条目
    都重新稳定 + 连续 stable 轮没有新名字，才放行。

    source 给了就顺便查迅雷那边的离线任务状态：迅雷解析失败时会回
    PHASE_TYPE_ERROR（"解析失败，请重试或更换链接"），但目录这边什么都不会
    有 —— 不查的话任务会干等两小时，用户只看到「卡在 cloud」。
    """
    need = max(MIN_STABLE, int(stable or DEFAULT_STABLE))
    loop = asyncio.get_event_loop()
    deadline = loop.time() + float(timeout or DEFAULT_TIMEOUT)
    settled: dict[str, Landing] = {}
    streak: dict[str, tuple[tuple, int]] = {}   # 条目 → (上一轮指纹, 连稳次数)
    known: set[str] = set()
    quiet = 0                                    # 连续几轮没出现新名字
    rounds = 0
    while True:
        rounds += 1
        if cancel_check and cancel_check():
            raise LandingTimeout("已停止")
        if source and rounds % 3 == 1:
            # 每三轮查一次：别把官方 API 打成筛子
            why = check_offline_task(source)
            if why:
                raise LandingTimeout(f"迅雷侧拒绝了这次下载：{why}")
        items = measure(backend, inbox)
        fresh = newcomers(before, items)
        if set(fresh) - known:
            known |= set(fresh)
            quiet = 0
        else:
            quiet += 1
        # 每轮对全部已知条目重算指纹；被观测到变化就把稳定撤销（重新计 0）
        for name in known:
            item = items.get(name) or {"files": [], "count": 0, "bytes": 0}
            fp = _fingerprint(item)
            last, hits = streak.get(name, (None, 0))
            hits = hits + 1 if fp == last else 1
            streak[name] = (fp, hits)
            if hits >= need:
                if name in settled and settled[name].total == int(item.get("bytes") or 0) \
                        and len(settled[name].files) == int(item.get("count") or 0):
                    continue                     # 没变，维持原判定
                files = [dict(f) for f in item.get("files") or []]
                settled[name] = Landing(folder=name, files=files,
                                        total=int(item.get("bytes") or 0), rounds=rounds)
                log.info("云添加已落地：%s（%d 个文件，%d 字节，第 %d 轮稳定）",
                         name, len(files), item.get("bytes"), rounds)
            elif name in settled:
                log.info("云添加 %s 体量又变了（第 %d 轮），撤销稳定重新等", name, rounds)
                settled.pop(name, None)
        if progress:
            try:
                progress({name: {"count": items[name]["count"], "bytes": items[name]["bytes"]}
                          for name in known if name in items}, rounds)
            except Exception:                   # noqa: BLE001 进度只是展示
                pass
        # 放行条件：所有已知条目都已（重新）稳定，且连续 need 轮没有新名字
        if settled and len(settled) == len(known) and quiet >= need:
            return sorted(settled.values(), key=lambda x: x.folder)
        if loop.time() > deadline:
            if settled:
                return sorted(settled.values(), key=lambda x: x.folder)
            raise LandingTimeout(
                f"等待落地超时（{int(timeout)}s）：接口接受了链接，但 {inbox} 里"
                f"始终没有出现新内容 —— 迅雷侧可能没接收（链接无效 / 无源 / 空间不足）")
        # 下限给得很小：真正的节奏由设置项 CLOUD_POLL（默认 10s）决定，
        # 这里只是防止 poll=0 把 CPU 打满。写死 1 秒会让测试跑不动。
        await asyncio.sleep(max(0.01, float(poll)))


def check_offline_task(url: str, *, client=None) -> str:
    """查迅雷那边这个磁力的离线任务状态；**失败时**返回原因，空串=还在跑。

    为什么需要（2026-10-24 实测）：迅雷云盘解析失败时会回
        phase = PHASE_TYPE_ERROR, message = "解析失败，请重试或更换链接"
    但目录那边什么都不会出现 —— 没有它，任务会**一直等下去**（默认 2 小时），
    用户只看到「卡在 cloud」，完全不知道迅雷早说不行了。
    （MIMK-267 就是这样：选的磁力 0 个做种，迅雷解析失败，任务干等。）

    查不到就当「还在跑」—— 不能因为查不到就把正常的任务判死。
    """
    if not url:
        return ""
    try:
        from . import xunleipan as xp
        info_hash = xp.info_hash_of(url)
        task = xp.offline_task_by_hash(info_hash, client=client)
    except Exception as exc:                    # noqa: BLE001 查不到不算失败
        log.info("查迅雷离线任务状态失败（忽略）：%s", exc)
        return ""
    if not task:
        return ""
    phase = str(task.get("phase") or "")
    if "ERROR" not in phase.upper():
        return ""
    reason = (task.get("message") or task.get("error_detail") or "").strip()
    return reason or "迅雷侧报任务失败"


def submit(backend, url: str, inbox: str) -> dict:
    """提交前的准备 + 提交。返回 {before, inbox}，供 wait_landing 用。

    快照**严格模式**：拍不到底片就抛 —— 拿空快照开工会把 inbox 里既有的
    目录全当成新落地（真实发生过）。
    """
    before = snapshot_strict(backend, inbox)
    try:
        backend.ensure_dir(inbox)
    except Exception as exc:                    # noqa: BLE001 目录已存在时后端自己会忽略
        log.info("确保 inbox %s 存在时出错（继续）：%s", inbox, exc)
    backend.offline_add(url, inbox)
    return {"before": before, "inbox": inbox}


def folder_key(url: str, landing: Landing) -> str:
    """落地目录的稳定键（写进任务 extra，便于排查同一条来源）。"""
    digest = hashlib.sha1((url or landing.folder).encode("utf-8")).hexdigest()[:12]
    return f"{digest}:{landing.folder}"


def no_local_files() -> list[Path]:
    """占位：云添加链路没有本地文件（让调用点语义清楚）。"""
    return []
