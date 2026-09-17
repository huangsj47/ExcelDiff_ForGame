# -*- coding: utf-8 -*-
"""修改行里的「高亮片段」不能移动文字。

## 缺陷形态（线上实测）

提交页/周版本页的 Excel 表里，修改行是上下两行：上面旧值、下面新值，被改动的
**参数片段**用背景色高亮（`.excel-text-bg-old` / `.excel-text-bg-new`）。

线上 5966 反馈：`13,39,10000` 与 `13,39,10001` 这一对里，被高亮的 `10000`/`10001`
跟前面的 `13,39` 对不齐。成因就在这两个类上：

    padding: 2px 4px;
    display: inline-block;
    vertical-align: top;

`inline-block` 让高亮段成为一个独立的行内块，`vertical-align: top` 把它的**顶边**
贴到行框顶部，再加上上下各 2px 的 padding —— 块里的文字比同一行里没高亮的文字
**低 2px**；左右各 4px 的 padding 又让高亮段的文字比原位置**右移 4px**。于是
「前缀 + 高亮片段」这种拼接出来的单元格，被高亮的那一段总是偏下偏右。

## 这里钉住什么

凡是 `.excel-text-bg-old` / `.excel-text-bg-new` 的规则（`static/css/*.css` 与各模板
内联 `<style>` 里各有一份，必须一致）：

1. 不得 `display: inline-block`（要用行内元素，按基线参与排版）；
2. 不得 `vertical-align: top`，必须是 `vertical-align: baseline`；
3. 左右 padding 必须为 0（改由不占布局的 `box-shadow` 撑出留白）。

这样「高亮」纯粹是视觉，文字位置与压根没高亮时**完全一致**。
"""
from __future__ import annotations

import glob
import os
import re

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_HIGHLIGHT_SELECTOR = re.compile(r'\.excel-text-bg-(old|new)\b')
# `:not(.excel-text-bg-old)` 之类的排除式选择器不是「定义高亮样式」，
# 参与匹配会误伤（.excel-table-wrapper *:not(…) 这样一条背景色规则）。
_NOT_CLAUSE = re.compile(r':not\([^)]*\)')
_RULE = re.compile(r'([^{}]+)\{([^{}]*)\}', re.S)
_DECL = re.compile(r'([a-z-]+)\s*:\s*([^;]+)', re.I)


def _sources():
    paths = sorted(glob.glob(os.path.join(PROJECT_ROOT, 'static', 'css', '*.css')))
    paths += sorted(glob.glob(os.path.join(PROJECT_ROOT, 'templates', '**', '*.html'), recursive=True))
    return paths


def _highlight_rules():
    """产出 [(相对路径, 行号, 选择器, {属性: 值}), …]。"""
    for path in _sources():
        with open(path, encoding='utf-8') as handle:
            text = handle.read()
        for match in _RULE.finditer(text):
            selector, body = match.group(1), match.group(2)
            if not _HIGHLIGHT_SELECTOR.search(_NOT_CLAUSE.sub('', selector)):
                continue
            decls = {}
            for name, value in _DECL.findall(body):
                decls[name.strip().lower()] = ' '.join(value.split())
            line_no = text.count('\n', 0, match.start()) + 1
            yield os.path.relpath(path, PROJECT_ROOT), line_no, ' '.join(selector.split()), decls


def _padding_sides(value):
    """把 padding 简写拆成 (top, right, bottom, left)。"""
    parts = value.replace('!important', '').split()
    numbers = [p for p in parts if p]
    if not numbers:
        return None
    if len(numbers) == 1:
        return (numbers[0],) * 4
    if len(numbers) == 2:
        return (numbers[0], numbers[1], numbers[0], numbers[1])
    if len(numbers) == 3:
        return (numbers[0], numbers[1], numbers[2], numbers[1])
    return tuple(numbers[:4])


def test_highlight_rules_are_baseline_aligned_inline():
    problems = []
    for path, line_no, selector, decls in _highlight_rules():
        where = f'{path}:{line_no} {selector}'
        display = (decls.get('display') or '').replace('!important', '').strip()
        if display and display != 'inline':
            problems.append(f'{where}：display 是 {display!r} —— 必须用行内元素（inline-block 会按顶边对齐、把文字压低）')
        valign = (decls.get('vertical-align') or '').replace('!important', '').strip()
        if valign != 'baseline':
            problems.append(f'{where}：vertical-align 是 {valign!r} —— 必须是 baseline，否则高亮段与同行文字不在一条基线上')
        padding = decls.get('padding')
        if padding:
            sides = _padding_sides(padding)
            if sides is None or sides[1] not in ('0', '0px') or sides[3] not in ('0', '0px'):
                problems.append(f'{where}：padding={padding!r} 左右不为 0 —— 高亮段会把后面的文字推走（留白请用不占布局的 box-shadow）')
        for side in ('padding-left', 'padding-right'):
            value = (decls.get(side) or '').replace('!important', '').strip()
            if value and value not in ('0', '0px'):
                problems.append(f'{where}：{side}={value!r} 会把同一行里后面的文字推走')
    assert not problems, (
        '高亮片段会移动文字（线上 5966：被高亮的数字跟前面的前缀对不齐）：\n  ' + '\n  '.join(problems))


def test_every_highlight_definition_agrees():
    """同一个类在多处定义（多个 CSS 文件 + 模板内联样式），关键属性必须一致。

    历史上这几份是各自演化的：`static/css/excel-scroll-fix.css` 最后加载、带
    `!important`，所以它才是最终生效值 —— 只改其中一份等于没改。
    """
    by_class = {}
    for path, line_no, selector, decls in _highlight_rules():
        for cls in ('excel-text-bg-old', 'excel-text-bg-new'):
            if cls in selector and f'.{cls}' in selector:
                for key in ('display', 'vertical-align'):
                    value = (decls.get(key) or '').replace('!important', '').strip()
                    if value:
                        by_class.setdefault((cls, key), {}).setdefault(value, []).append(f'{path}:{line_no}')
    disagreements = {k: v for k, v in by_class.items() if len(v) > 1}
    assert not disagreements, (
        '同名高亮类在不同文件里的关键属性不一致（最后加载的那份才生效，改一处必须一起改）：\n  '
        + '\n  '.join(f'{cls}.{key}: {values}' for (cls, key), values in disagreements.items()))


if __name__ == '__main__':
    import pytest
    pytest.main([__file__, '-v'])
