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
    assert 'el("input", "sval"' in html(), "设置项输入框要带 sval 类"
    assert '.panel input[type="password"]' in c, "密钥输入框也要深色样式"


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
    body = js[js.index("async function loadJobs"):js.index("// ── 任务时间线")]
    assert "scrollTop" in body, "轮询更新队列时要保留滚动位置"
    assert "box.scrollTop = keepTop" in body


def test_queue_poll_is_incremental_not_full_rebuild():
    """3 秒一次的轮询不能整块重绘：那是「迅雷面板一直在闪」的根因。

    整块清空再重建会先抹掉「迅雷面板」那块、等 /api/xunlei 回来才重现，
    中间有一帧空白；顺带丢掉 hover、展开的时间线、滚动锚点。
    """
    js = script(html())
    assert "function renderQueueBlock(" in js
    assert "const QUEUE_NODES = new Map()" in js
    # 内容没变就复用节点
    assert "if (old && old.sig === item.sig)" in js
    # 队列块里不能再出现整块清空
    body = js[js.index("async function loadJobs"):js.index("// ── 任务时间线")]
    assert 'box.textContent = ""' not in body, "队列轮询不许整块清空"
    # 迅雷面板也要增量（它才是闪得最明显的一块）
    panel = js[js.index("async function appendPanelTasks"):js.index("// 队列增量渲染")]
    assert "renderQueueBlock(box, panelItems" in panel
    assert "box.append(" not in panel, "面板任务要按签名复用，不能每次全量 append"


# ---------------------------------------------------------------- 8. 视图切换

def test_non_archive_views_hide_filter_bar():
    js = script(html())
    body = js[js.index("function showView"):js.index("function showArchive")]
    assert 'activefilters' in body, "切走归档视图时筛选条要一起收起"
    assert "closeSide()" in body, "窄屏选完视图要收起抽屉，否则抽屉挡住整个界面"


# ---------------------------------------------------------------- 9. 搜索框搜资源

def test_search_box_can_search_online_resources():
    """搜索框不再只筛本地：输入番号回车 / 「搜资源」能去网上找磁力。"""
    page = html()
    js = script(page)
    assert 'id="resbtn"' in page, "结果条要有「搜资源」入口"
    assert "function searchResources" in js
    assert '"/api/magnets?code="' in js, "资源搜索走磁力搜索接口"
    assert 'codeOfQuery' in js, "要从关键词里认出番号"
    body = js[js.index('$("q").addEventListener("keydown"'):]
    assert "searchResources(" in body, "回车要直接搜资源"


def test_resource_results_hidden_outside_archive():
    js = script(html())
    body = js[js.index("function showView"):js.index("function showArchive")]
    assert "resource-slot" in body, "切到设置/元数据时资源面板要跟着收起"


def test_resource_rows_offer_explicit_download():
    """搜到候选不等于开下：必须点「下载」才进队列。"""
    js = script(html())
    body = js[js.index("async function searchResources"):js.index("// ── 顶栏类型切换")]
    assert "downloadCandidate(" in body, "每条候选要有下载入口"
    assert "点「下载」才进队列" in js, "提示要说明搜索本身不落任务"


def test_code_of_query_normalizes():
    """SSIS123 / ssis_123 / FC2 PPV 4519894 都要认成标准番号。"""
    js = script(html())
    body = js[js.index("function codeOfQuery"):js.index("const resState")]
    assert "FC2-PPV-" in body and "toUpperCase()" in body


# ---------------------------------------------------------------- 10. 预览图灯箱

def test_preview_images_open_in_place_not_new_tab():
    """预览图/截图点开要走站内灯箱：以前 target=_blank，点一下新开标签页。"""
    page = html()
    js = script(page)
    assert "function showLightbox" in js and "function thumbWall" in js
    assert "lightbox" in css(page), "灯箱样式缺失"
    # 图片墙本身不能带 target=_blank（右键另存仍可用 href）
    body = js[js.index("function thumbWall"):js.index("// 番号详情")]
    assert 'target = "_blank"' not in body, "图片墙不该再新开标签页"
    assert 'showLightbox(full, i)' in body, "点击要进灯箱"
    # 详情 / 磁力预览两个弹层都用同一个图片墙
    detail = js[js.index("function showDetail"):js.index("// 单条磁力预览")]
    assert "thumbWall(info.previews" in detail
    preview = js[js.index("async function showMagnetPreview"):js.index("async function panelControl")]
    assert "thumbWall(info.screenshots" in preview


def test_lightbox_escape_does_not_close_parent_modal():
    """Esc 只关灯箱：父弹层的 keydown 处理要先看灯箱在不在。"""
    js = script(html())
    assert js.count("if (lightboxOpen()) return;") == 2, "两个弹层都要让位给灯箱"
    light = js[js.index("function showLightbox"):js.index("function thumbWall")]
    assert 'document.addEventListener("keydown", onKey, true)' in light, "灯箱要用捕获阶段抢 Esc"
    assert "closeLightbox();" in js[js.index("function showDetail"):js.index("async function showMagnetPreview")], \
        "关掉父弹层时要顺带收灯箱"


