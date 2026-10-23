"""C1/C2/C3 三个新界面的静态验收：元数据维护台、归档健康、任务时间线。

只做「前端有没有、串没串对」这类断言，不连服务：
  · 面板与入口 DOM 存在；
  · 新函数已定义、无重复、括号配平（复用 test_webui_script 的扫描器）；
  · 引用的后端路由在 app/web.py 里真的注册过，且前端拼的字符串与后端一字不差；
  · 纪律：没有任何「删除远端 / 自动补传」的入口，附件同步必须带 confirm。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from test_webui_script import PAGE, scan, script_of, top_level_functions

WEB_PY = Path(__file__).resolve().parent.parent / "app" / "web.py"


@pytest.fixture(scope="module")
def js() -> str:
    return script_of(PAGE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def html() -> str:
    return PAGE.read_text(encoding="utf-8")


# ─────────────────────────────────────────── C1 元数据维护台

def test_meta_panel_dom_exists(html):
    for hook in ('id="metatool-open"', 'id="metapanel"', 'id="meta-close"',
                 'id="meta-state"', 'id="meta-conn"', 'id="meta-pending"',
                 'id="meta-code"', 'id="meta-preview"', 'id="meta-resync"',
                 'id="meta-applyall"', 'id="meta-unlock"', 'id="meta-diff"',
                 'id="meta-sync"', 'id="meta-syncout"'):
        assert hook in html, f"缺少 {hook}"
    # 面板写法与订阅面板一致：section.panel + hidden
    assert re.search(r'<section class="panel[^"]*" id="metapanel" hidden', html), \
        "元数据面板应沿用 <section class=\"panel\" hidden> 写法"


def test_meta_functions_defined_once(js):
    names = top_level_functions(js)
    for fn in ("metaApi", "metaPreview", "metaApplyField", "metaApplyAll",
               "metaLockField", "metaSync", "runMetaSync", "metaFailedRow",
               "renderMetaHealth", "renderMetaDiff", "loadMetaStatus",
               "openMeta", "closeMeta", "metaUnlockAll"):
        assert names.count(fn) == 1, f"{fn} 未定义或重复定义（{names.count(fn)} 次）"
    assert scan(js) == [], "加入新面板后 JS 括号/字符串失衡"


def test_meta_routes_exist_in_backend(js):
    """前端调的路由必须真在后端注册过，否则界面全是 404。"""
    web = WEB_PY.read_text(encoding="utf-8")
    for route in ("/api/scrape/status", "/api/scrape/preview", "/api/scrape/sync"):
        assert f'"{route}"' in js, f"前端没用到 {route}"
        assert f'"{route}"' in web, f"后端没有注册 {route}"


def test_meta_preview_is_read_only(js):
    """预览只能打 preview 这一个接口，绝不能顺手调 sync。"""
    body = js[js.index("async function metaPreview()"):js.index("async function metaApplyField")]
    assert '"/api/scrape/preview"' in body
    assert "scrape/sync" not in body, "预览里不允许出现 sync"


def test_meta_apply_sends_single_field(js):
    """逐字段应用只传该字段，别把整张差异表一起推上去。"""
    body = js[js.index("async function metaApplyField"):js.index("async function metaApplyAll")]
    assert "fields: {[k]: v}" in body, "逐字段应用应只传 fields: {字段: 新值}"
    assert "confirm" not in body, "写站内记录不该带 confirm（confirm 是动 Drive 的开关）"


def test_meta_sync_needs_confirm_and_warns(html, js):
    """动云端附件必须显式点击 + confirm，并且文案要说清会改云端。"""
    body = js[js.index("async function runMetaSync"):js.index("function metaFailedRow")]
    assert "confirm: true" in body, "同步附件必须带 confirm=true"
    assert "会改动云端附件" in js, "同步前要提示会改动云端附件"
    assert 'id="meta-sync"' in html


def test_meta_status_shows_error_and_pending(js):
    body = js[js.index("function renderMetaHealth"):js.index("async function loadMetaStatus")]
    assert "d.error" in body, "未配置时要显示后端 error 原文"
    assert "d.pending" in body, "要列出待补刮番号"
    assert "没刮到，保留现有字段" in js, "found=false 时要明确说保留现有字段"


def test_meta_lock_only_adds(js):
    """后端只有 lock（加锁），前端不能假装能解锁。"""
    body = js[js.index("async function metaLockField"):js.index("// 全部解锁")]
    assert 'lock: [k]' in body
    assert "unlock" not in body, "后端没有 unlock 参数，前端不该发它"
    assert "暂不支持解锁" in body, "做不到就直说，别伪造"


def test_unlock_path_does_not_send_request(js):
    """解锁分支必须在发请求之前就停下——发了也无法生效，只会制造假象。"""
    body = js[js.index("async function metaLockField"):js.index("// 全部解锁")]
    head = body[:body.index("if (btn) btn.disabled = true;")]
    assert "if (!on) {" in head and "return" in head, "解锁要在发请求前返回"
    assert "metaApi" not in head, "解锁分支不许调后端"


def test_backend_still_has_no_unlock():
    """契约核对：如果后端后来加了 unlock，这条会失败，届时前端应当改成真解锁。"""
    web = WEB_PY.read_text(encoding="utf-8")
    body = web[web.index("def scrape_sync("):web.index("def sync_attachments(")]
    assert "locks |= set(lock or ())" in body, "后端锁定逻辑变了，前端要重新核对"
    assert "unlock" not in body, "后端开始支持解锁了——前端应改为真解锁而非提示"


# ─────────────────────────────────────────── C2 归档健康

def test_health_panel_dom_exists(html):
    for hook in ('id="health-open"', 'id="healthpanel"', 'id="health-close"',
                 'id="verify-sum"', 'id="verify-limit"', 'id="verify-run"',
                 'id="verify-cancel"', 'id="verify-dups"', 'id="verify-prog"',
                 'id="verify-list"', 'id="dups-list"'):
        assert hook in html, f"缺少 {hook}"
    assert re.search(r'<section class="panel[^"]*" id="healthpanel" hidden', html)


def test_health_functions_defined_once(js):
    names = top_level_functions(js)
    for fn in ("renderVerifySummary", "renderVerifyList", "verifyBatch",
               "cancelVerify", "loadDups", "verifyOne", "openHealth", "closeHealth"):
        assert names.count(fn) == 1, f"{fn} 未定义或重复定义"


def test_health_routes_exist_in_backend(js):
    web = WEB_PY.read_text(encoding="utf-8")
    for route in ("/api/archive/verify", "/api/archive/dups"):
        assert f'"{route}"' in js
        assert f'"{route}"' in web


def test_health_has_all_six_states(js):
    """六个状态都要在汇总里出现，否则「无清单 / 大小未知」会被悄悄吞掉。"""
    for state in ("校验通过", "缺文件", "大小异常", "大小未知", "无清单", "检查失败"):
        assert f'"{state}"' in js, f"缺少状态 {state}"
    for field in ("missing", "extra", "size_mismatch", "size_unknown"):
        assert field in js, f"差异明细缺 {field}"


def test_health_batches_and_cancels(js):
    body = js[js.index("async function verifyBatch"):js.index("function cancelVerify")]
    assert '"limit"' in body or "limit}" in body, "批量检查要带 limit"
    assert "verifyState.cancelled" in js and "function cancelVerify" in js
    # 取消不发请求、不改写结果
    cancel = js[js.index("function cancelVerify"):js.index("// 重复归档")]
    assert "fetch(" not in cancel, "取消只停后续请求，不该再发请求"
    assert "verifyState.rows =" not in cancel, "取消不许伪造结果"


def test_health_is_report_only(html):
    """项目纪律：健康面板只报告，不补传、不删除。"""
    assert "只报告，不自动补传或删除" in html
    start = html.index('id="healthpanel"')
    end = html.index('id="activefilters"') if 'id="activefilters"' in html[start:] else len(html)
    panel = html[start:start + 4000]
    for bad in ("删除远端", "补传", "自动修复", "清理远端"):
        # 「不自动补传」这类否定说明是允许的，按钮文案不允许
        assert not re.search(r'<button[^>]*>[^<]*' + bad, panel), f"健康面板出现危险按钮：{bad}"


# ─────────────────────────────────────────── C3 任务时间线

def test_timeline_defined_once(js):
    names = top_level_functions(js)
    assert names.count("jobTimeline") == 1
    assert "时间线" in js


def test_timeline_uses_existing_job_fields_only(js):
    """只用 /api/jobs 已返回的字段，不自己造节点。"""
    body = js[js.index("function jobTimeline"):js.index("// ── 元数据维护台")]
    for field in ("created_at", "updated_at", "retries", "error", "defer_until",
                  "queued_position", "stage", "owner", "total", "resume_from", "kept"):
        assert f"j.{field}" in body, f"时间线没用到 j.{field}"
    # 不能为了时间线去打新接口
    assert "fetch(" not in body, "时间线只读已有字段，不该自己发请求"


def test_timeline_titles_are_neutral(js):
    """只有最后一次动作的时间，标题不能写「完成于/失败于」这种假装是终态时刻的说法。"""
    body = js[js.index("function jobTimeline"):js.index("// ── 元数据维护台")]
    assert "最近更新" in body, "终态节点应叫「最近更新 · 状态」"
    for fake in ("完成于", "失败于", "停止于", "重试于"):
        assert fake not in body, f"不该出现把 updated_at 当终态时刻的措辞：{fake}"
    assert "仅显示可确认的节点，细粒度阶段历史待后端补齐" in body, "时间线底部要有缺口说明"
    assert "计划唤醒" in body, "defer_until 是计划值，措辞要标明"


def test_diff_table_scrolls_on_narrow_screen(html):
    """620px 下差异表不能把列压变形：要有横向滚动容器。"""
    assert ".diffwrap" in html and "overflow-x:auto" in html
    assert "min-width:520px" in html, "表格要给最小宽度，否则照样被挤扁"
    body = html[html.index("function renderMetaDiff"):html.index("// 预览：只读")]
    assert 'el("div", "diffwrap")' in body, "渲染时要把表格包进滚动容器"


def test_timeline_wired_into_job_card(js):
    body = js[js.index("function jobNode(j)"):js.index("// 展开中的时间线")]
    assert "jobTimeline(j)" in body, "任务卡片要能展开时间线"
    assert "tlOpen" in body, "重建卡片时要保持展开状态"


# ─────────────────────────────────────────── 全局纪律

def test_no_dangerous_remote_actions_anywhere(js):
    """整个页面都不该出现「删除远端文件 / 自动补传」这类入口。"""
    for bad in ("删除远端", "自动补传", "补传并删除"):
        assert bad not in js, f"出现危险文案：{bad}"
    # 本次新增的两个面板不得引用 delete / purge 之类的归档写接口
    for route in ("/api/archive/delete", "/api/archive/purge", "/api/archive/normalize"):
        start = js.index("// ── 元数据维护台")
        new_part = js[start:]
        assert f'"{route}"' not in new_part, f"新面板不该调用 {route}"


def test_new_handlers_are_bound(html):
    for hook in ("metatool-open", "meta-close", "meta-preview", "meta-applyall",
                 "meta-sync", "meta-unlock", "health-open", "health-close",
                 "verify-run", "verify-cancel", "verify-dups"):
        assert f'$("{hook}").addEventListener' in html, f"{hook} 没绑定事件"
