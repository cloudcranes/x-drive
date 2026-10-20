"""aria2 RPC 的薄封装（web 后台也用它，所以放在这里独立成模块）。

对应 p3terx/aria2-pro 的部署方式：RPC 端口 + RPC_SECRET。
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

log = logging.getLogger("x-drive.aria2")

DONE = "complete"
ERROR = "error"
REMOVED = "removed"
STATUS_FIELDS = [
    "gid", "status", "totalLength", "completedLength", "downloadSpeed",
    "uploadSpeed", "errorMessage", "files", "dir", "bittorrent", "connections",
    "followedBy", "belongsTo",
]


class RpcError(RuntimeError):
    pass

class Incomplete(RuntimeError):
    """状态显示完成但实际没有拿到完整内容（含 BT 元数据阶段误判）。"""


class Aria2:
    def __init__(self, endpoint: str, secret: str = "", poll: float = 5.0, timeout: float = 7200.0):
        self.endpoint = endpoint if endpoint.endswith("/jsonrpc") else endpoint.rstrip("/") + "/jsonrpc"
        self.secret = secret
        self.poll = poll
        self.timeout = timeout
        self._id = 0

    def _params(self, *args) -> list:
        return ([f"token:{self.secret}"] if self.secret else []) + list(args)

    async def call(self, client: httpx.AsyncClient, method: str, *args):
        self._id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": f"xd{self._id}",
            "method": f"aria2.{method}",
            "params": self._params(*args),
        }
        resp = await client.post(self.endpoint, json=payload, timeout=30.0)
        resp.raise_for_status()
        body = resp.json()
        if body.get("error"):
            raise RpcError(body["error"].get("message") or str(body["error"]))
        return body.get("result")

    # ---------------------------------------------------------------- 任务提交
    async def add_magnet(self, client, magnet: str, directory: str) -> str:
        options = {
            "dir": directory,
            "seed-time": "0",
            "bt-stop-timeout": "180",
            "file-allocation": "none",
            "auto-file-renaming": "false",
            "allow-overwrite": "true",
        }
        gid = await self.call(client, "addUri", [magnet], options)
        log.info("已提交磁力任务 gid=%s dir=%s", gid, directory)
        return gid

    async def add_uri(self, client, url: str, directory: str, filename: str = "") -> str:
        options = {
            "dir": directory,
            "file-allocation": "none",
            "auto-file-renaming": "false",
            "allow-overwrite": "true",
        }
        if filename:
            options["out"] = filename
        gid = await self.call(client, "addUri", [url], options)
        log.info("已提交直链任务 gid=%s dir=%s", gid, directory)
        return gid

    # ---------------------------------------------------------------- 查询
    async def list_tasks(self, client) -> dict:
        """活动 + 等待 + 已停止，供后台列表展示。"""
        active = await self.call(client, "tellActive", STATUS_FIELDS) or []
        waiting = await self.call(client, "tellWaiting", 0, 50, STATUS_FIELDS) or []
        stopped = await self.call(client, "tellStopped", 0, 50, STATUS_FIELDS) or []
        stat = await self.call(client, "getGlobalStat") or {}
        return {"active": active, "waiting": waiting, "stopped": stopped, "stat": stat}

    async def wait(self, client, gid: str, progress=None) -> dict:
        """轮询到「真的下载完」/失败/超时；progress(percent, speed, status) 用于回执。

        BT 任务会被 aria2 拆成两个 gid：磁力先是元数据任务，complete 时
        followedBy 指向真正的下载任务（此时一个字节的媒体都还没下）。只盯第一个
        gid 会立刻「完成」，上游照着去整理目录就什么都找不到。所以：
          · 顺着 followedBy 追到真正下载的 gid；
          · complete 之后还要核对 completedLength 与 totalLength。
        """
        deadline = time.monotonic() + self.timeout
        last = -1.0
        current = gid
        seen: set[str] = set()
        while True:
            status = await self.call(client, "tellStatus", current, STATUS_FIELDS)
            st = status.get("status")

            # 元数据任务完成 → 换成它指向的真实任务继续等
            followed = [g for g in (status.get("followedBy") or []) if g and g not in seen]
            if followed:
                seen.add(current)
                current = followed[0]
                log.info("BT 元数据就绪，转去跟踪 gid=%s（来自 %s）", current, gid)
                last = -1.0
                status = await self.call(client, "tellStatus", current, STATUS_FIELDS)
                st = status.get("status")

            total = int(status.get("totalLength") or 0)
            done = int(status.get("completedLength") or 0)
            percent = (done / total) if total else 0
            speed = int(status.get("downloadSpeed") or 0)
            if progress and (percent - last >= 0.02 or st != "active"):
                await progress(percent, speed, status)
                last = percent
            if st == DONE:
                if total and done < total:
                    raise Incomplete(
                        f"aria2 报告完成但只拿到 {done}/{total} 字节，"
                        f"可能是磁盘空间不足或文件被外部改动")
                return status
            if st in (ERROR, REMOVED):
                raise RuntimeError(f"aria2 任务失败：{status.get('errorMessage') or st}")
            if time.monotonic() > deadline:
                raise RuntimeError(f"aria2 下载超时（{int(self.timeout)}s）")
            await asyncio.sleep(self.poll)

    async def shutdown(self, client, gid: str) -> None:
        try:
            await self.call(client, "removeDownloadResult", gid)
        except Exception:
            pass

    async def abort(self, client, gid: str) -> None:
        """放弃任务：先停掉（不再往磁盘写），但保留记录便于排查。

        注意不要在这里 removeDownloadResult，更不要 forceRemove 后立刻让上游
        删目录 —— aria2 手里还握着文件句柄，删了目录它仍会往已 unlink 的
        inode 里写。
        """

        async def drain() -> None:
            status = await self.call(client, "tellStatus", gid, ["status", "followedBy"])
            if status.get("status") == REMOVED:
                return
            for child in status.get("followedBy") or []:
                try:
                    await self.call(client, "forcePause", child)
                except Exception:
                    pass

        try:
            await asyncio.gather(
                self.call(client, "forcePause", gid),
                drain(),
                return_exceptions=True,
            )
        except Exception as exc:      # 记录不存在等情况不必打断上层收尾
            log.warning("停止 aria2 任务 %s 失败：%s", gid, exc)

    async def remove(self, client, gid: str) -> None:
        """删除任务（活动任务先停再删记录）。"""
        try:
            await self.call(client, "forceRemove", gid)
        except Exception:
            pass
        await self.shutdown(client, gid)
