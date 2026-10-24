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

log = logging.getLogger("x-drive.cloudingest")

DEFAULT_POLL = 10.0
DEFAULT_TIMEOUT = 7200.0
DEFAULT_STABLE = 3
# 空目录也要给足稳定次数：种子刚解析完时目录先建出来、文件随后才写
MIN_STABLE = 2


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


def measure(backend, inbox: str) -> dict[str, dict]:
    """inbox 里**目录**的体量（往里数一层）。

    只认目录，不认 inbox 根下散落的裸文件 —— 这条是踩出来的：

      · 迅雷落地一定是「建一个种子同名目录」再往里写（实测）；
      · 而落地目录里本来就可能躺着用户自己的文件（实测 inbox 里有
        `MIMK-212ch.mp4`、几个 zip/iso）。把它们当「新落地」会拿一个**普通文件**
        去当目录名整理，报「是文件，不是目录」；
      · 根层读文件还可能超时，那种情况下 before 快照只有目录，
        于是所有既存文件都变成「新增」—— 只认目录就天然绕开了这个坑。
    """
    out: dict[str, dict] = {}
    try:
        names = backend.list_dirs(inbox)
    except Exception as exc:                     # noqa: BLE001 读不到就当没有
        log.info("列 %s 目录失败（当作空）：%s", inbox, exc)
        return out
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
                       progress=None, cancel_check=None) -> list[Landing]:
    """等 inbox 里出现新条目并稳定下来，返回所有已稳定的新条目。

    批量提交多个种子时会一次来好几个目录，全部收下；只要「连续 stable 轮没有
    新名字出现」且已有稳定的条目，就返回。一个都没来且超时 → LandingTimeout。
    """
    need = max(MIN_STABLE, int(stable or DEFAULT_STABLE))
    loop = asyncio.get_event_loop()
    deadline = loop.time() + float(timeout or DEFAULT_TIMEOUT)
    settled: dict[str, Landing] = {}
    streak: dict[str, tuple[tuple, int]] = {}
    known: set[str] = set()
    quiet = 0                                    # 连续几轮没出现新名字
    rounds = 0
    while True:
        rounds += 1
        if cancel_check and cancel_check():
            raise LandingTimeout("已停止")
        items = measure(backend, inbox)
        fresh = newcomers(before, items)
        if set(fresh) - known:
            known |= set(fresh)
            quiet = 0
        else:
            quiet += 1
        for name in fresh:
            if name in settled:
                continue
            item = items.get(name) or {"files": [], "count": 0, "bytes": 0}
            fp = _fingerprint(item)
            last, hits = streak.get(name, (None, 0))
            hits = hits + 1 if fp == last else 1
            streak[name] = (fp, hits)
            if hits >= need:
                files = [dict(f) for f in item.get("files") or []]
                settled[name] = Landing(folder=name, files=files,
                                        total=int(item.get("bytes") or 0), rounds=rounds)
                log.info("云添加已落地：%s（%d 个文件，%d 字节，第 %d 轮稳定）",
                         name, len(files), item.get("bytes"), rounds)
        if progress:
            try:
                progress({name: {"count": items[name]["count"], "bytes": items[name]["bytes"]}
                          for name in fresh if name in items}, rounds)
            except Exception:                   # noqa: BLE001 进度只是展示
                pass
        # 已有稳定条目，且连续 need 轮没有新名字 → 这一批就这些了
        if settled and quiet >= need:
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


def submit(backend, url: str, inbox: str) -> dict:
    """提交前的准备 + 提交。返回 {before, inbox}，供 wait_landing 用。"""
    before = snapshot(backend, inbox)
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
