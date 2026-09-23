# -*- coding: utf-8 -*-
"""Excel diff 表格「第一列冻结」的守卫。

## 缺陷形态（线上可见）

`.excel-row-number`（行号列）在 `excel-diff-new.css` 里本来是
`position: sticky; left: 0`，也就是设计上就是冻结列。但实测**同一张表里时冻时不冻**：

    第一列单元格               position     横向滚动 320px 后相对容器左缘
    ID / 字段（表头）           sticky       +1px    ✅ 冻结
    行号（未修改行）            sticky       +1px    ✅ 冻结
    行号 removed / added / modified  relative  −317px  ❌ 跟着滑走

两个各自独立的成因：

1. `excel-scroll-fix.css` 为了修「滚动时背景色扩散」，给
   `.excel-row-number.excel-removed/.excel-added/.excel-modified` 写了
   **`position: relative !important`** —— 特异性更高又带 `!important`，
   直接把 `sticky` 顶掉了。这三个恰好是评审最需要盯的行。
2. 即便改回 sticky，**修改行**的行号格仍会看不见（几何上位置正确，只是被画在下面）。
   修改行是**成对**渲染的，-old 里的未变更格子带 `rowspan="2"`，横跨到 -new 那一行带，
   于是横向滚动后它们滑到冻结列所在的 x，把 -new 自己的行号格盖住。
   历史上这里有两层错：`-old` 与 `-new` 各自被 `isolation: isolate` 变成层叠上下文
   （行号格的 z-index 被困在行里出不来），而当时的修法是给 `-old` 加
   `position: relative; z-index: 3` —— 那只把 **-old 整行**抬高，于是 -old 的 rowspan
   格子反过来盖住 -new 的行号格（线上表现：「改动前的行号冻住了，改动后的没冻住」）。
   现在的做法：**两个 tr 都不生成层叠上下文**（`isolation` / `z-index` /
   `contain: layout|paint` / `transform` / `filter` / `opacity<1` / `will-change`
   一个都不能有），行号格自己的 `position: sticky` + `z-index: 2` 于是直接在**表的
   层叠上下文**里说话，永远压在普通单元格之上 —— 现代形态（上下两格）与老形态
   （-old 上那个 `rowspan="2"` 的 `excel-modified` 格）同时成立。

表头那一格还有第三层：`thead` 与两条表头 `tr` 都是 `static`、不生成层叠上下文，
所有表头 th 在同一上下文里比 z-index，而 `.excel-row-header` 自己写的
`z-index: 8` **根本轮不到生效** —— `excel-diff-new.css` 的
`.excel-diff-table th`(10) 与 `.excel-field-row th`(9) 特异性都更高，
于是 ID 拿到 10、字段拿到 9，与同排其它表头**正好相等**；相等时按 DOM 顺序决定，
第一列在最前面 → 被后面的表头盖住。

## 为什么这些断言必须存在

三条都不抛异常、不报错，只是「列会滑走」「字会串」，人眼扫过去很容易当成正常的
横向滚动。`excel-scroll-fix.css` 里有 180 多条 `!important`，任何一个
`position` / `z-index` / `isolation` 声明都能静默地把冻结功能关掉，所以这里把
「行号列只能是 sticky，且必须压在普通单元格之上」这条不变量钉死。

第 2 条**量不出几何**：被盖住的那一格 `getBoundingClientRect()` 位置是对的
（这正是上一轮把缺陷判成绿的原因）。真渲染下的判据是
`document.elementFromPoint(行号格中心) === 该格`，见
`.pytest_tmp/ui_split/sticky_probe.py`（现代形态 + 老形态，scrollLeft 0/900/2500）。
"""
from __future__ import annotations

import os
import re

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS_DIR = os.path.join(PROJECT_ROOT, 'static', 'css')


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def _strip_comments(css: str) -> str:
    """注释里会举例写出要禁掉的写法，必须先剥离，否则把说明当成声明。"""
    return re.sub(r'/\*.*?\*/', ' ', css, flags=re.S)


def _rules(css: str):
    out = []
    for selector, body in re.findall(r'([^{}]+)\{([^{}]*)\}', _strip_comments(css)):
        decls = {}
        for decl in body.split(';'):
            if ':' not in decl:
                continue
            prop, _, value = decl.partition(':')
            decls[prop.strip().lower()] = ' '.join(value.split())
        out.append((selector.strip(), decls))
    return out


