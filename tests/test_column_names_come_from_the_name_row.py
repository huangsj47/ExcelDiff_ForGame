# -*- coding: utf-8 -*-
"""「名称行」：列名取表头块里的第几行（两种表形态都要支持）。

## 缺陷形态

读表一直是 `pd.read_excel(header=0)` —— **永远把第 1 行当列名**。而配表里很常见的
形态是「第 1 行是大标题（合并单元格）、第 2 行才是字段名」：

    | 道具配置表 |            |            |     ← 第 1 行（合并单元格的大标题）
    | id        | name       | icon       |     ← 第 2 行（真正的字段名）
    | 1         | a          | x          |

修前这张表的列名是 `道具配置表 / Unnamed: 1 / Unnamed: 2`，于是：

* 列头上直接显示 `Unnamed: 1`、`Unnamed: 2`（评审者看不到字段名）；
* **只改第 1 行的标题** → 报成「某列改名 `道具配置表 → 道具配置表v2`」——
  说得像是列身份变了，其实是标题那一个格子改了。

## 现在的口径

仓库上多一个「名称行」（`Repository.header_name_row`，默认 1 = 今天）：

* **列名取名称行那一行的取值**（`_build_column_names`，命名规则与 pandas 的
  `header=0` 逐字一致：空 → `Unnamed: i`、重名 → `X.N`），替换动作发生在
  `_pair_columns` 与 `header_changes` **之前** —— 所以名称行改名照旧报成列改名，
  而不是一串无名的假变更；
* **第 1 行照样看得见**：改名之后它的原文只存在于「改名前的列名」里，所以在表头块里
  按位置补出一行（`_plan_name_row`）。只改标题 → 表头块第 1 行一条 cell_change；
* **名称行本身不进表头块**（它的改动由 `header_changes` 表达，同一件事不报两遍）。

## 反向自检

「改名」这件事必须两头都验：`名称行 = 1` 时载荷与今天**逐字一致**（第 1 行由列改名
表达、表头块从第 2 行起），`名称行 = 2` 时同一份样本要给出**另一套**结论。只验一边的话，
一个「永远走回落分支」的实现也能全绿。
"""
from __future__ import annotations

import io
import os
import re
import sys
from collections import Counter

import openpyxl
import pandas as pd
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.diff_service import DiffService  # noqa: E402
from services.excel_diff_cache_service import ExcelDiffCacheService  # noqa: E402
from services.repository_diff_cache_reset import (  # noqa: E402
    DIFF_SETTING_FIELDS,
    diff_settings_changed,
    parse_header_name_row,
)
from utils.diff_data_utils import (  # noqa: E402
    header_rows_have_changes,
    validate_excel_diff_data,
)

# 「第 1 行是大标题、第 2 行才是字段名」的典型形态：第 1 行只有 A1 有值（其余是
# 合并单元格的空白），第 2 行是字段名。
TITLE = ['道具配置表', None, None]
NAMES = ['id', 'name', 'icon']
DATA = [['1', 'a', 'x'], ['2', 'b', 'y']]

NAME_ROW = 2
HEADER_ROWS = 2


def _xlsx(rows, sheet='S'):
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = sheet
    for row in rows:
        worksheet.append(list(row))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _sheet(previous_rows, current_rows, *, header_rows=HEADER_ROWS,
           header_name_row=NAME_ROW, name='S'):
    payload = DiffService().process_diff(
        'config/x.xlsx', _xlsx(current_rows), _xlsx(previous_rows),
        header_rows=header_rows, header_name_row=header_name_row)
    assert payload.get('type') == 'excel', payload
    return payload['sheets'][name]


def _baseline():
    return [TITLE, NAMES] + DATA


def _block_rows(sheet):
    return {row['row_number']: row for row in sheet.get('header_rows') or []}


def _changes(row):
    return {(c['column'], c.get('old_value'), c.get('new_value'))
            for c in row.get('cell_changes') or []}


# ---------------------------------------------------------------------------
#  配置解析
# ---------------------------------------------------------------------------

