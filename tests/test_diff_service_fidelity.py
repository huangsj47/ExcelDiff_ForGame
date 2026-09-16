# -*- coding: utf-8 -*-
"""`DiffService` 的两处**漏报**回归测试（services/diff_service.py）

本平台是**变更确认平台**，最坏的失败方式是「有变更 → 显示成没有变更」：
审核者看不到，也就不会去核对，直接漏审。本文件锁死两条真实的漏报路径。

## 一、行级黑名单过滤把「字面量形态的行」整行吞掉（`_has_valid_data` :601）

`_smart_row_diff` 在比较前先用 `_has_valid_data` 过滤「空行」:

    for col in columns:
        val = row_data.get(col, '')
        if val is not None and not pd.isna(val):
            if str(val).strip().lower() not in ['', 'nan', 'none', 'null', '<na>']:
                return True
    return False

这与本文件同级的 `_normalize_value`（:841，DIFF_LOGIC_VERSION 1.9.0 已修）**口径相反**：
`_normalize_value` 只把真正的空（None / NaN / ''）当空，文本 `null` / `None` / 空白串都算取值。
于是同一行数据出现两种判定：

    读取阶段 → 'null' 原样保留（dtype=str, keep_default_na=False）
    比较阶段 → _values_equal('null', 'None') = False（报变更）   ← 单格变更走这里，没问题
    过滤阶段 → _has_valid_data 把 'null' 当空 → **整行被丢掉**   ← 本文件要消灭的漏报

后果分三种，都被实测复现（见各类用例的 docstring）：

1. 整行取值都落在黑名单里（如一行全是 `null`）时，行内**任何**改动都不报：
   `null → None`、`1 个空格 → 2 个空格` 的总变更数是 0；
2. 这种行只在**一个版本**里存在时（新增/删除整行）完全不显示；
3. 修前 2 与 3 都表现为「整份文件零变更」—— 正是会触发「未检测出变更」提示的形态。

## 二、`.tsv` 声明支持但读取必然失败（`_read_excel_data` :344）

`CSV_EXTENSIONS = {'.csv', '.tsv'}`（:25）、`get_file_type` 把 `.tsv` 判为 `'excel'`
（:48，`tests/test_business_chain_integration.py:90` 也锁死了这个契约），
但读取分支只写了 `if ext == '.csv'`：

    if ext == '.csv':  ... 文本解析 ...
    else:              pd.ExcelFile(io.BytesIO(content))   ← .tsv 落这里

`.tsv` 不是 Excel 容器 → `ValueError: Excel file format cannot be determined` →
`process_diff` 返回 `{'type': 'excel', 'error': ...}`，**一个单元格都读不出来**。
`.tsv` 在游戏配表里是常规导出格式，所以这不是理论问题。

## 断言形状

* 漏报类：至少断言「变更被显示出来」（`modified + added + removed > 0`）。
  归类成 modified 还是 add/remove 由行配对阈值决定（`_calculate_row_similarity`，
  阈值见 :658），不值得在测试里钉死；**「不被静默吞掉」才是要锁的不变量**。
* 反向保险：真正的全空行仍必须被过滤（既有语义），不得为了保真凭空造变更。
* 端到端：读取 → 比较 → summary，不 import app、不碰数据库。
"""
import io
import os
import sys

import numpy as np
import pandas as pd
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.diff_service import DiffService  # noqa: E402

WIDE = ['id', 'code', 'name', 'type', 'value', 'desc']  # 6 列，贴近真实配表宽度


@pytest.fixture()
def svc():
    return DiffService()


def _xlsx(rows, header):
    """把 rows 写成 xlsx 字节流（Python str 会作为**文本单元格**写入）。"""
    buf = io.BytesIO()
    pd.DataFrame(rows, columns=header).to_excel(buf, index=False)
    return buf.getvalue()


def _summary(svc, before, after, path='a.xlsx'):
    """比较两个字节流，返回 summary。参数顺序：旧 -> 新。"""
    result = svc._compare_excel_data(
        svc._read_excel_data(after, path),
        svc._read_excel_data(before, path),
        path)
    return result['summary']


