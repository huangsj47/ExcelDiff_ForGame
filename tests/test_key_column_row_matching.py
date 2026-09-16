# -*- coding: utf-8 -*-
"""关键列行匹配 + 「不漏也不编」不变量测试。

## 背景

`templates/help.html` 的「关键列」一节已经把契约写清楚了：
列号从 1 开始、英文逗号分隔（`1,2,3`），「系统根据关键列匹配新旧版本中的同一行，
精确计算行级和单元格级变更」。但引擎历史上**从来没读过** `Repository.key_columns`
（`models/repository.py:55`），行匹配只按前 3 列的相似度猜配对，于是：

* 把不同的行配成一条「修改」，报出来的改前值是另一行的
  （线上审计 5988：报的行 9 里 old 取自 Excel 第 21 行、new 取自第 10 行）；
* 同一份表、同一个改动，行数跨过 100 结论就变（大表路径阈值 0.85/0.5，
  小表路径 0.6）—— 既有大表少报（5988 报 3 行而 git 是 456 行），也不可复现。

## 不变量（本文件的核心）

只看「平台报了几行」判断不了对错，所以这里用两条与算法无关的不变量：

* **不编**：每一条 `added`/`modified` 行报出来的当前值，必须真的出现在当前版本里；
  每一条 `removed` 行、以及每一条 `modified` 行按 `cell_changes` 还原出来的「改前行」，
  必须真的出现在上一版里。5988 那种「old 取自另一行」的错配会直接违反这一条。
* **不漏**：凡是「当前版本里有、上一版里没有」的行，必须能在 payload 里找到对应
  （added 行，或 modified 行）；反过来「上一版有、当前版没有」的行必须能找到
  removed/modified 行。

这两条一起把「行匹配错了 / 少报了 / 编造了改前值」都钉住，且不依赖具体算法实现。
"""
from __future__ import annotations

import io
import os
import sys
from collections import Counter

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.diff_service import DiffService  # noqa: E402

# ---------------------------------------------------------------------------
#  不变量检查器
# ---------------------------------------------------------------------------

def _rows_as_tuples(df):
    """把 DataFrame 变成可比较的「行多重集」（同内容的多行按出现次数计）。"""
    return Counter(tuple(str(v) for v in row) for row in df.itertuples(index=False))


def _changed_rows(sheet):
    return [row for row in sheet.get('rows') or []
            if row.get('status') in ('added', 'removed', 'modified')]


def _row_tuple(data, columns):
    return tuple(str((data or {}).get(column, '')) for column in columns)


def _previous_side_of(row, columns):
    """由一条 modified 行还原出它在上一版里的样子（把变更列换回 old_value）。"""
    data = dict(row.get('data') or {})
    for change in row.get('cell_changes') or []:
        data[change['column']] = change.get('old_value')
    return _row_tuple(data, columns)


def assert_diff_is_faithful(previous_df, current_df, sheet, *, context=""):
    """两条不变量：不编（报出来的东西真实存在）+ 不漏（真实变更都报出来了）。"""
    columns = list(current_df.columns)
    assert list(sheet.get('headers') or []) == columns or not sheet.get('headers'), (
        f"{context} headers 与当前版本列不一致: {sheet.get('headers')} vs {columns}"
    )

    current_rows = _rows_as_tuples(current_df)
    previous_rows = _rows_as_tuples(previous_df)
    reported = _changed_rows(sheet)

    # --- 不编 ---
    for row in reported:
        row_tuple = _row_tuple(row.get('data'), columns)
        if row['status'] in ('added', 'modified'):
            assert current_rows[row_tuple] > 0, (
                f"{context} 报了一条 {row['status']} 行，但这份内容在当前版本里根本不存在 "
                f"（行号 {row.get('row_number')}）：{row_tuple}"
            )
        if row['status'] == 'removed':
            assert previous_rows[row_tuple] > 0, (
                f"{context} 报了一条 removed 行，但这份内容在上一版里根本不存在 "
                f"（行号 {row.get('row_number')}）：{row_tuple}"
            )
        if row['status'] == 'modified':
            old_tuple = _previous_side_of(row, columns)
            assert previous_rows[old_tuple] > 0, (
                f"{context} 修改行（行号 {row.get('row_number')}）还原出的「改前行」"
                f"在上一版里不存在 —— 这条修改的改前值取自别的行：{old_tuple}"
            )

    # --- 不漏：当前版本独有的行必须被报出来 ---
    for row_tuple, count in (current_rows - previous_rows).items():
        covered = 0
        for row in reported:
            if row['status'] in ('added', 'modified') and _row_tuple(row.get('data'), columns) == row_tuple:
                covered += 1
        assert covered >= count, (
            f"{context} 当前版本里有 {count} 行是新出现的（或改动成新内容），"
            f"payload 只覆盖了 {covered} 行 —— 有变更没报出来：{row_tuple}"
        )

    # --- 不漏：上一版本独有的行必须被报出来 ---
    for row_tuple, count in (previous_rows - current_rows).items():
        covered = 0
        for row in reported:
            if row['status'] == 'removed' and _row_tuple(row.get('data'), columns) == row_tuple:
                covered += 1
            elif row['status'] == 'modified' and _previous_side_of(row, columns) == row_tuple:
                covered += 1
        assert covered >= count, (
            f"{context} 上一版里有 {count} 行消失了（或改动后不再存在），"
            f"payload 只覆盖了 {covered} 行 —— 有删除没报出来：{row_tuple}"
        )