class TestTheNameRowNumber:
    service = DiffService()

    @pytest.mark.parametrize('raw', [None, '', 'abc', 0, -1, '1', 1, 1.0])
    def test_unset_and_one_mean_the_first_row(self, raw):
        """没配 / 配 1 = 第 1 行就是字段名行（**今天的行为**）。"""
        assert self.service._header_name_row(raw, 3) == 1

    @pytest.mark.parametrize('raw,expected', [(2, 2), ('2', 2), (3, 3), (3.0, 3), (' 2 ', 2)])
    def test_configured_values_are_used(self, raw, expected):
        assert self.service._header_name_row(raw, 3) == expected

    @pytest.mark.parametrize('raw', [4, 9, '99'])
    def test_a_name_row_beyond_the_header_block_is_clamped(self, raw):
        """名称行只能在表头块里 —— 越界时收到最后一行，**绝不读到数据行上去**。

        读到数据行意味着把某一行数据当成列名：那一行的取值从此只在列头上出现，
        而它作为数据的那份内容再也不会被比到。
        """
        assert self.service._header_name_row(raw, 3) == 3

    def test_one_header_row_means_no_name_row_at_all(self):
        """表头块只有第 1 行时，名称行只能是第 1 行（配 2 也被夹回来）。"""
        assert self.service._header_name_row(2, 1) == 1


class TestColumnNameBuilding:
    """`_build_column_names` 必须与 pandas 的 `header=0` 命名**逐字一致**。

    `_pair_columns`（同名锚点）、`_is_placeholder_column_name`（`Unnamed: 7` 不当锚点、
    不报改名）、`_display_column_name`（`X.1` → 「同名列第 2 个」）三处都建立在这套
    约定上。规则一旦漂了，三处一起失效 —— 而且失效方式是「静默地多报/漏报列变更」。
    """
    service = DiffService()

    @pytest.mark.parametrize('row', [
        ['id', 'name', 'icon'],
        ['道具配置表', None, None],            # 合并单元格：只有 A1 有值
        ['id', None, 'id'],                   # 空列名 + 重名
        ['id', 'id', 'id.1'],                 # 重名撞上**本来就叫 `.1`** 的列名
        ['id', 'id.1', 'id'],                 # 同一个坑的另一侧
        ['id', 'id', 'id'],                   # 三个同名
        ['a', '', '  ', 'b'],                 # 空串（空）/ 空白串（**取值**）
        [1, 2, 3],                            # 数字表头
    ])
    def test_it_matches_what_pandas_would_have_named(self, row):
        """拿**同一行**喂给 pandas 当表头，两边的列名必须一模一样。

        补一行有值的表体：整列全空时 pandas 会把那一列整个丢掉，那是「读表」的
        另一件事，会把这条对照测成假绿。

        `str()` 那一层是读取口径的差别（见 `_build_column_names` 的说明）：名称行
        是按 `dtype=str` 读成文本的，pandas 的表头解析不做这个转换。
        """
        filler = [f'v{i}' for i in range(len(row))]
        frame = pd.read_excel(io.BytesIO(_xlsx([row, filler])),
                              dtype=str, keep_default_na=False)
        assert self.service._build_column_names(row) == [
            str(name) for name in frame.columns]

    def test_blank_cells_get_positional_placeholder_names(self):
        assert self.service._build_column_names(['id', None, None]) == [
            'id', 'Unnamed: 1', 'Unnamed: 2']

    def test_duplicates_get_occurrence_suffixes(self):
        assert self.service._build_column_names(['id', 'id', 'id']) == ['id', 'id.1', 'id.2']

    def test_a_duplicate_skips_a_suffix_that_is_already_taken(self):
        """`['id','id','id.1']` → 第二个 `id` 得叫 `id.2`（`id.1` 已经被第三个占了）。

        这条与「按出现次数直接加后缀」的写法**输出不同**，而那个写法看起来完全合理，
        所以必须单独钉住：`.N` 后缀的编号是 `_display_column_name` 还原「同名列第 N 个」
        的依据，错了就会在列变更提示里写错序号。
        """
        assert self.service._build_column_names(['id', 'id', 'id.1']) == ['id', 'id.2', 'id.1']
        assert self.service._build_column_names(['id', 'id.1', 'id']) == ['id', 'id.1', 'id.2']

    def test_a_numeric_name_row_is_read_as_text(self):
        """名称行按 `dtype=str` 读，所以 `'00123'` 的前导零保住了 —— 与整份读取纪律一致。"""
        assert self.service._build_column_names(['00123', 123]) == ['00123', '123']

    def test_placeholders_and_suffixes_are_the_kinds_the_rest_of_the_engine_knows(self):
        names = self.service._build_column_names(['id', None, 'id'])
        assert self.service._is_placeholder_column_name(names[1])
        assert self.service._display_column_name(names[2], names) == 'id（同名列第 2 个）'


