# -*- coding: utf-8 -*-
"""行号口径：**两个引擎对同一张表的同一行必须给出同一个 `row_number`**。

## 为什么需要这个文件

平台有两条 Excel 比较实现，而且都在活路径上：

* 主引擎 `services/diff_service.DiffService.process_diff`（页面与 AI 平时读的那一份）；
* 旧引擎 `GitService.parse_excel_diff` → `_generate_excel_diff_data` →
  `_parallel_compare_sheets_optimized` → `_compare_sheet_data` → `_fast_compare_rows`。
  主引擎没产出时由它兜底（`services/commit_diff_view_service.py` 的「使用旧的Excel处理
  逻辑作为备用」、`services/commit_diff_logic.py` 的 Excel 兜底与合并分支），**回落不报错**，
  页面与 AI 都看不出来。

两边的 `row_number` 是评审者回文件里核对的唯一坐标（`templates/help.html` 把它写成
「方便快速定位」），而 `services/ai/platform_provider.py::_render_row` 又把它原样渲染成
「第 N 行」写进提示词 —— 两个引擎差 1，模型报「第 12 行」时人回文件里核对就整体错一行。

## 两边喂进来的序列**起点不同**（这一点决定了「统一」不能是把算式抄成一样）

* 主引擎 `pandas.read_excel(header=0)`：物理第 1 行被当作列名吃掉，帧里第 0 条数据
  是**物理第 2 行**；
* 旧引擎 `git_excel_parser_helpers.extract_excel_data` 从 `range(1, max_row + 1)` 逐行读，
  第 0 个元素**就是物理第 1 行**（它拿列字母 `A`/`B`… 当列名，首行本身是一行普通数据）。

所以正确的统一方式是**一个函数 + 一个显式的偏移量**
（`services.diff_service.physical_row_number(index0_based, rows_before=…)`），两边都调它。
本文件直接断言「同一张表、同一行，两个引擎给出的数相同」，并且用**物理行号**（从 1 起的
真实 Excel 行号）当基准 —— 只断言「两边相等」是不够的：两边一起错也满足相等。
"""
from __future__ import annotations

import io
import os
import sys

import pandas as pd
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.diff_service import DiffService, physical_row_number  # noqa: E402
from services.git_excel_parser_helpers import extract_excel_data  # noqa: E402
from services.git_service import GitService  # noqa: E402

SHEET = 'Sheet1'


# ---------------------------------------------------------------------------
# 一、纯函数本身
# ---------------------------------------------------------------------------
class TestPhysicalRowNumber:
    """`physical_row_number` 是全平台唯一一份行号口径。"""

    def test_header_consumed_sequence(self):
        """主引擎：帧里第 0 条数据是物理第 2 行（`rows_before=1`）。"""
        assert physical_row_number(0, rows_before=1) == 2
        assert physical_row_number(4, rows_before=1) == 6

    def test_sequence_that_starts_at_physical_row_one(self):
        """旧引擎：序列第 0 个元素就是物理第 1 行（`rows_before=0`）。"""
        assert physical_row_number(0) == 1
        assert physical_row_number(4) == 5

    def test_default_is_the_one_based_row_number(self):
        assert physical_row_number(0) == 1
        assert physical_row_number(9) == 10


def _workbook_bytes(rows):
    """把 rows 写成真正的 xlsx 字节流（str 会成为文本单元格）。"""
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = SHEET
    for row_index, row in enumerate(rows, start=1):
        for col_index, value in enumerate(row, start=1):
            sheet.cell(row=row_index, column=col_index, value=value)
    buf = io.BytesIO()
    book.save(buf)
    return buf.getvalue()


class _FakeBlob:
    def __init__(self, data):
        self._data = data

    @property
    def data_stream(self):
        return io.BytesIO(self._data)


class _FakeTree:
    def __init__(self, files):
        self._files = files

    def __truediv__(self, path):
        if path not in self._files:
            raise KeyError(path)
        return self._files[path]


