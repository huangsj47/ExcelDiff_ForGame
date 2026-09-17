# -*- coding: utf-8 -*-
"""列身份（按配对而不是按列名）+ 表头行变更要能看见。

## 缺陷形态（线上实测）

读表用 `header=0`，第 1 行成了列名 —— 于是**列身份完全等于第 1 行的那串文字**。
第 1 行本身也是会被改的，一改，列身份就整体错位：

* 6657 `config/50_weapon/吸灵器交互显示.xlsx`：提交只是把「中文字段名行」和
  「英文字段名行」两行互换（物理 14 格）。旧实现把并集列里的 6 个名字两两配不上，
  每一行都多出「旧值→空 / 空→新值」，于是**0 个数据变更被加工成 9 行、92 条变更**，
  其中第 4 行同时被报成「新增」和「删除」（两版逐格完全相同）。
* 6427 / 6408：只改了一个列名（`保险箱模态房比例` → `双人风轮ID`、无名列 → `注释`），
  两列的值恰好每一行都相等，于是产出「零净变更」的幽灵修改行，
  `summary.modified` 从 7 虚增到 8 / 从 11 虚增到 12。
* 另一面：表头改名**从不作为变更呈现**（第 1 行被当成列名吃掉了），
  评审者只能从新旧列名成对出现去猜 —— 只改列名的一次提交会被显示成「未检测出变更」。

## 现在的口径

列身份按**配对**决定（`DiffService._pair_columns`）：先按同名列锚定，锚点之间
「两侧列数相等」才按顺序配对（那是改名），数量不等就不配（那是增删）。
上一版的列名按配对结果改写成当前列名之后再逐格比，列级差异单独放进
`header_changes`（renamed / added / removed），界面据此提示「列名变更」。

于是：改名不再放大成逐行假变更，改名这件事本身也不再隐形。
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


def _xlsx(header_row, data_rows, sheet='S'):
    """直接写原始行：第 1 行是表头，其余是数据。

    用 openpyxl 而不是 pandas，是为了能构造「表头行本身被改」的样本 ——
    pandas 写入时表头来自 DataFrame 的列名，表达不了这件事。
    """
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = sheet
    worksheet.append(list(header_row))
    for row in data_rows:
        worksheet.append(list(row))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _sheet(previous_bytes, current_bytes, name='S', **kwargs):
    payload = DiffService().process_diff('config/x.xlsx', current_bytes, previous_bytes, **kwargs)
    assert payload.get('type') == 'excel', payload
    return payload['sheets'][name]


def _changes(row):
    return {(c['column'], c.get('old_value'), c.get('new_value')) for c in row.get('cell_changes') or []}


# ---------------------------------------------------------------------------
#  表头行被改：只能是「列名变更 + 表头那一行」，不能放大成整表
# ---------------------------------------------------------------------------

class TestHeaderRowRewriteDoesNotAmplify:
    EN_NAMES = ['id', 'btnTitle', 'iconAbName']
    CN_NAMES = ['id中文', '按钮文本', '按钮图标ab']
    DATA = [['1', 'a', 'x'], ['2', 'b', 'y']]

    def _frames(self):
        # 提交只做了一件事：把两行表头互换（其余数据一行没动）
        previous = _xlsx(self.EN_NAMES, [self.CN_NAMES] + self.DATA)
        current = _xlsx(self.CN_NAMES, [self.EN_NAMES] + self.DATA)
        return previous, current

    def test_data_rows_are_not_reported_as_changed(self):
        previous, current = self._frames()
        sheet = _sheet(previous, current)

        reported = {r['row_number'] for r in sheet['rows']}
        assert reported <= {2}, (
            f"只有物理第 2 行（表头第二行）的内容真的变了，却报了第 {sorted(reported)} 行 —— "
            "数据行被列身份错位牵连成了变更，这正是线上 6657 的形态"
        )
        statuses = Counter(r['status'] for r in sheet['rows'])
        assert statuses.get('modified', 0) == 0, f"数据行不该出现修改行：{dict(statuses)}"
        # 第 2 行是「整行内容全换」（两行表头互换），按内容匹配认不出是同一行，
        # 现行口径报成一删一增 —— 这是行匹配的已知边界，不是列身份的问题，
        # 这里只钉住「别把没变的数据行卷进来」。

    def test_the_swap_is_visible_as_column_renames(self):
        previous, current = self._frames()
        sheet = _sheet(previous, current)

        changes = sheet['header_changes']
        assert [c['change'] for c in changes] == ['renamed'] * len(self.EN_NAMES), (
            f"表头两行互换没有被报成列名变更：{changes} —— 改名这件事对评审者隐形了"
        )
        assert [c['old_name'] for c in changes] == self.EN_NAMES
        assert [c['new_name'] for c in changes] == self.CN_NAMES
        assert [c['column_index'] for c in changes] == [1, 2, 3], "列序从 1 开始，与 Excel 一致"

    def test_only_a_renamed_column_leaves_no_ghost_row(self):
        """只改列名、两侧值逐行相等 —— 一行都不该报（历史行为：零净变更的幽灵行）。"""
        previous = _xlsx(['id', '保险箱模态房比例'], [['1', 'all'], ['2', 'all']])
        current = _xlsx(['id', '双人风轮ID'], [['1', 'all'], ['2', 'all']])

        sheet = _sheet(previous, current)
        assert sheet['rows'] == [], (
            f"只改了列名、值一格没变，却报了 {sheet['rows']} —— "
            "评审者会读成「旧列被清空、新列被填上」"
        )
        assert [c['change'] for c in sheet['header_changes']] == ['renamed']
        assert sheet['header_changes'][0]['old_name'] == '保险箱模态房比例'
        assert sheet['header_changes'][0]['new_name'] == '双人风轮ID'

    def test_a_real_change_in_a_renamed_column_is_still_reported(self):
        # 五列改一格 → 相似度 0.8，行身份没有歧义（两列的表改一格只有 0.5，
        # 按内容匹配本来就分不清「改了」还是「删了又加」，那是另一回事）。
        columns_previous = ['id', '旧名', 'c2', 'c3', 'c4']
        columns_current = ['id', '新名', 'c2', 'c3', 'c4']
        previous = _xlsx(columns_previous, [['1', 'a', 'p', 'q', 'r'], ['2', 'b', 'p', 'q', 'r']])
        current = _xlsx(columns_current, [['1', 'a', 'p', 'q', 'r'], ['2', 'B', 'p', 'q', 'r']])

        sheet = _sheet(previous, current)
        assert len(sheet['rows']) == 1, f"只有第 3 行改了一格，却报了 {len(sheet['rows'])} 行"
        row = sheet['rows'][0]
        assert row['row_number'] == 3, "第 3 行才是第二行数据（第 1 行表头、第 2 行第一条数据）"
        assert _changes(row) == {('新名', 'b', 'B')}, (
            "列改名之后，真正改掉的那一格没有被报出来（或报成了别的列）"
        )
        assert [c['change'] for c in sheet['header_changes']] == ['renamed']


# ---------------------------------------------------------------------------
#  列增删：报成列级事件，不能变成「逐行改值」
# ---------------------------------------------------------------------------

class TestColumnInsertAndDelete:
    def test_inserted_column_is_reported_as_an_added_column(self):
        previous = _xlsx(['id', 'a', 'b'], [['1', 'x', 'p'], ['2', 'y', 'q']])
        current = _xlsx(['id', 'a', '新列', 'b'], [['1', 'x', 'N1', 'p'], ['2', 'y', 'N2', 'q']])

        sheet = _sheet(previous, current)
        assert [c['change'] for c in sheet['header_changes']] == ['added'], (
            f"插了一列却报成 {sheet['header_changes']} —— 后面的列会被认成「改名」"
        )
        assert sheet['header_changes'][0]['column'] == '新列'
        # 该列是新的：每一行都是「空 → 值」，其它列一格未动
        for row in sheet['rows']:
            assert _changes(row) == {('新列', '', row['data']['新列'])}, (
                f"插入一列却把别的列也报成变了：{_changes(row)}"
            )

    def test_blank_header_placeholders_are_not_reported_as_renames(self):
        """空表头列的名字里带着列的位置（pandas 的 `Unnamed: N`）：插一列之后，
        后面所有空表头列的占位名都会平移一位。那是位置，不是改名 ——
        报出来只会淹没真正的列变更（线上 6556 插一列会附带一串
        `Unnamed: 75 → Unnamed: 76`）。取值仍按同一物理列比，所以这里没有多余的行变更。"""
        previous = _xlsx(['id', 'a', 'b', '', '', '', ''],
                         [['1', 'x', 'y', 'p', 'q', 'r', 's']])
        current = _xlsx(['id', 'a', '新列', 'b', '', '', '', ''],
                        [['1', 'x', 'N', 'y', 'p', 'q', 'r', 's']])

        sheet = _sheet(previous, current)
        placeholder = re.compile(r'Unnamed: \d+')
        for change in sheet['header_changes']:
            assert not (placeholder.fullmatch(change['old_name'] or '')
                        and placeholder.fullmatch(change['new_name'] or '')), (
                f"空表头占位列的位移被报成了列名变更：{change}"
            )
        assert [c['change'] for c in sheet['header_changes']] == ['added']
        assert sheet['header_changes'][0]['column'] == '新列'

    def test_deleted_column_is_reported_as_a_removed_column(self):
        previous = _xlsx(['id', 'a', '备注'], [['1', 'x', 'old1'], ['2', 'y', 'old2']])
        current = _xlsx(['id', 'a'], [['1', 'x'], ['2', 'y']])

        sheet = _sheet(previous, current)
        assert [c['change'] for c in sheet['header_changes']] == ['removed'], (
            f"删了一列却报成 {sheet['header_changes']}"
        )
        assert sheet['header_changes'][0]['old_name'] == '备注'
        for row in sheet['rows']:
            changes = _changes(row)
            assert len(changes) == 1 and next(iter(changes))[0] == '备注', (
                f"删掉一列却牵连了别的列：{changes}"
            )


# ---------------------------------------------------------------------------
#  行号 = Excel 行号
# ---------------------------------------------------------------------------

class TestRowNumbersAreExcelRows:
    def test_first_data_row_is_excel_row_2(self):
        previous = _xlsx(['id', '值'], [['1', 'a'], ['2', 'b']])
        current = _xlsx(['id', '值'], [['1', 'a'], ['2', 'B']])

        sheet = _sheet(previous, current)
        assert sheet['rows'][0]['row_number'] == 3, (
            "行号比文件里的真实行号小 1 —— 评审者按它回 Excel 里核对会整体错一行"
            "（线上 8 个分片独立复核，20/20 都是这个偏移）"
        )

    def test_added_and_deleted_sheets_use_the_same_convention(self):
        frame = [['1', 'a'], ['2', 'b']]
        added = DiffService().process_diff(
            'config/x.xlsx', _xlsx(['id', '值'], frame), _xlsx(['id', '值'], []))
        removed = DiffService().process_diff(
            'config/x.xlsx', _xlsx(['id', '值'], []), _xlsx(['id', '值'], frame))

        assert [r['row_number'] for r in added['sheets']['S']['rows']] == [2, 3]
        assert [r['row_number'] for r in removed['sheets']['S']['rows']] == [2, 3]


# ---------------------------------------------------------------------------
#  整表增删不把「表里本来就有的空行」算成新增/删除行
# ---------------------------------------------------------------------------

class TestBlankRowsAreNotCountedAsAddedOrRemoved:
    """线上形态：6011 一张表报「删除 5653 行」，其中大片是空行；6136 报 1699 行、
    1686 行整行空白。比较分支一直用 `_has_valid_data` 过滤空行，整表增删分支没有 ——
    同一个「有没有变更」的问题，答案不该取决于表的增删与否。
    """

    def test_added_sheet_skips_blank_rows(self):
        current = _xlsx(['id', '值'], [['1', 'a'], ['', ''], ['3', 'c']])
        payload = DiffService().process_diff('config/x.xlsx', current, _xlsx(['id', '值'], []))

        sheet = payload['sheets']['S']
        assert sheet['stats']['added'] == 2, (
            f"新增工作表把整行空白的行也算成新增行（{sheet['stats']}）—— 统计被抬高"
        )
        assert len(sheet['rows']) == 2
        assert all(any(str(v) for v in row['data'].values()) for row in sheet['rows'])

    def test_deleted_sheet_skips_blank_rows(self):
        previous = _xlsx(['id', '值'], [['1', 'a'], ['', ''], ['3', 'c']])
        payload = DiffService().process_diff('config/x.xlsx', _xlsx(['id', '值'], []), previous)

        sheet = payload['sheets']['S']
        assert sheet['stats']['removed'] == 2, (
            f"整表删除把空行也算成删除行（{sheet['stats']}）"
        )
        assert sheet['stats']['removed'] == len(sheet['rows']), "统计与正文行数必须一致"
        assert payload['summary']['removed'] == 2


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