class TestTheFirstRowIsStillVisible:
    """名称行不是第 1 行时，第 1 行**不在任何一帧里**（pandas 把它读成了列名）——
    只能靠改名前的列名补出来。不补的话，「只改标题」会什么也不显示。"""

    def test_the_title_row_shows_up_with_its_own_cells(self):
        sheet = _sheet(_baseline(), _baseline())
        first = _block_rows(sheet)[1]

        assert first['status'] == 'unchanged'
        # 空的那些格子就显示为空 —— 不显示 pandas 起的占位名（那是合成的，不是文件里的）。
        assert first['data'] == {'id': '道具配置表', 'name': '', 'icon': ''}

    def test_changing_only_the_title_is_a_cell_change_on_row_one(self):
        current = [['道具配置表v2', None, None], NAMES] + DATA
        sheet = _sheet(_baseline(), current)

        assert _changes(_block_rows(sheet)[1]) == {
            ('id', '道具配置表', '道具配置表v2')}
        # 而且**不再是**「某列改名」：列身份没有变。
        assert not sheet.get('header_changes')
        assert sheet['rows'] == [], "标题行改了，却被报成了数据行的变更"

    def test_a_blank_title_row_is_not_emitted_at_all(self):
        """第 1 行整行留白时不在表头块里凭空多出一行空格子（与数据行同一判空口径）。"""
        blank = [None, None, None]
        sheet = _sheet([blank, NAMES] + DATA, [blank, NAMES] + DATA)

        assert 'header_rows' not in sheet, (
            '表头块里只剩名称行的时候没有内容可显示，不该发一个空块出来'
        )

    def test_a_deleted_title_is_reported_as_removed(self):
        current = [[None, None, None], NAMES] + DATA
        sheet = _sheet(_baseline(), current)

        assert _block_rows(sheet)[1]['status'] == 'removed'


# ---------------------------------------------------------------------------
#  列名真的换了
# ---------------------------------------------------------------------------

class TestTheColumnsComeFromTheNameRow:
    def test_headers_are_the_name_row_not_the_title(self):
        sheet = _sheet(_baseline(), _baseline())

        assert sheet['headers'] == NAMES, (
            f"列头还在用第 1 行的标题：{sheet['headers']}"
        )
        assert sheet['columns'] == NAMES

    def test_the_name_row_itself_is_not_a_row_in_the_header_block(self):
        """名称行的改动由 `header_changes` 表达 —— 不在表头块里再报一遍。"""
        sheet = _sheet(_baseline(), _baseline())

        assert [row['row_number'] for row in sheet.get('header_rows') or []] == [1]

    def test_renaming_a_field_stays_a_column_rename(self):
        """名称行改名 → `header_changes` 的 renamed（列身份变了，这是列变更）。"""
        current = [TITLE, ['id', 'name2', 'icon']] + DATA
        sheet = _sheet(_baseline(), current)

        assert sheet['header_changes'] == [{
            'change': 'renamed', 'column_index': 2, 'column': 'name2',
            'old_name': 'name', 'new_name': 'name2',
        }]
        # 标题行没变，所以它照旧是 unchanged（不是被改名带出来的假变更）。
        assert _block_rows(sheet)[1]['status'] == 'unchanged'
        assert sheet['rows'] == []

    def test_a_data_change_is_still_a_data_change(self):
        """列名换来源之后，数据行的行号口径一个字都不能变（物理行 = 帧索引 + 2）。"""
        current = [TITLE, NAMES, ['1', 'a', 'x'], ['2', 'B', 'y']]
        sheet = _sheet(_baseline(), current)

        assert [row['row_number'] for row in sheet['rows']] == [4]
        assert sheet['rows'][0]['previous_row_number'] == 4
        assert _changes(sheet['rows'][0]) == {('name', 'b', 'B')}
        # 第 1、2 行都不该出现在数据行里（第 2 行是名称行）。
        assert sheet['stats']['total_rows_current'] == 2

    def test_an_empty_field_name_becomes_a_placeholder_not_a_change(self):
        """名称行里那一格是空的 → 占位名。插一列时占位名会整体错位，
        但那是位置造成的，**不许报成一串列改名**（`_is_placeholder_column_name`）。"""
        prev_names = ['id', None, 'icon']
        cur_names = ['id', None, None, 'icon']
        current = [['道具配置表', None, None, None], cur_names,
                   ['1', 'a', 'q', 'x'], ['2', 'b', 'r', 'y']]
        previous = [TITLE, prev_names, ['1', 'a', 'x'], ['2', 'b', 'y']]
        sheet = _sheet(previous, current)

        for change in sheet.get('header_changes') or []:
            assert not change['column'].startswith('Unnamed:'), (
                f"占位名被报成了列变更：{change}"
            )


