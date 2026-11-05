"""CloudDrive2 storage facade and configuration cache."""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from .. import netalias
from .. import settings as settings_mod
from .base import AuthError, Backend, FileEntry, Quota, StorageError

log = logging.getLogger("x-drive.storage")

KINDS = ("clouddrive2",)

_lock = threading.Lock()
_cache: dict[tuple, Backend] = {}


def _cast(kind: str, value):
    if kind == "bool":
        return str(value).strip().lower() not in ("0", "false", "no", "off", "")
    try:
        return {"int": int, "float": float, "str": str}[kind or "str"](value)
    except (TypeError, ValueError):
        return None


_fallback = None


def settings():
    """Read persisted settings shared by Web and Bot."""
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
    return "clouddrive2"


def root() -> str:
    """归档根目录。

    优先级：
      1. 设置里的 STORAGE_ROOT（网页「归档根目录」）
      2. **CLOUD_DEST**（云添加的落地目标）—— 两者本来指的是同一个地方：
         云添加把片子整理到 CLOUD_DEST 下，而全局根是「兜底路径」。
         这两个不一致时，兜底会指向一个不存在的位置（旧记录因此报「路径
         不存在」—— 实测 MIMK-267 就是：全局根已是 Downloads/downloads/test，
         而实际文件在 迅雷云盘/X-Drive 下）。
         所以留空时直接跟随 CLOUD_DEST，别让它跑到别处去。
      3. 老环境变量 DRIVE_ROOT
      4. X-Drive
    """
    value = str(_val("STORAGE_ROOT") or "").strip()
    if not value:
        try:
            value = str(_val("CLOUD_DEST") or "").strip()
        except Exception:                       # noqa: BLE001 取不到就往下走
            value = ""
    if not value:
        value = os.getenv("DRIVE_ROOT", "X-Drive")
    return value.strip("/") or "X-Drive"


def rel(*parts: str) -> str:
    """拼后端内相对路径：rel("SSIS-555") → 'X-Drive/SSIS-555'。"""
    bits = [str(p).strip("/") for p in parts if str(p or "").strip("/")]
    return "/".join([root(), *bits]) if bits else root()


def _fingerprint(which: str) -> tuple:
    keys = ("CD2_URL", "CD2_USER", "CD2_PASSWORD", "CD2_TLS", "CD2_CA_FILE",
            "CD2_CHUNK_MB", "CD2_CALL_TIMEOUT", "CD2_TASK_TIMEOUT",
            "CD2_LINK_MODE", "SHARE_LINK_TEMPLATE")
    return (which, *(str(_val(key) if _val(key) is not None else "") for key in keys))


def _build(which: str) -> Backend:
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
        raise AuthError(f"Unsupported storage backend: {target}")
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


def snapshot() -> dict:
    """面板用：当前后端 + 根目录 + 是否配置齐全（不回显任何密钥）。"""
    return {"kind": kind(), "root": root(), "configured": bool(_val("CD2_URL")),
            "link_mode": str(_val("CD2_LINK_MODE") or "none"),
            "net": netalias.describe()}


def quota() -> Quota:
    """问云盘还剩多少空间。

    本地下载取消后（2026-10-25）文件全程在云盘里，「还剩多少」是唯一有意义
    的空间指标 —— 报本地磁盘剩余空间对用户没有参考价值。

    问不到就返回空 Quota（known=False），别把 0 当成「满了」。
    """
    try:
        backend = get()
    except Exception as exc:                     # 后端没配好：问不了就是问不了
        log.warning("取存储后端失败，问不到容量：%s", exc)
        return Quota()
    probe = getattr(backend, "quota", None)
    if probe is None:                            # 契约外的后端：没有就是没有
        return Quota()
    try:
        return probe(root())
    except Exception as exc:                     # 网络/权限：不让它带走整条命令
        log.warning("问云盘容量失败：%s", exc)
        return Quota()


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
