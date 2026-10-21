"""前端脚本的静态检查：拦「Python 测试全绿但页面整段不执行」这类事故。

起因：给 webui.html 注入代码时把 `function openPanel() {` 写重复了一次，
括号就此失衡。整段 JS 解析失败 → 所有函数都没定义 → 左下角永远停在
「连接中…」。而当时 223 个 Python 测试全过，因为没有一个测试真的执行过 JS。

这里用纯 Python 做两件事，不依赖 node（CI 镜像是 python:3.12-slim）：
  1. 词法扫描时正确处理正则字面量，统计括号是否配平、字符串是否闭合；
  2. 检查有没有重复的顶层函数声明。

不会替代真正的 JS 解析器，但足以拦住括号失衡和重复声明这两类低级错误。
"""
from __future__ import annotations

import re
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "app" / "webui.html"


def script_of(html: str) -> str:
    m = re.search(r"<script>(.*?)</script>", html, re.S)
    assert m, "页面里找不到 <script> 块"
    return m.group(1)


def scan(js: str) -> list[str]:
    """返回问题清单；空列表表示括号和字符串都正常。"""
    problems: list[str] = []
    stack: list[tuple[str, int]] = []
    i, n, line = 0, len(js), 1
    # 上一个「有意义的字符」，用来判断 / 是除号还是正则开头
    prev = ""
    while i < n:
        c = js[i]
        nxt = js[i + 1] if i + 1 < n else ""

        if c == "\n":
            line += 1
            i += 1
            continue

        if c == "/" and nxt == "/":
            while i < n and js[i] != "\n":
                i += 1
            continue

        if c == "/" and nxt == "*":
            i += 2
            while i + 1 < n and not (js[i] == "*" and js[i + 1] == "/"):
                if js[i] == "\n":
                    line += 1
                i += 1
            i += 2
            continue

        if c in "\"'`":
            q = c
            start_line = line
            i += 1
            closed = False
            while i < n:
                if js[i] == "\\":
                    i += 2
                    continue
                if js[i] == q:
                    closed = True
                    break
                if js[i] == "\n":
                    if q == "`":
                        line += 1
                    else:
                        # 单双引号字符串不能跨行
                        problems.append(f"第 {start_line} 行字符串未闭合")
                        return problems
                i += 1
            if not closed:
                problems.append(f"第 {start_line} 行字符串未闭合")
                return problems
            i += 1
            prev = "a"
            continue

        # 正则字面量：只有前面不是标识符/数字/右括号时才可能是正则
        if c == "/" and (prev == "" or prev in "=(,:[!&|?{};+-*%~^"):
            i += 1
            in_class = False
            start_line = line
            closed = False
            while i < n:
                ch = js[i]
                if ch == "\\":
                    i += 2
                    continue
                if ch == "[":
                    in_class = True
                elif ch == "]":
                    in_class = False
                elif ch == "/" and not in_class:
                    closed = True
                    break
                elif ch == "\n":
                    break
                i += 1
            if not closed:
                problems.append(f"第 {start_line} 行正则字面量未闭合")
                return problems
            i += 1
            while i < n and js[i] in "gimsuyvd":
                i += 1
            prev = "a"
            continue

        if c in "([{":
            stack.append((c, line))
        elif c in ")]}":
            want = {")": "(", "]": "[", "}": "{"}[c]
            if stack and stack[-1][0] == want:
                stack.pop()
            else:
                problems.append(f"第 {line} 行出现多余的 {c}")
        if not c.isspace():
            # 左括号要原样记进 prev：`match(/…/)` 的正则紧跟在 `(` 后面，
            # 如果把 `(` 也归一成 "a"，这个正则就会被当除号，接着把 [ 当数组，
            # 于是把字符串里的引号误判成未闭合。
            prev = c if c in "()]}" else "a"
        i += 1

    for ch, ln in stack:
        problems.append(f"第 {ln} 行的 {ch} 没有闭合")
    return problems


def top_level_functions(js: str) -> list[str]:
    return re.findall(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", js, re.M)


def test_webui_script_has_balanced_brackets():
    """注入代码时最容易破坏的就是这里。括号一失衡整页就哑了。"""
    js = script_of(PAGE.read_text(encoding="utf-8"))
    assert scan(js) == [], "webui 的 JS 括号/字符串有问题"


def test_webui_has_no_duplicate_function_declarations():
    """`function f() {function f() {` 这种重复声明会直接让整段脚本失效。"""
    js = script_of(PAGE.read_text(encoding="utf-8"))
    names = top_level_functions(js)
    dup = sorted({n for n in names if names.count(n) > 1})
    assert dup == [], f"重复的函数声明：{dup}"


def test_webui_no_two_functions_on_one_line():
    """同一行塞两个 function 声明是重复注入的典型痕迹。"""
    js = script_of(PAGE.read_text(encoding="utf-8"))
    for idx, line in enumerate(js.split("\n"), 1):
        hits = re.findall(r"(?:^|[^\\w$])function\s+[A-Za-z_$][\\w$]*\s*\(", line)
        assert len(hits) <= 1, f"第 {idx} 行有多个函数声明"


def test_scan_detects_the_original_bug():
    """回归：把当初那行错误代码喂给扫描器，必须能报出来。"""
    broken = "function a() {\n  return 1;\n}\nfunction openPanel() {function openPanel() {\n"
    assert scan(broken) != []


def test_scan_accepts_regex_literals():
    """扫描器不能把正则里的引号当字符串，否则会误报。"""
    ok = 'const m = ($("dl-text").value.match(/magnet:\\?[^\\s"\']+/) || [""])[0];'
    assert scan(ok) == []


def test_scan_accepts_template_and_comments():
    ok = """
// 注释里的 " 不该算字符串
const a = `模板 ${1 + 2} 里的 " 引号`;
/* 块注释 ( [ { */
const b = /[/]/g;
"""
    assert scan(ok) == []