# ---------------------------------------------------------------------------
#  两种表形态：对照
# ---------------------------------------------------------------------------

class TestBothTableShapes:
    """同一个「只改标题」的样本，两种配置必须给出**两套不同的结论**。

    只验一边的话，一个「永远走回落分支」的实现也能全绿 —— 那正是修前的老行为。
    """

    def test_the_same_edit_reads_differently_under_the_two_configs(self):
        current = [['道具配置表v2', None, None], NAMES] + DATA

        as_two_row = _sheet(_baseline(), current, header_name_row=2)
        as_one_row = _sheet(_baseline(), current, header_name_row=1)

        assert _changes(_block_rows(as_two_row)[1]) == {('id', '道具配置表', '道具配置表v2')}
        assert not as_two_row.get('header_changes')

        assert [c['change'] for c in as_one_row['header_changes']] == ['renamed']
        # 名称行 = 1 时表头块里只有第 2 行（它是数据行了，与阶段 1 同一形态），
        # **没有**第 1 行的伪行 —— 第 1 行就是列名本身。
        assert [row['row_number'] for row in as_one_row['header_rows']] == [2]
        assert as_two_row['headers'] != as_one_row['headers']

    @pytest.mark.parametrize('name_row', [None, 1, '1', 0, 'abc'])
    def test_the_unconfigured_payload_is_the_old_one(self, name_row):
        """名称行没配（或 1）时，载荷与修前逐字一致：没有表头块，第 1 行就是列名。"""
        current = [['道具配置表v2', None, None], NAMES] + DATA
        sheet = _sheet(_baseline(), current, header_rows=None, header_name_row=name_row)

        assert 'header_rows' not in sheet and 'header_stats' not in sheet
        assert sheet['headers'] == ['道具配置表v2', 'Unnamed: 1', 'Unnamed: 2']

    def test_a_table_without_a_name_row_falls_back_instead_of_crashing(self):
        """表短到根本没有那一行（一行表、或空表）→ 两边都不改名，退回今天的行为。

        只给一边改名是最坏的做法：配对会拿新名字去比对旧名字，整张表每一行都会
        多出两处假变更。
        """
        one_row = [['a', 'b', 'c']]
        sheet = _sheet(one_row, one_row, header_rows=5, header_name_row=4)

        assert sheet['headers'] == ['a', 'b', 'c']
        assert sheet['rows'] == []

    def test_both_sides_are_renamed_together(self):
        """一边有名称行、另一边没有（例如上一版是空表）→ **两边都不改名**。

        这条是「一起改」的守卫：只改当前那一版的话，`_pair_columns` 会把每一列都
        判成「删除 + 新增」，于是每一行都报两处假变更。
        """
        empty_previous = [[None, None, None]]
        sheet = _sheet(empty_previous, _baseline(), header_name_row=2)

        # 上一版那一行全是空白、被 `_has_valid_data` 滤掉 ⇒ 帧里没有第 2 行？
        # 不：过滤发生在建块之后，帧本身有 1 行 —— 名称行取不到，两边都不改名。
        assert sheet['headers'] == ['道具配置表', 'Unnamed: 1', 'Unnamed: 2']

    def test_comparing_the_same_frames_twice_gives_the_same_answer(self):
        """**反向自检：帧不能被就地改名。**

        改名如果写成 `df.columns = …`，第二次拿同一份帧来比较时，「改名前的列名」
        已经是字段名了 —— 于是第 1 行（那行大标题）会显示成字段名，而名称行又会再被
        当成第 1 行。真实链路上每份帧只比一次，但「读一次比两次」是这个引擎里
        很自然的重构（缓存、逐段比较都会这么做），所以这里把它钉死。
        """
        service = DiffService()
        current = service._read_excel_data(_xlsx(_baseline()), 'config/x.xlsx')
        previous = service._read_excel_data(_xlsx(_baseline()), 'config/x.xlsx')

        first = service._compare_excel_data(current, previous, 'config/x.xlsx',
                                            header_rows=HEADER_ROWS,
                                            header_name_row=NAME_ROW)['sheets']['S']
        second = service._compare_excel_data(current, previous, 'config/x.xlsx',
                                             header_rows=HEADER_ROWS,
                                             header_name_row=NAME_ROW)['sheets']['S']

        assert first == second
        assert _block_rows(second)[1]['data'] == {
            'id': '道具配置表', 'name': '', 'icon': ''}  # 第 1 行的原文，不是字段名


