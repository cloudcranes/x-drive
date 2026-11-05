"""子路由与刷新保持测试。

验收点：
1. 后端 app/web.py 支持 SPA 子路由（/archive, /queue, /subs, /settings, /maint）直接访问返回 webui.html；
2. 前端 buildRouteHash / parseRoute / navigate 正确解析与生成 hash；
3. applyRoute 在刷新/初始加载时依据 hash 恢复目标视图，不强制返回 archive 主界面；
4. showView 切换视图时同步更新 hash，并在前进/后退/刷新时保持。
"""
from __future__ import annotations

import json
import subprocess
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.web import HTML_PATH, SPA_ROUTES, TEMPLATE_PATH, Handler
from tests.test_webui_script import PAGE, script_of


# ────────────────────────────────────────── 1. 后端 SPA 路由
def test_backend_spa_routes_defined():
    for r in ("/", "/index.html", "/archive", "/search", "/queue", "/subs", "/settings", "/maint"):
        assert r in SPA_ROUTES


def _get(path: str, status: bytes = b"200") -> bytes:
    handler = Handler.__new__(Handler)
    handler.client_address = ("127.0.0.1", 12345)
    handler.request_version = "HTTP/1.1"
    handler.command = "GET"
    handler.close_connection = True
    handler.server_version = "BaseHTTP/0.6"
    handler.sys_version = "Python/3.11"
    handler.path = path
    handler.headers = {}
    handler.rfile = BytesIO()
    handler.wfile = BytesIO()
    handler.requestline = f"GET {path} HTTP/1.1"

    with patch.object(Handler, "_authorized", return_value=True):
        handler.do_GET()

    output = handler.wfile.getvalue()
    assert status in output, f"{path} 应回 {status!r}"
    return output


def test_backend_spa_routes_serve_html():
    """SPA 子路由直接访问要送页面。

    新版看板住在 templates/index.html，旧版 webui.html 挪到 /legacy。
    """
    content = TEMPLATE_PATH.read_bytes()
    for route in ("/archive", "/search", "/rows", "/queue", "/subs", "/settings"):
        assert content in _get(route)


def test_backend_legacy_only_views_redirect():
    """维护台和日志只有旧版有 → 302 送 /legacy 对应视图。"""
    for path, view in (("/maint", "maint"), ("/logs", "logs")):
        out = _get(path, status=b"302")
        assert f"/legacy#/{view}".encode() in out


def test_backend_legacy_routes_serve_old_html():
    """旧版页面没删，挪到 /legacy 还能用。"""
    content = HTML_PATH.read_bytes()
    for route in ("/legacy", "/legacy/index.html"):
        assert content in _get(route)


# ────────────────────────────────────────── 2. 前端路由机制 (Node harness)
@pytest.fixture(scope="module")
def node_bin() -> str:
    from shutil import which
    for candidate in ("/var/apps/nodejs_v24/target/bin/node", which("node")):
        if candidate and Path(candidate).is_file():
            return candidate
    pytest.skip("没有 node 环境，跳过前端路由仿真测试")


ROUTER_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');

const htmlPath = process.argv[1];
const scenario = process.argv[2];
const html = fs.readFileSync(htmlPath, 'utf-8');
const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>/);
if (!scriptMatch) {
  console.error("No script tag found");
  process.exit(1);
}
const js = scriptMatch[1];

function createNode(tag) {
  return {
    tagName: tag, children: [], className: "", textContent: "", hidden: false,
    value: "", disabled: false, dataset: {}, listeners: {},
    classList: {
      _classes: new Set(),
      add(c) { this._classes.add(c); },
      remove(c) { this._classes.delete(c); },
      contains(c) { return this._classes.has(c); },
      toggle(c, on) { on ? this._classes.add(c) : this._classes.delete(c); }
    },
    append(...xs) { this.children.push(...xs); },
    appendChild(x) { this.children.push(x); },
    addEventListener(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); },
    setAttribute() {}, remove() {},
    get text() { return this._t || ""; },
    set text(v) { this._t = v; this.children = []; },
    scrollIntoView() {},
  };
}

const elements = {};
function getEl(id) {
  if (!elements[id]) elements[id] = createNode("div");
  return elements[id];
}

const windowListeners = {};
const mockWindow = {
  addEventListener(ev, fn) {
    (windowListeners[ev] = windowListeners[ev] || []).push(fn);
  },
  location: { hash: "", pathname: "/", search: "", href: "http://127.0.0.1:18091/" },
  history: {
    replaceState(state, title, url) {
      mockWindow.location.hash = url;
    },
    pushState(state, title, url) {
      mockWindow.location.hash = url;
    }
  },
  innerWidth: 1200,
};

const ctx = {
  console,
  window: mockWindow,
  globalThis: null,
  document: {
    getElementById: getEl,
    createElement: createNode,
    querySelector: (sel) => createNode("div"),
    querySelectorAll: (sel) => [],
    addEventListener() {},
    documentElement: createNode("html"),
    body: createNode("body"),
  },
  localStorage: { getItem: () => null, setItem() {} },
  location: mockWindow.location,
  history: mockWindow.history,
  navigator: { language: "zh" },
  AbortController, setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {},
  fetch: () => Promise.resolve({
    ok: true, status: 200, statusText: "OK",
    json: async () => ({ ok: true, stats: { tasks: 0, files: 0 }, tasks: [] }),
    text: async () => "{}"
  }),
  alert: () => {}, confirm: () => true, prompt: () => "",
  URLSearchParams,
};
ctx.globalThis = ctx;
mockWindow.document = ctx.document;

