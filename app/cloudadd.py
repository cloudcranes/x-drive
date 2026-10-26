"""云添加（迅雷云盘离线下载）：磁力 → 云盘落地 → 远端整理 → 记账。

**唯一**的下载链路（2026-10-25 起取消本地下载）：文件全程在云盘里，
不落本地磁盘，所以没有下载目录、也没有工作区。

本模块是纯逻辑层，不碰 web/bot 的任务表与租约 —— 那些由调用方注入回调。
两个入口共用它：

  · 网页：app/web.py 的 _cloud_run（后台线程里跑）
  · Bot：app/main.py 的 run_task（事件循环里跑）

因为「等待落地」是长轮询，网页侧用 asyncio.run 包，Bot 侧直接 await ——
所以这里把等待部分做成协程，整理与记账部分做成同步函数。
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from pathlib import Path
from typing import Callable

from . import cloudingest, cloudorganize, storage

log = logging.getLogger("x-drive.cloudadd")

# 读设置项的回调。**由调用方注入**（网页传 web.get_setting，bot 传自己的
# 那份）：本模块不直接依赖 app.settings —— 网页与 bot 的设置存储方式不同，
# 但云添加要的几个键（CLOUD_* / STORAGE_*）两边同名同义。
Setting = Callable[[str], object]

# 回调签名：
#   progress(count_files, total_bytes, rounds) -> None   落地进度
#   check_cancel() -> bool                                轮询期间是否被要求停止
#   event(kind, detail) -> None                          记一条时间线
#   lease_check() -> None                                 确认租约还在自己手里
#   lookup(folder) -> dict                                查元数据（失败返回空）
Progress = Callable[[int, int, int], None]
CheckCancel = Callable[[], bool]
Event = Callable[[str, str], None]
LeaseCheck = Callable[[], None]


def ready(get: Setting) -> tuple[bool, str]:
    """云添加能不能用。返回 (可用, 说明)。**不抛异常**：界面要直接显示原因。"""
    if not get("CLOUD_INGEST"):
        return False, "未启用「云添加」（设置 → 存储）"
    inbox = str(get("CLOUD_INBOX") or "").strip()
    if not inbox:
        return False, "没配「离线落地目录」"
    try:
        backend = storage.get()
    except Exception as exc:
        return False, f"存储后端不可用：{exc}"
    if backend.kind != "clouddrive2":
        return False, f"云添加只支持 CloudDrive2（当前 {backend.kind}）"
    probe = getattr(backend, "offline_supported", None)
    if probe is None:
        return False, "当前后端没有离线下载能力"
    try:
        ok, detail = probe(inbox)
    except Exception as exc:
        return False, f"探测失败：{exc}"
    return ok, detail


def destinations(get: Setting) -> tuple[str, str]:
    """(收件目录, 归档根)。收件目录是迅雷离线下载落地的地方，归档根是整理后放哪。"""
    inbox = str(get("CLOUD_INBOX") or "").strip().strip("/")
    dest = str(get("CLOUD_DEST") or "").strip().strip("/") or storage.root()
    return inbox, dest


def task_inbox(inbox: str, key: str) -> str:
    """任务专属收件目录 `<inbox>/<键>`。

    共享 inbox 曾让并发任务互相认领对方的落地目录（各自把「新增目录」当自己
    的，然后搬走、清掉）。观察范围圈死在自己的子目录里，别人的落地、用户
    手动加的目录都进不来。
    """
    return cloudingest.task_inbox(inbox, key)


def submit(backend, source: str, my_inbox: str) -> dict:
    """提交磁力到云盘。返回 {before, inbox}，before 是提交前的目录快照。

    交给调用方去记（网页写任务 extra，Bot 也写）—— 续接时靠它避免重复提交。
    """
    return cloudingest.submit(backend, source, my_inbox)


async def wait_landing(backend, my_inbox: str, before: dict, source: str, *,
                       get: Setting,
                       progress: Progress | None = None,
                       check_cancel: CheckCancel | None = None,
                       lease_check: LeaseCheck | None = None) -> list:
    """等云盘里真的出现落地内容。返回已稳定的落地条目列表。

    source 给了就顺便查迅雷那边的离线任务状态：迅雷解析失败时会回
    PHASE_TYPE_ERROR（"解析失败，请重试或更换链接"），但目录这边什么都不会
    有 —— 不查的话任务会干等两小时。
    """
    def on_progress(snapshot: dict, rounds: int, **kw) -> None:
        if progress is None:
            return
        count = sum(int(v.get("count") or 0) for v in snapshot.values())
        total = sum(int(v.get("bytes") or 0) for v in snapshot.values())
        try:
            progress(count, total, rounds, **kw)
        except TypeError:
            progress(count, total, rounds)

    return await cloudingest.wait_landing(
        backend, my_inbox, before,
        poll=float(get("CLOUD_POLL") or 10),
        timeout=float(get("CLOUD_LAND_TIMEOUT") or 7200),
        stable=int(get("CLOUD_LAND_STABLE") or 3),
        progress=on_progress,
        cancel_check=check_cancel,
        source=source)


def organize_landed(landed, backend, my_inbox: str, dest: str, code: str,
                    *, lease_check: LeaseCheck | None = None,
                    event: Event | None = None) -> tuple[list[dict], list[dict]]:
    """把落地的内容远端整理进归档根。返回 (归档记录列表, 待清理列表)。

    只做远端操作：move / rename / purge，**不删源目录** —— 清理是独立一步，
    由调用方在归档记录落盘之后执行（清理失败不影响归档成功）。
    """
    records: list[dict] = []
    pending_cleanup: list[dict] = []
    failed: list[dict] = []
    for item in landed:
        if lease_check is not None:
            lease_check()
        if event is not None:
            event("stage", f"landed {item.folder}（{item.total} 字节）")
        # 「文件直接躺在收件目录根下」的落地（单文件种子实测如此）：
        # 源目录就是收件目录本身，不是它下面的某个子目录。
        loose = item.folder == cloudingest.LOOSE_KEY
        source = "" if loose else item.folder
        # **逐条独立**：一个条目失败不许牵连其它条目。
        #
        # 以前是「全部整理完才返回」：一次落地两处（种子目录 + 根下散文件）
        # 时两处共用同一个 target，第二处撞「目标已存在」抛异常 —— 第一处
        # **已经搬进归档目录了**，但 records 一个都没返回 → 调用方不写归档
        # 记录 → 云盘里那个目录成了没人认领的**孤儿**（账实不符）。
        #
        # 现在每个条目单独 try：成功的照常记账，失败的单独记下来。
        try:
            plan = cloudorganize.plan(source, item.files, folder_code=code or "")
            moved = cloudorganize.apply(backend, my_inbox, plan, code or plan.target,
                                        dst_root=dest)
            # 重复下载：目标里本来就有（同名同大小），源文件是冗余 → 归入 drop
            if moved.get("repeated"):
                plan = dataclasses.replace(
                    plan, drop=list(plan.drop) + list(moved["repeated"]))
        except Exception as exc:            # noqa: BLE001 一个条目失败不影响其它
            log.warning("整理落地条目 %s 失败：%s", item.folder, exc)
            failed.append({"source": item.folder, "error": str(exc)[:200]})
            continue
        records.append({
            "folder": moved["target"],
            "path": f"{backend.kind}:{dest}/{moved['target']}",
            "backend": backend.kind,
            "root": dest,
            "rel": f"{dest}/{moved['target']}",
            "files": moved["files"],
            "files_detail": [{"name": n, "size": next(
                (int(f.get("size") or 0) for f in item.files if f.get("name") == old), 0)}
                for old, n in zip(plan.move, moved["files"])],
            "size": moved["size"],
            "inbox": my_inbox if loose else f"{my_inbox}/{item.folder}",
        })
        pending_cleanup.append({"plan": plan, "src_root": my_inbox, "dst_root": dest})
    return records, pending_cleanup, failed


def clean_sources(backend, pending_cleanup, *, lease_check: LeaseCheck | None = None,
                  event: Event | None = None) -> list[dict]:
    """清理源目录里已确认的丢弃文件。返回**没能清掉**的条目（要挂 cleanup_pending）。

    只逐项删除计划内的文件；源目录里有未知内容时保留现场并报告 ——
    嵌套的媒体从未进入归档清单，整目录 purge 会把它连带删掉。

    清理成功的源目录会变成空壳，顺手**非递归**删掉（见 remove_empty_shells）：
    源目录壳留着不清，收件目录会越积越多（实测每次云添加留一个）。

    lease_check 抛出的异常（失租/取消）**不吞**：那种情况要整个任务安静退出，
    不能当成「清理失败」继续往下走。
    """
    leftover: list[dict] = []
    shells: list[str] = []
    for item in pending_cleanup:
        plan, src_root, dst_root = item["plan"], item["src_root"], item["dst_root"]
        if lease_check is not None:
            lease_check()          # 失租/取消在这里抛出，不许被当成清理失败吞掉
        try:
            receipt = cloudorganize.cleanup_source(backend, src_root, plan, dst_root)
            if event is not None:
                event("stage", f"cleaned {src_root}/{plan.source}（{receipt}）")
            shell = cloudorganize.source_shell_path(src_root, plan)
            if shell not in shells:
                shells.append(shell)
            # 任务专属收件目录空了也顺便安全清理，避免收件目录留下一地空文件夹
            if src_root and src_root not in shells:
                shells.append(src_root)
        except Exception as exc:        # noqa: BLE001 清理失败只挂账
            log.warning("清理源目录 %s/%s 失败：%s", src_root, plan.source, exc)
            leftover.append({"source": plan.source, "src_root": src_root,
                             "dst_root": dst_root,
                             "target": plan.target, "error": str(exc)[:200]})
    # 空壳收尾：只删递归读过、确认是空的，且用非递归删除
    leftover.extend(remove_empty_shells(backend, shells,
                                        lease_check=lease_check, event=event))
    return leftover


def _is_empty_dir(backend, path: str) -> bool:
    """这个目录是不是**递归**空的。

    两层都要看：目录里有文件，或有子目录（子目录里还可能有东西），
    都不算空 —— 那种情况绝不能删。
    """
    try:
        if backend.list_files(path):
            return False
        for name in backend.list_dirs(path):
            if not _is_empty_dir(backend, f"{path.rstrip('/')}/{name}"):
                return False
    except Exception as exc:            # noqa: BLE001 读不出来 = 不确定 = 不算空
        log.info("检查 %s 是否为空失败（按非空处理）：%s", path, exc)
        return False
    return True


def remove_empty_shells(backend, paths: list[str], *,
                       lease_check: LeaseCheck | None = None,
                       event: Event | None = None) -> list[dict]:
    """删掉**确实空了**的源目录壳。返回没能删掉的（要挂 cleanup_pending）。

    只删「递归读过、确认是空」的目录，而且用**非递归**删除
    （backend.remove_dir）—— 整目录 purge 是递归的，删除前若有别的任务刚好
    往里落了文件，那份数据会跟着消失。

    目录不空、读不出来、或后端不支持非递归删除 → 一律保留，只记录原因。
    「壳必须删掉」不是成功条件：留着总比误删强。
    """
    leftover: list[dict] = []
    for path in paths:
        if not path:
            continue
        if lease_check is not None:
            lease_check()
        try:
            if not _is_empty_dir(backend, path):
                leftover.append({"source": path, "src_root": path,
                                "dst_root": "", "target": "",
                                "error": "目录里还有内容，保留"})
                continue
            remove = getattr(backend, "remove_dir", None)
            if remove is None:
                leftover.append({"source": path, "src_root": path,
                                "dst_root": "", "target": "",
                                "error": "后端不支持非递归删除，保留目录壳"})
                continue
            receipt = remove(path)
            if event is not None:
                event("stage", f"removed {path}（{receipt}）")
        except Exception as exc:        # noqa: BLE001 删不掉只挂账
            log.warning("删除空目录 %s 失败：%s", path, exc)
            leftover.append({"source": path, "src_root": path,
                            "dst_root": "", "target": "",
                            "error": str(exc)[:200]})
    return leftover


def build_record(rec: dict, scraped: dict | None = None, *,
                 source: str = "", infohash: str = "") -> dict:
    """按 metadata.archive_record 的契约组装一条归档记录。

    files 来自远端清单、path 是云盘路径。刮削附件这一步跳过：附件要写进
    云盘目录，云添加链路先不做（单独可回滚）。

    scraped 由调用方查好后传进来 —— 网页侧是 asyncio.run(meta.lookup(...))，
    Bot 侧直接在事件循环里 await，两个入口的异步上下文不一样。

    **source 与 infohash 必须一起落进记录**：
    · source = 磁力链接。丢了它，`unclassified-*` 的自愈（靠 source 认番号）
      与界面的「来源」显示都失灵；网页与 Bot 的记录字段也会不一致
      （Bot 侧以前单独补了 source，网页侧没补）。
    · infohash = 同一部片的身份。丢了它，dup_check 的「同 infohash 多份归档」
      这一层对云添加归档完全不生效。
    """
    from . import metadata

    folder = rec["folder"]
    names = list(rec.get("files") or [])
    size = int(rec.get("size") or 0)
    record = metadata.archive_record(
        folder, rec["path"], "", names, size, source or "",
        Path("/nonexistent-cloud"),            # 没有本地工作区，清单按远端回填
        scraped=scraped or {},
        manifest_files=names,
        pending=True,                            # 附件没推，标成待补
        infohash=infohash,
    )
    # 云端没有本地文件可 stat：清单大小直接用落地时读到的那份
    by_size = {str(f.get("name")): int(f.get("size") or 0)
               for f in (rec.get("files_detail") or [])}
    if by_size:
        for entry in (record.get("manifest") or {}).get("files") or []:
            if entry.get("name") in by_size:
                entry["size"] = by_size[entry["name"]]
        record["manifest"]["total_size"] = sum(
            int(e.get("size") or 0) for e in record["manifest"]["files"])
    record.update({k: rec[k] for k in ("backend", "root", "rel") if k in rec})
    record["cloud_inbox"] = rec.get("inbox") or ""
    return record