# ---------------------------------------------------------------------------
#  整表增删：列名同样取名称行
# ---------------------------------------------------------------------------

class TestWholeSheetBranches:
    ROWS = [TITLE, NAMES] + DATA

    def test_an_added_sheet_uses_the_name_row_for_its_headers(self):
        payload = DiffService().process_diff(
            'config/x.xlsx', _xlsx(self.ROWS), None,
            header_rows=HEADER_ROWS, header_name_row=NAME_ROW)
        sheet = payload['sheets']['S']

        assert sheet['operation'] == 'added'
        assert sheet['headers'] == NAMES, '整表新增时列头又退回第 1 行的标题了'
        assert [row['row_number'] for row in sheet['header_rows']] == [1]
        assert [row['row_number'] for row in sheet['rows']] == [3, 4]

    def test_a_deleted_file_uses_the_name_row_for_its_headers(self):
        result = DiffService().process_deleted_file(
            'config/x.xlsx', _xlsx(self.ROWS),
            header_rows=HEADER_ROWS, header_name_row=NAME_ROW)
        sheet = result['sheets']['S']

        assert sheet['operation'] == 'deleted'
        assert sheet['headers'] == NAMES
        assert _block_rows(sheet)[1]['status'] == 'removed'
        assert _block_rows(sheet)[1]['data']['id'] == '道具配置表'


# ---------------------------------------------------------------------------
#  载荷完整性：新形态照样过下游
# ---------------------------------------------------------------------------

class TestThePayloadSurvivesThePipeline:
    def test_a_title_only_change_is_content_even_without_data_rows(self):
        """「只改标题」必须被认成**有内容**，否则界面会显示「没有变更」。"""
        current = [['道具配置表v2', None, None], NAMES] + DATA
        payload = DiffService().process_diff(
            'config/x.xlsx', _xlsx(current), _xlsx(_baseline()),
            header_rows=HEADER_ROWS, header_name_row=NAME_ROW)
        sheet = payload['sheets']['S']

        assert header_rows_have_changes(sheet) is True
        valid, message = validate_excel_diff_data(payload)
        assert valid, message

    def test_the_header_block_survives_the_cache_optimizer(self):
        current = [['道具配置表v2', None, None], NAMES] + DATA
        payload = DiffService().process_diff(
            'config/x.xlsx', _xlsx(current), _xlsx(_baseline()),
            header_rows=HEADER_ROWS, header_name_row=NAME_ROW)
        optimized = ExcelDiffCacheService().optimize_diff_data(payload)
        sheet = optimized['sheets']['S']

        assert [row['row_number'] for row in sheet['header_rows']] == [1]
        assert sheet['header_rows'][0]['status'] == 'modified', (
            '第 1 行的 modified 被缓存的优化步骤吃掉了'
        )

    def test_the_status_vocabulary_is_unchanged(self):
        """表头块只用既有四种状态 —— 新状态会被 `optimize_diff_data` 的白名单静默删掉。"""
        current = [['道具配置表v2', None, None], ['id', 'name2', 'icon'], ['1', 'a', 'x'],
                   ['3', 'c', 'z']]
        sheet = _sheet(_baseline(), current)

        statuses = Counter(row['status'] for row in sheet['header_rows'])
        assert set(statuses) <= {'added', 'removed', 'modified', 'unchanged'}
        assert statuses == Counter({'modified': 1})