if (scenario === "queue") {
  mockWindow.location.hash = "#/queue";
} else if (scenario === "subs") {
  mockWindow.location.hash = "#/subs";
} else if (scenario === "settings") {
  mockWindow.location.hash = "#/settings?tab=%E5%AD%98%E5%82%A8";
} else if (scenario === "maint") {
  mockWindow.location.hash = "#/maint?tab=name";
} else if (scenario === "search") {
  mockWindow.location.hash = "#/search?q=MIMK";
} else if (scenario === "archive_filter") {
  mockWindow.location.hash = "#/archive?series=MIMK&dim=series";
} else if (scenario === "path_fallback") {
  mockWindow.location.hash = "";
  mockWindow.location.pathname = "/queue";
}

vm.createContext(ctx);
vm.runInContext(js, ctx);

// 读取运行后状态
const out = vm.runInContext(`({
  currentView,
  hash: window.location.hash,
  settingsTab,
  parsed: parseRoute(),
  buildArchive: buildRouteHash("archive"),
  buildSearch: buildRouteHash("search", {q: "MIMK"}),
  buildQueue: buildRouteHash("queue"),
  buildSubs: buildRouteHash("subs"),
  buildSettings: buildRouteHash("settings", {tab: "存储"}),
  buildMaint: buildRouteHash("maint", {tab: "name"}),
})`, ctx);

if (scenario === "nav_test") {
  vm.runInContext("showView('subs')", ctx);
  out.afterShowSubs = vm.runInContext("({ currentView, hash: window.location.hash })", ctx);
  vm.runInContext("showView('settings')", ctx);
  out.afterShowSettings = vm.runInContext("({ currentView, hash: window.location.hash })", ctx);
  vm.runInContext("showView('archive')", ctx);
  out.afterShowArchive = vm.runInContext("({ currentView, hash: window.location.hash })", ctx);
}

console.log(JSON.stringify(out));
"""


def _run_harness(node_bin: str, scenario: str) -> dict:
    proc = subprocess.run(
        [node_bin, "-e", ROUTER_HARNESS, str(PAGE), scenario],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


def test_router_hash_builders(node_bin):
    data = _run_harness(node_bin, "default")
    assert data["buildArchive"] in ("#/", "#/archive")
    assert data["buildSearch"] == "#/search?q=MIMK"
    assert data["buildQueue"] == "#/queue"
    assert data["buildSubs"] == "#/subs"
    assert "tab=" in data["buildSettings"]
    assert "tab=name" in data["buildMaint"]


def test_initial_load_restores_queue(node_bin):
    data = _run_harness(node_bin, "queue")
    assert data["currentView"] == "queue", "刷新处于 #/queue 时应保持任务队列视图"
    assert data["parsed"]["view"] == "queue"


def test_initial_load_restores_subs(node_bin):
    data = _run_harness(node_bin, "subs")
    assert data["currentView"] == "subs", "刷新处于 #/subs 时应保持订阅视图"
    assert data["parsed"]["view"] == "subs"


def test_initial_load_restores_settings_tab(node_bin):
    data = _run_harness(node_bin, "settings")
    assert data["currentView"] == "settings", "刷新处于 #/settings 时应保持设置视图"
    assert data["parsed"]["params"]["tab"] == "存储"


def test_initial_load_restores_maint_tab(node_bin):
    data = _run_harness(node_bin, "maint")
    assert data["currentView"] == "maint", "刷新处于 #/maint 时应保持维护台视图"
    assert data["parsed"]["params"]["tab"] == "name"


def test_initial_load_restores_search(node_bin):
    data = _run_harness(node_bin, "search")
    assert data["currentView"] == "search", "刷新处于 #/search?q= 时应保持搜索视图"
    assert data["parsed"]["params"]["q"] == "MIMK"


def test_initial_load_restores_archive_filter(node_bin):
    data = _run_harness(node_bin, "archive_filter")
    assert data["currentView"] == "archive"
    assert data["parsed"]["params"]["series"] == "MIMK"
    assert data["parsed"]["params"]["dim"] == "series"


def test_initial_load_path_fallback(node_bin):
    data = _run_harness(node_bin, "path_fallback")
    assert data["currentView"] == "queue", "直接访问 /queue 路径时应自动识别并定位到 queue"


def test_show_view_syncs_hash(node_bin):
    data = _run_harness(node_bin, "nav_test")
    assert data["afterShowSubs"]["currentView"] == "subs"
    assert data["afterShowSubs"]["hash"] == "#/subs"
    assert data["afterShowSettings"]["currentView"] == "settings"
    assert "#/settings" in data["afterShowSettings"]["hash"]
    assert data["afterShowArchive"]["currentView"] == "archive"
    assert data["afterShowArchive"]["hash"] in ("#/", "#/archive")
