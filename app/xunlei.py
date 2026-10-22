"""迅雷远程下载（cnk3x/xunlei 容器）客户端：投递、查进度、暂停、删除。

面板没有公开 API，只有它自己前端在用的一套内部接口（`/webman/3rdparty/pan-xunlei-com/index.cgi`）：

  GET  /                       首页 HTML，里面就有现成的 `function uiauth(value){ return "<JWT>" }`
  POST /device/info/watch      拿真实 space（返回的 target 就是 `device_id#<hex>`）
  POST /drive/v1/resource/list 解析磁力：名字、体积、文件数
  GET  /drive/v1/files         列目录，用 name 找「下载到」那个目录的 id
  POST /drive/v1/task          新建任务
  GET  /drive/v1/tasks         任务列表：phase / progress / speed / error
  POST /method/patch/drive/v1/task        暂停、继续（set_params.spec）
  POST /method/delete/drive/v1/tasks      删任务（task_ids 可重复）

三处必须照抄面板的行为，否则会「成功但没下」：
· `space` 必须是 `device/info/watch` 的 target；用占位 `device_id#` 投任务会进云盘阴影空间，
  接口回 200，但下载器永不认领，落盘永远是 0；
· `sub_file_index` 不是文件序号，而是选中区间：`0-<n-1>` 选全部，`--1,` 是单文件全选。
  传 "0"/"1" 只会下种子里的第一个文件（实测只落了一个 .srt）；
· token 不用手抄：首页 HTML 的 `uiauth` 就是它，取一次缓存一分钟，过期自动续。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from pathlib import Path, PurePosixPath

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
    """投递失败（目录 id 失效、磁力无效、面板未登录）。"""

def info_hash_of(url: str) -> str:
    m = re.search(r"xt=urn:btih:([0-9a-fA-F]{40})", url or "")
    return m.group(1).lower() if m else ""

def guess_name(url: str) -> str:
    tail = (url or "").split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    return Path(tail).stem[:100] or "download"

def token_expired(token: str, skew: float = 3600.0) -> bool:
    """token 的 exp 是否已过（或只剩 skew 秒）。解不开就当过期。"""
    try:
        body = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return float(data.get("exp") or 0) - time.time() < skew
    except Exception:
        return True

def file_index(count: int) -> str:
    """`sub_file_index` 是选中区间，不是序号：单文件 `--1,`，多文件 `0-<n-1>`。"""
    return "--1," if count <= 1 else f"0-{count - 1}"

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
        """找同一个来源已存在的任务（避免重复投递）。

        info_hash 要等面板解析完磁力才出现，刚投的那几秒是空的，
        所以还要比 url —— 否则「投递后马上再投一次」会变成两个任务抢同一份带宽。
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

    async def parse(self, client, url: str) -> list[dict]:
        """磁力解析：每个条目给出 name / file_size / file_count / url。失败返回 []。"""
        try:
            body = await self.call(client, "POST", "/drive/v1/resource/list",
                                   params={"space": PLACEHOLDER},
                                   json={"page_size": 1000, "urls": url})
        except (XunleiError, httpx.HTTPError) as exc:
            log.info("磁力解析失败（不影响投递）：%s", exc)
            return []
        return (body.get("list") or {}).get("resources") or []

    # ------------------------------------------------------------ 投递
    async def add(self, client, url: str, name: str = "", size: int = 0) -> str:
        """投递任务，返回迅雷任务 id。已在跑同一个种子时返回那条的 id。"""
        parent = await self.parent(client)
        running = await self.find(client, url)
        if running:
            log.info("同一个种子已在迅雷里，复用任务 %s（%s）", running.get("id"), running.get("phase"))
            return running.get("id") or ""

        space = await self.space(client)
        items = await self.parse(client, url) or [{}]
        first = ""
        for item in items:
            meta = item.get("meta") or {}
            count = int(item.get("file_count") or 1)
            payload = {
                "type": "user#download-url",
                "name": item.get("file_name") or name or guess_name(url),
                "file_name": item.get("file_name") or name or guess_name(url),
                "file_size": str(item.get("file_size") or size or 1),
                "space": space,
                "params": {
                    "target": space,
                    "url": meta.get("url") or url,
                    "total_file_count": str(count),
                    "sub_file_index": file_index(count),
                    "parent_folder_id": parent,
                    "mime_type": "",
                    "file_id": "",
                },
            }
            try:
                body = await self.call(client, "POST", "/drive/v1/task",
                                       params={"space": space}, json=payload)
            except XunleiError as exc:
                raise SubmitError(f"迅雷投递失败：{exc}") from exc
            task_id = (body.get("task") or {}).get("id") or ""
            if not task_id:
                raise SubmitError(f"迅雷没有返回任务 id：{str(body)[:200]}")
            log.info("已投递迅雷任务 id=%s name=%s 文件数=%d", task_id, payload["name"], count)
            first = first or task_id
        return first

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

    # ------------------------------------------------------------ 等待
    async def wait(self, client, task_id: str, url: str = "", poll: float = 10.0,
                   timeout: float = 7200.0, stall_timeout: float = 900.0,
                   revive_stall: bool = True, progress=None) -> dict:
        """轮询到任务完成；失败 / 死种 / 超时抛异常。

        progress(percent, speed, task) 只在进度变化 ≥2% 或每 4 个周期回调一次，
        取消检查也搭在这上面，所以别把间隔调得太细。
        """
        deadline = time.monotonic() + timeout
        last_progress_at = time.monotonic()
        last_done = -1.0
        last_report = -1.0
        started = False
        # 刚投的任务可能要过一两轮才出现在列表里，别把「还没登记」当成「被删了」
        missing_since = 0.0
        # 死种先给一次「暂停→继续」的机会：迅雷会重连 tracker/换 peer 组，
        # 实测不少任务卡住后这样能复活；复活过一次还不动才判死
        revived = False
        while True:
            tasks = await self.tasks(client, task_id)
            task = tasks[0] if tasks else (await self.find(client, url) if url else {})
            if not task:
                now = time.monotonic()
                missing_since = missing_since or now
                if now - missing_since > max(60.0, poll * 3):
                    raise XunleiError("迅雷里找不到这个任务（可能被手工删掉了）")
                await asyncio.sleep(poll)
                continue
            missing_since = 0.0

            params = task.get("params") or {}
            phase = task.get("phase") or ""
            percent = float(task.get("progress") or 0) / 100.0
            speed = int(params.get("speed") or 0)
            done = int(params.get("checked_size") or 0)
            if phase == RUNNING and done > last_done:
                last_progress_at, started = time.monotonic(), True
            last_done = max(last_done, done)

            if phase == ERROR:
                raise XunleiError(f"迅雷任务失败：{params.get('error') or task.get('message') or '未知错误'}")
            if (stall_timeout and started and phase in (RUNNING, PENDING)
                    and time.monotonic() - last_progress_at > stall_timeout):
                if revive_stall and not revived:
                    log.warning("任务 %s 停滞在 %.1f%%，尝试暂停后继续以重连 peers", task_id, percent * 100)
                    try:
                        await self.pause(client, task_id)
                        await asyncio.sleep(min(max(poll, 2.0), 15.0))
                        await self.control(client, task_id, "running")
                        revived = True
                        last_progress_at = time.monotonic()
                        continue
                    except XunleiError as exc:
                        log.warning("重连尝试失败：%s", exc)
                raise Stalled(f"{int(stall_timeout)} 秒没有任何进展（停在 {percent * 100:.1f}%），可能是死种")
            if progress and (percent - last_report >= 0.02 or phase == DONE
                             or time.monotonic() - last_progress_at > 0 and last_report < 0):
                await progress(min(percent, 1.0), speed, task)
                last_report = percent
            if phase == DONE:
                return task
            if time.monotonic() > deadline:
                raise XunleiError(f"迅雷下载超时（{int(timeout)}s，停在 {percent * 100:.1f}%）")
            await asyncio.sleep(poll)