def _first_keyword(decls: dict, prop: str):
    """取声明的第一个关键字，忽略 `!important`。缺声明时返回 None。"""
    value = decls.get(prop)
    if value is None:
        return None
    parts = value.split()
    return parts[0] if parts else None


def _number(decls: dict, prop: str):
    """取声明里的整数值，忽略 `!important`。取不到返回 None。"""
    value = decls.get(prop)
    if value is None:
        return None
    match = re.search(r'-?\d+', value)
    return int(match.group()) if match else None


def _all_rules():
    out = []
    for name in sorted(os.listdir(CSS_DIR)):
        if name.endswith('.css'):
            out.extend((name, sel, decls) for sel, decls in _rules(_read(os.path.join(CSS_DIR, name))))
    return out


def test_no_rule_ever_makes_a_row_number_relative():
    """行号列一旦被设成 relative，冻结就没了 —— 这条不变量是本文件的核心。

    `position: relative !important` 正是线上那个「新增/删除/修改行不冻结」的成因。
    允许出现 `sticky`（冻结）或干脆不声明 position（由低特异性规则给 sticky）。
    """
    offenders = []
    for name, selector, decls in _all_rules():
        if '.excel-row-number' not in selector:
            continue
        position = _first_keyword(decls, 'position')
        if position is not None and position != 'sticky':
            offenders.append(f'{name}: {selector} → position: {decls.get("position")}')
    assert not offenders, (
        '有规则把行号列的 position 设成了非 sticky，横向滚动时第一列会跟着滑走：\n  '
        + '\n  '.join(offenders)
    )


def test_the_background_bleed_fix_keeps_the_row_number_sticky():
    """专盯那条修「背景色扩散」的规则：它必须继续存在，但方式是 sticky 而不是 relative。

    它同时要求 `left: 0` 被显式写出来：低特异性那条虽已给 left:0，
    这里要保证将来有人给它加更高优先级的覆盖时仍是 0。

    同选择器上本文件里有好几条规则（背景色、尺寸、层叠…），所以按**属性**找，
    而不是按「第一条命中的规则」——后者会抓到不含 position 的那条。
    """
    matched = None
    bleed = None
    for name, selector, decls in _all_rules():
        if name != 'excel-scroll-fix.css':
            continue
        if '.excel-row-number' not in selector:
            continue
        if 'excel-removed' not in selector or 'excel-added' not in selector:
            continue
        if _first_keyword(decls, 'position') is not None:
            matched = (selector, decls)
        if 'background-attachment' in decls:
            bleed = (selector, decls)
    assert matched is not None, (
        'excel-scroll-fix.css 里找不到给「有背景色的行号」声明 position 的那条规则 —— '
        '冻结列的位置约定没了'
    )
    selector, decls = matched
    assert _first_keyword(decls, 'position') == 'sticky', (
        f'{selector} 的 position 不是 sticky：{decls.get("position")}'
    )
    assert _number(decls, 'left') == 0, (
        f'{selector} 没有显式写 left: 0（当前 {decls.get("left")}），冻结列的位置不确定'
    )
    assert bleed is not None, (
        '承载 background-attachment 修复的那条规则不见了 —— '
        '删掉会复发「横向滚动时背景色扩散」'
    )


def _opacity_creates_context(value):
    """opacity < 1 才生成层叠上下文；`opacity: 1` 不是。"""
    if value is None:
        return False
    match = re.search(r'\d*\.?\d+', value)
    return bool(match) and float(match.group()) < 1


# 会让元素成为**层叠上下文**的声明。任何一个落到修改行的 <tr> 上，行号格的
# z-index 就被关在那一行里，出不到表的层叠上下文 —— 于是 -old 的 rowspan 格子
# （横跨到 -new 那一行带）会盖住 -new 的行号格。
# `position` 只列 sticky/fixed：这两个**无条件**生成上下文；relative/absolute
# 要配 z-index 才算，由 z-index 那条兜住。
_CONTEXT_CREATORS = (
    ('isolation', lambda v: v not in (None, 'auto')),
    ('contain', lambda v: v is not None and any(
        word in v.replace('!important', '')
        for word in ('layout', 'paint', 'strict', 'content'))),
    ('transform', lambda v: v not in (None, 'none')),
    ('filter', lambda v: v not in (None, 'none')),
    ('backdrop-filter', lambda v: v not in (None, 'none')),
    ('perspective', lambda v: v not in (None, 'none')),
    ('will-change', lambda v: v not in (None, 'auto')),
    ('opacity', _opacity_creates_context),
)


