"""新版看板 templates/index.html 的静态审计。

跟 test_webui_routes.py 同一思路，但对着新模板：
前端用 api() 走 GET、post() 走 POST，两种调用都要抠出来，
再跟 app/web.py 的 do_GET / do_POST 注册表对账。

挂错方法 = 线上 404 + 浏览器一句没头没尾的 JSON.parse 异常。
这类事故肉眼看不出来：Python 测试全绿，JS 语法也没错。

顺带拦括号失衡（整套 JS 解析失败 → 整页不执行 → 「连接中…」永远不动）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from test_webui_script import scan

TEMPLATE = Path(__file__).resolve().parent.parent / "app" / "templates" / "index.html"
WEB_PY = Path(__file__).resolve().parent.parent / "app" / "web.py"


def script_of(html: str) -> str:
    m = re.search(r"<script>(.*?)</script>", html, re.S)
    assert m, "模板里找不到 <script> 块"
    return m.group(1)


def _region(src: str, marker: str) -> str:
    i = src.index(marker)
    j = src.index("\n    def ", i + 10)
    return src[i:j]


def backend_routes() -> dict[str, set[str]]:
    """{"GET": {路径…}, "POST": {路径…}}"""
    src = WEB_PY.read_text(encoding="utf-8")
    out: dict[str, set[str]] = {}
    for method, marker in (("GET", "    def do_GET"), ("POST", "    def do_POST")):
        out[method] = set(re.findall(r'path == "([^"]+)"', _region(src, marker)))
    return out


def normalize(path: str) -> str:
    return path.split("?")[0].rstrip("/") or "/"


def frontend_calls(js: str) -> list[tuple[str, str]]:
    """[(方法, 路径)] —— api() 是 GET，post() 是 POST。"""
    calls: list[tuple[str, str]] = []
    for m in re.finditer(r'(?<![.\w])post\(\s*"([^"]+)"', js):
        calls.append(("POST", m.group(1)))
    for m in re.finditer(r'(?<![.\w])api\(\s*"([^"]+)"', js):
        calls.append(("GET", m.group(1)))
    return calls


@pytest.fixture(scope="module")
def js() -> str:
    return script_of(TEMPLATE.read_text(encoding="utf-8"))


def test_frontend_calls_found(js):
    """审计本身要能抓到东西 —— 抠不出调用说明正则失效了，测试会假绿。"""
    calls = frontend_calls(js)
    assert len(calls) >= 10, f"只抠到 {len(calls)} 个调用，正则可能失效"
    paths = {normalize(p) for _, p in calls}
    for expected in ("/api/tasks", "/api/jobs", "/api/subs", "/api/settings"):
        assert expected in paths, f"没抠到 {expected}，审计正则失效了"


def test_every_frontend_call_matches_backend_method(js):
    """每个前端调用的路径，都要在**它用的那个方法**里注册过。"""
    routes = backend_routes()
    problems: list[str] = []
    for method, path in sorted(set(frontend_calls(js))):
        key = normalize(path)
        if key in routes.get(method, set()):
            continue
        other = "POST" if method == "GET" else "GET"
        if key in routes.get(other, set()):
            problems.append(
                f"{method} {path} → 后端把它注册在 do_{other} 上；"
                f"前端要用 {other}，或者把后端那行挪到 do_{method}"
            )
        else:
            problems.append(f"{method} {path} → 后端两个方法里都没有这条路由")
    assert not problems, "前端路由与后端方法对不上：\n  " + "\n  ".join(problems)


def test_js_still_balanced(js):
    assert scan(js) == [], "改完模板后 JS 括号/字符串失衡"


def test_template_has_no_legacy_leftovers(js):
    """旧版残留的接口早改过名，抄过来就会 404。"""
    for gone in ("subsApi(", "metaApi(", "normApi("):
        assert gone not in js, f"模板里还有旧版调用 {gone}"
