"""通过 aria2-pro 容器的 JSON-RPC 下载磁力/BT（替代镜像内置 aria2c）。

对应 p3terx/aria2-pro 的部署方式：RPC 端口 + RPC_SECRET，容器内常驻，
本服务只下发任务并轮询状态，下载好的文件落在共享卷里。

接口：http://<host>:<port>/jsonrpc
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

log = logging.getLogger("x-drive.aria2")

# 常见的过期/停止状态
DONE = "complete"
ERROR = "error"
REMOVED = "removed"


class RpcError(RuntimeError):
    pass


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

    async def ping(self, client: httpx.AsyncClient) -> bool:
        try:
            return bool(await self.call(client, "getVersion"))
        except Exception as exc:
            log.warning("aria2 rpc 不可用：%s", exc)
            return False

    async def add_magnet(
        self, client: httpx.AsyncClient, magnet: str, directory: str
    ) -> str:
        options = {
            "dir": directory,
            "seed-time": "0",            # 下完不做种
            "bt-stop-timeout": "180",    # 无进度 3 分钟退出
            "file-allocation": "none",
            "auto-file-renaming": "false",
            "allow-overwrite": "true",
        }
        gid = await self.call(client, "addUri", [magnet], options)
        log.info("已提交磁力任务 gid=%s dir=%s", gid, directory)
        return gid

    async def wait(
        self, client: httpx.AsyncClient, gid: str, progress=None
    ) -> dict:
        """轮询直到完成/失败/超时；progress(percent, speed, eta) 用于回执。"""
        deadline = time.monotonic() + self.timeout
        last = 0
        while True:
            status = await self.call(client, "tellStatus", gid, [
                "status", "totalLength", "completedLength", "downloadSpeed",
                "errorMessage", "files", "dir", "bittorrent",
            ])
            st = status.get("status")
            total = int(status.get("totalLength") or 0)
            done = int(status.get("completedLength") or 0)
            percent = (done / total) if total else 0
            speed = int(status.get("downloadSpeed") or 0)
            if progress and (percent - last >= 0.02 or percent >= 1 or st != "active"):
                await progress(percent, speed, status)
                last = percent
            if st == DONE:
                return status
            if st in (ERROR, REMOVED):
                raise RuntimeError(
                    f"aria2 任务失败：{status.get('errorMessage') or st}"
                )
            if time.monotonic() > deadline:
                raise RuntimeError(f"aria2 下载超时（{int(self.timeout)}s）")
            await asyncio.sleep(self.poll)

    async def shutdown(self, client: httpx.AsyncClient, gid: str) -> None:
        """下载结束后移除任务记录，避免影响下次。"""
        try:
            await self.call(client, "removeDownloadResult", gid)
        except Exception:
            pass
