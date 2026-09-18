# -*- coding: utf-8 -*-
"""「表头行数」这个配置要真的生效：表头行与数据行分开呈现，两边都看得见。

## 缺陷形态

`Repository.header_rows`（仓库上配的「表头行数」）**只有写没有读** —— 表单写它
（`services/repository_creation_handlers.py`、`services/repository_update_form_service.py`），
而 `process_diff()` 的签名里从来没有这个参数，读表也一直是 `pd.read_excel(header=0)`。
`templates/help.html` 的「表头行数」一节写着「系统根据此值区分表头和数据行，正确解析 Diff」，
这句话从初始提交起就没成立过。

于是三行表头的表里，**第 2、3 行被当成数据行**（实测）：

| 提交里只改 | 修前显示成 |
|---|---|
| 第 1 行（列名） | 列变更 `renamed` —— 还行 |
| 第 2 行 | 行 2 [modified]，按第 1 行的列名定位 |
| 第 3 行 | 行 3 [modified] |

「看得见，但说错了」：评审者看到的是「第 2 行数据被改了」，而文件里第 2 行是表头。
一张表几张表头行的表，每次提交都会带上几条这种行，真正的数据变更混在里面。

## 现在的口径

表头块（物理行 `2..header_rows`，含第 1 行的列名行，见 `DiffService._header_row_count`）
**单独成块**：每张表多两个键 `header_rows`（行列表，含未改动的行）与 `header_stats`，
`rows`/`stats` 只剩数据行。表头行照旧逐格显示变更（`status` 仍只用
added/removed/modified/unchanged 四种），只是不再冒充数据行。

**未配置（或配 1）时载荷与今天逐字一致** —— 这两个键根本不出现。

## 为什么必须分开、不能只是「多标一个状态」

`rows` 的契约是「有变更的数据行」，表头行整块常驻，下游四处会按这个契约弄错：
`optimize_diff_data` 的状态白名单（写缓存时静默删掉）、`validate_excel_diff_data` 的
「有没有变更」（常驻行让「完全没变」也判成有内容）、前端 `groupChangedRows` 的分组顺序、
AI 摘要的变更行过滤。见 `test_header_rows_are_not_data_rows_in_the_cached_payload`。
"""
from __future__ import annotations

import io
import os
import re
import sys
from collections import Counter

import openpyxl
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.diff_service import DiffService  # noqa: E402
from utils.diff_data_utils import header_rows_have_changes, validate_excel_diff_data  # noqa: E402

# 三行表头（配 header_rows=3）+ 两行数据。第 1 行是列名（引擎读进来当列名，不出现
# 在 rows 里），第 2、3 行是**引擎一直没读的那两行**。
NAMES = ['id', 'name', 'icon']
HEADER_2 = ['编号', '名称', '图标']
HEADER_3 = ['ID', 'Name', 'Icon']
DATA = [['1', 'a', 'x'], ['2', 'b', 'y']]

HEADER_ROWS = 3


def _xlsx(rows, sheet='S'):
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = sheet
    for row in rows:
        worksheet.append(list(row))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _sheet(previous_rows, current_rows, *, header_rows=HEADER_ROWS, name='S'):
    payload = DiffService().process_diff(
        'config/x.xlsx', _xlsx(current_rows), _xlsx(previous_rows),
        header_rows=header_rows)
    assert payload.get('type') == 'excel', payload
    return payload['sheets'][name]


def _baseline_rows():
    return [NAMES, HEADER_2, HEADER_3] + DATA


def _statuses(rows):
    return Counter(row['status'] for row in rows)


def _changes(row):
    return {(c['column'], c.get('old_value'), c.get('new_value'))
            for c in row.get('cell_changes') or []}


# ---------------------------------------------------------------------------
#  配置解析
# ---------------------------------------------------------------------------

class TestHeaderRowCount:
    service = DiffService()

    @pytest.mark.parametrize('raw', [None, '', 'abc', 0, -1, '1', 1, 1.0])
    def test_unset_and_one_mean_todays_behaviour(self, raw):
        """没配 / 配 1 = **没有表头块**（第 1 行就是列名行，与今天一致）。"""
        assert self.service._header_row_count(raw) == 1

    @pytest.mark.parametrize('raw,expected', [(2, 2), ('3', 3), (3.0, 3), (' 4 ', 4)])
    def test_configured_values_are_used(self, raw, expected):
        assert self.service._header_row_count(raw) == expected


