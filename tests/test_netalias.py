"""容器内地址改写：判据、边界、以及「原地址通就不动」这条底线。

为什么不能靠「检测这个 IP 是不是宿主机」：容器里查不到宿主的 LAN 地址
（/proc 只给得出自己的 IP 和默认网关）。所以改成连通性探测 —— 这条测试把
每种情形的判据都钉住，免得日后有人把它「优化」成猜 IP 段。
"""
from __future__ import annotations

import pytest

from app import netalias


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    netalias.clear_cache()
    monkeypatch.setattr(netalias, "in_container", lambda: True)
    monkeypatch.setattr(netalias, "container_gateway", lambda: "172.23.0.1")
    yield
    netalias.clear_cache()


def fake_probe(monkeypatch, reachable: set[tuple[str, int]]):
    """按 (host, port) 指定谁能连通。"""
    monkeypatch.setattr(netalias, "_probe",
                        lambda host, port, timeout=0.8: (host, port) in reachable)


# ─────────────────────────────────────────── 环境判定

def test_in_container_detects_cgroup(monkeypatch, tmp_path):
    monkeypatch.setattr(netalias.os.path, "exists", lambda p: False)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    cgroup = tmp_path / "cgroup"
    cgroup.write_text("12:devices:/docker/abc123\n", encoding="utf-8")
    real_open = open

    def fake_open(path, *a, **kw):
        return real_open(cgroup if path == "/proc/1/cgroup" else path, *a, **kw)

    monkeypatch.setattr("builtins.open", fake_open)
    assert netalias.in_container() is True


def test_gateway_parsed_from_route_table(monkeypatch, tmp_path):
    """Destination=00000000 的那行就是默认路由，Gateway 是十六进制小端。"""
    route = tmp_path / "route"
    route.write_text(
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t010017AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
        "eth0\t000017AC\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n",
        encoding="utf-8")
    real_open = open

    def fake_open(path, *a, **kw):
        return real_open(route if path == "/proc/net/route" else path, *a, **kw)

    monkeypatch.setattr("builtins.open", fake_open)
    assert netalias.container_gateway() == "172.23.0.1"


# ─────────────────────────────────────────── 不改写的三种情形

def test_not_in_container_never_rewrites(monkeypatch):
    monkeypatch.setattr(netalias, "in_container", lambda: False)
    fake_probe(monkeypatch, {("host.docker.internal", 19798)})
    url, note = netalias.resolve("http://192.168.1.170:19798")
    assert url == "http://192.168.1.170:19798" and note == ""


def test_reachable_address_is_left_alone(monkeypatch):
    """服务真在别的机器上（原地址通）：改写反而会指错地方。"""
    fake_probe(monkeypatch, {("192.168.1.50", 19798), ("host.docker.internal", 19798)})
    url, note = netalias.resolve("http://192.168.1.50:19798")
    assert url == "http://192.168.1.50:19798" and note == ""


def test_public_host_is_not_probed(monkeypatch):
    calls = []
    monkeypatch.setattr(netalias, "_probe",
                        lambda host, port, timeout=0.8: calls.append(host) or False)
    url, note = netalias.resolve("https://javbus.example.com")
    assert url == "https://javbus.example.com" and note == ""
    assert calls == [], "公网地址不该探测（白等延迟，改写更是错的）"


# ─────────────────────────────────────────── 改写的三种情形

def test_host_lan_address_falls_back_to_gateway(monkeypatch):
    """就是这个坑：宿主机 LAN 地址在容器里连不上，网关那头通。"""
    fake_probe(monkeypatch, {("host.docker.internal", 19798)})
    url, note = netalias.resolve("http://192.168.1.170:19798")
    assert url == "http://host.docker.internal:19798"
    assert "192.168.1.170:19798" in note and "host.docker.internal" in note


def test_loopback_means_the_container_itself(monkeypatch):
    fake_probe(monkeypatch, {("host.docker.internal", 5244)})
    url, note = netalias.resolve("http://127.0.0.1:5244")
    assert url == "http://host.docker.internal:5244" and "容器自身" in note


def test_gateway_ip_is_left_alone(monkeypatch):
    """填的正好是 docker 网关：本身在容器内可达，没必要换。"""
    fake_probe(monkeypatch, set())
    url, note = netalias.resolve("http://172.23.0.1:19798")
    assert url == "http://172.23.0.1:19798" and note == ""


def test_both_unreachable_keeps_original(monkeypatch):
    """都不通就别改：保留原地址，让后续报错暴露真实原因。"""
    fake_probe(monkeypatch, set())
    url, note = netalias.resolve("http://192.168.1.170:19798")
    assert url == "http://192.168.1.170:19798" and note == ""


def test_preserves_scheme_path_and_port(monkeypatch):
    fake_probe(monkeypatch, {("host.docker.internal", 5244)})
    url, _ = netalias.resolve("https://192.168.1.170:5244/dav/sub?x=1")
    assert url == "https://host.docker.internal:5244/dav/sub?x=1"


def test_default_port_follows_scheme(monkeypatch):
    seen = []
    monkeypatch.setattr(netalias, "_probe",
                        lambda host, port, timeout=0.8: seen.append((host, port)) or False)
    netalias.resolve("https://192.168.1.9/x")
    netalias.resolve("http://192.168.1.9/x")
    assert ("192.168.1.9", 443) in seen and ("192.168.1.9", 80) in seen


def test_describe_shape():
    info = netalias.describe()
    assert set(info) == {"container", "gateway", "alias"}
    assert info["alias"] == "host.docker.internal"
