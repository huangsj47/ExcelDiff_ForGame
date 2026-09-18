# -*- coding: utf-8 -*-
"""Excel diff 的表体要按「删除行数据 / 新增行数据 / 变更行数据」分段。

## 为什么要有这个测试

表体原本把所有变更按 Excel 行号铺成一条龙：整行删除、整行新增、逐格修改混在
一起，评审者得自己在几十行里认哪几行是「整行没了 / 整行冒出来」。对照的旧面板
是分段的 —— 段前一条横跨整表的标题行（`删除行数据：` / `新增行数据：` /
`变更行数据：`），段内再按行号升序。

## 2026 结构重构后的读法

分段逻辑原先在**两个模板里各有一份**（提交页 `commit_diff.html`、周版本页
`weekly_version_full_diff.html`），两份独立演化、极易漂移，所以老版本是「把两边
的实现抠出来用 node 真跑、要求输出逐字节一致」。

现在它只有一份实现：`static/js/excel_diff_table.js`（表体渲染的共享模块）。
于是本文件改成两件事：

1. **跑真的实现**（不是复刻）：把共享模块按浏览器的加载方式放进 node，
   调 `window.ExcelDiffTable`，喂同一组行，断言分段顺序、段内排序、
   colspan、以及「一行都不丢」——「两份输出一致」这条自然消失了（只有一份）。
2. **负向断言**：两个模板不许再自己实现一遍（段标题 / 行渲染的副本、
   `<td class="excel-cell…">` 都要消失），并且必须真的调共享实现。
   这条是「两份输出一致」的等价替代 —— 老断言防的是「两边漂移」，
   新断言防的是「有人又在某个模板里抄一份」，两者防的是同一件事：
   同一个页面集合里出现两套分段实现。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = shutil.which('node')

COMMIT_TEMPLATE = os.path.join(PROJECT_ROOT, 'templates', 'commit_diff.html')
WEEKLY_TEMPLATE = os.path.join(PROJECT_ROOT, 'templates', 'weekly_version_full_diff.html')
# 分段与行渲染的唯一实现
SHARED_MODULE = os.path.join(PROJECT_ROOT, 'static', 'js', 'excel_diff_table.js')

# 三个页面都加载并调用共享实现（合并页的入口函数也在这一批里改了）
DELEGATING_TEMPLATES = (
    COMMIT_TEMPLATE,
    WEEKLY_TEMPLATE,
    os.path.join(PROJECT_ROOT, 'templates', 'merge_diff.html'),
)
# 三个页面都不许自己再实现一遍（分段 / 行渲染的副本、`<td class="excel-cell…">` 都要消失）。
# 合并页原先还有一条按提交渲染的容器路径（showExcelSheetInContainer /
# createModifiedRowForContainer）是自己拼表的，2026 结构重构里也改成了调共享实现
# （旧行格式在交出去之前归一化），那一份副本已删除，所以它现在也在这一批里。
NO_LOCAL_IMPLEMENTATION_TEMPLATES = (
    COMMIT_TEMPLATE,
    WEEKLY_TEMPLATE,
    os.path.join(PROJECT_ROOT, 'templates', 'merge_diff.html'),
)

# 模板里**不许**再出现的分段/行渲染实现（抄一份回来就会命中）
FORBIDDEN_IN_TEMPLATES = (
    'var DIFF_ROW_GROUPS',
    'function groupChangedRows(',
    'function groupHeaderRowHtml(',
    'function changedRowHtml(',
    'function changedRowsGroupedHtml(',
    'function buildRowRenderPlan(',
)
TD_CELL = re.compile(r'<td class="excel-cell[^"]*"')

GROUP_LABELS = ['删除行数据', '新增行数据', '变更行数据']

# 按浏览器的方式加载共享模块：给它一个 window（模块只往外挂 ExcelDiffTable，
# 内部函数不外泄），并把展示口径 window.formatCellValue 装上 ——
# 模块自己**不重新实现**它（见文件头第 1 条契约）。
HARNESS = '''
const window = {};
window.formatCellValue = function (value) {
    if (value === null || value === undefined) return '';
    if (typeof value === 'number' && isNaN(value)) return '';
    return String(value);
};
%s
const T = window.ExcelDiffTable;
const rows = JSON.parse(process.argv[2]);
const headers = ['A', 'B', 'C'];
process.stdout.write(JSON.stringify({
    html: T.changedRowsGroupedHtml(rows, headers),
    groups: T.groupChangedRows(rows).map(g => ({label: g.label, rows: g.rows.map(r => r.row_number)}))
}));
'''

# 段里的行号 = 行号格里的文本（修改行是 rowspan=2 的那一格，也在其中）
ROW_NUMBER_RE = re.compile(r'class="excel-row-number[^"]*"[^>]*>([^<]*)<')


def _run(rows):
    if NODE is None:
        pytest.skip('本机没有 node，跳过（这是唯一能真跑共享实现的方式）')
    with open(SHARED_MODULE, encoding='utf-8') as handle:
        module_source = handle.read()
    script = HARNESS % module_source
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as handle:
        handle.write(script)
        path = handle.name
    try:
        proc = subprocess.run([NODE, path, json.dumps(rows)],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, f'node 跑失败：{proc.stderr[:500]}'
        return json.loads(proc.stdout)
    finally:
        os.unlink(path)


ROWS = [
    {'row_number': 9, 'status': 'modified', 'cell_changes': []},
    {'row_number': 3, 'status': 'added'},
    {'row_number': 12, 'status': 'removed'},
    {'row_number': 5, 'status': 'modified', 'cell_changes': []},
    {'row_number': 2, 'status': 'removed'},
    {'row_number': 7, 'status': 'added'},
]


def test_sections_are_ordered_and_labeled():
    result = _run(ROWS)
    labels = [group['label'] for group in result['groups']]
    assert labels == GROUP_LABELS, (
        f'分组顺序是 {labels}，应为 {GROUP_LABELS}（整行删除 → 整行新增 → 逐格修改）')

    html = result['html']
    positions = [html.index(f'{label}：') for label in GROUP_LABELS]
    assert positions == sorted(positions), f'标题行在正文里的出现顺序不对：{positions}'
    # 段与行要对得上：删除段里只能有删除行（按行号升序），不能混进别的段的行
    removed_segment = html[html.index('删除行数据：'):html.index('新增行数据：')]
    assert ROW_NUMBER_RE.findall(removed_segment) == ['2', '12'], (
        f'删除段里的行号是 {ROW_NUMBER_RE.findall(removed_segment)}，应为段内的删除行（升序）')


def test_rows_are_sorted_inside_a_section():
    result = _run(ROWS)
    by_label = {group['label']: group['rows'] for group in result['groups']}
    assert by_label['删除行数据'] == [2, 12], '删除段内应按行号升序'
    assert by_label['新增行数据'] == [3, 7], '新增段内应按行号升序'
    assert by_label['变更行数据'] == [5, 9], '变更段内应按行号升序'


def test_header_row_spans_the_whole_table():
    """段标题要横跨整表：colspan = 列数 + 1（行号列也要覆盖）。

    这条同时钉住「段标题以『标签：』开头」—— 计数只能是标签**之后**的
    inline 元素，插在标签与冒号之间就会让这个正则失配。
    """
    html = _run(ROWS)['html']
    for label in GROUP_LABELS:
        match = re.search(r'colspan="(\d+)"[^>]*>' + label + '：', html)
        assert match, f'标题行没有 colspan，或者「{label}：」不再是段标题的开头'
        assert match.group(1) == '4', (
            f'{label} 的 colspan 是 {match.group(1)}，应为 列数+1 = 4（行号列也要覆盖）')


def test_no_row_is_lost():
    """状态不是这三种的行（例如整表未变更行）不能因为分组被丢掉。"""
    rows = ROWS + [{'row_number': 42, 'status': 'unchanged'}]
    html = _run(rows)['html']
    numbers = ROW_NUMBER_RE.findall(html)
    assert '42' in numbers, '未分组状态的行被分组逻辑吞掉了'
    assert len(numbers) == len(rows), (
        f'渲染出的行号格有 {len(numbers)} 个，输入 {len(rows)} 行 —— 有行丢了或重复')


class TestTheTemplatesOnlyDelegate:
    """三个页面都只调共享实现（`test_the_template_loads_and_calls_the_shared_module`），
    其中两个入口页不得再自己实现一遍行渲染（`test_the_template_does_not_build_rows_itself`）。

    这是老断言 `test_both_templates_produce_identical_output`（「两份实现逐字节一致」）
    的等价替代：那份实现现在只有一份，逐字节比对没有了对象；真正还要防的是
    「有人在某个模板里又抄一份分段/行渲染」—— 一旦抄了，逐字节一致那条也守不住
    （两份会各自演化，正是这个仓库踩过的坑）。
    """

    @pytest.mark.parametrize('template', NO_LOCAL_IMPLEMENTATION_TEMPLATES)
    def test_the_template_does_not_build_rows_itself(self, template):
        with open(template, encoding='utf-8') as handle:
            source = handle.read()
        name = os.path.basename(template)
        leftovers = [marker for marker in FORBIDDEN_IN_TEMPLATES if marker in source]
        assert not leftovers, (
            f'{name} 里又出现了分段/行渲染的实现：{leftovers} —— '
            f'表体渲染的唯一实现在 static/js/excel_diff_table.js')
        assert not TD_CELL.findall(source), (
            f'{name} 又在自己拼 .excel-cell 单元格了 —— 行渲染只该有一份实现')

    @pytest.mark.parametrize('template', DELEGATING_TEMPLATES)
    def test_the_template_loads_and_calls_the_shared_module(self, template):
        with open(template, encoding='utf-8') as handle:
            source = handle.read()
        name = os.path.basename(template)
        assert "js/excel_diff_table.js" in source, f'{name} 没有加载共享模块'
        assert re.search(r'ExcelDiffTable\.(renderSheetTable|mountSheetTable|tableHeadHtml)',
                         source), (
            f'{name} 加载了共享模块却没有调它 —— 表体不会经由唯一那份实现渲染')


def test_the_shared_module_is_the_only_place_that_builds_row_html():
    """共享模块本身要真的会拼 `.excel-cell` —— 否则上面那两条负向断言会「全绿」而表格没了。"""
    with open(SHARED_MODULE, encoding='utf-8') as handle:
        source = handle.read()
    cells = TD_CELL.findall(source)
    assert cells, '共享模块里没有 .excel-cell 单元格 —— 行渲染的实现不见了'
    assert source.count('excel-cell-inner') == len(cells), (
        f'{len(cells)} 个单元格但只有 {source.count("excel-cell-inner")} 个内层容器')


def test_group_header_style_exists():
    """标题行得有样式，否则只是一行没背景的普通文字。"""
    for name in ('diff-styles.css', 'excel-scroll-fix.css'):
        with open(os.path.join(PROJECT_ROOT, 'static', 'css', name), encoding='utf-8') as handle:
            css = handle.read()
        assert 'excel-row-group-header' in css, f'{name} 里没有分组标题行的样式'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