# ---------------------------------------------------------------- 落盘

def local_path(real_path: str, tmp_root: str) -> Path | None:
    """迅雷给的 /downloads/xxx → 本机可见路径。不在 TMP_ROOT 下的不认。"""
    if not real_path:
        return None
    root, target = PurePosixPath(tmp_root), PurePosixPath(real_path)
    if root not in target.parents:
        return None
    return Path(tmp_root).joinpath(*target.relative_to(root).parts)

def owned(real_path: str, tmp_root: str, key: str) -> bool:
    """这个迅雷输出目录是不是本程序这次任务建的（目录名 = key 归一后的样子）。"""
    path = local_path(real_path, tmp_root)
    if path is None or not key:
        return False
    return path.name == "".join(ch for ch in key if ch.isalnum())

def content(path: Path, min_size: int = 1 << 20) -> list[Path]:
    """够大的媒体文件（视频或图片）。写真图包也要算数，不能只认视频。"""
    return [
        p for p in path.rglob("*")
        if p.is_file() and p.suffix.lower() in (VIDEO_EXT | IMAGE_EXT)
        and p.stat().st_size >= min_size
    ]

def growing(path: Path) -> bool:
    """还有迅雷自己的中间文件就说明在写。"""
    return any(p.suffix.lower() in {".xltd", ".downloading", ".tmp", ".part"}
               for p in path.rglob("*"))

def idle_seconds(path: Path) -> float:
    newest = 0.0
    for item in path.rglob("*"):
        try:
            newest = max(newest, item.stat().st_mtime)
        except OSError:
            continue
    return time.time() - newest if newest else float("inf")

def done(path: Path, min_age: float = 30.0, min_size: int = 1 << 20) -> bool:
    """有够大的媒体、没有临时文件、并且已经安静够久。"""
    if not path.is_dir():
        return False
    return (bool(content(path, min_size)) and not growing(path)
            and idle_seconds(path) >= min_age)