class _FakeCommit:
    def __init__(self, files):
        self.tree = _FakeTree(files)


@pytest.fixture()
def legacy():
    """旧引擎：绕过需要仓库参数的构造函数（本文件只测纯比较逻辑）。"""
    service = GitService.__new__(GitService)
    service.performance_stats = {'total_diff_time': 0.0, 'excel_processing_time': 0.0}
    return service


def _legacy_sheet_rows(rows):
    """旧引擎读取层读出来的工作表（列字母当键、含物理第 1 行）。"""
    service = GitService.__new__(GitService)
    return extract_excel_data(
        service, _FakeCommit({'a.xlsx': _FakeBlob(_workbook_bytes(rows))}), 'a.xlsx'
    )[SHEET]
HEADER = ['id', '名称', '备注']
DATA_ROWS = [
    HEADER,
    ['1', '甲', 'x'],
    ['2', '乙', 'y'],
    ['3', '丙', 'z'],
    ['4', '丁', 'w'],
]


def _shown_values(row):
    """一个差异行**展示出来的单元格取值**（两种载荷形态都要认）。

    * 主引擎 `DiffService` 给 `data`（按列名的字典）；
    * 旧引擎 `GitService` 给 `cells`（与 `headers` 逐列对齐的 `{'value', 'status'}`）。

    两者都是平台真实产出的形状（`services/ai/platform_provider._cell_entry` 的 docstring
    记着这件事）。这里只取「展示出来的那一行的取值」，用来核对行号指向的是哪一行。
    """
    data = row.get('data')
    if isinstance(data, dict):
        return ['' if value is None else str(value) for value in data.values()]
    return [
        '' if cell.get('value') is None else str(cell.get('value'))
        for cell in row.get('cells', [])
    ]