def _detected(stats):
    """变更被显示出来了（无论归类成 modified 还是 add/remove 对）。"""
    return stats['modified'] + stats['added'] + stats['removed']


def _big_table(n=120):
    """120 行普通数据：把表推到历史上「大表分支」的行数区间（>100 行）。

    那条分支已经删掉，行匹配只剩一条路径；这里仍然用 120 行，是为了挡住
    「行数一变结论就变」的回归。
    """
    return [[str(i), 'c%d' % i, 'A', 't', 'v', 'd'] for i in range(n)]


# ---------------------------------------------------------------------------
# 一、行过滤不得吞掉「字面量形态」的行
# ---------------------------------------------------------------------------
class TestRowFilterKeepsLiterals:
    """`_has_valid_data` / `_filter_nan_rows` 的口径必须与 `_normalize_value` 一致。"""

    @pytest.mark.parametrize('text', ['null', 'None', 'NULL', 'nan', '<NA>', ' '])
    def test_literal_row_is_valid_data(self, svc, text):
        """整行都是字面量取值时，这一行是**有数据**的行，不是空行。

        修前：`str(val).strip().lower()` 命中 `['', 'nan', 'none', 'null', '<na>']`
        → 返回 False → 整行在比较前被丢掉 → 行内任何改动都不报。
        """
        row = {'a': text, 'b': text}
        assert svc._has_valid_data(row, ['a', 'b']) is True, (
            f'整行都是 {text!r} 的行被 _has_valid_data 判成空行。\n'
            f'后果：这一行的所有改动（含 {text!r} → 另一个取值）都不会出现在 diff 里。'
        )
        assert svc._filter_nan_rows([row], ['a', 'b']) == [row], (
            f'_filter_nan_rows 把整行 {text!r} 丢掉了 —— '
            f'它与 _has_valid_data 是同一处黑名单的两个副本，必须一起改。'
        )

    @pytest.mark.parametrize('row', [
        {'a': '', 'b': ''},
        {'a': None, 'b': None},
        {'a': np.nan, 'b': np.nan},
        {'a': pd.NA, 'b': pd.NaT},
    ])
    def test_truly_blank_row_is_filtered(self, svc, row):
        """反向保险：真正的全空行仍必须被过滤（这是原有语义，不能被放宽）。

        pandas 会保留**中间**的全空行（`read_excel` 只丢尾部空行，实测：
        `[['1','x'],['',''],['3','z']]` 读出来 3 行），所以这层过滤仍然有用。
        """
        assert svc._has_valid_data(row, ['a', 'b']) is False, (
            f'{row!r} 是真正的空行，必须仍被判空 —— 否则 diff 里会凭空多出一堆空行'
        )
        assert svc._filter_nan_rows([row], ['a', 'b']) == []

    def test_row_with_one_real_cell_is_valid(self, svc):
        """一行里只要有一个真取值就是有效行（原有行为，保持不变）。"""
        assert svc._has_valid_data({'a': '', 'b': 'x'}, ['a', 'b']) is True