# ---------------------------------------------------------------------------
#  表头行被改：报成表头，不报成数据行
# ---------------------------------------------------------------------------

class TestHeaderRowChangesAreVisibleAsHeaderRows:
    def test_changing_the_second_header_row_is_reported_in_the_header_block(self):
        current = [NAMES, ['编号', '名字', '图标'], HEADER_3] + DATA
        sheet = _sheet(_baseline_rows(), current)

        assert sheet['rows'] == [], (
            f"只改了表头第 2 行，却报了数据行：{sheet['rows']} —— 修前就是这个形态"
        )
        reported = {row['row_number']: row for row in sheet['header_rows']}
        assert reported[2]['status'] == 'modified'
        assert _changes(reported[2]) == {('name', '名称', '名字')}, (
            f"表头第 2 行的改动没按格子报出来：{reported[2].get('cell_changes')}"
        )
        # 没动的那一行也在（界面要据此说明「表头共 3 行」），但状态是 unchanged。
        assert reported[3]['status'] == 'unchanged'
        assert _statuses(sheet['header_rows']) == Counter({'modified': 1, 'unchanged': 1})

    def test_changing_the_third_header_row_is_reported_in_the_header_block(self):
        current = [NAMES, HEADER_2, ['ID', 'DisplayName', 'Icon']] + DATA
        sheet = _sheet(_baseline_rows(), current)

        assert sheet['rows'] == []
        reported = {row['row_number']: row for row in sheet['header_rows']}
        assert reported[3]['status'] == 'modified'
        assert _changes(reported[3]) == {('name', 'Name', 'DisplayName')}

    def test_renaming_a_column_stays_a_header_change_and_not_a_data_change(self):
        """第 1 行（列名行）的改动走既有的 `header_changes`，且表头块那两行是 unchanged。"""
        current = [['id', 'title', 'icon'], HEADER_2, HEADER_3] + DATA
        sheet = _sheet(_baseline_rows(), current)

        assert sheet['rows'] == []
        assert [c['change'] for c in sheet['header_changes']] == ['renamed']
        assert _statuses(sheet['header_rows']) == Counter({'unchanged': 2})
        assert header_rows_have_changes(sheet) is False, (
            "表头块里两行都没动，`header_rows_have_changes` 不能说有改动 —— "
            "常驻的未改动行会让「完全没变」也判成有内容"
        )

    def test_a_data_change_is_still_reported_as_a_data_change(self):
        current = [NAMES, HEADER_2, HEADER_3, ['1', 'a', 'x'], ['2', 'ZZ', 'y']]
        sheet = _sheet(_baseline_rows(), current)

        assert [row['row_number'] for row in sheet['rows']] == [5]
        assert _changes(sheet['rows'][0]) == {('name', 'b', 'ZZ')}
        # 表头一行没动：块里全是 unchanged，于是「表头有没有改动」是 False。
        assert _statuses(sheet['header_rows']) == Counter({'unchanged': 2})
        assert header_rows_have_changes(sheet) is False

    def test_both_a_header_change_and_a_data_change_are_reported(self):
        current = [NAMES, ['编号', '名字', '图标'], HEADER_3, ['1', 'a', 'x'], ['2', 'ZZ', 'y']]
        sheet = _sheet(_baseline_rows(), current)

        assert [row['row_number'] for row in sheet['rows']] == [5]
        assert _statuses(sheet['header_rows']) == Counter({'modified': 1, 'unchanged': 1})
        assert header_rows_have_changes(sheet) is True

    def test_an_added_column_shows_up_on_both_sides(self):
        current = [NAMES + ['price'], HEADER_2 + ['价格'], HEADER_3 + ['Price'],
                   ['1', 'a', 'x', '9'], ['2', 'b', 'y', '8']]
        sheet = _sheet(_baseline_rows(), current)

        assert [c['change'] for c in sheet['header_changes']] == ['added']
        # 数据行整行新增（多了一列，每行都不同）→ 数据行进出 rows。
        assert _statuses(sheet['rows']) == Counter({'modified': 2}), sheet['rows']
        # 表头块的每一行也带着这一列的值，两行都真的变了。
        assert _statuses(sheet['header_rows']) == Counter({'modified': 2})


# ---------------------------------------------------------------------------
#  统计口径：数据行的统计不含表头
# ---------------------------------------------------------------------------

