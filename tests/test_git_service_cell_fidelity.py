# -*- coding: utf-8 -*-
"""旧 Excel 引擎（`services/git_service.py`）的单元格口径必须与主引擎一致

## 为什么需要这个文件

平台有**两条**Excel 比较实现：

* 主引擎 `services/diff_service.py`（`DiffService.process_diff`，DIFF_LOGIC_VERSION 1.9.0
  起按文本原样读取 + `_normalize_value` 只把真正的空当空）；
* 旧引擎 `GitService`（`parse_excel_diff` → `_generate_excel_diff_data` →
  `_parallel_compare_sheets_optimized` → `_compare_sheet_data` → `_fast_compare_rows`）。

旧引擎**在活路径上**，不是历史遗留的死代码：

* `services/commit_diff_view_service.py:182`（日志「使用旧的Excel处理逻辑作为备用」）：
  主引擎没产出时用它兜底并直接渲染；
* `services/commit_diff_logic.py:674`（`get_diff_data` 的 Excel 分支兜底）与 `:769`
  （`get_real_diff_data_for_merge` 缓存未命中时的合并数据）。

它的 `_normalize_cell_value`（`services/git_service.py:1485`）是主引擎那份黑名单的
**第三份副本**：

    val_str = str(val).strip()
    if val_str.lower() in ('', 'nan', 'none', 'null', '<na>', 'undefined'):
        return None

读取层（`git_excel_parser_helpers.extract_excel_data`）用 openpyxl 取
`cell.value` 后 `str(...)`，**不 NA 转换、不 strip** —— 字面量是完整送到比较层的。
于是这三类真实改动全部被判「没变」（实测 `has_changes=False`）：

    'null' → 空单元格      配表里把「无引用」清掉
    'null' → 'None'        两个不同字面量
    '  x  ' → 'x'          strip 掉的首尾空格

后果与主引擎那份完全相同：审核者看到「没有变更」。

## 关于 `'undefined'`（为什么它**不**算空值）

这一路的取值只来自 openpyxl 读到的单元格（`str(cell_value)`）。前端 JS 的 `undefined`
不会出现在这里：请求体里它会被 JSON 序列化成 `null` 或缺键，`JSON.stringify` 也不会
产出字符串 `"undefined"`。所以 Python 字符串 `'undefined'` 只可能是**配表里真实写下的
文本**（导出工具确实会写出这种占位）。按本仓库「表里怎么写就怎么比」的口径，
它必须与空值区分 —— 否则又变成「有变更显示成没有变更」。

## 关于 `strip()`（为什么必须去掉）

旧实现不仅拿 strip 去判空，还把 **strip 后的结果当作返回值**，于是
`'  x  '` 与 `'x'` 归一成同一个值。首尾空格在配表里是真实内容（对齐、占位、以及某些
导出格式用它表示空串），主引擎已明确不 strip（见 `tests/test_excel_literal_fidelity.py`），
两条实现必须同口径。此处的空值判定也不该用 strip：**只把真正的空**（None / NaN /
NaT / pd.NA / 空字符串）当空，空白串 `'   '` 是取值。

不 import app、不碰数据库（用 `GitService.__new__` 绕过需要仓库参数的构造函数）。
"""
import io
import os
import sys

import pandas as pd
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.git_excel_parser_helpers import extract_excel_data  # noqa: E402
from services.git_service import GitService  # noqa: E402

# 配表里会真实出现的「看起来像空值」的文本 —— 它们是取值，不是空。
NA_LIKE_LITERALS = ['NULL', 'null', 'None', 'none', 'nan', 'NaN', '<NA>', 'undefined']


@pytest.fixture()
def svc():
    """绕过需要仓库参数的构造函数：本文件只测纯比较/读取逻辑。"""
    return GitService.__new__(GitService)


# ---------------------------------------------------------------------------
# 一、单元格归一：只有真正的空才算空
# ---------------------------------------------------------------------------
class TestNormalizeCellValue:
    """`_normalize_cell_value` 与 `DiffService._normalize_value` 同口径。"""

    @pytest.mark.parametrize('text', NA_LIKE_LITERALS)
    def test_na_like_text_is_kept(self, text):
        got = GitService._normalize_cell_value(text)
        assert got is not None, (
            f'{text!r} 被判成了空值。\n'
            f'后果：它 → 空单元格、以及它 → 另一个字面量，都会被判「相等」→ 不报变更。'
        )
        assert got == text, f'{text!r} 被改写成 {got!r}'

    @pytest.mark.parametrize('text', ['  x  ', 'x ', ' x', '   '])
    def test_surrounding_spaces_are_not_stripped(self, text):
        """首尾空格是真实内容；空白串与「没有内容」也是两种写法。"""
        got = GitService._normalize_cell_value(text)
        assert got == text, (
            f'{text!r} 被归一成 {got!r} —— strip 之后「首尾空格变了」这类改动不会报出来，'
            f'空白串也会被当成空行/空格。'
        )

    def test_real_blanks_are_none(self):
        """反向保险：真正的空值仍必须归一为 None（否则会出现「空 vs 空」的假变更）。"""
        assert GitService._normalize_cell_value(None) is None
        assert GitService._normalize_cell_value('') is None
        assert GitService._normalize_cell_value(float('nan')) is None
        assert GitService._normalize_cell_value(pd.NA) is None
        assert GitService._normalize_cell_value(pd.NaT) is None