# ---------------------------------------------------------------- 11. 队列不再是右侧抽屉

def test_queue_is_a_main_view_not_a_right_drawer():
    """下载队列和设置/元数据一样是主内容区视图：不再从右侧滑出。"""
    page = html()
    c = css(page)
    # 不能还有 fixed/sticky 的右侧栏与「展开宽度」那套 transitional 尺寸
    assert "position:sticky; top:0; height:100vh" not in c, "队列不该再是粘性右栏"
    assert "translateX(101%)" not in c, "队列不该再是滑出抽屉"
    # 队列容器在主内容区里，并且是 .panel 视图、默认隐藏
    m = re.search(r'<section class="panel[^"]*queuepanel" id="queue" hidden', page)
    assert m, "队列应是 panel 视图"
    js = script(page)
    body = js[js.index("function showView"):js.index("function showArchive")]
    assert '["queue", name === "queue"]' in body, "showView 要管队列视图"
    assert '["queuebtn", name === "queue"]' in body, "进队列时侧栏要高亮"


def test_queue_open_close_switch_views():
    js = script(html())
    body = js[js.index("function openQueue"):js.index("function syncScrim")]
    assert 'showView("queue")' in body
    assert '"archive"' in body, "关闭队列要回到归档"
    assert "localStorage" not in body, "不再记抽屉开合状态"


# ---------------------------------------------------------------- 12. 新建下载并入搜索框

def test_new_task_panel_is_gone():
    """「新建下载」与搜索框重复：侧栏入口与面板都撤掉，能力并入搜索框。"""
    page = html()
    assert 'id="new-task"' not in page, "侧栏不该再有新建下载"
    assert 'id="panel"' not in page and "dl-text" not in page, "下载面板应已移除"
    assert 'id="submit-slot"' in page, "磁力/直链提交区要顶上来"


def test_pasting_magnet_opens_submit_panel():
    js = script(html())
    assert "function queryKind" in js, "搜索框要能分辨番号 / 磁力 / 直链"
    assert "function renderSubmit" in js and "function submitDownload" in js
    assert 'magnet:\\?[^\\s"\']+' in js or "magnet:" in js
    q = js[js.index('$("q").addEventListener("input"'):]
    assert 'renderSubmit(' in q, "粘磁力要出提交区"
    key = js[js.index('$("q").addEventListener("keydown"'):]
    assert 'submitDownload(' in key, "粘磁力后回车直接提交"


def test_magnet_is_not_a_local_filter():
    """磁力/直链不是筛选词：不能拿整串 URL 去比本地文本，也别做成筛选胶囊。"""
    js = script(html())
    body = js[js.index("function matches"):js.index("// 一个归档在某个维度上")]
    assert "queryKind(view.q)" in body, "matches 要先判断输入类型"
    filters = js[js.index("function renderActiveFilters"):js.index("function card")]
    assert 'qk.kind !== "magnet"' in filters


# ---------------------------------------------------------------- 13. 智能整理是视图

def test_normalize_is_a_view_with_plan_lists():
    """智能整理不再是「点一下弹两个 confirm」的动作：点得进去、先列清单。"""
    page = html()
    assert 'id="normpanel"' in page and 'id="norm-adopt"' in page and 'id="norm-rename"' in page
    js = script(page)
    assert "function loadNormPlan" in js and "function runNorm" in js
    assert "askNormalize" not in js, "旧的一键动作应已移除"
    body = js[js.index("function showView"):js.index("function showArchive")]
    assert '["normpanel", name === "norm"]' in body, "showView 要管智能整理视图"
    assert '["norm-btn", name === "norm"]' in body, "进智能整理时侧栏要高亮"
    nav = js[js.index('$("norm-btn").addEventListener'):]
    assert "openNorm" in nav and "closeNorm" in nav


def test_normalize_actions_live_outside_scroll_area():
    """两份清单可以很长：执行按钮要钉在面板底部，不能滚到底才点得到。"""
    page = html()
    body_start = page.index('<div class="panel-body">', page.index('id="normpanel"'))
    body_end = page.index('</div>', page.index('id="norm-out"'))
    foot = page[body_end:page.index('</section>', body_end)]
    assert 'id="norm-rename-run"' in foot, "改名按钮应在 panel-body 之外"
    assert 'id="norm-adopt-run"' in foot


# ---------------------------------------------------------------- 8. 存储后端面板