# ---------------------------------------------------------------------------
#  配置：表单 → 库 → 引擎
# ---------------------------------------------------------------------------

REPO_ROOT = PROJECT_ROOT


def _read(rel: str) -> str:
    with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as handle:
        return handle.read()


def _strip_comments(source: str) -> str:
    """静态断言前先剥注释。

    注释里会**原样引用**「要禁掉的写法」（本仓库的注释习惯），不剥就会假失败；
    反过来也会假通过（注释里写过正确的顺序，坏代码排在后面也算通过）。
    """
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    source = re.sub(r"(?m)^\s*//.*$", "", source)
    source = re.sub(r"<!--.*?-->", "", source, flags=re.S)
    source = re.sub(r"\{#.*?#\}", "", source, flags=re.S)
    return re.sub(r"(?m)#.*$", "", source)


class TestTheNameRowNumberParsing:
    """表单口径：空 / 1 = 不配置；`2..表头行数` 才接受；其余给一句能照着改的话。"""

    @pytest.mark.parametrize('submitted', [
        {}, {'header_name_row': ''}, {'header_name_row': '   '}, {'header_name_row': '1'},
        {'header_name_row': 1},
    ])
    def test_blank_and_one_mean_unconfigured(self, submitted):
        assert parse_header_name_row(submitted, '3') == (None, '')

    @pytest.mark.parametrize('raw,expected', [('2', 2), (' 3 ', 3), (2, 2)])
    def test_a_valid_value_is_stored_as_an_integer(self, raw, expected):
        assert parse_header_name_row({'header_name_row': raw}, '3') == (expected, '')

    @pytest.mark.parametrize('raw', ['abc', '2.5', '一'])
    def test_a_non_number_is_refused(self, raw):
        value, error = parse_header_name_row({'header_name_row': raw}, '3')
        assert value is None and '不是数字' in error, error

    @pytest.mark.parametrize('raw,count', [('3', '2'), ('9', '3')])
    def test_a_value_beyond_the_header_block_is_refused(self, raw, count):
        """**不许静默夹到别的行上** —— 那等于替用户猜他想要哪一行。

        名称行越界意味着它落在了数据行上：那一行的取值从此只在列头出现、再也不会被
        比到（静默漏审），而它下面的数据行又整体错位一格。
        """
        value, error = parse_header_name_row({'header_name_row': raw}, count)
        assert value is None
        assert f'不能超过表头行数（{count}）' in error, error

    def test_no_header_rows_configured_means_only_the_first_row(self):
        """没配表头行数（非 table 类型 / 老数据）→ 名称行只能是第 1 行。"""
        assert parse_header_name_row({'header_name_row': '2'}, '') == (
            None, '名称行（2）不能超过表头行数（1）')

    def test_a_non_mapping_submission_is_not_a_crash(self):
        assert parse_header_name_row(None, '3') == (None, '')