class TestHeaderRowsDoNotCountAsDataRows:
    def test_totals_count_data_rows_only(self):
        current = [NAMES, HEADER_2, HEADER_3, ['1', 'a', 'x'], ['2', 'b', 'y'], ['3', 'c', 'z']]
        sheet = _sheet(_baseline_rows(), current)

        assert sheet['stats']['total_rows_current'] == 3
        assert sheet['stats']['total_rows_previous'] == 2
        assert sheet['stats']['added'] == 1
        # 表头块自己有一份统计，界面用它写「表头 3 行 · 无改动」。
        assert sheet['header_stats'] == {
            'total_rows_current': 2, 'total_rows_previous': 2,
            'added': 0, 'removed': 0, 'modified': 0,
        }

    def test_without_the_configuration_the_counts_include_those_rows(self):
        """不配表头行数时，第 2、3 行照旧算数据行 —— 今天的口径一个字没动。"""
        sheet = _sheet(_baseline_rows(), _baseline_rows(), header_rows=None)

        assert 'header_rows' not in sheet and 'header_stats' not in sheet
        assert sheet['stats']['total_rows_current'] == 4
        assert sheet['rows'] == []


# ---------------------------------------------------------------------------
#  未配置 / 配 1：与今天逐字一致
# ---------------------------------------------------------------------------

class TestUnconfiguredPayloadIsUnchanged:
    @pytest.mark.parametrize('header_rows', [None, 1, '1', 0, 'abc'])
    def test_no_header_block_is_emitted(self, header_rows):
        current = [NAMES, HEADER_2, HEADER_3, ['1', 'a', 'x'], ['2', 'b', 'y']]
        sheet = _sheet(_baseline_rows(), current, header_rows=header_rows)

        assert 'header_rows' not in sheet
        assert 'header_stats' not in sheet
        assert {'rows', 'stats', 'headers', 'columns'} <= set(sheet)

    def test_a_header_row_change_still_shows_up_as_a_data_row(self):
        """反向守卫：不配时「改了第 2 行」**照旧**报成行 2 —— 别把这条路径一起改了。"""
        current = [NAMES, ['编号', '名字', '图标'], HEADER_3] + DATA
        sheet = _sheet(_baseline_rows(), current, header_rows=None)

        assert [row['row_number'] for row in sheet['rows']] == [2], (
            "未配置表头行数时第 2 行仍应按数据行报出来（今天的口径）"
        )


# ---------------------------------------------------------------------------
#  整表增删的两条分支同样要分
# ---------------------------------------------------------------------------

class TestWholeSheetBranchesAlsoSplit:
    ROWS = [NAMES, HEADER_2, HEADER_3] + DATA

    def test_an_added_sheet_counts_only_data_rows(self):
        payload = DiffService().process_diff(
            'config/x.xlsx', _xlsx(self.ROWS), None, header_rows=HEADER_ROWS)
        sheet = payload['sheets']['S']

        assert sheet['operation'] == 'added'
        assert [row['row_number'] for row in sheet['header_rows']] == [2, 3]
        assert _statuses(sheet['header_rows']) == Counter({'added': 2})
        assert [row['row_number'] for row in sheet['rows']] == [4, 5]
        assert sheet['stats']['added'] == 2, (
            f"「新增 N 行」把表头那两行也算进去了：{sheet['stats']}"
        )

    def test_a_deleted_file_counts_only_data_rows(self):
        result = DiffService().process_deleted_file(
            'config/x.xlsx', _xlsx(self.ROWS), header_rows=HEADER_ROWS)
        sheet = result['sheets']['S']

        assert sheet['operation'] == 'deleted'
        assert [row['row_number'] for row in sheet['header_rows']] == [2, 3]
        assert [row['row_number'] for row in sheet['rows']] == [4, 5]
        assert sheet['stats']['removed'] == 2

    def test_a_sheet_with_only_header_rows_is_not_empty(self):
        """只有表头、没有数据行的表：表头块仍然要说得出「表头长什么样」。"""
        payload = DiffService().process_diff(
            'config/x.xlsx', _xlsx(self.ROWS), None, header_rows=HEADER_ROWS)
        sheet = payload['sheets']['S']
        assert [row['row_number'] for row in sheet['header_rows']] == [2, 3]


# ---------------------------------------------------------------------------
#  服务端渲染那条路（templates/diff_partials/excel_diff.html）
# ---------------------------------------------------------------------------