def test_settings_renders_choices_as_select():
    """存储后端 / 链接形态是枚举：必须渲染下拉，别让人手打错字。"""
    js = script(html())
    assert "Array.isArray(it.choices)" in js, "设置项要支持 choices"
    assert 'el("select"' in js, "枚举项应渲染 <select>"
    assert "select.sval" in css(html()), "下拉框也要跟着深色面板走"


def test_settings_has_storage_status_row():
    """存储分区顶部：当前后端 + 测试连接（改完地址不用等下一次上传才知道通不通）。"""
    js = script(html())
    assert "function storageStatusRow()" in js
    assert 'subsApi("/api/storage/test"' in js
    assert 'group === "存储"' in js, "存储分区要单独插一行状态 + 测试连接"


# ---------------------------------------------------------------- 9. 设置分区 tab

def test_settings_have_horizontal_tabs():
    """十来个分组平铺成一长条找起来太费劲：按功能分成横向子 tab。"""
    page = html()
    js = script(page)
    assert 'id="settings-tabs"' in page and 'role="tablist"' in page
    assert "function switchSettingsTab(" in js
    # 每个分组一个 tab，面板只显示当前分区
    assert 'tab.dataset.group = group' in js
    assert 'pane.hidden = pane.dataset.group !== name' in js
    # 标签吸顶：滚到底部改「存储」时还看得见自己在哪个分区
    assert "position:sticky" in css(page)
    assert ".settings-pane[hidden]{display:none}" in css(page)


def test_settings_tabs_mark_unsaved_groups():
    """改动可能散在别的分区：标签上要有点，否则用户不知道还剩哪几处没保存。"""
    js = script(html())
    assert "function settingsTabOf(" in js
    assert 'tab.classList.toggle("dirty"' in js
    assert ".stabs button.dirty::after" in css(html())


def test_settings_tab_survives_reload_and_arrow_keys():
    """保存后重载不该把用户弹回第一页；左右方向键能换分区。"""
    js = script(html())
    assert "if (!names.includes(settingsTab)) settingsTab = names[0]" in js
    assert 'e.key !== "ArrowRight" && e.key !== "ArrowLeft"' in js
    assert "switchSettingsTab(next.dataset.group)" in js


def test_card_shows_path_when_no_share_link():
    """没有分享能力的后端（CloudDrive2）：卡片要显示真实路径，不是一句「无链接」。"""
    js = script(html())
    assert "} else if (t.path) {" in js
    assert "分享 ↗" in js, "非 Drive 链接不该再叫 Drive"


def test_settings_marks_unsaved_rows_and_wide_paths():
    """「N 项待保存」看不出是哪一项：改过没存的输入框自己标黄；路径类给宽框。"""
    js = script(html())
    assert 'input.classList.toggle("dirty", key in settingsDirty)' in js
    assert 'sel.classList.toggle("dirty", key in settingsDirty)' in js
    assert ".kvrow.setting .sval.dirty" in css(html())
    assert "_ROOT|_PATH|_TEMPLATE|_CA_FILE" in js, "路径/模板类字段要用宽输入框"


def test_storage_test_button_shows_elapsed():
    """rclone 的探测要问云端配额，可能几十秒：必须把秒数跑出来，否则像按钮没反应。"""
    js = script(html())
    # 递归 setTimeout：页面里只该有那两个轮询定时器（见 test_webui_backlog）
    assert "let ticking = true;" in js
    assert "测试中… ${Math.round((Date.now() - started) / 1000)}s" in js
    assert "ticking = false;" in js


def test_storage_params_are_scoped_to_backend():
    """切到 CloudDrive2 时不该还挂着 OpenList 的十来项参数（看着像切换没生效）。"""
    js = script(html())
    # 后端在 SETTINGS_SPEC 里给每项标 only，界面按它收起无关行
    assert 'row.dataset.scope = it.only' in js
    assert "function applyStorageScope()" in js
    assert 'row.hidden = row.dataset.scope !== chosen' in js
    # 换后端立刻生效，不必等保存
    assert 'if (key === "STORAGE_BACKEND") applyStorageScope()' in js
    # 保存后重载也要按已保存的后端过滤
    assert "applyStorageScope();" in js


def test_storage_scope_keeps_hidden_dirty_visible():
    """被收起的那几项若有未保存改动，必须提示：否则「保存全部」会存掉看不见的东西。"""
    js = script(html())
    assert "hiddenDirty" in js
    assert "的改动还没保存" in js


def test_storage_status_row_marks_unsaved_backend_switch():
    """状态行要写「已保存 → 待保存」，别让人以为已经切过去了。"""
    js = script(html())
    assert "（待保存）" in js
    assert 'val.classList.toggle("dirty", chosen !== saved)' in js