class TestLiteralRowChangesAreReported:
    """端到端：整行字面量形态的改动必须显示出来。"""

    def test_null_literal_becoming_none_is_reported(self, svc):
        """一行的取值全是 `null`，其中一列改成 `None` —— 必须报变更。

        修前：两个版本里这 6 个格子全都命中黑名单 → 两侧整行被丢 →
        `summary = {'added': 0, 'removed': 0, 'modified': 0}`，即「整份文件没有变更」。
        """
        before = _xlsx([['null'] * 6], WIDE)
        after = _xlsx([['null', 'null', 'null', 'null', 'None', 'null']], WIDE)
        stats = _summary(svc, before, after)
        assert _detected(stats) > 0, (
            f"'null' → 'None' 的行报出了 0 变更：{stats}\n"
            f'审核者会看到「没有变更」，而表里确实改了。'
        )
        assert stats['modified'] == 1, (
            f'6 列里改 1 列，相似度 5/6 ≈ 0.83 > 0.6（:658），应归类成 modified：{stats}'
        )

    def test_whitespace_only_row_change_is_reported(self, svc):
        """整行都是空格时，`1 个空格 → 2 个空格` 也是真实变更。

        修前：`' '.strip()` 为空串 → 命中黑名单 → 整行被丢 → 0 变更。
        """
        before = _xlsx([[' ', ' ']], ['a', 'b'])
        after = _xlsx([['  ', '  ']], ['a', 'b'])
        stats = _summary(svc, before, after)
        assert _detected(stats) > 0, (
            f"' ' → '  ' 报出了 0 变更：{stats}\n"
            f'配表里全空格的占位行被改宽/改窄同样是真实改动。'
        )

    def test_literal_cleared_to_blank_is_reported(self, svc):
        """整行都是 `null` 时，把其中一列清成真空单元格也必须报变更。

        这是 `_read_excel_data` 顶部注释（:341）明确承诺的场景 ——
        「真正的空单元格读数仍是空值，所以『清空单元格』这类变更不会被吃掉」。
        读取阶段确实如此，但行过滤阶段把这行整个丢了，承诺在整行字面量时失效。
        """
        before = _xlsx([['null'] * 6], WIDE)
        after = _xlsx([['null', 'null', 'null', 'null', 'null', '']], WIDE)
        stats = _summary(svc, before, after)
        assert _detected(stats) > 0, (
            f"整行 null 时「清空单元格」报出了 0 变更：{stats}\n"
            f'审核者看不到这一格被清空了。'
        )

    def test_added_literal_row_is_reported(self, svc):
        """新增一整行 `null`（占位行）必须显示为 added。

        修前：这一行在过滤阶段被丢掉，新增行在整个界面里不存在。
        """
        before = _xlsx([['1', 'a', 'b', 't', 'v', 'd']], WIDE)
        after = _xlsx([['1', 'a', 'b', 't', 'v', 'd'], ['null'] * 6], WIDE)
        stats = _summary(svc, before, after)
        assert stats['added'] == 1, (
            f'新增一行 null 占位行没有被报成 added：{stats}\n'
            f'审核者不知道表里多了一行。'
        )
        assert stats['removed'] == 0, stats

    def test_literal_row_change_is_reported_in_large_table(self, svc):
        """大表（>100 行）同样不得吞掉整行字面量的变更。

        行匹配现在只有一条路径（`_find_row_matches` → 认没变的行 → 锚点切段 →
        段内对齐），历史上那条「超 100 行改走哈希桶 + 位置匹配、阈值降到 0.5」的
        大表分支已经删掉。这条测试留着，是为了挡住「行数一变结论就变」的回归
        （实测修前同样是 0 变更）。
        """
        before = _xlsx(_big_table() + [['null'] * 6], WIDE)
        after = _xlsx(_big_table() + [['null', 'null', 'null', 'null', 'None', 'null']], WIDE)
        stats = _summary(svc, before, after)
        assert _detected(stats) > 0, (
            f'120 行的表里，整行 null 的一行改了一格却报出 0 变更：{stats}'
        )

    def test_identical_literal_rows_report_nothing(self, svc):
        """反向保险：两侧完全一样的字面量行不得报变更（不得为了保真造假变更）。"""
        rows = [['1', 'a', 'b', 't', 'null', 'None'], ['null'] * 6]
        content = _xlsx(rows, WIDE)
        stats = _summary(svc, content, content)
        assert stats['modified'] == 0 and stats['added'] == 0 and stats['removed'] == 0, (
            f'同一份文件与自己比较却报出变更：{stats}'
        )

    def test_blank_row_present_in_both_versions_reports_nothing(self, svc):
        """反向保险：两版都有的**中间全空行**仍按既有语义被过滤，不报 add/remove。"""
        rows = [['1', 'a', 'b', 't', 'v', 'd'], [''] * 6, ['3', 'c', 'd', 't', 'w', 'e']]
        content = _xlsx(rows, WIDE)
        stats = _summary(svc, content, content)
        assert _detected(stats) == 0, f'中间空行被当成变更报了出来：{stats}'


