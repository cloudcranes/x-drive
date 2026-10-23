"""前端视觉回归：把「看着坏了」的几处固定成断言。

这里不看后端行为，只拦那些「功能都对、界面却塌了」的回归 ——
它们不会让任何一条接口测试变红，但用户一打开就看得见：

  1. 卡片系列名的 .series 类和侧栏 .side-block.series 撞名，
     把侧栏那块压到 38% 宽（系列名全截断、维度切换被挤扁）；
  2. 侧栏内容超过一屏时，月份被压成 0 高、页脚的「刷新」被顶出可视区；
  3. 订阅的演员 ID、元数据番号、设置项输入框没写样式，在深色面板里落回白色默认框；
  4. disabled 的按钮和可点的长得一模一样（「保存全部改动」「全部应用」「取消」）；
  5. .ghost 的 flex:1 让「关闭 / 刷新待办 / 同步附件」被拉成巨宽按钮；
  6. 弹层的「关闭」借用 26px 见方的 .iconbtn，两个字被挤成竖排并溢出；
  7. 下载队列每 3 秒整块重绘，把用户翻到的位置拉回顶部。
"""
from __future__ import annotations

import re
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "app" / "webui.html"


def html() -> str:
    return PAGE.read_text(encoding="utf-8")


def css(page: str) -> str:
    m = re.search(r"<style>(.*?)</style>", page, re.S)
    assert m, "页面里找不到 <style> 块"
    return m.group(1)


def script(page: str) -> str:
    m = re.search(r"<script>(.*?)</script>", page, re.S)
    assert m, "页面里找不到 <script> 块"
    return m.group(1)


# ---------------------------------------------------------------- 1. 类名冲突

def test_card_series_class_does_not_collide_with_sidebar_block():
    """卡片系列名必须用独立类名：`.series` 的 max-width:38% 会把侧栏筛选块压扁。"""
    page = html()
    assert ".card-series{" in css(page), "卡片系列名应使用 .card-series"
    assert 'el("span", "card-series"' in script(page), "渲染卡片时要用 card-series"
    # 全页不应再有裸 `.series{...}` 规则（那会把 .side-block.series 一起改掉）
    assert re.search(r"\n\s*\.series\{", css(page)) is None, \
        "还有 .series{} 规则，侧栏系列块会被它压成 38% 宽"


# ---------------------------------------------------------------- 2. 侧栏可滚

def test_sidebar_middle_scrolls_and_footer_stays():
    """系列 / 月份放进独立滚动容器，页脚不参与压缩。"""
    page = html()
    assert 'class="side-mid"' in page, "侧栏应有一个可滚动的中段容器"
    assert ".side-mid{" in css(page) and "overflow-y:auto" in css(page), "中段要能滚动"
    # 旧写法：两块各自 max-height，超过一屏就把月份压成 0 高
    assert ".side-block.series{max-height" not in css(page)
    assert ".side-block.month{max-height" not in css(page)
    # 页脚是固定项，不被 flex 压缩
    assert ".side > .brand, .side > .stat, .side > .navmain, .side > .side-foot{flex:none}" in css(page)


# ---------------------------------------------------------------- 3. 输入框

def test_panel_text_inputs_are_themed():
    """深色面板里的文本框统一深色底，别落回浏览器默认白框。"""
    c = css(html())
    assert '.panel input[type="text"]' in c, "要有 .panel input[type=text] 的统一样式"
    assert "background:rgba(0,0,0,.6)" in c
    # 三个曾经是白框的输入点仍在（#meta-code / #sub-actor 是 text input，.sval 同理）
    assert 'id="meta-code" type="text"' in html()
    assert 'id="sub-actor" type="text"' in html()
    assert 'el("input", "sval")' in html()


# ---------------------------------------------------------------- 4. 禁用态

def test_disabled_buttons_look_disabled():
    c = css(html())
    assert "button:disabled{" in c, "禁用态必须有视觉差异"
    rule = c[c.index("button:disabled{"):c.index("button:disabled{") + 200]
    assert "opacity" in rule and "not-allowed" in rule


# ---------------------------------------------------------------- 5. ghost 宽度

def test_ghost_buttons_do_not_stretch_by_default():
    """只有侧栏底部那两个按钮平分宽度，其余按内容取宽。"""
    c = css(html())
    # 只允许 `.side-actions .ghost{flex:1}` 这一种写法（前面必须另有选择器）
    assert not re.search(r"\n\s*\.ghost\{flex:1", c), \
        ".ghost 不该默认 flex:1，否则「关闭」被拉成巨宽"
    assert ".side-actions .ghost{flex:1}" in c


# ---------------------------------------------------------------- 6. 弹层关闭

def test_modal_close_button_has_room_for_text():
    page = html()
    assert "关闭" in page
    assert ".iconbtn.txt{" in css(page), "文字版 iconbtn 要放开宽度"
    assert page.count('el("button", "iconbtn txt", "关闭")') == 2, \
        "两个弹层（详情 / 磁力预览）的关闭按钮都要带 txt 类"


# ---------------------------------------------------------------- 7. 队列滚动

def test_queue_keeps_scroll_position_on_poll():
    js = script(html())
    body = js[js.index("async function loadJobs"):js.index("// 搜磁力")]
    assert "scrollTop" in body, "轮询重建队列时要保留滚动位置"
    assert body.count("box.scrollTop = keepTop") >= 2, "空队列与有队列两条路径都要还原"


# ---------------------------------------------------------------- 8. 视图切换

def test_non_archive_views_hide_filter_bar():
    js = script(html())
    body = js[js.index("function showView"):js.index("function showArchive")]
    assert 'activefilters' in body, "切走归档视图时筛选条要一起收起"
    assert "closeSide()" in body, "窄屏选完视图要收起抽屉，否则抽屉挡住整个界面"
