"""E9：预览差异表必须绑定番号 —— 用假 DOM 真跑前端函数，不做源码字符串断言。

两个已确证的行为缺陷：
  1. 预览 B 时网络异常，catch 只改提示不清 changes，残留的「应用」按钮仍指向
     上一个番号 A 的字段，一按就把 A 的元数据写进 B；
  2. 慢请求 A 与快请求 B 并发时，A 后到会覆盖 B 的差异表，同样写到错的番号上。

做法：把 app/webui.html 的 <script> 抽出来，在 node:vm 里给一个只满足这些
函数所需接口的假 DOM，然后真的 await metaPreview() / metaApplyField()。
没有浏览器、没有网络：fetch 是桩，只记录它被喂了什么。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parent.parent / "app" / "webui.html"

HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const html = fs.readFileSync(process.argv[2], "utf8");
const src = html.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];

function node(tag) {
  return {
    tagName: tag, children: [], className: "", textContent: "", hidden: false,
    value: "", disabled: false, dataset: {},
    classList: { add(){}, remove(){}, contains(){ return false; } },
    append(...xs) { this.children.push(...xs); },
    appendChild(x) { this.children.push(x); },
    addEventListener() {}, setAttribute() {}, remove() {},
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
  setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {},
  fetch: () => Promise.resolve({ ok: true, status: 200, json: async () => ({}) }),
  alert: () => {}, confirm: () => true,
};
ctx.window = ctx; ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(src + "\n;globalThis.__probe = () => ({metaState, metaPreview, "
  + "metaApplyField, metaApplyAll});", ctx);
const P = ctx.__probe();
ctx.metaState = P.metaState;
const api = { preview: P.metaPreview, apply: P.metaApplyField, applyAll: P.metaApplyAll };

const calls = [];
const out = {};
function reply(payload) {
  return { ok: true, status: 200, json: async () => payload };
}

(async () => {
  // ── 场景 1：成功预览 A → 预览 B 网络异常 → 差异表必须清空，且不许把 A 发出去
  ctx.fetch = async (url, opts) => {
    const body = opts && opts.body ? JSON.parse(opts.body) : {};
    calls.push({ url, body });
    if (url.includes("/preview") && body.code === "A-001") {
      return reply({ ok: true, code: "A-001", found: true, locked: [], provider: "p",
                     changes: { title: { from: "A old", to: "A new" } } });
    }
    if (url.includes("/preview") && body.code === "B-002") {
      throw new Error("network down");
    }
    if (url.includes("/scrape/sync")) {
      return reply({ ok: true, locked: [], changed: {} });
    }
    return reply({});
  };

  get("meta-code").value = "A-001";
  await api.preview();
  out.afterA = JSON.parse(JSON.stringify(ctx.metaState.changes));

  get("meta-code").value = "B-002";
  await api.preview();
  out.afterBFailed = JSON.parse(JSON.stringify(ctx.metaState.changes));
  out.codeAfterB = ctx.metaState.code;

  // 旧代码在此处 changes 里还留着 A 的字段，且 metaState.code 已是 B-002：
  // 一按「应用」就会发出 {code:"B-002", fields:{title:"A new"}}。
  // 直接调用应用逻辑，把真正发给后端的 body 记下来。
  out.residualFields = Object.keys(ctx.metaState.changes || {});
  // 两条真实交互都要挡住：一键「全部应用」与逐字段「应用」。
  // 必须在**同一个失败残留态**上分别测，否则前一条的写成功会把 changes 删空、
  // 让后一条变成空断言（曾经就这么把 applyAll 漏过去了）。
  // apply 还有一道「输入框 != metaState.code」的提前返回，只能挡手改输入框的
  // 情形；差异表本身没清空，就仍是错的数据源。
  const syncFrom = (n) => calls.slice(n)
    .filter(c => c.url.includes("/scrape/sync") && c.body && c.body.fields)
    .map(c => c.body);
  const resetFailedState = () => {
    // 还原到「预览 B 失败之后」的状态：差异表是 A 的，code 已是 B
    ctx.metaState.code = "B-002";
    ctx.metaState.changes = JSON.parse(JSON.stringify(out.afterBFailed));
    ctx.metaState.locked = []; ctx.metaState.found = false;
  };

  resetFailedState();
  let n = calls.length;
  await api.applyAll();
  out.syncAfterApplyAll = syncFrom(n);

  resetFailedState();
  n = calls.length;
  for (const k of Object.keys(ctx.metaState.changes || {})) await api.apply(k, null);
  out.syncAfterApply = syncFrom(n);

  // ── 场景 2：乱序响应 —— SLOW-1 慢、FAST-2 快，慢的后到必须被丢弃
  let release;
  ctx.metaState.changes = {}; ctx.metaState.code = ""; ctx.metaState.locked = [];
  ctx.fetch = async (url, opts) => {
    const body = JSON.parse(opts.body || "{}");
    if (body.code === "SLOW-1") {
      await new Promise(r => { release = r; });
      return reply({ ok: true, code: "SLOW-1", found: true, locked: [],
                     changes: { title: { from: "s", to: "SLOW" } } });
    }
    return reply({ ok: true, code: "FAST-2", found: true, locked: [],
                   changes: { title: { from: "f", to: "FAST" } } });
  };
  get("meta-code").value = "SLOW-1";
  const pSlow = api.preview();
  get("meta-code").value = "FAST-2";
  await api.preview();
  out.afterFast = JSON.parse(JSON.stringify(ctx.metaState.changes));
  release();
  await pSlow;
  out.afterSlowLanded = JSON.parse(JSON.stringify(ctx.metaState.changes));
  out.codeAfterSlow = ctx.metaState.code;

  // ── 场景 3：服务端回报的番号与当前不符 → 整条丢弃
  ctx.metaState.changes = {}; ctx.metaState.code = ""; ctx.metaState.locked = [];
  ctx.fetch = async () => reply({ ok: true, code: "OTHER-9", found: true, locked: [],
                                  changes: { title: { from: "x", to: "y" } } });
  get("meta-code").value = "MINE-1";
  await api.preview();
  out.afterMismatch = JSON.parse(JSON.stringify(ctx.metaState.changes));

  console.log(JSON.stringify(out));
})().catch(e => { console.error("HARNESS ERROR", e); process.exit(2); });
"""


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    node = shutil.which("node")
    if not node:
        pytest.skip("没有 node，跳过前端行为验证")
    script = tmp_path_factory.mktemp("e9") / "harness.js"
    script.write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run([node, str(script), str(PAGE)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"harness 失败：{proc.stderr[:800]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_preview_failure_clears_previous_diff(harness):
    """成功预览 A 后，B 的预览失败不能留下 A 的差异。"""
    assert harness["afterA"] == {"title": {"from": "A old", "to": "A new"}}
    assert harness["afterBFailed"] == {}, "失败后仍残留上一个番号的差异"
    assert harness["codeAfterB"] == "B-002"


def test_no_residual_diff_after_failed_preview(harness):
    """失败后差异表里不许留下任何可应用的字段 —— 这是两个入口共同的前提。"""
    assert harness["residualFields"] == [], \
        f"失败后仍残留可应用字段：{harness['residualFields']}"


def test_apply_after_failed_preview_sends_nothing(harness):
    """逐字段「应用」不能把上一个番号的字段发给新番号。"""
    assert harness["syncAfterApply"] == [], \
        f"把上一个番号的字段写出去了：{harness['syncAfterApply']}"


def test_apply_all_after_failed_preview_sends_nothing(harness):
    """一键「全部应用」更不能：旧代码这里会发出 {code:'B-002',fields:{title:'A new'}}。"""
    assert harness["syncAfterApplyAll"] == [], \
        f"全部应用把上一个番号的字段写出去了：{harness['syncAfterApplyAll']}"


def test_stale_slow_response_is_discarded(harness):
    """慢响应后到不许覆盖快响应：差异表必须仍属于 FAST-2。"""
    assert harness["afterFast"] == {"title": {"from": "f", "to": "FAST"}}
    assert harness["afterSlowLanded"] == harness["afterFast"], "慢响应覆盖了快响应"
    assert harness["codeAfterSlow"] == "FAST-2"


def test_mismatched_code_reply_is_discarded(harness):
    """服务端回报的番号与当前番号不符时，整条结果丢弃。"""
    assert harness["afterMismatch"] == {}