def _stacking_context_reason(decls: dict):
    """这个声明块会不会让元素成为层叠上下文？返回那条声明，否则 None。"""
    position = _first_keyword(decls, 'position')
    if position in ('sticky', 'fixed'):
        return f'position: {decls["position"]}'
    zindex = _number(decls, 'z-index')
    if zindex is not None and _first_keyword(decls, 'z-index') != 'auto':
        return f'z-index: {decls["z-index"]}'
    for prop, is_context in _CONTEXT_CREATORS:
        if is_context(decls.get(prop)):
            return f'{prop}: {decls[prop]}'
    return None


def test_the_modified_rows_do_not_become_stacking_contexts():
    """修改行的两个 <tr> 都不许生成层叠上下文 —— 这是「行号格压在普通单元格之上」的前提。

    守的行为：**横向滚动后，四个修改行的行号格都看得见**（现代形态：粉/绿上下两格；
    老形态：-old 上一个 `rowspan="2"` 的 `excel-modified` 格）。修改行成对渲染，
    -old 的未变更格子带 `rowspan="2"`、横跨到 -new 那一行带，滚动后正好滑到冻结列
    所在的 x；只要两个 tr 都不生成层叠上下文，行号格自己的 `z-index: 2`（下面那条
    测试找的就是它）就能在**表的层叠上下文**里压住这些格子。

    反例（都真实出现过）：给 -old 加 `isolation: isolate`，或加
    `position: relative; z-index: 3`。后者是历史上的「修法」，它把 -old **整行**
    抬高，于是 -old 的 rowspan 格子反过来盖住 -new 的行号格 —— 线上表现就是
    「改动前的行号冻住了、改动后的没冻住」。所以这里不是钉某一条写法，而是钉
    「这两个 tr 上不许出现任何生成层叠上下文的声明」。
    """
    offenders = []
    for name, selector, decls in _all_rules():
        if 'tr.excel-row-modified-old' not in selector and 'tr.excel-row-modified-new' not in selector:
            continue
        if 'td' in selector:                      # 单元格自己的规则不在此列
            continue
        reason = _stacking_context_reason(decls)
        if reason:
            offenders.append(f'{name}: {selector} → {reason}')
    assert not offenders, (
        '修改行的 <tr> 成了层叠上下文，行号格的 z-index 被困在行里：\n  '
        + '\n  '.join(offenders)
        + '\n（-old 的 rowspan 格子横跨到 -new 那一行带，横向滚动后会盖住 -new 的行号格：'
          '「改动前的行号冻住了、改动后的没冻住」）'
    )


def test_row_numbers_are_lifted_above_ordinary_cells():
    """行号格的 z-index 要在**表的层叠上下文**里高过普通单元格。

    上一条测试保证行号格不在行级上下文里；这一条保证它确实被抬起来了（否则
    DOM 顺序更靠后的 -new 的单元格会盖住 -old 的行号格）。
    另外顺带钉住反方向：普通单元格（`.excel-cell`，不含行号格）不许拿到
    比行号格更高的 z-index —— 那会从另一头把冻结列盖掉。
    """
    lifted = None
    for name, selector, decls in _all_rules():
        if '.excel-row-number' not in selector:
            continue
        if not ('excel-removed' in selector and 'excel-added' in selector):
            continue
        zindex = _number(decls, 'z-index')
        if zindex is not None and _first_keyword(decls, 'z-index') != 'auto':
            lifted = (f'{name}: {selector}', zindex)
    assert lifted is not None, (
        '找不到给 removed/added/modified 行号格声明 z-index 的规则 —— 行号格没有被抬起来，'
        'DOM 更靠后的修改行单元格会盖住它'
    )
    where, zindex = lifted
    assert zindex >= 1, f'{where} 的 z-index={zindex}，压不住 z-index: auto 的普通单元格'

    offenders = []
    for name, selector, decls in _all_rules():
        if '.excel-cell' not in selector or '.excel-row-number' in selector:
            continue
        other = _number(decls, 'z-index')
        if other is not None and _first_keyword(decls, 'z-index') != 'auto' and other >= zindex:
            offenders.append(f'{name}: {selector} → z-index: {decls["z-index"]}')
    assert not offenders, (
        f'这些规则给普通单元格的 z-index ≥ 行号格的 {zindex}，会从另一头盖住冻结列：\n  '
        + '\n  '.join(offenders)
    )


