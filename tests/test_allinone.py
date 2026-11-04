"""单容器入口：网页跑后台线程、bot 占主线程，两半谁挂了容器都得退出。

合并的关键风险不是「能不能跑起来」，而是「跑起来之后的收口」：
  · bot 的 run_polling 必须在主线程 —— python-telegram-bot 在启动时注册
    SIGINT/SIGTERM/SIGABRT，信号处理器只能在主线程装；
  · 网页线程退出必须让整个进程退出（os._exit），抛 SystemExit 只会收掉
    那一条线程，留下「bot 还在轮询、界面没了」的半死状态；
  · healthcheck 要同时看两半，只看一边等于没看。

这些都不需要真连 Telegram / 真起 HTTP 服务，用桩把边界钉住即可；
真起容器的验证在部署那一步做。
"""
from __future__ import annotations

import ast
import json
import threading
import urllib.error
from pathlib import Path

import pytest

from app import allinone

ROOT = Path(__file__).resolve().parents[1]


class _Resp:
    """最小可用的 urlopen 返回：只给 read()。"""

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _patch_opener(monkeypatch, handler) -> None:
    """替换 urllib 的 opener：handler(url, timeout) 返回 _Resp 或抛异常。"""
    class _Opener:
        def open(self, url, timeout=None):  # noqa: A003 (跟随 urlopen 的签名)
            return handler(url, timeout)

    monkeypatch.setattr(allinone.urllib.request, "build_opener",
                        lambda *a, **kw: _Opener())


# ─────────────────────────────────────────── 网页探活

def test_probe_web_ok(monkeypatch):
    seen = {}

    def handler(url, timeout):
        seen["url"] = url
        seen["timeout"] = timeout
        return _Resp({"ok": True, "pending": 2, "tasks": 7, "files": 31})

    _patch_opener(monkeypatch, handler)
    monkeypatch.setattr(allinone.webmod, "PORT", 18091)
    monkeypatch.setattr(allinone.webmod, "BIND", "0.0.0.0")

    ok, why = allinone._probe_web()
    assert ok is True
    # 监听 0.0.0.0 时探回环，不能探 0.0.0.0
    assert seen["url"] == "http://127.0.0.1:18091/api/health"
    assert "待办 2" in why and "归档 7" in why and "文件 31" in why


def test_probe_web_uses_bound_address(monkeypatch):
    """绑了具体网卡就探那个地址（回环上没有监听，探 127.0.0.1 会假红）。"""
    seen = {}

    def handler(url, timeout):
        seen["url"] = url
        return _Resp({"ok": True})

    _patch_opener(monkeypatch, handler)
    monkeypatch.setattr(allinone.webmod, "PORT", 8080)
    monkeypatch.setattr(allinone.webmod, "BIND", "172.23.0.5")

    ok, _ = allinone._probe_web()
    assert ok is True
    assert seen["url"] == "http://172.23.0.5:8080/api/health"


def test_probe_web_unreachable_is_false(monkeypatch):
    def handler(url, timeout):
        raise urllib.error.URLError("Connection refused")

    _patch_opener(monkeypatch, handler)
    ok, why = allinone._probe_web()
    assert ok is False
    assert "网页不可达" in why and "URLError" in why


def test_probe_web_not_ok_payload_is_false(monkeypatch):
    """服务在、但自己说 ok=false（state.json 解析不了）—— 这也是不健康。"""
    _patch_opener(monkeypatch, lambda url, timeout: _Resp({"ok": False}))
    ok, why = allinone._probe_web()
    assert ok is False
    assert "网页报告异常" in why


# ─────────────────────────────────────────── 健康检查要两半都在

def test_health_requires_both_halves(monkeypatch, capsys):
    async def bot_ok(cfg):
        print("自检 通过")
        return True

    monkeypatch.setattr(allinone.botmain, "self_check", bot_ok)

    _patch_opener(monkeypatch, lambda url, timeout: _Resp({"ok": True}))
    assert allinone.asyncio.run(allinone._health(object())) is True

    # bot 侧挂了，网页再好也不能算健康
    async def bot_bad(cfg):
        return False

    monkeypatch.setattr(allinone.botmain, "self_check", bot_bad)
    assert allinone.asyncio.run(allinone._health(object())) is False

    # 反过来：网页挂了，bot 自检通过也不算
    monkeypatch.setattr(allinone.botmain, "self_check", bot_ok)

    def boom(url, timeout):
        raise urllib.error.URLError("refused")

    _patch_opener(monkeypatch, boom)
    assert allinone.asyncio.run(allinone._health(object())) is False

    out = capsys.readouterr().out
    assert "网页:" in out
    assert "容器健康检查 未通过" in out


# ─────────────────────────────────────────── 入口分派

@pytest.mark.parametrize("flag", ["--check", "--health"])
def test_main_dispatches_check_flags(monkeypatch, flag):
    """带自检开关就只自检、绝不去起网页或 bot。"""
    called = {}

    async def fake_self_check(cfg):
        called["kind"] = "check"
        called["cfg"] = cfg
        return True

    async def fake_health(cfg):
        called["kind"] = "health"
        called["cfg"] = cfg
        return True

    monkeypatch.setattr(allinone.botmain, "self_check", fake_self_check)
    monkeypatch.setattr(allinone, "_health", fake_health)
    monkeypatch.setattr(allinone, "main", allinone.main)     # 保持原函数
    monkeypatch.setattr(allinone.sys, "argv", ["app.allinone", flag])
    # 真起线程/真轮询就是事故，直接判失败
    monkeypatch.setattr(allinone, "_start_web_thread",
                        lambda: pytest.fail("自检分支不该起网页"))
    monkeypatch.setattr(allinone.botmain, "main",
                        lambda: pytest.fail("自检分支不该起 bot"))

    with pytest.raises(SystemExit) as exc:
        allinone.main()
    assert exc.value.code == 0
    assert called["kind"] == ("health" if flag == "--health" else "check")


