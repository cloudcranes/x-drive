"""生产入口 `python -m app.web` 的加载路径。

docker-compose 就是这么起的：模块以 __main__ 身份执行，`if __name__ == "__main__"`
那一刻就把 main() 跑起来。只要这个 guard 写在中途，后面定义的路由（刮削、校验、
备份、恢复、日报）在模块里就不存在，访问即 NameError —— 光 import 测不出来，
因为 import 不会执行 guard。

所以这里真的起一次 `python -m app.web` 子进程：端口用临时的，一旦服务应答就把
GET /api/scrape/status 打一遍（这条路由背后正是 scrape_status），再收掉进程。
**不**去碰真实迅雷 / MetaTube / Drive：刮削状态接口在未配置时会直接返回
「未配置 METATUBE_URL」，不会外呼。
"""
import ast
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "app" / "web.py"

# 曾经定义在 main() 之后、guard 一提前就会「消失」的那批函数
LATE = ["scrape_status", "scrape_sync", "verify_archive", "backup_now",
        "restore_check", "restore_apply", "daily_digest"]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_http(port: int, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def test_module_entry_serves_late_routes(tmp_path):
    """真跑 `python -m app.web`：服务能起来，且背后在 main() 之后定义的路由可用。"""
    port = _free_port()
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": str(ROOT),
        "WEB_PORT": str(port),
        "WEB_BIND": "127.0.0.1",
        "WEB_AUTH": "",
        "STATE_PATH": str(tmp_path / "state.json"),
        "JOBS_DB": str(tmp_path / "jobs.db"),
        "METRICS_PATH": str(tmp_path / "m.json"),
        "PREVIEW_DB": str(tmp_path / "preview.json"),
        "METATUBE_URL": "",              # 未配置：状态接口只回「未配置」，不外呼
        "SUBS_PATH": str(tmp_path / "subs.json"),
        "BACKLOG_PATH": str(tmp_path / "backlog.json"),
        "STATE_DIR": str(tmp_path / "work"),
        "TMP_ROOT": str(tmp_path / "dl"),
        "SUBS_INTERVAL": "3600",
    })
    proc = subprocess.Popen([sys.executable, "-m", "app.web"], cwd=str(ROOT), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        assert _wait_http(port), "`python -m app.web` 没能起来"

        def get(path: str, method: str = "GET") -> tuple[int, dict]:
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                         data=b"{}" if method == "POST" else None)
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return resp.status, json.loads(resp.read().decode())
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode(errors="replace")
                try:
                    return exc.code, json.loads(raw)
                except json.JSONDecodeError:
                    return exc.code, {"_raw": raw[:300]}

        # 这几条都在 main() 之后定义；guard 若提前，路由分发时就是 NameError
        # （健康检查 500 或连接直接断）—— 所以断言 200/ok 是「顺序对了」的直接证据
        try:
            status, health = get("/api/health")
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            raise AssertionError(f"/api/health 拿不到响应（main 之后定义的路由不可用）：{exc}")
        assert status == 200 and health.get("ok") is True, health
        status, scrape = get("/api/scrape/status", "POST")
        # 未配置 MetaTube 时 scrape_status() 直接返回「未配置」，不外呼
        assert status == 200, scrape
        text = json.dumps(scrape, ensure_ascii=False)
        assert "未配置 METATUBE_URL" in text, scrape
        assert "NameError" not in text, scrape
    finally:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
    assert "NameError" not in (out or ""), f"启动过程中出现 NameError：\n{out}"


def test_main_guard_is_last_and_only_statement():
    """静态检查：文件里只许有一处 guard，且它必须是最后一条语句。"""
    tree = ast.parse(WEB.read_text())
    guards = [n for n in tree.body
              if isinstance(n, ast.If) and "__main__" in ast.unparse(n.test)]
    assert len(guards) == 1, "只应有一处 __main__ guard"
    assert tree.body[-1] is guards[0], "guard 必须是文件最后一条语句"


def test_no_definition_after_main_guard():
    """guard 之后不许再有 def/class —— 那正是当初 NameError 的成因。"""
    tree = ast.parse(WEB.read_text())
    idx = next(i for i, n in enumerate(tree.body)
               if isinstance(n, ast.If) and "__main__" in ast.unparse(n.test))
    trailing = [n.name for n in tree.body[idx + 1:]
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    assert trailing == [], f"guard 之后还有定义：{trailing}"


def test_main_definition_is_last_function():
    """main() 自身也得排在所有函数之后：模块级线性求值，提前断言就拿到 None。"""
    tree = ast.parse(WEB.read_text())
    names = [n.name for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert names[-1] == "main", f"main 必须是最后一个函数定义，实际是 {names[-1]}"


def test_late_functions_are_defined_before_main_definition():
    """所有 LATE 里的函数，定义行号都必须小于 main() 的定义行号。"""
    tree = ast.parse(WEB.read_text())
    lines = {n.name: n.lineno for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    main_line = lines["main"]
    late_lines = {n: lines[n] for n in LATE if n in lines}
    assert set(late_lines) == set(LATE), f"缺少定义：{set(LATE) - set(late_lines)}"
    too_late = {n: ln for n, ln in late_lines.items() if ln > main_line}
    assert not too_late, f"这些函数定义在 main() 之后：{too_late}"