def test_the_frozen_header_outranks_the_other_header_cells():
    """左上角那格必须是表头里最高的。

    `.excel-row-header` 自己写的 z-index: 8 会被 `.excel-diff-table th`(10) /
    `.excel-field-row th`(9) 以更高特异性压掉，实测拿到 10 / 9 ——
    与同排其它表头相等，于是按 DOM 顺序被它们盖住。
    这里直接跟「其它表头实际拿到的值」比，而不是硬编码一个常量。
    """
    sibling_max = 0
    for name, selector, decls in _all_rules():
        if '.excel-column-header' in selector or '.excel-field-row th' in selector:
            z = _number(decls, 'z-index')
            if z is not None:
                sibling_max = max(sibling_max, z)
    assert sibling_max > 0, '找不到其它表头单元格的 z-index，断言失去参照'

    frozen_max = 0
    for name, selector, decls in _all_rules():
        # 需要「th + .excel-row-header」这种高于裸 .excel-row-header 的选择器
        if '.excel-row-header' not in selector or 'th' not in selector:
            continue
        z = _number(decls, 'z-index')
        if z is not None:
            frozen_max = max(frozen_max, z)
    assert frozen_max > sibling_max, (
        f'冻结表头列的 z-index({frozen_max}) 没有高过同排其它表头的最大值({sibling_max})；'
        'z-index 相等时由 DOM 顺序决定，第一列在最前面，会被后面的表头盖住'
    )


def test_both_templates_keep_sticky_available_on_the_row_number():
    """生成表格的三个模板都要经由**渲染行号列的那一份实现**出表。

    这条拦的是「CSS 修好了，但某个页面换了类名/没这个类」—— 那个页面的第一列会
    完全不冻结，而且不会有任何报错。

    2026 结构重构后，`excel-row-number` 不再写在三个模板里，而是写在表体渲染的
    共享实现 `static/js/excel_diff_table.js` 里（三个页面的表体都由它渲染）。
    所以断言换成同一条链的两端：**共享实现里必须真的产出 `.excel-row-number`**
    （否则三个页面一起丢冻结），**且这三个模板都必须加载并调用它**（某个页面漏掉
    脚本或改回自己拼表，那一页就又回到「第一列不冻结」）。牙齿没拔：
    老断言管「每个模板都渲染行号列」，新断言管「每个模板都经由唯一那份渲染行号列的
    实现」，中间被绕过的可能性反而更小了（类名只有一处可改）。
    """
    with open(os.path.join(PROJECT_ROOT, 'static/js/excel_diff_table.js'), encoding='utf-8') as fh:
        shared = fh.read()
    assert 'excel-row-number' in shared, (
        'static/js/excel_diff_table.js 不产出 excel-row-number —— '
        '三个页面的第一列都会不冻结'
    )
    assert re.search(r'class="excel-row-number', shared), (
        '共享实现里 excel-row-number 只出现在注释里，没有真的拼进行号格'
    )

    for template in ('templates/commit_diff.html', 'templates/merge_diff.html',
                     'templates/weekly_version_full_diff.html'):
        path = os.path.join(PROJECT_ROOT, template)
        with open(path, encoding='utf-8') as fh:
            src = fh.read()
        assert "js/excel_diff_table.js" in src, (
            f'{template} 没有加载表体渲染的共享实现 —— 它的表体要么渲染不出来，'
            f'要么走的是另一份实现（那一份的行号列不一定冻结）'
        )
        assert re.search(r'ExcelDiffTable\.(renderSheetTable|mountSheetTable|tableHeadHtml)', src), (
            f'{template} 加载了共享实现却没有调它 —— 该页的表格不由它渲染，'
            f'所以「第一列冻结」在这页没有任何保证'
        )