# ---------------------------------------------------------------------------
# 二、`.tsv` 必须真的能被读出来
# ---------------------------------------------------------------------------
TSV_BEFORE = 'id\tcode\tname\ttype\tvalue\tdesc\n1\ta\tA\tt\tv\td\n'.encode('utf-8')
TSV_AFTER = 'id\tcode\tname\ttype\tvalue\tdesc\n1\ta\tA\tt\tv2\td\n'.encode('utf-8')


class TestTsvIsReadable:
    """`CSV_EXTENSIONS` 声明支持 `.tsv`，读取阶段就必须按制表符解析。"""

    @pytest.mark.parametrize('path', ['config/a.tsv', 'config/A.TSV', 'config/a.Tsv'])
    def test_tsv_file_type_is_tabular(self, svc, path):
        """契约：`.tsv`（任意大小写）被当作表格文本处理，而不是二进制。"""
        assert svc.get_file_type(path) == 'excel', (
            f'{path} 的文件类型不是 excel —— 声明支持与实现必须对齐'
        )

    def test_tsv_reads_with_tab_separator(self, svc):
        """`.tsv` 走文本解析器并显式用 `\\t` 作分隔符（修前落进 pd.ExcelFile 报错）。

        修前实测：`ValueError: Excel file format cannot be determined,
        you must specify an engine manually.`
        """
        df = svc._read_excel_data(TSV_AFTER, 'config/a.tsv')['Sheet1']
        assert list(df.columns) == ['id', 'code', 'name', 'type', 'value', 'desc'], (
            f'列名不对（说明没有按 \\t 切分）：{list(df.columns)}'
        )
        assert df['value'].iloc[0] == 'v2', df.to_dict('records')

    def test_tsv_process_diff_reports_change(self, svc):
        """端到端：`.tsv` 的单元格改动必须被检出，而不是返回 error。"""
        result = svc.process_diff('config/a.tsv', TSV_AFTER, TSV_BEFORE)
        assert 'error' not in result, (
            f"`.tsv` 走 process_diff 返回了错误而不是 diff：{result.get('error') or result.get('message')}"
        )
        assert result['type'] == 'excel', result.get('type')
        summary = result['summary']
        assert _detected(summary) > 0, f'`.tsv` 里的改动没有被检出：{summary}'

    def test_tsv_uppercase_extension_is_read(self, svc):
        """大小写不敏感：`CONFIG/A.TSV` 也必须能读（`get_file_type` 已 lower()）。"""
        result = svc.process_diff('CONFIG/A.TSV', TSV_AFTER, TSV_BEFORE)
        assert 'error' not in result, f'大写扩展名的 .tsv 读取失败：{result.get("error")}'
        assert result['summary']['total'] > 0

    def test_tsv_keeps_literals(self, svc):
        """口径一致：`.tsv` 同样走 dtype=str + keep_default_na=False，字面量不得被改写。"""
        content = 'code\tvalue\n00123\tNULL\n'.encode('utf-8')
        df = svc._read_excel_data(content, 'config/a.tsv')['Sheet1']
        assert str(df['code'].iloc[0]) == '00123', f"前导零丢失：{df['code'].iloc[0]!r}"
        assert not pd.isna(df['value'].iloc[0]), f'NULL 变成 NaN：{df["value"].iloc[0]!r}'

    def test_tsv_keeps_tab_inside_field_only_when_quoted(self, svc):
        """制表符切分与引号规则由同一个解析器负责，字段内的逗号不受影响。"""
        content = 'id\tname\n1\ta,b\n'.encode('utf-8')
        df = svc._read_excel_data(content, 'config/a.tsv')['Sheet1']
        assert str(df['name'].iloc[0]) == 'a,b', df.to_dict('records')

    def test_csv_is_not_split_on_tab(self, svc):
        """反向保险：CSV 仍按 `,` 切分，字段里的制表符是内容而不是分隔符。"""
        content = 'id,name\n1,"a\tb"\n'.encode('utf-8')
        df = svc._read_excel_data(content, 'config/a.csv')['Sheet1']
        assert list(df.columns) == ['id', 'name'], list(df.columns)
        assert df['name'].iloc[0] == 'a\tb', (
            f'CSV 被按制表符切分了：{df.to_dict("records")}'
        )

    def test_csv_diff_still_works(self, svc):
        """反向保险：CSV 链路不得因为这次改动而回归。"""
        before = b'id,name\n1,Alice\n'
        after = b'id,name\n1,Bob\n'
        result = svc.process_diff('config/a.csv', after, before)
        assert 'error' not in result, result.get('error')
        assert result['summary']['total'] > 0, result['summary']