class TestBothEnginesAgreeOnTheSameRow:
    """同一张表、同一行 → 两个引擎给出**相同且等于物理行号**的 `row_number`。"""

    def _new_engine_rows(self, current, previous):
        payload = DiffService().process_diff('a.xlsx', current, previous)
        return payload['sheets'][SHEET]['rows']

    def _legacy_rows(self, legacy, current, previous):
        """旧引擎：入参是**行清单**（不是字节），因为读取层要按行清单现造工作簿。"""
        cur = _legacy_sheet_rows(current)
        prev = _legacy_sheet_rows(previous) if previous else []
        payload = legacy._generate_excel_diff_data({SHEET: cur}, {SHEET: prev}, 'a.xlsx')
        return payload['sheets'][SHEET]['rows']

    def test_a_modified_row_gets_the_same_number(self, legacy):
        """物理第 3 行改了一格：两个引擎都必须说「第 3 行」。"""
        before = [list(row) for row in DATA_ROWS]
        after = [list(row) for row in DATA_ROWS]
        after[2] = ['2', '乙', 'y-改']

        new_rows = self._new_engine_rows(_workbook_bytes(after), _workbook_bytes(before))
        old_rows = self._legacy_rows(legacy, after, before)

        new_numbers = [row['row_number'] for row in new_rows]
        old_numbers = [row['row_number'] for row in old_rows]
        assert new_numbers, f'主引擎没报出这条改动：{new_rows}'
        assert old_numbers, f'旧引擎没报出这条改动：{old_rows}'
        assert new_numbers == [3], f'主引擎的行号不是物理第 3 行：{new_rows}'
        assert old_numbers == [3], f'旧引擎的行号不是物理第 3 行：{old_rows}'
        assert new_numbers == old_numbers, (
            f'两个引擎对同一行给出了不同的行号：主引擎 {new_numbers}、旧引擎 {old_numbers}。\n'
            f'同一个改动在页面上与 AI 提示词里会是两个坐标，而回落不报错。'
        )

    def test_rows_inserted_and_removed_keep_both_engines_aligned(self, legacy):
        """插了一行之后：两个引擎报出的行号，指向的都是**它自己展示的那一行**。

        这里不断言两边的**结论**相同 —— 主引擎按内容配对、旧引擎逐位置比较（它没有配对
        算法），所以「哪几行算修改、哪一行算新增」两边本来就不同，那是另一个议题。
        本次统一的是**行号**：每个差异行的行号必须等于它展示的那一行在文件里的物理行号，
        否则评审者拿着「第 5 行」回文件里核对会找到另一行。
        """
        before = [HEADER, ['1', '甲', 'x'], ['2', '乙', 'y'], ['3', '丙', 'z']]
        after = [HEADER, ['1', '甲', 'x'], ['9', '新', 'n'], ['2', '乙', 'y'], ['3', '丙', 'z']]

        new_rows = self._new_engine_rows(_workbook_bytes(after), _workbook_bytes(before))
        old_rows = self._legacy_rows(legacy, after, before)

        for rows in (new_rows, old_rows):
            for row in rows:
                if row['status'] == 'removed':
                    continue  # 删除行展示的是上一版的内容，拿当前表核对没有意义
                number = row['row_number']
                shown = _shown_values(row)
                assert shown == [str(value) for value in after[number - 1]], (
                    f'第 {number} 行展示的是 {shown}，而文件里第 {number} 行是 '
                    f'{after[number - 1]} —— 行号指向了别的一行。'
                )

        # 主引擎认得「9,新,n」是插进来的那一行，并把它报在物理第 3 行。
        inserted = [row for row in new_rows if row['status'] == 'added']
        assert [row['row_number'] for row in inserted] == [3], new_rows

    def test_deleted_sheet_rows_are_physical_rows_too(self, legacy):
        """整张表被删除时也一样：共同报出的那几行行号一致，且都是物理行号。

        **两个引擎在这里的清单长度本来就不同**：主引擎 `header=0` 把物理第 1 行当列名
        （它不是数据行），旧引擎没有「表头」这个概念 —— 它用列字母 `A`/`B`… 当列名，
        物理第 1 行是一行普通数据，所以它的清单多一行。这一点本次**没有改**（改它等于
        动旧引擎的比较语义，超出「行号口径统一」的范围），报告里记着。
        """
        rows = [HEADER, ['1', '甲', 'x'], ['2', '乙', 'y']]

        new_payload = DiffService().process_deleted_file('a.xlsx', _workbook_bytes(rows))
        # 旧引擎的「整个文件被删除」走的是 `_deleted_sheet_diff`（`parse_excel_diff` 里
        # 读到父提交的每一张表后逐个调它，因为 `_generate_excel_diff_data` 在
        # `current_data` 为空时就返回「无法读取Excel文件内容」了）。
        old_payload = legacy._deleted_sheet_diff(_legacy_sheet_rows(rows))

        new_numbers = [row['row_number'] for row in new_payload['sheets'][SHEET]['rows']]
        old_numbers = [row['row_number'] for row in old_payload['rows']]
        assert old_numbers == [1, 2, 3], (
            f'旧引擎的行号不是物理行号（1..N）：{old_numbers}'
        )
        assert new_numbers == [2, 3], (
            f'主引擎的行号不是物理行号（列名行被 `header=0` 吃掉）：{new_numbers}'
        )
        assert set(new_numbers) <= set(old_numbers) and (
            new_numbers == [number for number in old_numbers if number in set(new_numbers)]
        ), (
            f'两个引擎共同报出的行号必须一致：主引擎 {new_numbers}、旧引擎 {old_numbers}'
        )

    def test_the_dataframe_helper_uses_the_shared_function(self):
        """主引擎那一步的算式直接钉住：`header=0` ⇒ 第 0 条数据是物理第 2 行。"""
        df = pd.DataFrame([[1, 2], [3, 4]], columns=['a', 'b'])
        indexed = DiffService()._dataframe_rows_with_index(df)
        assert [number for number, _ in indexed] == [2, 3]