# ---------------------------------------------------------------------------
#  构造两版工作簿
# ---------------------------------------------------------------------------

def _workbook(sheets):
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=name, index=False)
    return buffer.getvalue()


def _diff(previous_sheets, current_sheets, *, key_columns=None, path="config/x.xlsx"):
    payload = DiffService().process_diff(
        path, _workbook(current_sheets), _workbook(previous_sheets), key_columns=key_columns)
    assert payload.get('type') == 'excel', payload
    return payload


def _sheet(previous_sheets, current_sheets, name, *, key_columns=None):
    payload = _diff(previous_sheets, current_sheets, key_columns=key_columns)
    return payload['sheets'][name]


# ---------------------------------------------------------------------------
#  关键列配置解析
# ---------------------------------------------------------------------------

COLUMNS = ['id', '名字', '数值', '备注']


class TestKeyColumnParsing:
    service = DiffService()

    def test_digits_are_one_based(self):
        assert self.service._resolve_key_columns('1', COLUMNS) == ['id']
        assert self.service._resolve_key_columns('2', COLUMNS) == ['名字']
        assert self.service._resolve_key_columns('1,2', COLUMNS) == ['id', '名字']

    def test_letters_and_chinese_separators_are_accepted(self):
        assert self.service._resolve_key_columns('B', COLUMNS) == ['名字']
        assert self.service._resolve_key_columns('A，C', COLUMNS) == ['id', '数值']

    def test_out_of_range_and_garbage_are_ignored(self):
        assert self.service._resolve_key_columns('0', COLUMNS) == []
        assert self.service._resolve_key_columns('99', COLUMNS) == []
        assert self.service._resolve_key_columns('abc,x', COLUMNS) == []
        assert self.service._resolve_key_columns('1,99', COLUMNS) == ['id']

    def test_duplicates_collapse_and_empty_means_unconfigured(self):
        assert self.service._resolve_key_columns('1,1', COLUMNS) == ['id']
        assert self.service._resolve_key_columns('', COLUMNS) == []
        assert self.service._resolve_key_columns(None, COLUMNS) == []


# ---------------------------------------------------------------------------
#  关键列驱动的行匹配
# ---------------------------------------------------------------------------