# ---------------------------------------------------------------------------
# 三、修复必须能让已缓存的旧结果失效（否则修复在界面上看不见）
# ---------------------------------------------------------------------------
#
# 这两条修复都**只放宽了被报出来的变更**：旧的 Excel diff 缓存（含被行过滤吞掉的
# 那些行）仍会按旧版本号命中，审核者看到的还是「没有变更」—— 修复等于没做。
# 仓库的既定做法就是升 `DIFF_LOGIC_VERSION`（注释见 app.py:310 附近）。
#
# 这个版本号有**三份字面量**：app.py（驱动缓存失效）、config.py（界面展示）、
# services/excel_diff_cache_service.py（未 configure 时的兜底，`cleanup_old_cache()`
# 用它判定「版本不匹配 → 当过期缓存删掉」）。只升前两份会让第三份比真实版本旧，
# 于是**刚生成的当前版本缓存**被当成过期数据清掉 —— 没有任何运行时信号。
# 既有测试 tests/test_diff_logic_version_single_source.py 只覆盖前两份，
# 第三份文件里却写着「该测试会扫描全部三处」，所以这层一致性在此补上。
DIFF_LOGIC_VERSION_FILES = [
    'app.py',
    'config.py',
    os.path.join('services', 'excel_diff_cache_service.py'),
]


def _read_version_literal(filename):
    """从源码里取出 `DIFF_LOGIC_VERSION = "..."` 的字面量（不 import，避免副作用）。"""
    import re

    with open(os.path.join(PROJECT_ROOT, filename), encoding='utf-8') as fh:
        for line in fh:
            match = re.match(r'^DIFF_LOGIC_VERSION\s*=\s*["\']([^"\']+)["\']', line)
            if match:
                return match.group(1)
    raise AssertionError(f'{filename} 里找不到 DIFF_LOGIC_VERSION 字面量')


class TestDiffLogicVersionSingleSource:
    """三份字面量必须一致，且必须已越过本次口径变更。"""

    def test_all_three_literals_are_identical(self):
        versions = {name: _read_version_literal(name) for name in DIFF_LOGIC_VERSION_FILES}
        assert len(set(versions.values())) == 1, (
            f'DIFF_LOGIC_VERSION 三份字面量不一致：{versions}。\n'
            f'只升一半的两个后果都是静默的：'
            f'（a）缓存没失效 → 本次漏报修复在界面上看不见；'
            f'（b）excel_diff_cache_service.py 那份旧了 → cleanup_old_cache() '
            f'把刚生成的当前版本缓存当过期数据清掉。'
        )

    @pytest.mark.parametrize('filename', ['app.py', 'config.py'])
    def test_version_was_bumped_for_the_fidelity_fix(self, filename):
        """口径变更必须伴随版本号提升，否则旧缓存继续命中、修复不可见。

        `1.9.0` 是「读取阶段 + `_normalize_value` 保真」那一版；本次改的是
        行过滤与 .tsv 读取，属于**又一次**口径变更，必须再升一版。
        """
        version = _read_version_literal(filename)
        parts = tuple(int(p) for p in version.split('.'))
        assert parts > (1, 9, 0), (
            f'{filename} 的 DIFF_LOGIC_VERSION={version!r} 没有越过 1.9.0。\n'
            f'后果：已缓存的 Excel diff（按旧版本号存）继续命中，'
            f'被行过滤吞掉的行这次仍然不会出现在界面上 —— 修复等于没做。'
        )
