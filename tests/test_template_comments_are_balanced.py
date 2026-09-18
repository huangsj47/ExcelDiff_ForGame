# -*- coding: utf-8 -*-
"""模板里的注释必须**配对**：游离的结尾会被当成正文渲染出来。

## 两个实测缺陷（同一类，一个是用户报的、一个是查它时顺带发现的）

1. **`templates/merged_project_view.html`**：一大段 HTML 注释的收尾行写了两遍
   （`=============== -->` 出现两次）。HTML 注释**不嵌套**，第一个 `-->` 就把注释
   关掉了，于是第二行连同那串等号成了**页面正文** —— 用户在
   `/projects/1/merged-view` 上看到的正是这个。
2. **同一个文件的 CSS**：注释正文里写了「**34 个汉字**/行」，其中「星号紧挨斜杠」
   就地把 CSS 注释结束了。后果不是显示错，而是**静默丢规则**：注释后面的散文行
   被当成声明整段丢弃，直到下一个分号 —— 把紧接着的 `max-height: 320px` 一起吃掉
   （无头 Chrome 实测：改前 `getComputedStyle` 得到 `max-height: none`，改后 `320px`）。

## 为什么现有测试一个都没拦住

它们找的是「某个字符串在不在」——而这两个缺陷的表现是**多了**一段谁也不认识的字
（一个在 HTML 正文里、一个在 CSS 里），没有任何用例的断言会因为多了它而变红；
CSS 那一处更彻底：丢了 `max-height` 之后页面看起来仍然「差不多」。

所以这里换一种判据：**注释必须严格成对**，把「多出来的结尾」本身当成缺陷。
用真渲染去兜这一条是另一条路（也可以），但那样每加一个模板都要起浏览器。

## 扫描范围与它**不**覆盖什么

* HTML 注释：全仓库的 `templates/**/*.html`，Jinja 标签（`{# #}` / `{% %}` / `{{ }}`）
  先挖掉 —— 它们里面可以合法地出现 `-->`。
* CSS 注释：`static/**/*.css`，以及模板里 `<style>…</style>` 之间的部分。
  **不扫 `<script>`**：JS 里 `*/` 是合法片段（本仓库就有 `/^\s*\d+\s*/` 这样的正则、
  以及 `data-*/title` 这种注释里的路径写法），扫它只会产出假失败 ——
  一个会误报的守卫比没有守卫更糟，它会被绕过或被加豁免注释。
"""
from __future__ import annotations

import os
import re

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Jinja 标签：它们里面可以合法地出现 HTML/CSS 注释记号，先按同长度挖空再配对。
_JINJA_TAG = re.compile(r"\{#.*?#\}|\{%.*?%\}|\{\{.*?\}\}", re.S)
_STYLE_BLOCK = re.compile(r"<style[^>]*>(.*?)</style>", re.S)


def _stray_positions(text: str, opener: str, closer: str) -> list:
    """返回**没有开头的结尾记号**所在的行号（1 起算）。

    按出现顺序配对：遇到开头记住位置，遇到结尾就配对消掉；若遇到结尾时手上没有
    未闭合的开头，它就是游离的 —— 正是会被渲染/解析成正文的那一个。
    """
    tokens = list(re.finditer(re.escape(opener) + "|" + re.escape(closer), text))
    stray = []
    open_at = None
    for token in tokens:
        if token.group(0) == opener:
            if open_at is None:
                open_at = token.start()
        elif open_at is None:
            stray.append(text.count("\n", 0, token.start()) + 1)
        else:
            open_at = None
    return stray


def _templates() -> list:
    root = os.path.join(PROJECT_ROOT, "templates")
    found = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.endswith(".html"):
                found.append(os.path.join(dirpath, name))
    return sorted(found)


def _stylesheets() -> list:
    root = os.path.join(PROJECT_ROOT, "static")
    found = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.endswith(".css"):
                found.append(os.path.join(dirpath, name))
    return sorted(found)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _report(path: str) -> list:
    """这个文件里所有游离的注释结尾，形如 `['HTML 注释第 1643 行', ...]`。"""
    text = _read(path)
    masked = _JINJA_TAG.sub(lambda match: " " * len(match.group(0)), text)
    problems = [f"HTML 注释第 {line} 行"
                for line in _stray_positions(masked, "<!--", "-->")]
    css_regions = [masked] if path.endswith(".css") else _STYLE_BLOCK.findall(masked)
    for region in css_regions:
        problems.extend(f"CSS 注释第 {line} 行"
                        for line in _stray_positions(region, "/*", "*/"))
    return problems


# ---------------------------------------------------------------------------
#  全仓库：注释必须配对
# ---------------------------------------------------------------------------