def test_storage_panel_explains_host_address_rewrite():
    """容器里填宿主机 LAN 地址会被后端改写：面板要提一句，否则用户看不懂报错里的别名。"""
    js = script(html())
    assert "function storageNetNote(chosen)" in js
    assert "storageInfo.net.container" in js
    assert "storageInfo.net.alias" in js, "要写出别名具体是什么"
    assert 'if (chosen === "rclone") return "";' in js, "rclone 走挂载配置，不该提改写"


def test_failed_jobs_can_be_removed_from_ui():
    """下载界面的失败任务以前没有任何删除入口（「清空已完成」还清不掉库里的记录）。"""
    page = html()
    js = script(page)
    # 每张已结束的卡片自带「删记录」，并走 /api/job/delete
    assert 'const DONE_STAGES = new Set(["done", "failed", "cancelled"])' in js
    assert 'el("button", "jstop", "删记录")' in js
    assert '"/api/job/delete"' in js
    # 队列头部多一个「清空失败」，且清理结果要说出来（以前静默）
    assert 'id="queue-clear-failed"' in page
    assert "async function clearJobs(" in js
    assert "已清理 ${d.count" in js
    # 提示落在队列自己那行：全局状态行每 30 秒会被覆盖，结果会一闪就没
    assert 'id="queue-flash"' in page and "function queueFlash(" in js


# ---------------------------------------------------------------- 10. 首页媒体墙

def test_home_has_media_wall():
    page = html()
    js = script(page)
    assert 'id="wall"' in page and 'id="wall-grid"' in page
    assert "async function loadWall(" in js
    # 点一张卡 = 填进搜索框并搜资源，不在墙上直接发下载
    assert "searchResources(item.code)" in js
    assert 'view.q = item.code' in js
    # 封面走同源代理（javbus 图床直连会被拦）
    assert '"/proxy?u=" + encodeURIComponent(item.cover)' in js
    # 图床偶发 502：先重试一次再认成无封面
    assert "img.dataset.retried" in js
    # 首屏分几批摆，别一次加载三十张 3:4 封面
    assert "function wallBatch()" in js and "function drainWall()" in js
    # 已归档的给绿标
    assert 'el("span", "wall-owned", "已归档")' in js


def test_wall_hidden_when_off_or_empty():
    """关掉或取不到就别占版面（空墙比没有更难看）。"""
    js = script(html())
    assert "wallState.items.length > 0 && !wallState.off" in js
    assert "if (d && d.off) { wallState.off = true; slot.hidden = true; return; }" in js


def test_home_settings_show_image_cache_row():
    """图片缓存占了多少、在哪、能清空 —— 缓存是派生数据，敢给直连按钮。"""
    js = script(html())
    assert "function imageCacheRow()" in js
    assert 'subsApi("/api/images/clear", {confirm: true})' in js
    assert 'if (group === "首页") pane.append(imageCacheRow());' in js
    assert "function mbText(" in js


def test_search_box_keyword_enter_searches():
    """输入 MIMK 回车以前什么都不发生（看着像搜索框坏了）：现在按关键词找片。"""
    page = html()
    js = script(page)
    assert 'id="find-slot"' in page
    assert "async function searchKeyword(" in js
    assert '"/api/find?q="' in js
    # 番号仍走精确磁力搜索，其余非空输入走关键词搜索
    assert "searchResources(q.code)" in js and "searchKeyword(text)" in js
    # 点结果卡 = 切到精确番号
    assert "closeFind();" in js


def test_wall_badge_falls_back_to_rating():
    """javdb 的第一条标签是评分（★4.29）：没有高清/字幕标记时把评分露出来。"""
    js = script(html())
    assert '|| tags.find(t => t.startsWith("★"))' in js


def test_render_queue_block_removes_rebuilt_node():
    """内容变了要先把旧节点摘掉：否则同一 key 覆盖映射，旧节点留在 DOM 里成残影。"""
    js = script(html())
    body = js[js.index("function renderQueueBlock"):js.index("async function loadJobs")]
    assert "if (old) old.node.remove();" in body
    assert 'key.startsWith(scope)' in body, "清理必须按区限定，否则两次渲染互相删"


# ---------------------------------------------------------------- 11. 云盘归档

def test_cloud_ingest_has_status_row_and_stage():
    """云添加依赖很硬（CD2 的离线能力），面板要先把「能不能用」摆出来。"""
    page = html()
    js = script(page)
    assert 'if (group === "云盘归档") pane.append(cloudStatusRow());' in js
    assert "function cloudStatusRow()" in js
    # 必须是 GET（subsApi 传 body 会变成 POST，而 capability 只挂在 GET 上）
    assert 'subsApi("/api/cloud/capability")' in js
    assert 'subsApi("/api/cloud/capability", {})' not in js
    # 队列里要有「云端落地中」这个阶段
    assert 'cloud: "云端落地中"' in js
    assert '"uploading", "cloud"]' in js, "云端任务也要能停止（只是不再等）"
    # 如实说明没有进度接口
    assert "不提供离线任务状态" in js
