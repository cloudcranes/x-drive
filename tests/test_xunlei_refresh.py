"""迅雷凭据的主动续期：在 access_token 过期前就续，不等「用到才续」。

**为什么必须主动**：续期以前只在「用到官方 API」时触发（_get 里调 auto_refresh）。
而直链走 CD2 的 gRPC、云添加落地判定走 CD2 目录指纹 —— 官方 API 可能一整天
没人碰。等到真要用（取直链）时才续，那时 access_token 早就过期了；而
refresh_token 那时可能已被**浏览器**轮换掉（旧值作废），第一次续期就
invalid_grant。

实测（2026-10-25）：access_token 09:18 过期，19:49 才第一次触发续期 ——
而 refresh_token 早被浏览器轮换掉了。
"""
from __future__ import annotations

import time

import pytest

from app import xunleipan


def _token(exp_in: float) -> str:
    """造一个 exp 在 exp_in 秒后的假 JWT（只要能解出 exp 就行）。"""
    import base64
    import json
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(time.time() + exp_in)}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{payload}.sig"


def test_needs_refresh_uses_margin(tmp_path, monkeypatch):
    """快过期（margin 内）就要续。"""
    monkeypatch.setattr(xunleipan, "_setting",
                        lambda key: {"XLPAN_TOKEN": _token(600),
                                     "XLPAN_REFRESH_TOKEN": "a1.x"}.get(key, ""))
    assert xunleipan._needs_refresh(margin=3600) is True, "1 小时后过期 → 该续"
    assert xunleipan._needs_refresh(margin=300) is False, "10 分钟后过期 → 还不急"


def test_refresh_loop_refreshes_before_expiry(tmp_path, monkeypatch):
    """循环会在过期前主动续期 —— 不等有人用到官方 API。"""
    calls = []

    monkeypatch.setattr(xunleipan, "_setting",
                        lambda key: {"XLPAN_TOKEN": _token(1800),
                                     "XLPAN_REFRESH_TOKEN": "a1.x"}.get(key, ""))
    monkeypatch.setattr(xunleipan, "auto_refresh",
                        lambda **kw: calls.append("refresh") or {"ok": True, "skipped": False,
                                                               "expires_in": 43200})
    # 跑一轮就退出：sleep 抛 StopIteration
    slept = []

    def fake_sleep(n):
        slept.append(n)
        raise StopIteration

    with pytest.raises(StopIteration):
        xunleipan.refresh_loop(interval=1800, lead=3600, sleep=fake_sleep)
    assert calls == ["refresh"], "过期前就该续一次"
    assert slept == [1800]


def test_refresh_loop_skips_when_token_fresh(tmp_path, monkeypatch):
    """token 还早得很 → 不续（省得白打服务端）。"""
    calls = []
    monkeypatch.setattr(xunleipan, "_setting",
                        lambda key: {"XLPAN_TOKEN": _token(36000),
                                     "XLPAN_REFRESH_TOKEN": "a1.x"}.get(key, ""))
    monkeypatch.setattr(xunleipan, "auto_refresh",
                        lambda **kw: calls.append("refresh") or {"ok": True})

    def fake_sleep(n):
        raise StopIteration

    with pytest.raises(StopIteration):
        xunleipan.refresh_loop(interval=1800, lead=3600, sleep=fake_sleep)
    assert calls == [], "没到续期点就不该续"


def test_refresh_loop_survives_failed_refresh(tmp_path, monkeypatch):
    """续期失败不致命：记一笔，等下一轮再试（不能整个循环挂掉）。"""
    calls = []
    monkeypatch.setattr(xunleipan, "_setting",
                        lambda key: {"XLPAN_TOKEN": _token(600),
                                     "XLPAN_REFRESH_TOKEN": "a1.x"}.get(key, ""))

    def boom(**kw):
        calls.append("refresh")
        raise RuntimeError("invalid_grant")
    monkeypatch.setattr(xunleipan, "auto_refresh", boom)

    slept = []

    def fake_sleep(n):
        slept.append(n)
        if len(slept) >= 3:
            raise StopIteration

    with pytest.raises(StopIteration):
        xunleipan.refresh_loop(interval=1800, lead=3600, sleep=fake_sleep)
    assert len(calls) == 3, "失败后还要继续下一轮"
