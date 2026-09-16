# -*- coding: utf-8 -*-
"""整张工作表被删除时，**必须把被删掉的内容渲染出来**。

## 缺陷形态（线上真实踩到）

`services/diff_service.py::_compare_dataframes` 的「工作表被删除」分支原先这样返回：

    return {
        'operation': 'deleted',
        'message': f'工作表 "{sheet_name}" 已被删除',
        'headers': list(previous_df.columns),
        'rows': [],                     # ← 行数据被丢弃，只留了个计数
        'stats': {'removed': len(previous_df), ...},
    }

对比同文件里「新增工作表」分支（一直保留 `rows`），这是明显的不对称。后果有两层，
**每一层单独都足以让评审者盲签**：

1. **正文什么都没有**：两条渲染路径都靠 `rows` 决定「有没有变更」——
   前端 `static/js/diff-handlers.js` 用 `rows.some(status ∈ added/removed/modified)`，
   服务端 `templates/diff_partials/excel_diff.html` 直接逐 `rows` 渲染。
   `rows=[]` ⇒ 两张表都显示成「工作表 X 没有数据或无变更」。
2. **统计却说有几百行删除**：`_compare_excel_data` 会把 `stats.removed` 累加进
   `summary`，于是顶部横幅写着「删除 232」，正文一个字都看不到。

线上实例（`config/60_skill/角色属性表.xlsx` 的一次提交）：`summary.removed = 232`，
而正文只渲染出 1 行 —— 同一份 payload 里另外 5 张被删的工作表合计 231 行凭空消失。

## 这些测试各钉什么

* `test_deleted_sheet_keeps_its_rows`：删除分支必须返回行，且状态是 `removed`、
  带 `data`（前端 `createRemovedRow()` 就是从 `row.data[header]` 取值）。
* `test_deleted_sheet_row_count_matches_stats`：`stats.removed` 必须等于实际行数 ——
  这两个数一旦不一致，就是「统计说有 N 行、正文只有 M 行」那种误导。
* `test_added_sheet_still_keeps_rows`：对照组，防止有人为了对称把新增分支也改成丢行。
* `TestPartialRendersBothShapes`：模板侧。两种数据形态都必须渲染出**单元格内容**：
  形态一（整行 `cells`）、形态二（只有 `data` + `cell_changes`）。原先模板只认形态一，
  而提交页/缓存里实际流通的是形态二 —— 渲染出来是一排排只有行号的空表格。
"""
from __future__ import annotations

import os
import sys

import pandas as pd
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.diff_service import DiffService  # noqa: E402


@pytest.fixture()
def service():
    return DiffService()


_PREV = pd.DataFrame({
    'id': ['TYPE', 'DEFAULT', '测试id'],
    '字段': ['int', None, '1'],
    '每日获得次数上限': ['int', None, '1000'],
})


class TestDeletedSheetProducer:
    def test_deleted_sheet_keeps_its_rows(self, service):
        result = service._compare_dataframes(None, _PREV, '奖励模式')
        assert result['operation'] == 'deleted'
        assert result['rows'], (
            "整张表被删除时没有返回任何行 —— 正文里就什么都看不到，"
            "而顶部统计照样写着「删除 N」。"
        )
        assert all(row['status'] == 'removed' for row in result['rows'])
        for row in result['rows']:
            assert 'data' in row and row['data'], (
                "删除行必须带 data：前端 createRemovedRow() 靠 row.data[header] 取值，"
                "没有它就渲染不出被删掉的内容。"
            )

    def test_deleted_sheet_row_count_matches_stats(self, service):
        result = service._compare_dataframes(None, _PREV, '奖励模式')
        assert result['stats']['removed'] == len(result['rows']), (
            f"stats.removed={result['stats']['removed']} 与实际行数 {len(result['rows'])} 不一致 —— "
            "统计与正文对不上，评审者按统计判断工作量、按正文看内容，两边都不可信。"
        )

    def test_deleted_sheet_keeps_headers_and_values(self, service):
        result = service._compare_dataframes(None, _PREV, '奖励模式')
        assert list(result['headers']) == list(_PREV.columns)
        # 行号从 1 起，且内容与原始表逐格一致（不能因为「整表删除」就丢掉值）
        first = result['rows'][0]
        assert first['row_number'] == 1
        assert first['data']['id'] == 'TYPE'
        assert result['rows'][2]['data']['每日获得次数上限'] == '1000'

    def test_added_sheet_still_keeps_rows(self, service):
        """对照组：新增分支必须继续保留行（别为了「对称」把它也改掉）。"""
        result = service._compare_dataframes(_PREV, None, '奖励模式')
        assert result['operation'] == 'added'
        assert len(result['rows']) == len(_PREV)
        assert all(row['status'] == 'added' for row in result['rows'])

    def test_summary_counts_the_deleted_rows(self, service):
        """端到端：summary 与 sheets 里的行数必须一致（这就是线上对不上的那处）。"""
        current = {'保留表': pd.DataFrame({'a': [1]})}
        previous = {'保留表': pd.DataFrame({'a': [1]}), '删除表': _PREV}
        result = service._compare_excel_data(current, previous, 'config/示例.xlsx')
        deleted = result['sheets']['删除表']
        assert result['summary']['removed'] == len(deleted['rows']), (
            f"summary.removed={result['summary']['removed']}，"
            f"但正文只有 {len(deleted['rows'])} 行可渲染"
        )
        assert result['summary']['removed'] == len(_PREV)


