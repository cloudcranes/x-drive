"""容器里的「宿主机地址」自动改写。

问题：x-drive 跑在容器里，而 OpenList / CloudDrive2 / MetaTube 这些服务通常装在
**同一台宿主机**上。用户按习惯填 `http://192.168.1.170:19798`（宿主机的 LAN 地址），
在容器里却连不上 —— 这台机器上实测 docker 网络打不回宿主 LAN IP，只有
`host.docker.internal` 通（compose 里已经加了 `extra_hosts: host-gateway`）。
症状是 gRPC 报 `Deadline Exceeded`、HTTP 报连接超时，而服务本身其实是好的。

为什么不用「检测这个 IP 是不是宿主机」来判：**容器内做不到**。容器能查到自己的
IP 和默认网关（`/proc/net/route`，就是 docker 网桥那头，属于宿主机），但宿主机在
LAN 上的地址不经过内核就查不到 —— 没有 /proc 能列出宿主机的网卡。

所以这里改用**连通性探测**，判据是「原地址连不上 + host.docker.internal 连得上」
才改写。这既覆盖了本机宿主机这种最常见的填法，也不会把真正在别的机器上的服务
误改（那种情况 host.docker.internal 同样连不上，原样保留并把两个错误都带出来）。

几条边界：
  · 非容器环境一律不改写（本机直装时 LAN IP 就是对的）；
  · 公网地址不探测、不改写（没必要，也不该给外部服务加延迟）；
  · `127.0.0.1` / `localhost` 在容器里指向容器自己，若宿主同端口可达则改写；
  · 探测结果按 (host, port) 缓存 5 分钟，改设置会重建客户端自然重探。
"""
from __future__ import annotations

import ipaddress
import logging
import os
import socket
import threading
import time
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger("x-drive.netalias")

GATEWAY_HOST = "host.docker.internal"
PROBE_TIMEOUT = 0.8          # 单次 TCP 探测；配置变更时才探，不放在热路径
CACHE_TTL = 300.0
_LOOPBACK = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}

_lock = threading.Lock()
_cache: dict[tuple[str, int], tuple[float, bool]] = {}


# ─────────────────────────────────────────── 环境判定

def in_container() -> bool:
    """是不是跑在容器里。

    三条独立证据，命中一条即可：`/.dockerenv`（docker 会建）、
    `/proc/1/cgroup` 里出现 docker/containerd/kubepods、以及 K8s 注入的环境变量。
    """
    if os.path.exists("/.dockerenv"):
        return True
    if os.getenv("KUBERNETES_SERVICE_HOST"):
        return True
    try:
        with open("/proc/1/cgroup", "r", encoding="utf-8", errors="replace") as fh:
            blob = fh.read()
    except OSError:
        return False
    return any(mark in blob for mark in ("docker", "containerd", "kubepods", "lxc"))


def container_gateway() -> str:
    """容器默认网关（docker 网桥那头，也就是宿主机）。拿不到给空串。

    /proc/net/route 每行：Iface Destination Gateway Flags ...，全部以十六进制
    小端存；Destination=00000000 的那行就是默认路由，Gateway 反过来转就是 IP。
    """
    try:
        with open("/proc/net/route", "r", encoding="utf-8") as fh:
            next(fh)                     # 表头
            for line in fh:
                fields = line.split()
                if len(fields) > 3 and fields[1] == "00000000":
                    raw = int(fields[2], 16)
                    return str(ipaddress.IPv4Address(raw.to_bytes(4, "little")))
    except (OSError, StopIteration, ValueError):
        pass
    return ""


# ─────────────────────────────────────────── 探测

def _probe(host: str, port: int, timeout: float = PROBE_TIMEOUT) -> bool:
    """能不能建立 TCP 连接。只看「连得上」，不管协议层对不对。"""
    key = (host, port)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]
    ok = False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            ok = True
    except OSError:
        ok = False
    with _lock:
        _cache[key] = (time.monotonic(), ok)
    return ok


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def _port_of(parts) -> int:
    if parts.port:
        return parts.port
    return 443 if (parts.scheme or "").lower() == "https" else 80


def _is_private(host: str) -> bool:
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False                     # 域名：交给 DNS，不猜
    return addr.is_private and not addr.is_loopback


# ─────────────────────────────────────────── 改写

def resolve(url: str) -> tuple[str, str]:
    """返回 (可用的 URL, 说明)。说明为空表示没改写。

    只在容器里、且原地址确实连不上时才改写 —— 「先试原样」是关键：
    服务真在别的机器上时，原地址是通的，改写反而会把它指到别处去。
    """
    text = (url or "").strip()
    if not text or not in_container():
        return url, ""
    parts = urlsplit(text if "//" in text else f"//{text}")
    host = parts.hostname or ""
    if not host:
        return url, ""
    port = _port_of(parts)
    gateway = container_gateway()

    # 1) 已经写的是 host.docker.internal / 网关本身：不用动
    if host == GATEWAY_HOST or (gateway and host == gateway):
        return url, ""

    # 2) 127.0.0.1 在容器里指的是容器自己：宿主同端口能连上就改写
    if host in _LOOPBACK:
        if _probe(GATEWAY_HOST, port) and not _probe(host, port):
            return _swap_host(text, parts, GATEWAY_HOST), \
                f"{host}:{port} 在容器里指向容器自身，已改用 {GATEWAY_HOST}"
        return url, ""

    # 3) 公网地址：不探测也不改（探测只会白等，改写更是错的）
    if not _is_private(host):
        return url, ""

    # 4) 私网地址：先试原样，连不上再试网关那头
    if _probe(host, port):
        return url, ""
    if _probe(GATEWAY_HOST, port):
        note = (f"{host}:{port} 在容器内连不上（像是宿主机 LAN 地址），"
                f"已改用 {GATEWAY_HOST}:{port}")
        log.info("地址改写：%s → %s:%d", text, GATEWAY_HOST, port)
        return _swap_host(text, parts, GATEWAY_HOST), note
    return url, ""


def _swap_host(url: str, parts, new_host: str) -> str:
    netloc = new_host if not parts.port else f"{new_host}:{parts.port}"
    return urlunsplit((parts.scheme or "http", netloc, parts.path, parts.query,
                       parts.fragment))


def describe() -> dict:
    """给接口/面板用：当前是不是容器、网关是谁。"""
    return {"container": in_container(), "gateway": container_gateway(),
            "alias": GATEWAY_HOST}