def test_no_stray_html_comment_terminator_in_templates():
    """**用户报的这一条。** 多出来的 `-->` 会把注释尾巴当正文渲染到页面上。"""
    offenders = {path: _report(path) for path in _templates()}
    offenders = {os.path.relpath(path, PROJECT_ROOT): problems
                 for path, problems in offenders.items()
                 if [p for p in problems if p.startswith("HTML")]}
    assert not offenders, (
        f"模板里有游离的 HTML 注释结尾（页面正文里会多出一段谁也不认识的文字）：{offenders}"
    )


def test_no_stray_css_comment_terminator():
    """**顺带查出来的那一条。** 注释被提前结束之后，后面的规则会被整段丢弃。

    最坏的地方在于它不报错、页面也「看起来差不多」——只有一个属性的取值悄悄回到默认值。
    """
    targets = _stylesheets() + _templates()
    offenders = {}
    for path in targets:
        problems = [p for p in _report(path) if p.startswith("CSS")]
        if problems:
            offenders[os.path.relpath(path, PROJECT_ROOT)] = problems
    assert not offenders, (
        f"CSS 注释被提前结束（后面的声明会被浏览器整段丢弃）：{offenders}"
    )


def test_an_unclosed_comment_is_not_reported_here():
    """反向自检：本守卫只管「多出来的结尾」。

    少一个结尾（注释没关）不是同一件事：它的表现是**吞掉**后面的正文，而定位它要
    分辨「这个 `<!--` 到底是没关，还是作者有意留着」—— 交给渲染层的用例更合适。
    这里明确钉住「不报」，免得以后有人以为它漏了而把判据改坏。
    """
    assert _stray_positions("<!-- 开着没关\n正文\n", "<!--", "-->") == []


# ---------------------------------------------------------------------------
#  反向自检：扫描器真的认得出缺陷
# ---------------------------------------------------------------------------

class TestTheScannerActuallyDetects:
    """扫描器自己也要有证据 —— 一个永远返回空列表的扫描器会让上面两条全绿。"""

    def test_it_finds_the_reported_duplicate_terminator(self):
        text = "<!-- 说明\n=============== -->\n=============== -->\n<div>正文</div>"
        assert _stray_positions(text, "<!--", "-->") == [3]

    def test_it_finds_a_premature_css_close(self):
        text = "/* 一行里写了 34 个汉字*/行 的宽度\n  还有一行散文\n*/\n.a { max-height: 3px; }"
        assert _stray_positions(text, "/*", "*/") == [3]

    def test_a_healthy_file_is_not_reported(self):
        text = "<!-- 一段注释 -->\n<style>/* 另一段 */ .a { color: red; }</style>"
        assert _stray_positions(text, "<!--", "-->") == []
        assert _stray_positions(text, "/*", "*/") == []

    def test_jinja_tags_are_masked_out(self):
        """`{# … --> … #}` 里的 `-->` 是合法的（Jinja 先把整段吃掉），不许误报。"""
        text = "{# 这里可以写 --> 这种东西 #}\n<!-- 真注释 -->"
        masked = _JINJA_TAG.sub(lambda m: " " * len(m.group(0)), text)
        assert _stray_positions(masked, "<!--", "-->") == []

    def test_script_blocks_are_out_of_scope_on_purpose(self):
        """JS 里 `*/` 是合法片段（正则与路径写法都有），扫它只会误报。"""
        path = os.path.join(PROJECT_ROOT, "templates", "commit_diff.html")
        assert ".js" not in path
        problems = _report(path)
        assert not [p for p in problems if p.startswith("CSS")], (
            f"扫到了 <script> 里的 */（假失败）：{problems}"
        )


# ---------------------------------------------------------------------------
#  用户报的那一处
# ---------------------------------------------------------------------------

class TestTheReportedPage:
    def test_the_merged_view_no_longer_prints_a_comment_tail(self):
        path = os.path.join(PROJECT_ROOT, "templates", "merged_project_view.html")
        source = _read(path)
        assert source.count("=============================================================================== -->") == 1, (
            "收尾行又写了两遍：第二个 --> 会作为正文显示在页面上"
        )

    def test_the_rule_that_was_being_dropped_is_still_there(self):
        """那处注释被提前结束吃掉的正是这条规则 —— 它必须还在，且注释之后紧跟它。"""
        path = os.path.join(PROJECT_ROOT, "templates", "merged_project_view.html")
        source = _read(path)
        marker = source.index(".ai-example__body {")
        block = source[marker:marker + 1400]
        assert "max-height: 320px;" in block
        # 注释里不许再出现「星号紧挨斜杠」那种写法
        comment = block[:block.index("*/")]
        assert "**/行" not in comment and "*/行" not in comment
