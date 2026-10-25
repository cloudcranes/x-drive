"""迅雷面板（cnk3x/xunlei 容器）只读客户端：列任务、暂停、继续、删记录。

面板没有公开 API，只有它自己前端在用的一套内部接口（`/webman/3rdparty/pan-xunlei-com/index.cgi`）：

  GET  /                       首页 HTML，里面就有现成的 `function uiauth(value){ return "<JWT>" }`
  POST /device/info/watch      拿真实 space（返回的 target 就是 `device_id#<hex>`）
  GET  /drive/v1/files         列目录，用 name 找「下载到」那个目录的 id
  GET  /drive/v1/tasks         任务列表：phase / progress / speed / error
  POST /method/patch/drive/v1/task        暂停、继续（set_params.spec）
  POST /method/delete/drive/v1/tasks      删任务（task_ids 可重复）

**只读 + 面板控制**（2026-10-25 起本地下载已取消）：本模块不再负责「把磁力
投给迅雷、盯进度、确认落盘」——那套逻辑（投递/等待/落盘判定）随本地下载一起
移除了。现在它只服务界面上那个「迅雷面板」折叠区：看一眼外部迅雷里还有什么
任务，必要时暂停/继续/删掉它的**任务记录**（不动已下载的文件）。

token 不用手抄：首页 HTML 的 `uiauth` 就是它，取一次缓存一分钟，过期自动续。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time

import httpx

log = logging.getLogger("x-drive.xunlei")

PREFIX = "/webman/3rdparty/pan-xunlei-com/index.cgi"
PLACEHOLDER = "device_id#"
VIDEO_EXT = {".mp4", ".mkv", ".avi", ".wmv", ".mov", ".m4v", ".ts", ".flv", ".rmvb"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
TASK_TYPE = "user#download-url,user#download"

DONE, ERROR = "PHASE_TYPE_COMPLETE", "PHASE_TYPE_ERROR"
PENDING, RUNNING, PAUSED = "PHASE_TYPE_PENDING", "PHASE_TYPE_RUNNING", "PHASE_TYPE_PAUSED"

_UIAUTH_RE = re.compile(r'function\s+uiauth\s*\([^)]*\)\s*\{\s*return\s*"([^"]+)"')

class XunleiError(RuntimeError):
    pass

class Stalled(XunleiError):
    """连续很久没有任何进展（死种）。"""

class SubmitError(XunleiError):
    """面板操作失败（目录 id 失效、磁力无效、面板未登录）。"""

def info_hash_of(url: str) -> str:
    m = re.search(r"xt=urn:btih:([0-9a-fA-F]{40})", url or "")
    return m.group(1).lower() if m else ""

def token_expired(token: str, skew: float = 3600.0) -> bool:
    """token 的 exp 是否已过（或只剩 skew 秒）。解不开就当过期。"""
    try:
        body = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return float(data.get("exp") or 0) - time.time() < skew
    except Exception:
        return True

class Xunlei:
    def __init__(self, base: str = "http://127.0.0.1:2345", parent_id: str = "",
                 tmp_root: str = "/downloads", token: str = ""):
        self.base = base.rstrip("/")
        self.parent_id = parent_id
        self.tmp_root = tmp_root
        self._token = (token or "").strip()
        self._token_at = 0.0
        self._space = ""

    def client(self, timeout: float = 90.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base, follow_redirects=True, timeout=timeout)

    def _auth(self) -> dict:
        return {"pan-auth": self._token} if self._token else {}

    async def token(self, client, refresh: bool = False) -> str:
        """面板 token。首页 HTML 里就写着，取一次缓存一分钟。

        配置里给的 token 只当兜底（老版本面板走的是另一套签名）。
        """
        if not refresh and self._token and time.monotonic() - self._token_at < 60:
            return self._token
        try:
            # 用绝对地址：只给相对路径时 httpx 会把请求发到裸路径上
            # （"Request URL is missing an 'http://' or 'https://' protocol"），
            # 面板明明可达却取不到 token
            resp = await client.get(self.base + PREFIX + "/")
            resp.raise_for_status()
            found = _UIAUTH_RE.search(resp.text)
        except httpx.HTTPError as exc:
            found = None
            log.warning("取面板 token 失败：%s", exc)
        if found:
            self._token, self._token_at = found.group(1), time.monotonic()
            return self._token
        if self._token:
            self._token_at = time.monotonic()
            return self._token
        raise SubmitError(f"面板没有给出 uiauth token，检查 {self.base} 是否是迅雷面板")

    async def call(self, client, method: str, path: str, **kwargs):
        """带认证的请求。

        两种重试：402 说明 token 过期，刷新后重来；读超时说明面板忙（任务列表能拖几十秒）。
        """
        for attempt in (1, 2):
            await self.token(client, refresh=attempt == 2)
            try:
                # 绝对地址：只给相对路径时 httpx 会把它当成新 URL（面板可达却报缺协议）
                resp = await client.request(method, self.base + PREFIX + path,
                                            headers=self._auth(), **kwargs)
            except httpx.ReadTimeout:
                if attempt == 2:
                    raise XunleiError(f"面板无响应（{path} 读超时）")
                continue
            if resp.status_code != 402:
                break
        else:
            raise XunleiError(f"面板拒绝认证（402）：{resp.text[:200]}")
        body = resp.json() if resp.content else {}
        if isinstance(body, dict) and (body.get("error") or body.get("error_code")):
            raise XunleiError(body.get("error") or str(body.get("error_description") or body.get("error_code")))
        return body

    # ------------------------------------------------------------ 查询
    async def space(self, client) -> str:
        """真实 space 名（`device_id#<hex>`）：device/info/watch 的 target。"""
        if self._space:
            return self._space
        body = await self.call(
            client, "POST", "/device/info/watch",
            params={"space": PLACEHOLDER}, json={"space": PLACEHOLDER},
        )
        target = body.get("target") or ""
        if not target.startswith("device_id#"):
            raise SubmitError(f"取不到迅雷 space（device/info/watch 返回 {str(body)[:200]}）")
        self._space = target
        return target

    async def parent(self, client) -> str:
        """「下载到」目录 id：配置优先，否则在真实 space 里找 `RealPath == /downloads/` 的那个。

        自己找比让人去 F12 里抄一份可靠 —— 它随实例变，抄错就是「成功但没下」。
        """
        if self.parent_id:
            return self.parent_id
        body = await self.call(
            client, "GET", "/drive/v1/files",
            params={"space": await self.space(client), "parent_id": "", "limit": 200},
        )
        for item in body.get("files") or []:
            params = item.get("params") or {}
            if item.get("kind") == "drive#folder" and params.get("RealPath") in ("/downloads/", "/downloads"):
                self.parent_id = item.get("id") or ""
                log.info("自动识别下载目录 id=%s", self.parent_id)
                return self.parent_id
        raise SubmitError("在迅雷 space 里没找到下载目录（/downloads），请手配 XUNLEI_PARENT_ID")

    async def tasks(self, client, task_id: str = "") -> list[dict]:
        """任务列表。给 task_id 时只要那一个。"""
        filters = {"type": {"in": TASK_TYPE}}
        if task_id:
            filters["id"] = {"in": task_id}
        body = await self.call(
            client, "GET", "/drive/v1/tasks",
            params={"space": await self.space(client), "limit": 100,
                    "filters": json.dumps(filters, separators=(",", ":"))},
        )
        return body.get("tasks") or []

    async def find(self, client, url: str) -> dict:
        """找同一个来源已存在的任务（面板里按 infohash / url 匹配）。

        只读查询：用来回答「这条磁力是不是已经在迅雷里跑着」。
        """
        info_hash = info_hash_of(url)
        for task in await self.tasks(client):
            params = task.get("params") or {}
            found_hash = (params.get("info_hash") or "").lower()
            if info_hash and found_hash == info_hash:
                return task
            if not found_hash and (params.get("url") or "") == url:
                return task
        return {}

    # ------------------------------------------------------------ 控制
    async def control(self, client, task_id: str, phase: str) -> None:
        """改任务状态：running / pause / delete。"""
        task = (await self.tasks(client, task_id) or [{}])[0]
        await self.call(
            client, "POST", "/method/patch/drive/v1/task",
            params={"space": PLACEHOLDER}, json={
                "id": task_id,
                "space": task.get("space") or await self.space(client),
                "type": task.get("type") or "user#download-url",
                "set_params": {"spec": json.dumps({"phase": phase})},
            },
        )

    async def pause(self, client, task_id: str) -> None:
        await self.control(client, task_id, "pause")

    async def remove(self, client, task_id: str) -> None:
        """删任务记录（不删已下载的文件）。

        出错就抛：看板上的删除按钮要能报错，内部收尾自己包 try。
        """
        await self.call(
            client, "POST", "/method/delete/drive/v1/tasks",
            params={"space": await self.space(client), "task_ids": task_id}, json={},
        )

    async def alive(self, client) -> None:
        """自检：面板可达、下载目录 id 已配、token 与 space 都取得到。"""
        await self.token(client, refresh=True)
        await self.parent(client)
        await self.space(client)
