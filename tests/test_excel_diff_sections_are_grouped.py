# -*- coding: utf-8 -*-
"""Excel diff 的表体要按「删除行数据 / 新增行数据 / 变更行数据」分段。

## 为什么要有这个测试

表体原本把所有变更按 Excel 行号铺成一条龙：整行删除、整行新增、逐格修改混在
一起，评审者得自己在几十行里认哪几行是「整行没了 / 整行冒出来」。对照的旧面板
是分段的 —— 段前一条横跨整表的标题行（`删除行数据：` / `新增行数据：` /
`变更行数据：`），段内再按行号升序。

分段逻辑在**两个模板里各有一份**（提交页 `commit_diff.html`、周版本页
`weekly_version_full_diff.html`），而这两份是独立演化的，极易漂移。所以这里
不是「grep 一下有没有这段代码」，而是**把两边的实现抠出来用 node 真跑**，
喂同一组行，要求输出逐字节一致。
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

# 抠出「分组 + 分段渲染」那段实现：从分组定义起，到下一个函数区之前
EXTRACT = {
    COMMIT_TEMPLATE: (r'var DIFF_ROW_GROUPS = \[', r'\n// 创建新增行'),
    WEEKLY_TEMPLATE: (r'var DIFF_ROW_GROUPS = \[', r'\n// 获取修改的列'),
}

GROUP_LABELS = ['删除行数据', '新增行数据', '变更行数据']

HARNESS = '''
// 模板里的转义函数（两个模板各有一份，实现相同；这里给个等价的最小实现）
function escapeHtml(text) {
    if (text === null || text === undefined) return '';
    return String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
// 用桩替换真正的行渲染：只关心「标题行插在哪、段的顺序、有没有丢行」
function changedRowHtml(row) {
    return 'ROW:' + (row.row_number === undefined ? '?' : row.row_number) + ':' + row.status + ';';
}
const rows = JSON.parse(process.argv[2]);
const headers = ['A', 'B', 'C'];
process.stdout.write(JSON.stringify({
    html: changedRowsGroupedHtml(rows, headers),
    groups: groupChangedRows(rows).map(g => ({label: g.label, rows: g.rows.map(r => r.row_number)}))
}));
'''


def _extract(template_path):
    with open(template_path, encoding='utf-8') as handle:
        text = handle.read()
    start_pat, end_pat = EXTRACT[template_path]
    start = re.search(start_pat, text)
    assert start, f'{template_path}：找不到分组实现（DIFF_ROW_GROUPS）'
    end = re.search(end_pat, text[start.start():])
    assert end, f'{template_path}：找不到分组实现的结尾锚点 {end_pat!r}'
    return text[start.start():start.start() + end.start()]


def _run(template_path, rows):
    if NODE is None:
        pytest.skip('本机没有 node，跳过（这是唯一能真跑模板 JS 的方式）')
    script = _extract(template_path) + HARNESS
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


@pytest.mark.parametrize('template', [COMMIT_TEMPLATE, WEEKLY_TEMPLATE])
def test_sections_are_ordered_and_labeled(template):
    result = _run(template, ROWS)
    labels = [group['label'] for group in result['groups']]
    assert labels == GROUP_LABELS, (
        f'{os.path.basename(template)}：分组顺序是 {labels}，应为 {GROUP_LABELS}'
        '（整行删除 → 整行新增 → 逐格修改）')

    html = result['html']
    positions = [html.index(f'{label}：') for label in GROUP_LABELS]
    assert positions == sorted(positions), f'标题行在正文里的出现顺序不对：{positions}'
    assert html.index('删除行数据：') < html.index('ROW:12:removed') < html.index('新增行数据：'), (
        '删除段里不该混进别的段的行')


@pytest.mark.parametrize('template', [COMMIT_TEMPLATE, WEEKLY_TEMPLATE])
def test_rows_are_sorted_inside_a_section(template):
    result = _run(template, ROWS)
    by_label = {group['label']: group['rows'] for group in result['groups']}
    assert by_label['删除行数据'] == [2, 12], '删除段内应按行号升序'
    assert by_label['新增行数据'] == [3, 7], '新增段内应按行号升序'
    assert by_label['变更行数据'] == [5, 9], '变更段内应按行号升序'


@pytest.mark.parametrize('template', [COMMIT_TEMPLATE, WEEKLY_TEMPLATE])
def test_header_row_spans_the_whole_table(template):
    html = _run(template, ROWS)['html']
    for label in GROUP_LABELS:
        match = re.search(r'colspan="(\d+)"[^>]*>' + label + '：', html)
        assert match, f'标题行没有 colspan（{label}）'
        assert match.group(1) == '4', (
            f'{label} 的 colspan 是 {match.group(1)}，应为 列数+1 = 4（行号列也要覆盖）')


@pytest.mark.parametrize('template', [COMMIT_TEMPLATE, WEEKLY_TEMPLATE])
def test_no_row_is_lost(template):
    """状态不是这三种的行（例如整表未变更行）不能因为分组被丢掉。"""
    rows = ROWS + [{'row_number': 42, 'status': 'unchanged'}]
    html = _run(template, rows)['html']
    assert 'ROW:42:unchanged' in html, '未分组状态的行被分组逻辑吞掉了'
    assert html.count('ROW:') == len(rows), '报出的行数与输入不符（有行丢了或重复）'


def test_both_templates_produce_identical_output():
    """两份实现必须完全一致 —— 提交页与周版本页看到的分段必须一模一样。"""
    commit = _run(COMMIT_TEMPLATE, ROWS)
    weekly = _run(WEEKLY_TEMPLATE, ROWS)
    assert commit == weekly, (
        '提交页与周版本页的分段实现已经漂移：\n'
        f'  提交页: {commit}\n  周版本页: {weekly}')


def test_group_header_style_exists():
    """标题行得有样式，否则只是一行没背景的普通文字。"""
    for name in ('diff-styles.css', 'excel-scroll-fix.css'):
        with open(os.path.join(PROJECT_ROOT, 'static', 'css', name), encoding='utf-8') as handle:
            css = handle.read()
        assert 'excel-row-group-header' in css, f'{name} 里没有分组标题行的样式'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