class TestKeyColumnMatching:
    def test_narrow_table_single_cell_change_is_modified_not_add_plus_remove(self):
        """窄表里改一格：不配关键列时相似度只有 0.5（<0.6 阈值）→ 报成「删一行+加一行」；
        配了关键列，行身份是明确的，就该是「1 行修改」。"""
        previous = {'S': pd.DataFrame({'id': ['1', '2', '3'], '值': ['a', 'b', 'c']})}
        current = {'S': pd.DataFrame({'id': ['1', '2', '3'], '值': ['a', 'B', 'c']})}

        without = _sheet(previous, current, 'S')
        assert sorted(r['status'] for r in without['rows']) == ['added', 'removed'], (
            "未配置关键列时的启发式行为变了 —— 这会让下面的对照失去意义"
        )

        with_key = _sheet(previous, current, 'S', key_columns='1')
        assert len(with_key['rows']) == 1
        row = with_key['rows'][0]
        assert row['status'] == 'modified'
        assert row['cell_changes'] == [{'column': '值', 'old_value': 'b', 'new_value': 'B'}]
        assert_diff_is_faithful(previous['S'], current['S'], with_key, context='窄表+关键列')

    def test_id_change_is_remove_plus_add_under_the_key_contract(self):
        """关键列是行身份的契约：ID 改了就是「删一行 + 加一行」，
        不能被相似度匹配认成「同一行被修改」。"""
        previous = {'S': pd.DataFrame({'id': ['1', '2'], '值': ['a', 'b']})}
        current = {'S': pd.DataFrame({'id': ['1', '7'], '值': ['a', 'b']})}

        sheet = _sheet(previous, current, 'S', key_columns='1')
        assert sorted(r['status'] for r in sheet['rows']) == ['added', 'removed']
        assert_diff_is_faithful(previous['S'], current['S'], sheet, context='ID 变更')

    def test_inserted_rows_do_not_drag_the_rest_into_the_diff(self):
        """插入/删除整块行时，只有那几行该报出来 —— 相似度匹配最容易在这里
        「按位置错配」把一大片行算成修改。"""
        base = pd.DataFrame({'id': [str(i) for i in range(1, 21)],
                             '名字': [f'name{i}' for i in range(1, 21)],
                             '数值': ['0'] * 20})
        inserted = pd.DataFrame({'id': ['900', '901'], '名字': ['新行1', '新行2'], '数值': ['0', '0']})
        current = pd.concat([base.iloc[:5], inserted, base.iloc[5:]], ignore_index=True)

        sheet = _sheet({'S': base}, {'S': current}, 'S', key_columns='1')
        assert len(sheet['rows']) == 2, (
            f"插入 2 行却报了 {len(sheet['rows'])} 行 —— 插行把后面的行都算成了变更"
        )
        assert all(r['status'] == 'added' for r in sheet['rows'])
        assert_diff_is_faithful(base, current, sheet, context='插入行')

    def test_reordering_alone_is_not_a_change(self):
        """行序变化不算变更（这是关键列契约的直接推论，也是「按 ID 确认」功能的前提）。"""
        base = pd.DataFrame({'id': ['1', '2', '3'], '值': ['a', 'b', 'c']})
        reordered = base.iloc[[2, 0, 1]].reset_index(drop=True)

        sheet = _sheet({'S': base}, {'S': reordered}, 'S', key_columns='1')
        assert sheet['rows'] == [], f"只是换了行序却报了变更: {sheet['rows']}"
        assert_diff_is_faithful(base, reordered, sheet, context='纯换序')

    def test_duplicated_keys_fall_back_and_never_cross_pair(self):
        """关键列重复说明这组配置不足以唯一标识一行（配表里 类型+id 才唯一很常见）：
        此时不能硬配对，只能退回相似度，且不变量仍要成立。"""
        previous = {'S': pd.DataFrame({'id': ['1', '1', '2'], '值': ['a', 'b', 'c']})}
        current = {'S': pd.DataFrame({'id': ['1', '1', '2'], '值': ['a', 'B', 'c']})}

        sheet = _sheet(previous, current, 'S', key_columns='1')
        assert_diff_is_faithful(previous['S'], current['S'], sheet, context='重复关键列')

    def test_rows_without_a_key_value_use_similarity(self):
        """键为空的行走相似度（表头/汇总这类没有 ID 的行）。"""
        previous = {'S': pd.DataFrame({'id': ['', '1'], '名字': ['表头说明', 'a'], '值': ['', 'x']})}
        current = {'S': pd.DataFrame({'id': ['', '1'], '名字': ['表头说明改', 'a'], '值': ['', 'x']})}

        sheet = _sheet(previous, current, 'S', key_columns='1')
        assert_diff_is_faithful(previous['S'], current['S'], sheet, context='键为空的行')
        assert sheet['rows'], "键为空的行改了却没报出来"


# ---------------------------------------------------------------------------
#  口径统一：行数不该改变结论
# ---------------------------------------------------------------------------

