# -*- coding: utf-8 -*-
"""修改行的行号要给出**两版各一个**：上一版本的第几行、当前版本的第几行。

## 缺陷形态（线上报障）

修改行的行号列只写了一个数（`改 6`）。插/删一行之后，同一个逻辑行在两版里的行号
并不相同 —— 实测当前第 11 行的对应行在旧文件里是第 12 行。只有一个数时，评审者拿它
回**旧文件**里核对就会整体错一行，而「对照旧版本确认改了什么」正是这个页面的用途。

对照的旧面板就是两格（`12` / `11`）。这里把这条口径钉在三个地方：

* 引擎：修改行带 `previous_row_number`（`services/diff_service.py::_smart_row_diff`）；
* 表体渲染（`static/js/excel_diff_table.js`，三张页面共用）：上下两半各一格；
* **老载荷必须退化成今天的样子**：`previous_row_number` 是 1.18.0 才加的字段，
  缓存里更早的载荷没有它，那时要退回「一个 rowspan=2 的行号格」，而不是渲染成空行号。
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import openpyxl
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.diff_service import DiffService  # noqa: E402

NODE = shutil.which('node')
SHARED_MODULE = os.path.join(PROJECT_ROOT, 'static', 'js', 'excel_diff_table.js')

HEADERS = ['id', 'name']
BASE = [['1', 'a'], ['2', 'b'], ['3', 'c'], ['4', 'd']]


def _xlsx(rows, sheet='S'):
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = sheet
    for row in rows:
        worksheet.append(list(row))
    return (lambda buffer: (workbook.save(buffer), buffer.getvalue())[1])(io.BytesIO())


def _sheet(current_rows, previous_rows, **kwargs):
    # 按**关键列**（第 1 列 = id）配对：配表几乎都有 ID 列，这样「哪一行对应哪一行」
    # 是确定的 —— 相似度匹配在「上面插了一行」时会把同一行的前后两版认成两条不同的行
    # （实测：报成一删一增），拿它测行号会得到一个与线上形态无关的样本。
    kwargs.setdefault('key_columns', '1')
    payload = DiffService().process_diff(
        'config/x.xlsx', _xlsx(current_rows), _xlsx(previous_rows), **kwargs)
    return payload['sheets']['S']


def _modified_rows(sheet):
    return [row for row in sheet['rows'] if row['status'] == 'modified']


# ---------------------------------------------------------------------------
#  引擎
# ---------------------------------------------------------------------------

class TestTheEngineCarriesThePreviousRowNumber:
    def test_same_position_reports_the_same_number(self):
        sheet = _sheet([HEADERS] + [['1', 'a'], ['2', 'ZZ'], ['3', 'c'], ['4', 'd']],
                       [HEADERS] + BASE)
        row = _modified_rows(sheet)[0]
        assert row['row_number'] == 3 and row['previous_row_number'] == 3, row

    def test_a_row_inserted_above_shifts_the_previous_number(self):
        """线上形态：在表头下面插一行，后面所有行的两版行号就整体错开一位。"""
        current = [HEADERS] + [['9', 'new'], ['1', 'a'], ['2', 'ZZ'], ['3', 'c'], ['4', 'd']]
        sheet = _sheet(current, [HEADERS] + BASE)

        changed = [_modified_rows(sheet)[0]] if _modified_rows(sheet) else []
        assert changed, sheet['rows']
        row = changed[0]
        assert row['row_number'] == 4, (
            f"当前版本的那一行应该是第 4 行（前面插进来一行）：{row}"
        )
        assert row['previous_row_number'] == 3, (
            f"它在上一版本里是第 3 行 —— 只报一个数就会让评审者按错的行号回旧文件核对：{row}"
        )

    def test_added_and_removed_rows_have_no_previous_number(self):
        """新增行在上一版里不存在（没有旧行号）；删除行的行号本来就是旧文件的。"""
        sheet = _sheet([HEADERS] + BASE[:2] + [['9', 'z']], [HEADERS] + BASE)
        added = [row for row in sheet['rows'] if row['status'] == 'added']
        removed = [row for row in sheet['rows'] if row['status'] == 'removed']
        assert 'previous_row_number' not in added[0], added[0]
        assert removed and 'previous_row_number' not in removed[0], removed

    def test_the_stale_row_number_matches_the_old_file(self):
        """自检：`previous_row_number` 指的确实是**旧文件**里的那一行（按内容核）。"""
        current = [HEADERS] + [['9', 'new'], ['1', 'a'], ['2', 'ZZ'], ['3', 'c'], ['4', 'd']]
        sheet = _sheet(current, [HEADERS] + BASE)
        row = _modified_rows(sheet)[0]

        old_file_row = BASE[row['previous_row_number'] - 2]      # 表头占第 1 行
        assert old_file_row == ['2', 'b'], (
            f"上一版第 {row['previous_row_number']} 行是 {old_file_row}，"
            f"而这一行的改前值来自 {row['cell_changes'][0]['old_value']!r}"
        )


# ---------------------------------------------------------------------------
#  表体渲染（共享模块）
# ---------------------------------------------------------------------------

HARNESS = """
const window = {};
window.formatCellValue = function (value) {
    if (value === null || value === undefined) return '';
    return String(value);
};
%s
const T = window.ExcelDiffTable;
const request = JSON.parse(process.argv[2]);
process.stdout.write(JSON.stringify({html: T.changedRowsGroupedHtml([request.row], ['id', 'name'])}));
"""


def _render_row(row):
    if NODE is None:
        pytest.skip('本机没有 node，跳过（这是唯一能真跑共享实现的方式）')
    with open(SHARED_MODULE, encoding='utf-8') as handle:
        source = handle.read()
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as handle:
        handle.write(HARNESS % source)
        path = handle.name
    try:
        proc = subprocess.run([NODE, path, json.dumps({'row': row})],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, f'node 跑失败：{proc.stderr[:500]}'
        return json.loads(proc.stdout)['html']
    finally:
        os.unlink(path)


MODIFIED = {'row_number': 11, 'previous_row_number': 12, 'status': 'modified',
            'data': {'id': '1', 'name': 'b'},
            'cell_changes': [{'column': 'name', 'old_value': 'a', 'new_value': 'b'}]}


class TestTheTableShowsBothRowNumbers:
    def _cells(self, html):
        return re.findall(r'<td class="excel-row-number([^"]*)">([^<]*)</td>', html)

    def test_the_old_and_new_numbers_are_two_cells(self):
        cells = self._cells(_render_row(MODIFIED))

        assert cells == [(' excel-removed', '12'), (' excel-added', '11')], (
            f'修改行的行号不是「上一版 / 当前版」两格：{cells}'
        )

    def test_the_old_cell_keeps_the_non_colour_cue(self):
        """两格之后，「这一对是修改」只剩「改」字这个非颜色线索（CSS 的 ::before）。"""
        html = _render_row(MODIFIED)
        assert 'excel-row-modified-old' in html and 'excel-row-modified-new' in html
        with open(os.path.join(PROJECT_ROOT, 'static', 'css', 'diff-table-ux.css'),
                  encoding='utf-8') as handle:
            css = handle.read()
        assert re.search(r'tr\.excel-row-modified-old > td\.excel-row-number::before\s*\{[^}]*content: "改"',
                         css, re.S), '行号格里的「改」字不见了 —— 上下两格只剩底色可认'

    def test_a_legacy_payload_falls_back_to_one_spanning_cell(self):
        """**反向守卫**：没有 previous_row_number 的老载荷必须退回今天的样子。"""
        legacy = {key: value for key, value in MODIFIED.items() if key != 'previous_row_number'}
        html = _render_row(legacy)
        cells = self._cells(html)

        assert cells == [], f'老载荷渲染出了两格行号：{cells}'
        assert 'class="excel-row-number excel-modified" rowspan="2">11<' in html, (
            f'老载荷没有退回「一个 rowspan=2 的行号格」：{html[:200]}'
        )

    def test_the_new_row_gets_no_number_cell_when_the_old_one_spans(self):
        """rowspan 那格已经覆盖两行 —— 新行再写一格就会多出一个错位的行号。"""
        legacy = {key: value for key, value in MODIFIED.items() if key != 'previous_row_number'}
        html = _render_row(legacy)
        new_half = html.split('excel-row-modified-new', 1)[1]
        assert 'excel-row-number' not in new_half, '新值那一半不该再有行号格'

    def test_the_numbers_are_escaped(self):
        row = dict(MODIFIED, row_number='<img src=x>', previous_row_number='<b>')
        html = _render_row(row)
        assert '<img' not in html and '<b>' not in html, html


# ---------------------------------------------------------------------------
#  另外两条渲染路径：服务端 partial 与合并页的字段白名单
# ---------------------------------------------------------------------------

def test_the_server_rendered_partial_shows_both_numbers():
    from jinja2 import Environment, FileSystemLoader

    def letter(index):
        result = ''
        while index >= 0:
            result = chr(65 + index % 26) + result
            index = index // 26 - 1
        return result

    env = Environment(
        loader=FileSystemLoader(os.path.join(PROJECT_ROOT, 'templates')), autoescape=True)
    env.filters['excel_column_letter'] = letter
    env.filters['format_cell_value'] = lambda value: '' if value is None else str(value)
    sheet = {'has_changes': True, 'headers': HEADERS, 'stats': {}, 'rows': [MODIFIED]}
    payload = {'type': 'excel', 'sheets': {'S': sheet}}
    html = env.get_template('diff_partials/excel_diff.html').render(
        diff_data=payload, excel_data=payload)

    assert '12' in html and '11' in html, '服务端渲染的正文里没有两个行号'
    assert 'excel-row-number-prev' in html, '上一版的行号没有自己的类（压淡一档看不出来）'


def test_the_merge_page_keeps_the_previous_row_number():
    """合并页的行归一化是**刻意写的白名单**，漏一个字段就是静默丢内容。"""
    with open(os.path.join(PROJECT_ROOT, 'templates', 'merge_diff.html'), encoding='utf-8') as handle:
        source = handle.read()
    body = source[source.index('function normalizeLegacyRow'):]
    body = body[:body.index('\n}')]
    assert 'previous_row_number' in body, (
        '合并页的 normalizeLegacyRow 把 previous_row_number 丢了 —— '
        '合并视图里的修改行会退回单行号'
    )


def test_the_ai_diff_summary_mentions_the_previous_row_number():
    from services.ai.platform_provider import _render_row

    text = _render_row(MODIFIED)
    assert '第 11 行' in text and '上一版第 12 行' in text, text
    # 两版行号相同时不啰嗦
    same = _render_row(dict(MODIFIED, previous_row_number=11))
    assert '上一版' not in same, same