def test_main_check_flag_exits_nonzero_when_unhealthy(monkeypatch):
    async def fake_health(cfg):
        return False

    monkeypatch.setattr(allinone, "_health", fake_health)
    monkeypatch.setattr(allinone.sys, "argv", ["app.allinone", "--health"])
    with pytest.raises(SystemExit) as exc:
        allinone.main()
    assert exc.value.code == 1


def test_main_starts_web_before_bot(monkeypatch):
    """网页必须先起：bot 的 run_polling 一进去就不返回。"""
    order: list[str] = []

    class _Thr:
        name = "web-main"

    monkeypatch.setattr(allinone.sys, "argv", ["app.allinone"])
    monkeypatch.setattr(allinone, "_start_web_thread",
                        lambda: order.append("web") or _Thr())
    monkeypatch.setattr(allinone.botmain, "main",
                        lambda: order.append("bot"))

    allinone.main()
    assert order == ["web", "bot"]


# ─────────────────────────────────────────── 网页线程收口

def test_web_thread_exit_kills_process(monkeypatch):
    """网页线程退出必须结束整个进程，而不是只收掉自己。"""
    died: list[int] = []
    monkeypatch.setattr(allinone, "_die", died.append)
    monkeypatch.setattr(allinone.webmod, "main", lambda: None)

    allinone._run_web()
    assert died and died[-1] == 0, "网页正常返回也要让容器退出（docker 会重启）"


def test_web_thread_exception_kills_process_with_failure(monkeypatch):
    died: list[int] = []
    monkeypatch.setattr(allinone, "_die", died.append)

    def boom() -> None:
        raise RuntimeError("serve() 绑定端口失败")

    monkeypatch.setattr(allinone.webmod, "main", boom)

    allinone._run_web()
    assert died == [1]


def test_web_thread_is_daemon_and_named(monkeypatch):
    """非 daemon 线程会让 docker stop 卡到 SIGKILL。"""
    started = {}

    def fake_web_main() -> None:
        started["name"] = threading.current_thread().name
        started["daemon"] = threading.current_thread().daemon
        started["ran"] = True

    monkeypatch.setattr(allinone.webmod, "main", fake_web_main)
    # 别让收口把 pytest 带走
    monkeypatch.setattr(allinone, "_die", lambda code: started.update(died=code))

    thread = allinone._start_web_thread()
    thread.join(timeout=5)
    assert started["ran"] is True
    assert started["daemon"] is True
    assert started["name"] == "web-main"


# ─────────────────────────────────────────── 静态：入口与部署文件对得上

def test_allinone_guard_is_last():
    """和 app/web.py 同一条纪律：guard 之后不许再有定义。"""
    tree = ast.parse((ROOT / "app" / "allinone.py").read_text(encoding="utf-8"))
    idx = next(i for i, n in enumerate(tree.body)
               if isinstance(n, ast.If) and "__main__" in ast.unparse(n.test))
    trailing = [n.name for n in tree.body[idx + 1:]
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    assert trailing == [], f"guard 之后还有定义：{trailing}"


def test_compose_is_a_single_service():
    """compose 只剩一个 service，且入口/健康检查都指向 allinone。"""
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    services = [ln for ln in text.splitlines()
                if ln.startswith("  ") and not ln.startswith("    ")
                and ln.rstrip().endswith(":")]
    assert services == ["  web:"], f"service 列表应为单个 web：{services}"
    assert '"app.allinone"' in text
    assert '"--health"' in text
    # fake-IP 型 DNS 下这条是必需的，合并时最容易顺手删掉
    assert "host.docker.internal:host-gateway" in text


def test_dockerfile_default_command_is_allinone():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    cmds = [ln for ln in text.splitlines() if ln.startswith("CMD ")]
    assert cmds and "app.allinone" in cmds[-1], cmds


def test_docs_reference_the_shipped_entrypoint():
    """文档/脚本里让人敲的自检命令，模块必须是真实存在的那个。"""
    for rel in ("README.md", "scripts/bootstrap.sh", "scripts/verify.sh"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "app.allinone" in text, f"{rel} 没提到单容器入口"
        assert "app.main --check" not in text, f"{rel} 还留着旧的自检命令"


def test_compose_override_targets_the_single_service():
    """override 里的 service 名必须与正式文件一致，否则 docker 会报未定义 service。

    override 是部署现场才有的文件（host 网络 + 自定义端口），不进仓库，
    所以不在时跳过，而不是把整份测试判红。
    """
    path = ROOT / "docker-compose.override.yml"
    if not path.exists():
        pytest.skip("本机没有 docker-compose.override.yml（部署现场才有的文件）")
    text = path.read_text(encoding="utf-8")
    names = [ln.strip()[:-1] for ln in text.splitlines()
             if ln.startswith("  ") and not ln.startswith("    ")
             and ln.rstrip().endswith(":")]
    assert names == ["web"], names
    assert "--health" in text
