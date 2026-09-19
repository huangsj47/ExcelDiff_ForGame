# -*- coding: utf-8 -*-
"""一个页面不许调用**另一个页面**的私有函数。

## 为什么要有这条

`templates/weekly_version_diff.html` 里原来写着 `refreshWeeklyAiLatest()` —— 那个函数
定义在 `merged_project_view.html`。两者是**两个不同的页面**，各自的内联脚本互不可见，
所以那一行是一个必然抛 `ReferenceError` 的调用。

它的坏处不在"那一行没生效"，而在**它把后面整个函数带走了**：切换仓库那条路上，
`closeWeeklyAiStream()` 已经关掉了流、`configId` 已经改了，接下来的清空数据、复位筛选、
`updateTabState`、`loadFileList`、`pushState` 一条都不执行 —— 页面半途而废，
而浏览器控制台里只有一行红字。**这个形态不会让任何既有测试变红**：它是运行期才发生的，
而且只在「浏览器前进/后退切仓库」这条路上发生。

两个页面各自 80+ 个内联函数、三份抽屉长得几乎一样 —— 同一个名字在两处各写一份是常态
（那种调用是安全的：两边都有）。危险的是**只在一处有**的名字。所以判据是：
「这个名字在全部页面里只被定义过一次，却在**另一个**页面里被调用」。

## 它现在报不出什么

* 拼错的、哪里都没有的名字（与浏览器/框架提供的全局无法区分，只能靠 lint，靠不住）；
* 名字在 `static/js/` 里也有一份的（那是共享模块的全局，本来就该跨页面用）。

所以这条测试**不能**代替真跑页面，只是把上面那个已经踩到过的坑钉住。
"""
from __future__ import annotations

import collections
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = PROJECT_ROOT / "templates"
SHARED_JS = PROJECT_ROOT / "static" / "js"

# 内联脚本里的两种写法（`function foo(` 与 `var foo = function(`）。
_FUNCTION_DECL = re.compile(r"^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", re.M)
_FUNCTION_ASSIGN = re.compile(
    r"\b(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?function"
)

# **不是页面**的模板：它们的函数与被插入的那一页同处一个作用域，跨页面调用是合法的。
_BASE_LAYOUT = "base.html"  # 每个页面都 `{% extends %}` 它
_INCLUDED_DIR = "diff_partials"  # 被 `{% include %}` 插进 commit_diff*.html

# 外部脚本（CDN）提供的名字，任何页面都能用。jQuery 从 `code.jquery.com` 引入
# （见各页面底部的 `<script src>`），它当然不是任何模板定义的。
_VENDOR_PROVIDED = {"$", "jQuery"}


def _page_files() -> list[Path]:
    pages = []
    for path in sorted(TEMPLATES.glob("**/*.html")):
        rel = path.relative_to(TEMPLATES).as_posix()
        if rel == _BASE_LAYOUT or rel.startswith(_INCLUDED_DIR + "/"):
            continue
        pages.append(path)
    return pages


def _strip_js_comments(text: str) -> str:
    """去掉 `//` 与 `/* */` 注释（**字符串里的原样保留**）。

    **必须先剥**：上面那句被删掉的调用，在注释里是**原样引用**着的（注释的职责就是说明
    这里原来写的是什么）—— 不剥的话，改好了的代码照样会被判为「还在调用」，
    护栏从此只能靠把注释写含糊来通过。（这个仓库为此栽过不止一次。）
    """
    out: list[str] = []
    index = 0
    size = len(text)
    while index < size:
        char = text[index]
        if char in "\"'`":
            quote = char
            out.append(char)
            index += 1
            while index < size:
                if text[index] == "\\":
                    out.append(text[index:index + 2])
                    index += 2
                    continue
                out.append(text[index])
                if text[index] == quote:
                    index += 1
                    break
                index += 1
            continue
        if char == "/" and text[index:index + 2] == "//":
            while index < size and text[index] != "\n":
                index += 1
            continue
        if char == "/" and text[index:index + 2] == "/*":
            index += 2
            while index + 1 < size and text[index:index + 2] != "*/":
                index += 1
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _script_of(path: Path) -> str:
    return _strip_js_comments(path.read_text(encoding="utf-8"))