class TestMatchingPolicyIsSizeIndependent:
    """同一个改动，表从 60 行长到 101 行，结论必须一致。

    历史行为（已修）：跨过 100 行会切到另一套阈值（哈希 0.85 / 位置 0.5），
    与下面的小表路径（0.6）不同 —— 同一个改动在大表里被认成「修改」、
    在小表里被认成「删+加」，用户看到的就是两份不一样的 diff。
    """

    @staticmethod
    def _frames(row_count):
        # 五列：改其中两格 → 相似度 3/5 = 0.6，正好卡在两套阈值之间
        previous = pd.DataFrame(
            {'id': [str(i) for i in range(row_count)],
             'c1': [f'v{i}' for i in range(row_count)],
             'c2': ['a'] * row_count,
             'c3': ['b'] * row_count,
             'c4': ['c'] * row_count})
        current = previous.copy()
        current.loc[1, 'c3'] = 'B'
        current.loc[1, 'c4'] = 'C'
        return previous, current

    def test_same_verdict_below_and_above_the_large_table_boundary(self):
        small_previous, small_current = self._frames(60)
        large_previous, large_current = self._frames(101)

        small = _sheet({'S': small_previous}, {'S': small_current}, 'S')
        large = _sheet({'S': large_previous}, {'S': large_current}, 'S')

        assert [r['status'] for r in small['rows']] == [r['status'] for r in large['rows']], (
            f"同一个改动，60 行报 {[r['status'] for r in small['rows']]}、"
            f"101 行报 {[r['status'] for r in large['rows']]} —— 结论随表大小漂移"
        )
        assert_diff_is_faithful(small_previous, small_current, small, context='60 行')
        assert_diff_is_faithful(large_previous, large_current, large, context='101 行')

    def test_large_table_still_reports_the_change(self):
        """大表路径是**加速**，不是「少报」：真变更必须报出来。"""
        previous, current = self._frames(150)
        sheet = _sheet({'S': previous}, {'S': current}, 'S')
        assert_diff_is_faithful(previous, current, sheet, context='150 行')


# ---------------------------------------------------------------------------
#  阈值边界：相似度**等于**阈值也要认成「修改」
# ---------------------------------------------------------------------------

class TestSimilarityThresholdIsInclusive:
    """`ROW_SIMILARITY_THRESHOLD` 是「**最低**相似度」，判定必须取等号。

    五列里改两格 → 相似度恰好 0.6。用严格大于会把它拒配，同一行的改写就降级成
    「删一行 + 加一行」：线上审计 6549（Loot点位物资表）`{19, M4-折纸房-19旋转金币}`
    被改成 `{9, M4-折纸房-旋转金币}`（列 D、E 两格），平台报的是 added 1 + removed 1，
    页面上同一行既红又绿。
    """

    @staticmethod
    def _frames():
        previous = pd.DataFrame({'id': ['1'], 'a': ['x'], 'b': ['y'], 'c': ['z'], 'd': ['w']})
        current = pd.DataFrame({'id': ['9'], 'a': ['x'], 'b': ['y'], 'c': ['z'], 'd': ['QQ']})
        return previous, current

    def test_row_at_exactly_the_threshold_is_a_modification(self):
        previous, current = self._frames()
        sheet = _sheet({'S': previous}, {'S': current}, 'S')

        statuses = [r['status'] for r in sheet['rows']]
        assert statuses == ['modified'], (
            f"三列相同（3/5 = 0.6，正好等于阈值）却被判成 {statuses} —— "
            "同一行的改写被拆成「删一行 + 加一行」"
        )
        assert_diff_is_faithful(previous, current, sheet, context='相似度正好等于阈值')


