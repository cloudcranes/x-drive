"""待办面板的前端行为验证：node vm + 假 DOM，真 await 前端函数。

后端早就把每条待办翻译成人话了（why：「多少分钟后再查」「任务 downloading：xxx」，
约 app/web.py:1355 backlog_view），但前端从来没读过 data.backlog —— 用户只看到
订阅卡片上一行数字，看不出卡在哪、也动不了它。

所以这里不满足于「页面上有这些字符串」：把 <script> 抽出来放进 node vm，
给一个只满足用到的接口的假 DOM，真的调 renderBacklog / backlogAction，
观察**实际渲染出的行**与**实际发出的请求载荷**。
没有浏览器、没有后端：fetch 是桩，只记录它被喂了什么。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parent.parent / "app" / "webui.html"

# 假 DOM：够这些函数用就行
DOM = r"""
function node(tag) {
  return {
    tagName: tag, children: [], className: "", textContent: "", hidden: false,
    value: "", disabled: false, dataset: {}, listeners: {},
    classList: { add(){}, remove(){}, contains(){ return false; } },
    append(...xs) { this.children.push(...xs); },
    appendChild(x) { this.children.push(x); },
    addEventListener(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); },
    setAttribute() {}, remove() {},
    get text() { return this._t || ""; },
    set text(v) { this._t = v; this.children = []; },
  };
}
const els = {};
const get = (id) => (els[id] = els[id] || node("div"));
const ctx = {
  console,
  document: {
    getElementById: get, createElement: node,
    querySelector: () => node("div"), querySelectorAll: () => [],
    addEventListener() {}, documentElement: node("html"), body: node("body"),
  },
  localStorage: { getItem: () => null, setItem() {} },
  location: { hash: "", search: "", href: "http://127.0.0.1:8080/",
              protocol: "http:", host: "127.0.0.1:8080" },
  navigator: { language: "zh" },
  AbortController, setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {},
  fetch: () => Promise.resolve({ ok: true, status: 200, statusText: "OK",
    json: async () => ({}), text: async () => "{}" }),
  alert: () => {}, confirm: () => true,
};
ctx.window = ctx; ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(src + "\n;globalThis.__probe = () => ({renderBacklog, backlogAction, "
  + "BACKLOG_STATE, subsApi});", ctx);
const P = ctx.__probe();
ctx.renderBacklog = P.renderBacklog;
ctx.backlogAction = P.backlogAction;
ctx.BACKLOG_STATE = P.BACKLOG_STATE;
ctx.subsApi = P.subsApi;
ctx.el = (tag, cls, text) => {
  const n = node(tag);
  n.className = cls || ""; n.textContent = text == null ? "" : text;
  return n;
};
"""

SCRIPT_HEAD = r"""
const fs = require("fs"), vm = require("vm");
const src = fs.readFileSync(process.argv[2], "utf8")
  .match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];
"""


def _run(node_bin, body: str, page: Path) -> dict:
    """把 DOM 脚手架与场景脚本拼起来跑，返回它打印的 JSON。"""
    script = SCRIPT_HEAD + DOM + body
    tmp = page.parent / ".backlog-harness.js"
    tmp.write_text(script, encoding="utf-8")
    try:
        proc = subprocess.run([node_bin, str(tmp), str(page)],
                              capture_output=True, text=True, timeout=120)
    finally:
        tmp.unlink(missing_ok=True)
    assert proc.returncode == 0, f"harness 失败：{proc.stderr[:1200]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def node_bin():
    exe = shutil.which("node")
    if not exe:
        pytest.skip("没有 node，跳过前端行为验证")
    return exe


# ─────────────────────────────────────────── 渲染

RENDER_BODY = r"""
const out = {};
const ITEMS = [
  {sub: "javbus:actor:ABC", code: "SSIS-123", state: "pending", title: "测试片",
   why: "37 分钟后再查", tries: 2, next_at: 0, note: "", date: "2026-10-01"},
  {sub: "javbus:actor:ABC", code: "ABC-456", state: "queued", title: "",
   why: "任务 downloading：磁力无响应", tries: 1, next_at: 0, note: "", date: "2026-10-02"},
  {sub: "javbus:actor:ABC", code: "ZZZ-999", state: "skipped", title: "跳过的那部",
   why: "已按策略跳过", tries: 0, next_at: 0, note: "", date: "2026-10-03"},
];
ctx.renderBacklog({ok: true, items: ITEMS,
                   counts: {pending: 1, queued: 1, skipped: 1, done: 0}});