# ---------------------------------------------------------------------------
# 二、比较层：这些改动必须被判成「有变更」
# ---------------------------------------------------------------------------
class TestCompareSheetDataReportsChanges:
    """`_compare_sheet_data` 是旧引擎活路径上的入口（经 `_parallel_compare_sheets_optimized`）。"""

    @pytest.mark.parametrize('before_val,after_val', [
        ('null', ''),            # 把字面量清成空单元格
        ('null', 'None'),        # 两个不同字面量
        ('  x  ', 'x'),          # 首尾空格
        (' ', '  '),             # 全空格占位行被改宽
        ('undefined', ''),       # 导出工具写下的占位文本被清掉
        ('NULL', 'null'),        # 大小写
    ])
    def test_change_is_reported(self, svc, before_val, after_val):
        result = svc._compare_sheet_data([{'A': before_val}], [{'A': after_val}])
        assert result['has_changes'] is True, (
            f'{before_val!r} → {after_val!r} 被判成「没有变更」：{result}\n'
            f'审核者看不到这条改动。'
        )
        assert result['rows'], f'报了 has_changes 却没有行数据：{result}'

    def test_cell_change_carries_both_values(self, svc):
        """报出来的变更要能看出改了什么（不是只有一句「有变更」）。

        载荷形状：`rows[i]['cells']` 与 `headers` 逐列对齐，被改的格子带
        `old_value` / `new_value`（见 `_fast_compare_rows` 末尾的格式化）。
        """
        result = svc._compare_sheet_data([{'A': 'null'}], [{'A': ''}])
        row = result['rows'][0]
        cells = row.get('cells') or []
        assert len(cells) == 1, f'cells 应与 headers 对齐：{row}'
        cell = cells[0]
        assert cell.get('status') == 'changed', f'这格被改过，状态应为 changed：{row}'
        assert cell.get('old_value') == '' and cell.get('new_value') == 'null', (
            f'单元格级变更没有带上新旧值，界面无法显示改了什么：{row}'
        )

    def test_identical_literal_sheets_report_nothing(self, svc):
        """反向保险：两侧完全一样（含字面量与空白串）不得凭空报变更。"""
        for value in ['null', 'NULL', 'None', 'undefined', '  x  ', ' ', 'v']:
            result = svc._compare_sheet_data([{'A': value}], [{'A': value}])
            assert result['has_changes'] is False, (
                f'两侧都是 {value!r} 却报出了变更：{result}'
            )

    def test_real_blanks_still_match(self, svc):
        """反向保险：真正的空值之间仍必须判「没变」（None 与空串都是空）。"""
        result = svc._compare_sheet_data([{'A': None, 'B': ''}], [{'A': '', 'B': None}])
        assert result['has_changes'] is False, f'空 vs 空被判成了变更：{result}'


# ---------------------------------------------------------------------------
# 三、端到端：读取层不改写 → 比较层必须报出来
# ---------------------------------------------------------------------------
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


def _workbook_bytes(rows):
    """把 rows 写成真正的 xlsx 字节流（str 会成为文本单元格）。"""
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = 'Sheet1'
    for row_index, row in enumerate(rows, start=1):
        for col_index, value in enumerate(row, start=1):
            sheet.cell(row=row_index, column=col_index, value=value)
    buf = io.BytesIO()
    book.save(buf)
    return buf.getvalue()


def _read_sheet(rows):
    return extract_excel_data(GitService.__new__(GitService), _FakeCommit({'a.xlsx': _FakeBlob(_workbook_bytes(rows))}), 'a.xlsx')['Sheet1']


class TestReadToCompareChain:
    """读取层（openpyxl）原样保留字面量，所以比较层必须能报出它们的变化。"""

    def test_reader_keeps_literals(self):
        """先钉住前提：这一路**不做** NA 转换、不 strip。"""
        sheet = _read_sheet([['null', 'NULL', '  x  ', 'undefined', '']])
        assert sheet[0]['A'] == 'null', sheet[0]
        assert sheet[0]['B'] == 'NULL', sheet[0]
        assert sheet[0]['C'] == '  x  ', (
            f"读取层把首尾空格吃了：{sheet[0]['C']!r} —— 比较层再怎么保真也救不回来"
        )
        assert sheet[0]['D'] == 'undefined', sheet[0]
        assert sheet[0]['E'] == '', (
            f'真正的空单元格应读成空串，实际 {sheet[0]["E"]!r}'
        )

    def test_literal_change_survives_the_whole_legacy_chain(self, svc):
        """走 `_parallel_compare_sheets_optimized`（旧引擎真正产出的那条链）。"""
        before = _read_sheet([['1', 'null', 'A']])
        after = _read_sheet([['1', 'None', 'A']])
        diff_sheets = svc._parallel_compare_sheets_optimized({'Sheet1': after}, {'Sheet1': before})
        sheet = diff_sheets['Sheet1']
        assert sheet.get('has_changes') is True, (
            f'整条旧引擎链路把这条改动吞掉了：{sheet}\n'
            f'审核者界面/AI 看到的是「没有变更」。'
        )

    def test_cleared_reference_survives_the_whole_legacy_chain(self, svc):
        """把引用清空（字面量 → 空单元格）同样必须显示出来。"""
        before = _read_sheet([['1', 'null', 'A']])
        after = _read_sheet([['1', '', 'A']])
        diff_sheets = svc._parallel_compare_sheets_optimized({'Sheet1': after}, {'Sheet1': before})
        assert diff_sheets['Sheet1'].get('has_changes') is True, diff_sheets['Sheet1']
