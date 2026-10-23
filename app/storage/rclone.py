"""rclone 适配器：把原来的 subprocess 调用原样包成 Backend。

行为必须与改造前逐字一致 —— 现网就是它在跑，argv 多一个少一个参数都可能让
上传变慢或报错。所以这里不做「顺手优化」（不加 --progress、不改 --retries）。

`run` 是注入的子进程执行器（签名同 web.rclone：(*args, timeout=) -> (rc, out, err)）。
网页与 bot 各自注入自己的那层，测试 monkeypatch 的也正是那一层。
"""
from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .base import AuthError, Backend, FileEntry, Progress, StorageError

# 这些字样出现时不是抖动，是配置/权限问题：重试只会把配额撞得更死
# 配置/权限类：重试一万次也一样。rclone 的「没这个 remote / 配置文件里没这一段」
# 也算配置错（以前会当网络抖动重试两轮，白等两分钟）。
AUTH_HINTS = ("unauthorized", "invalid_grant", "forbidden", "permission denied",
              "401", "403", "doesn't exist", "directory not found", "not found",
              "didn't find section", "config file", "no such remote")


def _default_run(*args: str, timeout: float = 7200.0) -> tuple[int, str, str]:
    """默认执行器：直接调 rclone（单测里没人注入时用它）。"""
    cmd = ["rclone", "--config", os.getenv("RCLONE_CONFIG", "/config/rclone/rclone.conf"), *args]
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", "rclone 超时"
    except FileNotFoundError:
        return 127, "", "找不到 rclone（镜像里没装，或 WITH_RCLONE=0 构建）"
    return (done.returncode, done.stdout.decode(errors="replace"),
            done.stderr.decode(errors="replace"))


class Rclone(Backend):
    kind = "rclone"

    def __init__(self, run=None, remote: str = "gdrive"):
        self.run = run or _default_run
        self.remote = (remote or "gdrive").strip()

    # ── 路径：协议里的相对路径 → rclone 的 `remote:path`
    def spec(self, path: str) -> str:
        return f"{self.remote}:{(path or '').strip('/')}"

    def _fail(self, what: str, err: str) -> None:
        text = (err or "").strip()[:300]
        message = f"{what}失败：{text}"
        lowered = text.lower()
        if any(hint in lowered for hint in AUTH_HINTS):
            raise AuthError(message)
        raise StorageError(message)

    def _run(self, *args: str, timeout: float = 7200.0) -> tuple[str, str]:
        rc, out, err = self.run(*args, timeout=timeout)
        if rc != 0:
            self._fail(args[0], err)
        return out, err

    # ── 目录
    def ensure_dir(self, path: str) -> str:
        self._run("mkdir", self.spec(path), timeout=120.0)
        return path.rstrip("/").rsplit("/", 1)[-1]

    def list_dirs(self, path: str) -> list[str]:
        out, _ = self._run("lsf", self.spec(path), "--dirs-only", timeout=180.0)
        return [line.strip().strip("/") for line in out.splitlines() if line.strip()]

    def list_files(self, path: str) -> list[FileEntry]:
        out, _ = self._run("lsjson", self.spec(path), "--files-only", timeout=180.0)
        try:
            items = json.loads(out or "[]")
        except json.JSONDecodeError as exc:
            raise StorageError(f"列目录返回不可解析：{exc}") from exc
        return [FileEntry(str(i.get("Name") or ""), int(i.get("Size") or 0))
                for i in items if i.get("Name")]

    # ── 写入
    def upload_dir(self, local: Path, dest: str, *, transfers: int = 4,
                   chunk: str = "64M", progress: Progress | None = None) -> int:
        target = self.spec(dest)
        self._run("mkdir", target, timeout=120.0)
        self._run("copy", str(local), target,
                  "--transfers", str(transfers),
                  "--drive-chunk-size", str(chunk),
                  "--retries", "2", "--timeout", "30m",
                  "--drive-stop-on-upload-limit",
                  timeout=7200.0)
        total = 0
        for path in Path(local).rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                pass
        return total

    def upload_file(self, local: Path, dest: str) -> None:
        self._run("copyto", str(local), self.spec(dest), timeout=300.0)

    # ── 分享 / 变更
    def link(self, dest: str) -> str | None:
        rc, out, _ = self.run("link", self.spec(dest), timeout=120.0)
        text = (out or "").strip()
        return text if rc == 0 and text.startswith("http") else None

    def purge(self, dest: str) -> str:
        self._run("purge", self.spec(dest), timeout=1800.0)
        return "已删除"

    def rename_dir(self, src: str, new_name: str) -> None:
        parent = src.rstrip("/").rsplit("/", 1)[0] if "/" in src.strip("/") else ""
        dst = f"{parent}/{new_name}" if parent else new_name
        self._run("move", self.spec(src), self.spec(dst), timeout=3600.0)
        rc, _, _ = self.run("rmdir", self.spec(src), timeout=180.0)   # 空目录留着没用
        if rc != 0:
            raise StorageError("移动完成，但清空原目录失败（不影响归档，可稍后重试）")

    # ── 自检
    def check(self) -> tuple[bool, str]:
        """探一次：能不能连上、能不能写。

        以前第一探是 `lsd <根目录>`，那会把根下所有番号目录列一遍 —— 归档上千条
        时这一步能拖到几十秒（实测 92s），面板上「测试连接」像死了。改成先用
        `about`（云端配额，单个轻请求，顺带给出剩余空间），拿不到配额的后端才退回
        `lsd remote:` 这种浅探测。
        """
        # 两件事并行：写探测（必须）+ 配额（附赠信息）。一个一个来要串满两次云端
        # 往返（实测 about 40s + mkdir 10s + rmdir 10s = 一分钟），并起来只等最慢那个，
        # 而配额查询给一个短超时：问不到就只报「可读写」，不拖住整个测试。
        def probe_write():
            rc, _, err = self.run("mkdir", self.spec("_healthcheck"), timeout=45.0)
            if rc != 0:
                return False, f"{self.remote}: 可读但不可写：{(err or '').strip()[:200]}"
            self.run("rmdir", self.spec("_healthcheck"), timeout=45.0)
            return True, ""

        def probe_quota():
            rc, out, _err = self.run("about", f"{self.remote}:", timeout=20.0)
            if rc != 0:
                return ""
            for line in (out or "").splitlines():
                if line.lower().startswith("free:"):
                    return f" · 剩余 {line.split(':', 1)[1].strip()}"
            return ""

        with ThreadPoolExecutor(max_workers=2) as pool:
            wrote = pool.submit(probe_write)
            quota = pool.submit(probe_quota)
            ok, why = wrote.result()
            free = quota.result() if ok else ""
        if not ok:
            return False, why
        return True, f"rclone {self.remote}: 可读写{free}"
