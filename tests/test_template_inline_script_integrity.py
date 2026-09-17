# -*- coding: utf-8 -*-
"""模板内联脚本必须能在浏览器里完整闭合。

## 缺陷形态（线上实测）

周版本文件完整 diff 页面（`/weekly-version-config/<id>/file-full-diff`）控制台报：

    file-full-diff?file_path=…:1030 Uncaught SyntaxError: Unexpected end of input

页面只剩静态骨架：`loadDiffContent()` 里 `.then(data => {` 之后的函数
（`showWeeklyExcelSheet`、`createModifiedRow`、`highlightDifferences` …）**全都没定义**。

原因不在 JS 语法，而在 HTML 的解析规则：`templates/weekly_version_full_diff.html`
的一条 JS **注释**里写了脚本结束标签的字面序列（小于号紧跟斜杠再跟 script）。
HTML 解析器在脚本的数据态里看到它**就地结束 script 元素** —— JS 的注释语法对它
没有意义。于是脚本在 `.then(data => {` 的函数体中间被截断，剩下的大半脚本被当成
HTML 文本，末尾那个真正的结束标签变成孤立的结束标签（被解析器忽略）。

## 这里钉住什么

按 HTML 的 script-data 规则把每个模板切一遍脚本元素，然后要求：

1. 每个开始的脚本元素都能找到自己的结束标签（不存在「未闭合」）；
2. 切完之后**不剩下**任何孤立的结束标签 —— 剩下的那一个正是「注释里写了字面
   结束标签、把脚本提前截断」留下的痕迹，也就是本次线上缺陷的指纹。

`<style>` 同理会提前结束，一并检查。
"""
from __future__ import annotations

import glob
import os
import re

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(PROJECT_ROOT, 'templates')

# 起始标签：<script ...>（不匹配自闭合写法之外的怪东西；模板里没有自闭合脚本）
_START = re.compile(r'<(script|style)\b[^>]*>', re.I)
# 结束标签：</script 后面跟空白、/ 或 > —— 这是 HTML 认可的结束标签形态
_END = re.compile(r'</(script|style)[\s/>]', re.I)


def _scan(text):
    """返回 (elements, leftovers)。

    elements: [(tag, 起始标签文本, 内容), …]，按 HTML 的规则切分 ——
              内容在遇到第一个结束标签处就结束，**无论它当时处于什么 JS/CSS 上下文**。
    leftovers: 切分结束后剩下的孤立结束标签 [(tag, 行号, 该行文本), …]。
    """
    elements, leftovers = [], []
    pos, n = 0, len(text)
    while True:
        m = _START.search(text, pos)
        if not m:
            break
        tag = m.group(1).lower()
        e = _END.search(text, m.end())
        if not e:
            # 没有结束标签：内容一直吃到文件末尾 —— 未闭合
            elements.append((tag, m.group(0), text[m.end():], None))
            pos = n
            break
        elements.append((tag, m.group(0), text[m.end():e.start()], e.start()))
        pos = e.start() + 1

    for m in _END.finditer(text, pos):
        line_no = text.count('\n', 0, m.start()) + 1
        line = text.splitlines()[line_no - 1].strip()
        leftovers.append((m.group(1).lower(), line_no, line))
    return elements, leftovers


def _template_files():
    return sorted(glob.glob(os.path.join(TEMPLATES, '**', '*.html'), recursive=True))


def test_every_inline_script_or_style_closes():
    problems = []
    for path in _template_files():
        with open(path, encoding='utf-8') as handle:
            text = handle.read()
        elements, leftovers = _scan(text)
        for tag, start_tag, _content, terminator in elements:
            if terminator is None:
                problems.append(f"{os.path.relpath(path, PROJECT_ROOT)}：{start_tag!r} 没有结束标签（脚本会一路吃到文件末尾）")
        for tag, line_no, line in leftovers:
            problems.append(
                f"{os.path.relpath(path, PROJECT_ROOT)}:{line_no}：脚本/样式区里出现了字面结束标签 —— "
                f"HTML 解析器会在这里就地结束该元素。该行：{line[:80]!r}")
    assert not problems, (
        "模板里的内联脚本会被提前截断（浏览器报 Unexpected end of input，后续函数全部不定义）：\n  "
        + "\n  ".join(problems))


def test_script_areas_do_not_contain_the_literal_end_tag_sequence():
    """更直接的指纹：脚本元素的**内容**里不应该出现结束标签序列。

    `_scan` 是按「第一个结束标签就是真结束」切的，所以内容里天然不会有；
    这里换个方向 —— 直接在整份文本里找「结束标签序列」，再确认它只出现在
    每个脚本元素的末尾。任何一个多出来的都是提前截断的信号。
    """
    offenders = []
    for path in _template_files():
        with open(path, encoding='utf-8') as handle:
            text = handle.read()
        elements, leftovers = _scan(text)
        expected_terminators = {term for _t, _s, _c, term in elements if term is not None}
        for m in _END.finditer(text):
            if m.start() in expected_terminators:
                continue
            # 允许写在 JS 字符串里的转义写法 <\/script —— 它不含结束标签序列
            if text[max(0, m.start() - 1)] == '\\':
                continue
            line_no = text.count('\n', 0, m.start()) + 1
            offenders.append(f"{os.path.relpath(path, PROJECT_ROOT)}:{line_no}")
    assert not offenders, (
        "这些位置出现了不属于任何脚本元素结尾的结束标签序列（会把插在它前面的脚本提前截断）：\n  "
        + "\n  ".join(sorted(set(offenders))))


if __name__ == '__main__':
    import pytest
    pytest.main([__file__, '-v'])
