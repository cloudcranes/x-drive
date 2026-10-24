"""存储后端的注册表与门面。

上层（web / bot）只调这里的函数，不直接碰具体实现：

    storage.kind()                    当前后端种类
    storage.root()                    归档根目录（STORAGE_ROOT，回落 DRIVE_ROOT）
    storage.rel(*parts)               拼「后端内相对路径」，如 rel("SSIS-555")
    storage.get()                     拿到当前后端实例（按配置指纹缓存）
    storage.real_folder(name)         问「这个名字实际落地叫什么」（CD2 会归一化）
    storage.openlist_link_id(url)     从分享链接里取 share id（删归档时顺手删分享）
    storage.check()                   /api/storage/test 用：不抛异常，返回字典

rclone 的子进程执行器由 web / bot 在启动时注入（`set_runner`），这样既复用
它们各自那层日志与超时，也让测试里 monkeypatch 的 `web.rclone` 照样生效。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from .. import netalias
from .. import settings as settings_mod
from .base import AuthError, Backend, FileEntry, StorageError
from .rclone import Rclone

log = logging.getLogger("x-drive.storage")

KINDS = ("rclone", "openlist", "clouddrive2")

_runner = None
_lock = threading.Lock()
_cache: dict[tuple, Backend] = {}


def set_runner(fn) -> None:
    """注入 rclone 执行器（签名 (*args, timeout=) -> (rc, out, err)）。"""
    global _runner
    with _lock:
        _runner = fn
        for key in [k for k in _cache if k and k[0] == "rclone"]:
            _cache.pop(key, None)


def _cast(kind: str, value):
    if kind == "bool":
        return str(value).strip().lower() not in ("0", "false", "no", "off", "")
    try:
        return {"int": int, "float": float, "str": str}[kind or "str"](value)
    except (TypeError, ValueError):
        return None


_fallback = None


def settings():
    """取一份能读网页设置的 Settings。

    优先用进程里已绑定的那份；没绑定（bot 的 --check、离线脚本）就按同一个
    JOBS_DB 自己建一个并绑上。**不能退回去只读环境变量**：网页里选的
    STORAGE_BACKEND 存在 state 表里，只看 env 会让 `app.main --check`
    对着 rclone 自检，而实际上传早就换成 CloudDrive2 了（真出过这个误判）。
    """
    global _fallback
    inst = settings_mod.bound()
    if inst is not None:
        return inst
    if _fallback is None:
        from .. import store as store_mod
        path = (os.getenv("JOBS_DB")
                or str(Path(os.getenv("STATE_PATH", "/data/state.json")).parent / "jobs.db"))
        _fallback = settings_mod.Settings(store_mod.Store(path))
        settings_mod.bind(_fallback)
    return _fallback


def _val(key: str):
    """读一项存储设置：网页设置 > 环境变量 > 默认值。"""
    spec = settings_mod.SETTINGS_SPEC.get(key) or {}
    try:
        ok, value = settings().explicit(key)
    except Exception:                 # 库打不开就别把整个上传搞挂
        ok, value = False, None
    if ok:
        return value
    env = os.getenv(key)
    if env not in (None, ""):
        cast = _cast(spec.get("type"), env)
        if cast is not None:
            return cast
    return spec.get("default")


def kind() -> str:
    value = str(_val("STORAGE_BACKEND") or "rclone").strip().lower()
    if value not in KINDS:
        log.warning("未知存储后端 %r，回退 rclone", value)
        return "rclone"
    return value


def root() -> str:
    """归档根目录。没在网页配过就看老环境变量 DRIVE_ROOT，再不行 X-Drive。"""
    value = str(_val("STORAGE_ROOT") or "").strip()
    if not value:
        value = os.getenv("DRIVE_ROOT", "X-Drive")
    return value.strip("/") or "X-Drive"


def rel(*parts: str) -> str:
    """拼后端内相对路径：rel("SSIS-555") → 'X-Drive/SSIS-555'。"""
    bits = [str(p).strip("/") for p in parts if str(p or "").strip("/")]
    return "/".join([root(), *bits]) if bits else root()


def _fingerprint(which: str) -> tuple:
    keys = {
        "rclone": ("RCLONE_REMOTE",),
        "openlist": ("OPENLIST_URL", "OPENLIST_USER", "OPENLIST_PASSWORD", "OPENLIST_OTP",
                     "OPENLIST_TIMEOUT", "OPENLIST_UPLOAD_TIMEOUT", "OPENLIST_MULTIPART",
                     "OPENLIST_MULTIPART_MB", "OPENLIST_SHARE", "OPENLIST_SHARE_DAYS",
                     "OPENLIST_SHARE_PWD"),
        "clouddrive2": ("CD2_URL", "CD2_USER", "CD2_PASSWORD", "CD2_TLS", "CD2_CA_FILE",
                        "CD2_CHUNK_MB", "CD2_CALL_TIMEOUT", "CD2_TASK_TIMEOUT",
                        "CD2_LINK_MODE", "SHARE_LINK_TEMPLATE"),
    }[which]
    values = []
    for key in keys:
        if key == "RCLONE_REMOTE":
            values.append(os.getenv("RCLONE_REMOTE", "gdrive"))
        else:
            values.append(str(_val(key) if _val(key) is not None else ""))
    return (which, *values)


def _build(which: str) -> Backend:
    if which == "rclone":
        return Rclone(run=_runner, remote=os.getenv("RCLONE_REMOTE", "gdrive"))
    if which == "openlist":
        from .openlist import OpenList
        return OpenList(
            base=str(_val("OPENLIST_URL") or ""),
            user=str(_val("OPENLIST_USER") or ""),
            password=str(_val("OPENLIST_PASSWORD") or ""),
            otp=str(_val("OPENLIST_OTP") or ""),
            timeout=float(_val("OPENLIST_TIMEOUT") or 30),
            upload_timeout=float(_val("OPENLIST_UPLOAD_TIMEOUT") or 3600),
            multipart=bool(_val("OPENLIST_MULTIPART")),
            multipart_mb=float(_val("OPENLIST_MULTIPART_MB") or 512),
            share=bool(_val("OPENLIST_SHARE")),
            share_days=int(_val("OPENLIST_SHARE_DAYS") or 0),
            share_pwd=str(_val("OPENLIST_SHARE_PWD") or ""),
        )
    from .clouddrive2 import CloudDrive2
    return CloudDrive2(
        url=str(_val("CD2_URL") or ""),
        user=str(_val("CD2_USER") or ""),
        password=str(_val("CD2_PASSWORD") or ""),
        tls=bool(_val("CD2_TLS")),
        ca_file=str(_val("CD2_CA_FILE") or ""),
        chunk_mb=float(_val("CD2_CHUNK_MB") or 4),
        call_timeout=float(_val("CD2_CALL_TIMEOUT") or 60),
        task_timeout=float(_val("CD2_TASK_TIMEOUT") or 7200),
        link_mode=str(_val("CD2_LINK_MODE") or "none"),
        link_template=str(_val("SHARE_LINK_TEMPLATE") or ""),
    )


def get(which: str | None = None) -> Backend:
    """取后端实例。配置变了下一次调用就换新的（按指纹缓存，别每次重建连接）。"""
    target = (which or kind()).strip().lower()
    if target not in KINDS:
        target = "rclone"
    key = _fingerprint(target)
    with _lock:
        inst = _cache.get(key)
        if inst is None:
            inst = _build(target)
            _cache[key] = inst
        return inst


def reset() -> None:
    """设置保存后清缓存（连接会重建，token 也重新取）。"""
    with _lock:
        _cache.clear()


def real_folder(name: str, which: str | None = None) -> str:
    """问后端「这个名字实际落地叫什么」。没有这个能力的后端原样返回。"""
    backend = get(which)
    probe = getattr(backend, "real_folder", None)
    if probe is None:
        return name
    try:
        return probe(rel(name)) or name
    except (StorageError, AuthError) as exc:
        log.warning("查询 %s 的真实目录名失败，按原名记账：%s", name, exc)
        return name


def openlist_link_id(link: str) -> str:
    """从 OpenList 分享链接里取 id（`…/s/abc` → `abc`），取不到给空串。"""
    text = (link or "").strip().rstrip("/")
    if "/s/" not in text:
        return ""
    return text.rsplit("/s/", 1)[-1]


def snapshot() -> dict:
    """面板用：当前后端 + 根目录 + 是否配置齐全（不回显任何密钥）。"""
    which = kind()
    configured = True
    if which == "openlist":
        configured = bool(_val("OPENLIST_URL"))
    elif which == "clouddrive2":
        configured = bool(_val("CD2_URL"))
    else:
        configured = True                     # rclone 只依赖挂载进来的配置文件
    return {"kind": which, "root": root(), "configured": configured,
            "link_mode": (str(_val("CD2_LINK_MODE") or "none") if which == "clouddrive2"
                          else ("share" if which == "openlist" else "rclone")),
            # 容器内填宿主机 LAN 地址会被自动改写成 host.docker.internal，
            # 面板据此提一句，免得用户以为「我明明填的是 192.168.1.170」
            "net": netalias.describe()}


def check() -> dict:
    """连通性自检：任何异常都收成 {ok: false, error}，接口层只负责原样回显。"""
    started = time.monotonic()
    snap = snapshot()
    out = {"ok": False, "kind": snap["kind"], "root": snap["root"],
           "configured": snap["configured"], "latency_ms": 0, "detail": "", "error": ""}
    try:
        backend = get()
        ok, detail = backend.check(root())
        out.update(ok=bool(ok), detail=detail)
        if not ok:
            out["error"] = detail
    except AuthError as exc:
        out["error"] = str(exc)
    except StorageError as exc:
        out["error"] = str(exc)
    except Exception as exc:                  # 配置缺项、依赖没装都算「不可用」
        out["error"] = f"{type(exc).__name__}: {exc}"
    out["latency_ms"] = int((time.monotonic() - started) * 1000)
    return out
