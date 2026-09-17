# -*- coding: utf-8 -*-
"""修改行里的「高亮片段」不能移动文字，也不能盖住旁边的字。

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

线上 5978 反馈（同一个类的另一半约束）：`玩家不带动作&不打断技能` 里那个 `&`
**显示不全，被颜色色块挡住了**。为了补上面的左右留白，这两个类一度改成用
不占布局的 `box-shadow: 3px 0 0 …, -3px 0 0 …`，但阴影是不透明的、会真的画出去 ——
`&` 只有几像素宽，被两侧各 3px 盖掉大半。同时 `excel-scroll-fix.css` 末尾的通配
规则还给高亮 span 套了个 1px 边框，行内元素的左右边框参与排版，又把后面的文字
推走 2px。做法见下面第二条测试。

## 这里钉住什么

凡是 `.excel-text-bg-old` / `.excel-text-bg-new` 的规则（`static/css/*.css` 与各模板
内联 `<style>` 里各有一份，必须一致）：

1. 不得 `display: inline-block`（要用行内元素，按基线参与排版）；
2. 不得 `vertical-align: top`，必须是 `vertical-align: baseline`；
3. 左右 padding 必须为 0；
4. **左右不得有 box-shadow / border** —— 高亮段两侧没有预留空间，向左右画出去
   就会盖住或推走紧挨着的分隔符。

这样「高亮」纯粹是视觉：既不移动文字，也不挡住别人的文字。
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


# 会向左右画出去、或推走后续文字的声明。
_SIDE_EFFECTS = (
    'border-left', 'border-right', 'border-width', 'border-left-width', 'border-right-width',
)
_NEUTRAL = ('0', '0px', 'none')


def test_the_highlight_does_not_cover_the_neighbouring_character():
    """高亮**不能盖住紧挨着的那个字符**（线上 5978）。

    分隔符（`,` `/` `&` `|`）只有几像素宽，而高亮段**没有**为左右余白预留空间 ——
    相邻文字就紧贴在它后面。所以任何向左右延伸的绘制都会画在人家身上：

    * `box-shadow: 3px 0 0 <色>, -3px 0 0 <色>`（曾用来撑左右留白）：阴影不参与排版，
      但会**画**出去。「玩家不带动作&不打断技能」里那个 `&` 被两侧各 3px 盖掉大半，
      用户看到的就是「显示不全，被颜色色块挡住了」。逗号、斜杠同样窄，一样躲不过。
    * `border-left/right`：除了多画一圈框，行内元素的左右边框还参与排版，会把这一段
      之后的文字整体推走 2px（正是本文件开头要避免的「移动文字」）。
      `excel-scroll-fix.css` 末尾的通配规则
      （`.excel-table-wrapper *, .excel-diff-table * { border-width: 1px !important }`）
      会给高亮 span 也套上 1px —— 所以必须在高亮自己的规则里显式清掉。

    结论：高亮的左右一律不留余白，背景**恰好**覆盖改动的那几个字符。
    """
    problems = []
    for path, line_no, selector, decls in _highlight_rules():
        where = f'{path}:{line_no} {selector}'
        shadow = (decls.get('box-shadow') or '').replace('!important', '').strip()
        if shadow != 'none':
            problems.append(
                f'{where}：box-shadow={shadow or "(未声明)"!r} —— 必须是 none。'
                '向左右画出去会盖住紧挨着的分隔符（线上 5978 的 `&`）'
            )
        # 必须**显式**清掉边框，而不是「没写就行」：通配规则
        # （`.excel-table-wrapper *, .excel-diff-table * { border-width: 1px !important }`）
        # 会给高亮 span 也套上 1px，不显式清掉就会多出一圈灰框、并把后面的文字推走 2px。
        border = (decls.get('border') or '').replace('!important', '').strip()
        width = (decls.get('border-width') or '').replace('!important', '').strip()
        style = (decls.get('border-style') or '').replace('!important', '').strip()
        cleared = border in _NEUTRAL or (width in _NEUTRAL and style in ('none', '0'))
        if not cleared:
            problems.append(
                f'{where}：没有显式清掉 border（border={border or "(未声明)"!r}, '
                f'border-width={width or "(未声明)"!r}, border-style={style or "(未声明)"!r}）—— '
                '通配规则会给高亮套上 1px 灰框并把后面的文字推走 2px'
            )
        for side in _SIDE_EFFECTS:
            value = (decls.get(side) or '').replace('!important', '').strip()
            if value and value not in _NEUTRAL:
                problems.append(f'{where}：{side}={value!r} —— 会推走后面的文字并多画一圈框')
    assert not problems, (
        '高亮会盖住或推走紧挨着的字符（线上 5978：「玩家不带动作&不打断技能」的 `&` 显示不全）：\n  '
        + '\n  '.join(problems)
    )


if __name__ == '__main__':
    import pytest
    pytest.main([__file__, '-v'])