def _names(text: str) -> set[str]:
    return set(_FUNCTION_DECL.findall(text)) | set(_FUNCTION_ASSIGN.findall(text))


def _declared_by_each_page() -> dict[Path, set[str]]:
    return {path: _names(_script_of(path)) for path in _page_files()}


def _declared_in_shared_modules() -> set[str]:
    names: set[str] = set()
    for path in SHARED_JS.glob("*.js"):
        names |= _names(_script_of(path))
    return names


def _called(name: str, text: str) -> bool:
    """`name(` 出现算调用。**排除 `obj.name(` 与 `foo_name(`**（那是别的东西的成员/另一个名字）。"""
    return re.search(r"(?<![\w$.])" + re.escape(name) + r"\s*\(", text) is not None


@pytest.fixture(scope="module")
def scan() -> dict:
    pages = _declared_by_each_page()
    shared = _declared_in_shared_modules()
    counts = collections.Counter()
    for names in pages.values():
        for name in names:
            counts[name] += 1
    # 只在一个页面里定义过、而且共享模块里也没有 → 这个名字是**那个页面的私有物**。
    private: dict[str, Path] = {}
    for path, names in pages.items():
        for name in names:
            if counts[name] == 1 and name not in shared and name not in _VENDOR_PROVIDED:
                private.setdefault(name, path)
    offenders = []
    for path, _names_here in pages.items():
        text = _script_of(path)
        for name, home in private.items():
            if home == path:
                continue
            if _called(name, text):
                offenders.append((name, home.name, path.name))
    return {"pages": pages, "private": private, "offenders": sorted(offenders)}


def test_the_scan_itself_is_not_vacuous(scan):
    """扫描坏掉时（正则不匹配了、目录改了）这条测试必须**失败**，而不是安静地全绿。"""
    assert len(scan["pages"]) >= 20, "页面数量少得不像话 —— 扫描多半没读到东西"
    assert len(scan["private"]) >= 100, "「只在一个页面里定义过」的名字少得不像话"


def test_no_page_calls_a_function_that_another_page_owns(scan):
    offenders = scan["offenders"]

    assert not offenders, (
        "这些调用在本页面里找不到定义，会抛 ReferenceError（而且它后面的语句全部不执行）：\n"
        + "\n".join(f"  {name}() 定义在 {home}，却在 {called_in} 里被调用"
                    for name, home, called_in in offenders)
    )


def test_the_guard_would_have_caught_the_bug_it_exists_for(scan):
    """这条测试的来历：`weekly_version_diff.html` 里那句 `refreshWeeklyAiLatest()`。

    它现在两边都在（调用已改掉、定义仍在合并视图）—— 把判据本身钉住，
    免得哪天判据松了，这条护栏悄悄变成摆设。
    """
    pages = scan["pages"]
    merged = [p for p in pages if p.name == "merged_project_view.html"]
    weekly = [p for p in pages if p.name == "weekly_version_diff.html"]
    assert merged and weekly, "前提：两个页面都要在扫描范围内"

    merged_text = _script_of(merged[0])
    weekly_text = _script_of(weekly[0])
    assert _called("refreshWeeklyAiLatest", merged_text), "前提：合并视图里还在调它"
    assert not _called("refreshWeeklyAiLatest", weekly_text), (
        "weekly 这一页没有这个函数 —— 调它必然 ReferenceError"
    )
    assert "refreshWeeklyAiLatest" in scan["private"], (
        "它应该被判为「合并视图私有」——判据不成立的话上面那条断言就失去意义了"
    )