class TestAlignmentTracksCumulativeOffset:
    """插入 40 行之后，被改动的那一行对应的前一版行号偏移了 40。

    窗口如果固定钉在 `i` 上（而不是跟着累计位移走），那一片「插入 + 改动」会被
    算成一大片「删除 + 新增」—— 评审者看到的就是几十行红绿，而真实改动只有一行。
    """

    @staticmethod
    def _frames(insert_count):
        previous = pd.DataFrame({
            'id': ['h'] + [str(i) for i in range(1, 61)],
            '名字': ['表头'] + [f'name{i}' for i in range(1, 61)],
            '数值': [''] + ['0'] * 60,
        })
        inserted = pd.DataFrame({'id': [str(900 + i) for i in range(insert_count)],
                                 '名字': [f'新{i}' for i in range(insert_count)],
                                 '数值': ['0'] * insert_count})
        # 在第 20 行之后插入一整块，并改动块后面的一行
        current = pd.concat([previous.iloc[:20], inserted, previous.iloc[20:]], ignore_index=True)
        current.loc[20 + insert_count + 5, '数值'] = '999'
        return previous, current

    def test_change_after_a_large_block_insertion(self):
        previous, current = self._frames(40)
        sheet = _sheet({'S': previous}, {'S': current}, 'S', key_columns='1')
        statuses = sorted(r['status'] for r in sheet['rows'])
        assert statuses.count('added') == 40, f"插入的 40 行没被完整识别: {statuses}"
        assert statuses.count('modified') == 1, (
            f"插入 40 行后改的那一行没被认成「修改」，而是 {statuses} —— "
            "配对窗口没有跟着累计位移走"
        )
        assert_diff_is_faithful(previous, current, sheet, context='大块插入后修改')

    def test_change_after_a_large_block_insertion_without_key_columns(self):
        """不配关键列也要能认出来：靠的是「锚点切段 + 段内对齐」，
        而不是把改动的那一行错配成「删一行 + 加一行」。"""
        previous, current = self._frames(40)
        sheet = _sheet({'S': previous}, {'S': current}, 'S')
        statuses = sorted(r['status'] for r in sheet['rows'])
        assert statuses.count('added') == 40, f"插入的 40 行没被完整识别: {statuses}"
        assert statuses.count('modified') == 1, (
            f"插入 40 行后改的那一行被算成了 {statuses} —— "
            "对齐窗口没跟着位移走（应当是 40 个 added + 1 个 modified）"
        )
        assert_diff_is_faithful(previous, current, sheet, context='无关键列+大块插入后修改')

    def test_change_after_a_large_block_deletion(self):
        previous, current = self._frames(0)
        # 反向：从当前版本里删掉 40 行，并改一行
        trimmed = previous.drop(index=range(20, 60)).reset_index(drop=True)
        trimmed.loc[5, '数值'] = '888'
        sheet = _sheet({'S': previous}, {'S': trimmed}, 'S', key_columns='1')
        statuses = sorted(r['status'] for r in sheet['rows'])
        assert statuses.count('removed') == 40, f"删除的 40 行没被完整识别: {statuses}"
        assert statuses.count('modified') == 1, f"删块之后改的行没被认成修改: {statuses}"
        assert_diff_is_faithful(previous, trimmed, sheet, context='大块删除后修改')


# ---------------------------------------------------------------------------
#  不变量本身要能抓住线上那两类错
# ---------------------------------------------------------------------------

class TestInvariantCatchesTheRealDefects:
    def test_invariant_rejects_a_fabricated_old_value(self):
        """模拟 5988：报一条修改，但改前值取自别的行。"""
        previous = pd.DataFrame({'id': ['1', '2'], '值': ['a', 'b']})
        current = pd.DataFrame({'id': ['1', '2'], '值': ['a', 'B']})
        fabricated = {
            'headers': ['id', '值'],
            'rows': [{'row_number': 2, 'status': 'modified',
                      'data': {'id': '2', '值': 'B'},
                      'cell_changes': [{'column': '值', 'old_value': '第21行的值', 'new_value': 'B'}]}],
        }
        try:
            assert_diff_is_faithful(previous, current, fabricated, context='伪造改前值')
        except AssertionError as exc:
            assert '改前值取自别的行' in str(exc)
        else:
            raise AssertionError("不变量没能抓住伪造的改前值")

    def test_invariant_rejects_a_missing_change(self):
        """模拟 6626：真变更没报出来。"""
        previous = pd.DataFrame({'id': ['1', '2'], '值': ['a', 'b']})
        current = pd.DataFrame({'id': ['1', '2'], '值': ['a', 'B']})
        silent = {'headers': ['id', '值'], 'rows': []}
        try:
            assert_diff_is_faithful(previous, current, silent, context='漏报')
        except AssertionError as exc:
            assert '有变更没报出来' in str(exc)
        else:
            raise AssertionError("不变量没能抓住漏报")