class TestTheFormCarriesTheField:
    def test_both_create_paths_store_it(self):
        source = _strip_comments(_read("services/repository_creation_handlers.py"))
        assert source.count("header_name_row=header_name_row,") == 2, (
            "git / svn 两个创建入口没有都把名称行写进 Repository"
        )
        assert source.count("parse_header_name_row(request.form, header_rows)") == 2

    def test_the_value_is_validated_before_it_is_written(self):
        """校验必须排在赋值**之前**：写在后面的话，越界的值已经进了 ORM 对象，
        校验失败只是不提交 —— 而这段代码后面还有别的分支会 commit。"""
        source = _strip_comments(_read("services/repository_update_form_service.py"))
        guard = source.index("parse_header_name_row(request.form, header_rows)")
        write = source.index("repository.header_name_row = header_name_row")
        assert guard < write, "名称行没有先校验再写"
        assert "flash(name_row_error, \"error\")" in source

    def test_changing_it_is_a_diff_setting(self):
        """改了名称行必须清该仓库的 diff 缓存 —— 否则用户改完看到的还是旧结果，
        会以为这个配置没用（`表头行数` / `关键列` 同样的问题，见模块 docstring）。"""
        assert "header_name_row" in DIFF_SETTING_FIELDS

    def test_a_changed_name_row_is_reported_as_changed(self):
        from types import SimpleNamespace

        repository = SimpleNamespace(header_rows=2, header_name_row=None, key_columns=None)
        assert diff_settings_changed(repository, {"header_name_row": "2"}) == ["header_name_row"]

    def test_a_partial_form_without_the_field_is_not_a_change(self):
        """局部表单（只提交一部分字段）不该被误判成「改成了空」而白清一遍缓存。"""
        from types import SimpleNamespace

        repository = SimpleNamespace(header_rows=2, header_name_row=2, key_columns="1")
        assert diff_settings_changed(repository, {"key_columns": "1"}) == []
        assert diff_settings_changed(repository, {}) == []

    @pytest.mark.parametrize('template', [
        "templates/add_git_repository.html", "templates/add_svn_repository.html",
    ])
    def test_the_field_is_on_the_form(self, template):
        source = _read(template)
        assert 'name="header_name_row"' in source, f"{template} 上没有名称行这一项"
        assert 'id="header_name_row"' in source
        # 非 table 类型时也要清掉（与关键列同一处），否则会把上一个类型填的值一起提交。
        script = _strip_comments(source)
        assert "document.getElementById('header_name_row').value = '';" in script

    def test_the_help_page_explains_it(self):
        """帮助页要写清**真实语义**：列名取第几行、第 1 行不会被藏起来。

        （历史上「表头行数」那一节写着「系统根据此值区分表头和数据行」，而引擎从初始
        提交起就没读过这个配置 —— 帮助页写着做不到的事比不写更糟。）
        """
        source = _read("templates/help.html")
        marker = source.index("名称行")
        chunk = source[marker: marker + 900]
        assert "列名" in chunk and "第 2 行" in chunk
        assert "Unnamed" in chunk, "没写清不配它的时候列头会显示成占位名"
        assert "不会被藏起来" in chunk, "没写明第 1 行仍然看得见（用户最担心的就是这条）"

    def test_the_migration_adds_the_column_to_existing_databases(self):
        """`db.create_all()` 只建新表 —— 老库必须走 ALTER TABLE 才会多出这一列。

        顺带钉住 `header_rows` / `key_columns`：这两项也一直不在迁移清单里，
        老库上读它们会直接抛 `no such column`。
        """
        source = _strip_comments(_read("services/db_migration_service.py"))
        marker = source.index("def _migrate_repository_columns(")
        chunk = source[marker: source.index("def _migrate_commits_log_columns(")]
        for column in ("header_rows", "header_name_row", "key_columns"):
            assert f'"{column}"' in chunk, f"repository 表的 {column} 没有迁移项"

    def test_the_engine_entry_points_pass_it_through(self):
        """三个调用点都要传：提交页主漏斗、提交页删除态、周版本删除态。

        漏传一处的表现是「同一张表在两种页面上列头不一样」，而且不报错。
        """
        vcs = _strip_comments(_read("services/vcs_content_service.py"))
        weekly = _strip_comments(_read("services/weekly_deleted_excel_helpers.py"))
        assert vcs.count('header_name_row=getattr(repository, "header_name_row", None)') == 2 \
            or vcs.count("header_name_row=getattr(repository, 'header_name_row', None)") == 2, (
                "提交页的两个入口没有都传名称行"
            )
        assert 'header_name_row=getattr(repository, "header_name_row", None)' in weekly