class TestPartialRendersBothShapes:
    """模板必须把两种数据形态都渲染出单元格内容。

    直接渲染 partial（沿用 `tests/test_excel_sheet_name_injection.py` 的做法）：
    它只依赖 `excel_column_letter` / `format_cell_value` 两个过滤器，
    不需要拉起整个 Flask 应用。注意模板第 3 行是
    `{% set excel_data = sheet_data if sheet_data else diff_data %}` ——
    所以载荷要作为 `diff_data` 传进去。
    """

    def _render(self, payload) -> str:
        from jinja2 import Environment, FileSystemLoader

        def _column_letter(index: int) -> str:
            result = ""
            while index >= 0:
                result = chr(65 + (index % 26)) + result
                index = index // 26 - 1
            return result

        templates_dir = os.path.join(PROJECT_ROOT, "templates")
        env = Environment(loader=FileSystemLoader(templates_dir), autoescape=True)
        env.filters["excel_column_letter"] = _column_letter
        env.filters["format_cell_value"] = lambda value: "" if value is None else str(value)
        return env.get_template("diff_partials/excel_diff.html").render(diff_data=payload)

    @staticmethod
    def _shape_two_sheet():
        """形态二：diff_service 的产出（`data` + `cell_changes`，没有 cells）。"""
        return {
            'operation': 'deleted',
            'message': '工作表 "奖励模式" 已被删除',
            'headers': ['id', '字段'],
            'rows': [
                {'row_number': 1, 'status': 'removed', 'data': {'id': 'TYPE', '字段': 'int'}},
                {'row_number': 2, 'status': 'modified',
                 'data': {'id': 'EXPORT', '字段': 'all'},
                 'cell_changes': [{'column': '字段', 'old_value': 'server', 'new_value': 'all'}]},
            ],
            'stats': {'added': 0, 'removed': 1, 'modified': 1},
        }

    @staticmethod
    def _shape_one_sheet():
        """形态一：git_service 的产出（整行 cells）。"""
        return {
            'status': 'modified',
            'headers': ['id'],
            'rows': [{'row_number': 1, 'status': 'removed',
                      'cells': [{'value': 'TYPE', 'status': 'removed'}]}],
            'stats': {'added': 0, 'removed': 1, 'modified': 0},
        }

    @staticmethod
    def _payload(sheet):
        return {'type': 'excel', 'file_path': 'config/x.xlsx',
                'sheets': {'奖励模式': sheet},
                'summary': {'added': 0, 'removed': 1, 'modified': 0, 'total': 1}}

    def test_shape_two_renders_cell_values(self):
        html = self._render(self._payload(self._shape_two_sheet()))
        assert 'TYPE' in html and 'EXPORT' in html, (
            "形态二（data + cell_changes）没有渲染出任何单元格内容 —— "
            "线上正文会变成一排排只有行号的空表格。"
        )
        assert 'server' in html, "cell_changes 里的旧值没有渲染出来（修改行看不到改前内容）"
        assert html.count('excel-row-removed') >= 1
        # 修改行必须带自己的行级标记。原先 modified 和 unchanged 都输出 normal，
        # 于是「只有修改」的提交在这份 HTML 里数出来是 0 个变更行。
        assert '<tr class="excel-row-modified">' in html, (
            "修改行没有行级标记，按 class 统计变更行的下游会把「只有修改」数成 0 行"
        )

    def test_shape_one_deleted_sheet_shows_notice(self):
        """形态一的产出方把整表删除写成 sheet 级 `status='deleted'`（不是 operation），
        模板必须同时认这两个字段，否则那条路径上「工作表已被删除」的提示不会出现。"""
        sheet = {
            'status': 'deleted',
            'headers': ['A'],
            'rows': [{'row_number': 1, 'status': 'removed',
                      'cells': [{'value': 'TYPE', 'status': 'removed'}]}],
            'has_changes': True,
        }
        html = self._render(self._payload(sheet))
        assert '已被删除' in html, "形态一的整表删除提示没渲染（status=deleted 未被识别）"
        assert 'TYPE' in html, "形态一的删除行没有渲染出被删内容"

    def test_shape_two_shows_deleted_sheet_notice(self):
        html = self._render(self._payload(self._shape_two_sheet()))
        assert '已被删除' in html, (
            "整表被删除的提示没有渲染 —— 判据字段必须是 operation（产出方实际写的字段），"
            "而不是从来没有产出方写过的 status。"
        )

    def test_shape_one_still_renders(self):
        html = self._render(self._payload(self._shape_one_sheet()))
        assert 'TYPE' in html, "形态一（整行 cells）反而渲染不出来了 —— 这是回归"
        assert html.count('excel-row-removed') >= 1