def _jinja_env():
    """直接渲染 partial：它只用到 excel_column_letter / format_cell_value 两个过滤器。

    （与 `tests/test_excel_sheet_name_injection.py` 同一套做法：不起 Flask 应用。）
    """
    from jinja2 import Environment, FileSystemLoader

    def _letter(index):
        result = ''
        while index >= 0:
            result = chr(65 + index % 26) + result
            index = index // 26 - 1
        return result

    env = Environment(
        loader=FileSystemLoader(str(os.path.join(PROJECT_ROOT, 'templates'))),
        autoescape=True,
    )
    env.filters['excel_column_letter'] = _letter
    env.filters['format_cell_value'] = lambda value: '' if value is None else str(value)
    return env


def _render_partial(payload):
    template = _jinja_env().get_template('diff_partials/excel_diff.html')
    return template.render(diff_data=payload, excel_data=payload)


def _partial_sheet(header_rows=None, rows=None, headers=None):
    sheet = {
        'status': 'modified',
        'headers': headers or ['id', 'name', 'icon'],
        'rows': rows if rows is not None else [
            {'row_number': 5, 'status': 'modified',
             'data': {'id': '2', 'name': 'ZZ', 'icon': 'y'},
             'cell_changes': [{'column': 'name', 'old_value': 'b', 'new_value': 'ZZ'}]},
        ],
    }
    if header_rows is not None:
        sheet['header_rows'] = header_rows
        sheet['header_stats'] = {'total_rows_current': len(header_rows),
                                 'total_rows_previous': len(header_rows),
                                 'added': 0, 'removed': 0, 'modified': 0}
        sheet['has_changes'] = True
    return {'type': 'excel', 'file_path': 'config/x.xlsx', 'sheets': {'S': sheet}}


PARTIAL_HEADER_CLEAN = [
    {'row_number': 2, 'status': 'unchanged', 'data': {'id': '编号', 'name': '名称', 'icon': '图标'}},
    {'row_number': 3, 'status': 'unchanged', 'data': {'id': 'ID', 'name': 'Name', 'icon': 'Icon'}},
]

PARTIAL_HEADER_CHANGED = [
    {'row_number': 2, 'status': 'modified', 'data': {'id': '编号', 'name': '名字', 'icon': '图标'},
     'cell_changes': [{'column': 'name', 'old_value': '名称', 'new_value': '名字'}]},
    {'row_number': 3, 'status': 'unchanged', 'data': {'id': 'ID', 'name': 'Name', 'icon': 'Icon'}},
]


class TestTheServerRenderedPartialShowsTheHeaderBlock:
    def test_a_changed_header_row_is_shown_with_its_old_and_new_values(self):
        html = _render_partial(_partial_sheet(PARTIAL_HEADER_CHANGED))

        assert '表头行数据：' in html, '改了表头行，服务端渲染的正文里没有这一段'
        assert html.index('表头行数据：') < html.index('excel-row-modified'), (
            '表头块排在了数据行后面 —— 它是这张表最上面的一段')
        assert '名称' in html and '名字' in html, '表头行的改前/改后取值没有渲染出来'
        assert re.search(r'excel-row-number[^>]*>\s*3\s*<', html), '没改动的那一行表头没有渲染'

    def test_a_clean_header_block_collapses_to_one_line(self):
        html = _render_partial(_partial_sheet(PARTIAL_HEADER_CLEAN))

        assert '表头 2 行（第 2–3 行） · 无改动' in html, '没有改动时没有给出表头块的说明'
        assert '表头行数据：' in html
        assert 'excel-row-normal' not in html, '没有改动却把表头行逐行铺开了'

    def test_a_header_only_change_is_not_rendered_as_no_changes(self):
        """**只改表头**时 rows 是空的 —— 正文不能显示成「没有数据或无变更」。"""
        html = _render_partial(_partial_sheet(PARTIAL_HEADER_CHANGED, rows=[]))

        assert '没有数据或无变更' not in html, (
            '这次提交只改了表头，正文却说「没有数据或无变更」—— 评审者会直接放过'
        )
        assert '表头行数据：' in html and '名字' in html

    def test_without_the_configuration_nothing_changes(self):
        """载荷里没有 header_rows（没配「表头行数」）：渲染结果里不该出现表头块。"""
        html = _render_partial(_partial_sheet())

        assert '表头行数据' not in html and 'excel-row-note' not in html
        assert 'ZZ' in html, '数据行本身没有渲染出来（这一条就成了空测）'