const box = get("backlog-list");
out.rowCount = box.children.length;
out.rows = box.children.map(r => ({
  cls: r.className,
  parts: r.children.map(c => ({cls: c.className, text: c.textContent})),
  buttons: (r.children.find(c => c.className === "r") || {children: []})
    .children.map(b => b.textContent),
}));
out.hint = get("backlog-hint").textContent;
// 为什么按钮要能点：把监听器摘出来
out.hasRetry = box.children.some(r =>
  (r.children.find(c => c.className === "r") || {children: []})
    .children.some(b => b.textContent === "立刻重试"));
out.hasIgnore = box.children.some(r =>
  (r.children.find(c => c.className === "r") || {children: []})
    .children.some(b => b.textContent === "忽略"));
// 已归档/已跳过的行不该给「立刻重试」（跳过是用户的决定，翻回来等于撤销决定）
const skippedRow = box.children[2];
out.skippedButtons = (skippedRow.children.find(c => c.className === "r") || {children: []})
  .children.map(b => b.textContent);
console.log(JSON.stringify(out));
"""


def test_renders_one_row_per_item(node_bin):
    data = _run(node_bin, RENDER_BODY, PAGE)
    assert data["rowCount"] == 3, f"应渲染 3 行，实际 {data['rowCount']}"


def test_each_row_shows_the_why(node_bin):
    """why 是后端特意算的人话（「多少分钟后再查」「任务 downloading：xxx」），
    它不出现的话用户还是不知道卡在哪。"""
    data = _run(node_bin, RENDER_BODY, PAGE)
    texts = [" ".join(p["text"] for p in row["parts"]) for row in data["rows"]]
    assert "37 分钟后再查" in texts[0], texts[0]
    assert "任务 downloading：磁力无响应" in texts[1], texts[1]


def test_row_shows_code_title_state_and_tries(node_bin):
    data = _run(node_bin, RENDER_BODY, PAGE)
    row0 = data["rows"][0]
    joined = " ".join(p["text"] for p in row0["parts"])
    assert "SSIS-123" in joined
    assert "测试片" in joined
    assert "待重查" in joined, "状态要有中文说明，不能只显示 pending"
    assert "试过 2 次" in joined


def test_buttons_exist_per_row(node_bin):
    data = _run(node_bin, RENDER_BODY, PAGE)
    assert data["hasRetry"] is True
    assert data["hasIgnore"] is True


def test_skipped_row_has_no_retry(node_bin):
    """已跳过是用户的决定，给「立刻重试」等于让用户自己撤销自己的决定。"""
    data = _run(node_bin, RENDER_BODY, PAGE)
    assert "立刻重试" not in data["skippedButtons"], data["skippedButtons"]
    assert "忽略" not in data["skippedButtons"], data["skippedButtons"]


def test_counts_shown_in_hint(node_bin):
    data = _run(node_bin, RENDER_BODY, PAGE)
    assert "待重查 1" in data["hint"], data["hint"]


# ─────────────────────────────────────────── 空状态与失败

EMPTY_BODY = r"""
const out = {};
ctx.renderBacklog({ok: true, items: [], counts: {pending: 0, queued: 0, skipped: 0, done: 0}});
const box = get("backlog-list");
out.children = box.children.length;
out.text = box.children.map(c => c.textContent).join(" | ");
ctx.renderBacklog(null);
out.failedText = get("backlog-list").children.map(c => c.textContent).join(" | ");
out.failedHint = get("backlog-hint").textContent;
console.log(JSON.stringify(out));
"""


def test_empty_state_is_a_sentence_not_an_empty_table(node_bin):
    data = _run(node_bin, EMPTY_BODY, PAGE)
    assert data["children"] == 1, "空状态应给一句话，不是空表格"
    assert "没有待办" in data["text"], data["text"]


def test_null_backlog_reports_failure(node_bin):
    data = _run(node_bin, EMPTY_BODY, PAGE)
    assert "失败" in data["failedText"], data["failedText"]
    assert "失败" in data["failedHint"], data["failedHint"]


# ─────────────────────────────────────────── 两个操作

ACTION_BODY = r"""
const out = {};
const calls = [];
ctx.subsApi = async (path, body) => {
  calls.push({path, body});
  if (out.failPath && path.includes(out.failPath)) {
    return {ok: false, error: "待办里没有这个番号"};
  }
  if (out.throwPath && path.includes(out.throwPath)) {
    throw new Error("network down");
  }
  return {ok: true, sub: "javbus:actor:ABC", code: "SSIS-123"};
};
ctx.renderSubs = async () => { out.rerendered = (out.rerendered || 0) + 1; };

