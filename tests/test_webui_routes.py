"""前端调的路由，必须在后端**对应的方法**里注册过。

起因：/api/xlp/credentials 被误挂在 do_GET 上，而前端用 POST 打它。
do_POST 落不到那条分支 → 走到末尾 self._send(404, b"not found", "text/plain")
→ 浏览器 r.json() 抛「JSON.parse: unexpected keyword at line 1 column 1」。
页面只给这句裸异常，看不出真正原因是「路由挂在错的方法上」。

这类事故纯靠肉眼看不出来：Python 测试全绿（后端函数本身没问题），
JS 静态扫描也全绿（语法没问题）。只有把**前端用的方法**和
**后端注册的方法**对着核，才能拦住。

所以这里做一次全量审计：把 webui.html 里所有 fetch/subsApi 调用连同方法
一起抠出来，跟 app/web.py 的 do_GET / do_POST 注册表对账。

规则：
  · subsApi(path)          → GET
  · subsApi(path, body)    → POST
  · fetch(path, {method:"POST"...}) → POST
  · fetch(path, {...}) 没写 method → GET
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from test_webui_script import PAGE, scan, script_of

WEB_PY = Path(__file__).resolve().parent.parent / "app" / "web.py"


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


def frontend_calls(js: str) -> list[tuple[str, str]]:
    """[(方法, 路径)] —— 抠出前端所有后端调用及其 HTTP 方法。"""
    calls: list[tuple[str, str]] = []

    # 两个通用 JSON 包装器都以有无 body 决定方法；normApi 则始终 POST。
    for m in re.finditer(r'(?:subsApi|metaApi)\(\s*"([^"]+)"\s*(,)?', js):
        calls.append(("POST" if m.group(2) else "GET", m.group(1)))
    for m in re.finditer(r'normApi\(\s*"([^"]+)"', js):
        calls.append(("POST", m.group(1)))

    # fetch(BASE + "path", {method:"POST" ...})
    for m in re.finditer(
        r'fetch\(\s*BASE \+ "([^"]+)"\s*,\s*(?:authInit\()?\s*\{\s*method:\s*"POST"',
        js, re.S,
    ):
        calls.append(("POST", m.group(1)))

    # fetch(BASE + "path", { ... }) —— 没写 method 就是 GET
    for m in re.finditer(
        r'fetch\(\s*BASE \+ "([^"]+)"\s*,\s*(?:authInit\()?\s*\{(?![\s]*method:\s*"POST")',
        js, re.S,
    ):
        calls.append(("GET", m.group(1)))

    return calls


def normalize(path: str) -> str:
    return path.split("?")[0].rstrip("/") or "/"


@pytest.fixture(scope="module")
def js() -> str:
    return script_of(PAGE.read_text(encoding="utf-8"))


def test_frontend_calls_found(js):
    """审计本身要能抓到东西 —— 抠不出调用说明正则失效了，测试会假绿。"""
    calls = frontend_calls(js)
    assert len(calls) >= 20, f"只抠到 {len(calls)} 个调用，正则可能失效"
    paths = {normalize(p) for _, p in calls}
    for expected in ("/api/settings", "/api/tasks", "/api/jobs", "/api/storage"):
        assert expected in paths, f"没抠到 {expected}，审计正则失效了"


def test_every_frontend_call_matches_backend_method(js):
    """每个前端调用的路径，都要在**它用的那个方法**里注册过。

    挂错方法 = 线上 404 + 浏览器报一句没头没尾的 JSON.parse 异常。
    """
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
                f"前端要用 {other}，或者把后端那行挪到 do_{method}")
        else:
            problems.append(f"{method} {path} → 后端两个方法里都没有这条路由")
    assert not problems, "前端路由与后端方法对不上：\n  " + "\n  ".join(problems)


def test_wrapped_calls_are_scanned_with_method():
    assert frontend_calls('metaApi("/status"); metaApi("/sync", {code: "A"}); '
                          'subsApi("/subs"); normApi("/normalize");') == [
        ("GET", "/status"), ("POST", "/sync"), ("GET", "/subs"), ("POST", "/normalize")]


def test_metadata_routes_are_included(js):
    calls = set(frontend_calls(js))
    for path in ("/api/scrape/status", "/api/scrape/preview", "/api/scrape/sync",
                 "/api/archive/verify", "/api/archive/dups"):
        assert ("POST", path) in calls


def test_meta_api_uses_read_json(js):
    body = js[js.index("async function metaApi"):js.index("function renderMetaHealth")]
    assert "readJson(r, path)" in body
    assert "r.json()" not in body


def test_xlp_credentials_is_post(js):
    """踩过的那条：/api/xlp/credentials 之前挂在 do_GET 上。

    前端 POST 过去 → do_POST 找不到 → 404 text/plain →
    「JSON.parse: unexpected keyword at line 1 column 1」。
    """
    routes = backend_routes()
    assert "/api/xlp/credentials" in routes["POST"], \
        "/api/xlp/credentials 必须在 do_POST 里（前端用 POST 提交凭据）"
    assert "/api/xlp/credentials" not in routes["GET"], \
        "它不该同时挂在 do_GET 上：GET 分支读不到请求体，粘什么都会被当空"
    assert 'subsApi("/api/xlp/credentials", {text:' in js, \
        "前端应把凭据文本 POST 过去"


def test_read_json_reports_real_error(js):
    """响应不是 JSON 时，要报出状态码和正文，别只抛裸的 JSON.parse 异常。"""
    assert "function readJson" in js, "缺少 readJson"
    body = js[js.index("async function readJson"):js.index("async function readJson") + 900]
    assert "r.status" in body, "readJson 要带上 HTTP 状态码"
    assert "r.text()" in body, "readJson 要先取文本再判能不能解析"
    assert "JSON.parse" in body
    # 裸的 await r.json()（没有 .catch / readJson 兜底）不许留在关键路径上
    bare = re.findall(r'await r\d?\.json\(\)(?!\.catch)', js)
    assert not bare, f"还有 {len(bare)} 处裸的 r.json()，非 JSON 响应会抛裸异常"


def test_subs_api_uses_read_json(js):
    """subsApi 是绝大多数接口的入口，它必须走 readJson。"""
    m = re.search(r"async function subsApi\([^)]*\)\s*\{(.*?)\n\}", js, re.S)
    assert m, "subsApi 未找到"
    body = m.group(1)
    assert "readJson(" in body, "subsApi 应走 readJson"
    assert "r.json()" not in body, "subsApi 里不该再有裸 r.json()"


def test_js_still_balanced(js):
    assert scan(js) == [], "改完前端后 JS 括号/字符串失衡"


def test_xlp_row_has_three_paste_boxes(js):
    """三个粘贴框：凭据 / deviceid / captcha。

    captcha 那一格是后来加的 —— 官方只认浏览器签的那枚（带设备签名），
    程序自己签的会被判 no client info found，所以只能手工粘。
    """
    body = js[js.index("function xlpAuthRow"):js.index("function cloudStatusRow")]
    assert body.count('el("input", "xlp-paste")') == 3, "应有三个粘贴框"
    for kind in ('"cred"', '"device"', '"captcha"'):
        assert kind in body, f"缺少 {kind} 这一类的提交"
    assert "填入 captcha" in body, "缺少「填入 captcha」按钮"
    assert "captcha_{clientid}" in body, "提示里要说明从哪取 captcha"