class TestHeaderRowsSurviveThePayloadPipeline:
    def _sheet_with_a_header_change(self):
        current = [NAMES, ['编号', '名字', '图标'], HEADER_3] + DATA
        return _sheet(_baseline_rows(), current)

    def test_optimize_diff_data_keeps_the_header_block(self):
        from services.excel_diff_cache_service import ExcelDiffCacheService

        sheet = self._sheet_with_a_header_change()
        optimized = ExcelDiffCacheService().optimize_diff_data(
            {'type': 'excel', 'sheets': {'S': sheet}, 'summary': {}})['sheets']['S']

        assert [row['row_number'] for row in optimized['header_rows']] == [2, 3], (
            "`optimize_diff_data` 把表头块丢了 —— 它的行是状态白名单过滤的，"
            "表头行如果混在 rows 里就会被静默删掉，这正是要单独成键的原因"
        )
        assert optimized['header_rows'][0]['status'] == 'modified'
        assert optimized['header_stats']['modified'] == 1

    def test_validate_excel_diff_data_counts_a_header_only_change(self):
        sheet = self._sheet_with_a_header_change()
        sheet['rows'] = []
        sheet['stats'] = {'total_rows_current': 2, 'total_rows_previous': 2,
                          'added': 0, 'removed': 0, 'modified': 0}

        valid, message = validate_excel_diff_data({'type': 'excel', 'sheets': {'S': sheet}})
        assert valid is True, (
            f"只改表头的提交被判成「没有差异」（{message}）—— 评审者会以为这次提交什么都没改"
        )

    def test_a_clean_header_block_alone_is_not_treated_as_content(self):
        """反面：表头块里全是未改动的行时，不能因为「列表非空」就算有内容。"""
        current = [NAMES, HEADER_2, HEADER_3] + DATA
        sheet = _sheet(_baseline_rows(), current)
        sheet['rows'] = []

        assert header_rows_have_changes(sheet) is False
        valid, _message = validate_excel_diff_data({'type': 'excel', 'sheets': {'S': sheet}})
        assert valid is False

    def test_weekly_merge_keeps_the_header_block_and_the_header_changes(self):
        """周版本按提交分段合并：表头块不能按分段数复制，列名变更也不能被丢掉。"""
        from services.weekly_excel_merge_helpers import merge_segmented_excel_diff_payload

        # 第 1 段：表头没变；第 2 段：表头第 2 行改了。两段都是同一个文件的完整 diff，
        # 各自带着同一份表头块。
        clean = _sheet(_baseline_rows(), _baseline_rows())
        changed = self._sheet_with_a_header_change()
        changed['header_changes'] = [{'change': 'renamed', 'column': 'name',
                                      'old_name': 'name', 'new_name': 'title',
                                      'column_index': 2}]

        def _payload(sheet):
            return {
                'type': 'excel', 'file_path': 'config/x.xlsx',
                'summary': {'added': 0, 'removed': 0, 'modified': 0, 'total': 0},
                'sheets': {'S': sheet},
            }

        merged = merge_segmented_excel_diff_payload([_payload(clean), _payload(changed)])

        merged_sheet = merged['sheets']['S']
        assert [row['row_number'] for row in merged_sheet['header_rows']] == [2, 3], (
            f"合并后的表头块行号不对（按分段重复了？）："
            f"{[r['row_number'] for r in merged_sheet['header_rows']]}"
        )
        assert merged_sheet['header_rows'][0]['status'] == 'modified', (
            "两段里有一段说表头第 2 行改了，合并后必须是有改动的那一条赢"
        )
        assert merged_sheet['header_stats']['modified'] == 1
        assert merged_sheet.get('header_changes'), (
            "分片合并把列名变更整份丢掉 —— 周版本视图里的「列名变更」提示永远是空的"
        )
        assert merged_sheet['has_changes'] is True

    def test_weekly_merge_deduplicates_a_column_change_repeated_in_every_segment(self):
        from services.weekly_excel_merge_helpers import merge_segmented_excel_diff_payload

        rename = {'change': 'renamed', 'column': 'name', 'old_name': 'name',
                  'new_name': 'title', 'column_index': 2}
        sheet = _sheet(_baseline_rows(), _baseline_rows())
        sheet['header_changes'] = [rename]
        payload = {
            'type': 'excel', 'file_path': 'config/x.xlsx',
            'summary': {'added': 0, 'removed': 0, 'modified': 0, 'total': 0},
            'sheets': {'S': sheet},
        }

        merged = merge_segmented_excel_diff_payload([payload, payload, payload])
        assert len(merged['sheets']['S']['header_changes']) == 1, (
            "同一次列名变更在每一段里都有，合并后不该出现三条"
        )