(async () => {
  // 正常路径：点「立刻重试」
  await ctx.backlogAction("/api/backlog/retry",
    {code: "SSIS-123", sub: "javbus:actor:ABC"}, null);
  out.retryCall = calls[0];
  out.hintAfterRetry = get("backlog-hint").textContent;
  out.rerendered = out.rerendered || 0;

  // 「忽略」
  calls.length = 0;
  await ctx.backlogAction("/api/backlog/ignore",
    {code: "SSIS-123", sub: "javbus:actor:ABC"}, null);
  out.ignoreCall = calls[0];
  out.hintAfterIgnore = get("backlog-hint").textContent;

  // 后端返回失败：要有提示，不能静默
  out.failPath = "retry";
  calls.length = 0;
  await ctx.backlogAction("/api/backlog/retry",
    {code: "SSIS-123", sub: "javbus:actor:ABC"}, null);
  out.failHint = get("backlog-hint").textContent;

  // 网络异常：也要有提示
  out.throwPath = "ignore";
  calls.length = 0;
  await ctx.backlogAction("/api/backlog/ignore",
    {code: "SSIS-123", sub: "javbus:actor:ABC"}, null);
  out.throwHint = get("backlog-hint").textContent;

  console.log(JSON.stringify(out));
})().catch(e => { console.error("HARNESS ERROR", e); process.exit(2); });
"""


def test_retry_calls_right_endpoint_with_right_body(node_bin):
    data = _run(node_bin, ACTION_BODY, PAGE)
    assert data["retryCall"]["path"] == "/api/backlog/retry"
    assert data["retryCall"]["body"]["code"] == "SSIS-123"
    assert data["retryCall"]["body"]["sub"] == "javbus:actor:ABC"


def test_ignore_calls_right_endpoint_with_right_body(node_bin):
    data = _run(node_bin, ACTION_BODY, PAGE)
    assert data["ignoreCall"]["path"] == "/api/backlog/ignore"
    assert data["ignoreCall"]["body"]["code"] == "SSIS-123"
    assert data["ignoreCall"]["body"]["sub"] == "javbus:actor:ABC"


def test_backend_failure_is_shown(node_bin):
    data = _run(node_bin, ACTION_BODY, PAGE)
    assert "失败" in data["failHint"], data["failHint"]
    assert "待办里没有这个番号" in data["failHint"], "要显示后端的原文"


def test_network_exception_is_shown(node_bin):
    data = _run(node_bin, ACTION_BODY, PAGE)
    assert "失败" in data["throwHint"], data["throwHint"]
    assert "network down" in data["throwHint"], "网络异常也要看得见"


def test_success_triggers_a_rerender(node_bin):
    """操作完待办状态变了，列表要跟着刷新，否则用户看不到自己点的效果。"""
    data = _run(node_bin, ACTION_BODY, PAGE)
    assert data["rerendered"] >= 1, "操作后没有刷新待办"


# ─────────────────────────────────────────── 静态约束

def test_panel_dom_exists():
    html = PAGE.read_text(encoding="utf-8")
    for hook in ('id="backlog-list"', 'id="backlog-hint"', 'id="backlog-refresh"'):
        assert hook in html, f"缺少 {hook}"


def test_reads_backlog_from_subs_response():
    """前端必须真的读 data.backlog —— 不是读别的字段名。"""
    html = PAGE.read_text(encoding="utf-8")
    assert "data.backlog" in html or "(data && data.backlog)" in html, \
        "前端没有读 /api/subs 响应里的 backlog"
    # 前端不该自己算待办（后端已经算好了 why）。只查代码，不查注释 ——
    # 注释里提到 backlog_view 是为了说明出处，不是调用。
    code = re.sub(r"//[^\n]*", "", html)
    assert "backlog_view" not in code, "前端不该自己算待办（后端已经算好了）"


def test_both_endpoints_referenced():
    html = PAGE.read_text(encoding="utf-8")
    assert "/api/backlog/retry" in html
    assert "/api/backlog/ignore" in html


def test_no_new_interval_added():
    """待办不新增定时器：页面已有 30 秒轮询，再加一个只会多打后端。"""
    html = PAGE.read_text(encoding="utf-8")
    assert html.count("setInterval(") == 2, \
        f"setInterval 数量变了：{html.count('setInterval(')}"
