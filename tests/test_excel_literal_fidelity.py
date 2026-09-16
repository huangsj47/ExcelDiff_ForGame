# -*- coding: utf-8 -*-
"""Excel 单元格「字面量保真」回归测试（services/diff_service.py）

## 为什么需要这个文件

本平台是**变更确认平台**。它最坏的失败方式不是报错，而是
**把「有变更」显示成「没变更」** —— 审核者看不到、也就不会去核对，直接漏审。

而当前实现有三处会静默改写/吞掉字面量：

### 一、读取阶段：pandas 用默认参数，字面量在比较之前就被改掉了
`_read_excel_data`（`services/diff_service.py:317`）用的是全默认参数：

    pd.read_csv(io.StringIO(text_content))                      # :330
    pd.read_excel(excel_file, sheet_name=sheet_name)            # :340

实测 pandas 默认行为（keep_default_na=True + 类型推断）：

    '00123' -> int 123        前导零丢失
    '1.10'  -> float 1.1      尾零丢失
    '1e3'   -> float 1000.0   字面量被改写
    'TRUE'  -> bool True      大小写丢失
    'NULL' / 'null' / 'nan' / 'N/A' / 'None' / 'NA' / '<NA>' -> NaN
                              **真实取值被当成空值**

后果：审核者把 `00123` 改成 `123`（或 `NULL` 改成空）时，两侧读出来是同一个值，
比较相等 → 平台显示「没有变更」。注意提交页的「未检测出变更」提示只在
**整份文件零变更**时出现；若文件里另有其它真实变更，这条被吞掉的变更
完全不会被提示。

### 二、比较阶段：`_normalize_value` 的字符串黑名单 + strip
`services/diff_service.py:819`：

    val_str = str(val).strip().lower()
    if val_str in ('', 'nan', 'none', 'null', '<na>'):
        return None                       # 文本 'null' 与「空」被判等价
    return str(val).strip()               # 首尾空格被吞掉

而 `_values_equal`（:864）正是 `_normalize_value(a) == _normalize_value(b)`，
`_calculate_row_similarity` / `_rows_equal` 也共用它。于是：
  - 文本 `null` → 空单元格：判「相等」→ 不报变更；
  - 文本 `null` → 文本 `None`：判「相等」→ 不报变更；
  - `'  x  '` → `'x'`：判「相等」→ 不报变更。

配表里用 `null` / `None` / `None` 这类字面量表示「无掉落 / 无引用」极常见，
所以这不是理论问题。

### 三、`_normalize_value` 被 `@staticmethod` 装饰，且是热路径共用函数
改它的口径会同时影响相似度与行配对，因此**必须跑全量测试**确认没有把
行匹配改坏（`_calculate_row_similarity` 的阈值、`_rows_matchable` 的判定）。

## 本文件锁死的行为

* 读取阶段：字面量不得被改写（含前导零、尾零、大小写、NA 形式的文本）。
* 比较阶段：文本 `null`/`none`/`nan`/`<na>` 不得与「空」等价；不得 trim。
* 端到端：上述变更必须被报成 modified（这是本文件存在的理由）。
* 反向保险：真正的空值仍必须算空；完全相同的文件不得报出变更。

不 import app、不碰数据库。
"""
import io
import os
import sys

import pandas as pd
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.diff_service import DiffService  # noqa: E402

# 用户可能在配表里真实写入的「看起来像空值」的文本字面量。
# 它们**不是**空值 —— 与 None / NaN / '' 是两种不同的内容。
NA_LIKE_LITERALS = ['NULL', 'null', 'None', 'none', 'nan', 'NaN', 'N/A', 'NA', '<NA>']


@pytest.fixture()
def svc():
    return DiffService()


def _xlsx(rows, header):
    """把 rows 写成 xlsx 字节流（Python str 会作为**文本单元格**写入）。"""
    buf = io.BytesIO()
    pd.DataFrame(rows, columns=header).to_excel(buf, index=False)
    return buf.getvalue()


def _read(svc, content, file_path='config/a.xlsx'):
    return svc._read_excel_data(content, file_path)['Sheet1']


def _summary(svc, before, after, path='a.xlsx'):
    """比较两个 xlsx 字节流，返回 summary。参数顺序：旧 -> 新。"""
    result = svc._compare_excel_data(
        svc._read_excel_data(after, path),
        svc._read_excel_data(before, path),
        path)
    return result['summary']


# ---------------------------------------------------------------------------
# 一、读取阶段：不得改写字面量
# ---------------------------------------------------------------------------
class TestReadStageKeepsLiterals:
    """`_read_excel_data` 必须原样保留单元格字面量。"""

    def test_leading_zero_text_is_preserved(self, svc):
        """'00123' 是文本，不能被读成整数 123。"""
        got = _read(svc, _xlsx([['00123']], ['code']))['code'].iloc[0]
        assert str(got) == '00123', (
            f'前导零被 pandas 的类型推断吃掉了：{got!r}（{type(got).__name__}）。\n'
            f'后果：把 00123 改成 123 时两侧读出来是同一个数，平台显示「没有变更」。'
        )

    def test_trailing_zero_is_preserved(self, svc):
        got = _read(svc, _xlsx([['1.10']], ['ratio']))['ratio'].iloc[0]
        assert str(got) == '1.10', f'尾零被改写：{got!r}'

    def test_case_is_preserved(self, svc):
        got = _read(svc, _xlsx([['TRUE']], ['flag']))['flag'].iloc[0]
        assert str(got) == 'TRUE', f'大小写被改写：{got!r}'

    @pytest.mark.parametrize('text', NA_LIKE_LITERALS)
    def test_na_like_text_is_not_converted_to_nan(self, svc, text):
        """这些是配表里真实会写的取值，不能被 pandas 的 NA 转换吃掉。"""
        got = _read(svc, _xlsx([[text]], ['value']))['value'].iloc[0]
        assert not pd.isna(got), (
            f'{text!r} 被 pandas 转成了 NaN —— 于是它会被当成「空单元格」，'
            f'与真正的空值等价，变更被静默吞掉。'
        )
        assert str(got) == text, f'{text!r} 被改写成 {got!r}'

    def test_csv_path_preserves_literals(self, svc):
        """CSV 走 read_csv 那一支，同样不得做 NA 转换与类型推断。"""
        content = 'code,value\n00123,NULL\n'.encode('utf-8')
        df = svc._read_excel_data(content, 'config/a.csv')['Sheet1']
        assert str(df['code'].iloc[0]) == '00123', f"前导零丢失：{df['code'].iloc[0]!r}"
        assert not pd.isna(df['value'].iloc[0]), f"NULL 变成 NaN：{df['value'].iloc[0]!r}"

    def test_empty_cell_is_still_blank(self, svc):
        """反向保险：关掉 NA 转换后，空单元格仍必须算空。

        否则「把单元格清空」会变成「空 vs 空」而被漏掉。
        fixture 必须「有空格子但整行不空」：整行全空会被 read_excel 直接丢行。
        """
        df = _read(svc, _xlsx([['', 'A']], ['value', 'name']))
        assert DiffService._normalize_value(df['value'].iloc[0]) is None, (
            f"空单元格读成了 {df['value'].iloc[0]!r}，被判为非空 —— "
            f"「清空单元格」这类变更会被看漏"
        )


# ---------------------------------------------------------------------------
# 二、比较阶段：文本字面量不得与「空」等价，不得 trim
# ---------------------------------------------------------------------------
class TestNormalizeValue:
    """`_normalize_value` 是 `_values_equal` / 相似度 / 行相等的共同底座。"""

    @pytest.mark.parametrize('text', NA_LIKE_LITERALS)
    def test_na_like_text_is_not_normalised_to_none(self, text):
        assert DiffService._normalize_value(text) is not None, (
            f'{text!r} 被 _normalize_value 判成了空值。\n'
            f'后果：文本 {text!r} → 空单元格、以及 {text!r} → 另一个字面量，'
            f'都会被判「相等」→ 不报变更（配表里用它们表示「无引用」极常见）。'
        )

    def test_surrounding_spaces_are_not_stripped(self):
        """首尾空格是真实内容（配表里常用于对齐/占位）。"""
        assert DiffService._normalize_value('  x  ') == '  x  ', (
            '_normalize_value 把首尾空格 strip 掉了 —— '
            "于是 '  x  ' 与 'x' 判「相等」，变更被吞掉"
        )

    def test_real_blanks_are_none(self):
        """反向保险：真正的空值仍必须归一为 None。"""
        assert DiffService._normalize_value(None) is None
        assert DiffService._normalize_value('') is None
        assert DiffService._normalize_value(float('nan')) is None
        assert DiffService._normalize_value(pd.NA) is None
        assert DiffService._normalize_value(pd.NaT) is None

    @pytest.mark.parametrize('text', NA_LIKE_LITERALS)
    def test_literal_is_not_equal_to_blank(self, svc, text):
        assert svc._values_equal(text, '') is False, (
            f'{text!r} 与空单元格被判「相等」→ 该变更不会出现在 diff 里'
        )
        assert svc._values_equal(text, None) is False

    def test_whitespace_difference_is_a_change(self, svc):
        assert svc._values_equal('  x  ', 'x') is False, (
            "'  x  ' 与 'x' 被判「相等」→ 界面会出现「x → x」这种无法核对的变更行"
            "（或更糟：根本不报变更）"
        )

    def test_distinct_literals_are_not_equal(self, svc):
        assert svc._values_equal('null', 'None') is False, (
            "两个不同的字面量被判「相等」→ 真实变更被吞掉"
        )


# ---------------------------------------------------------------------------
# 三、端到端：这些变更必须被检出（本文件存在的理由）
# ---------------------------------------------------------------------------
#
# 关于断言的形状：本文件真正要锁死的不变量是「变更必须被**显示出来**」，
# 而不是「必须被归类成 modified」。两者是不同的保证：
#
#   * 行配对靠 `_calculate_row_similarity`，阈值 `score > 0.6`（diff_service.py:653）。
#     两列的表改一列 = 0.5 → 低于阈值 → 该行被判成「删一行 + 加一行」。
#     变更**照样显示**，只是归类不同。真实配表十几列，改一列 ≈ 0.9 → 归为 modified。
#   * 所以下面宽表用例断言 modified == 1；窄表用例只断言「被检出」
#     （modified + added + removed > 0），把「任何宽度都不会被静默吞掉」也钉住。
#
# 修前这两类用例**全部**为 0 变更（字面量在读取阶段就被改写成同一个值），
# 所以它们对本次修复是有效的回归保护。

WIDE = ['id', 'code', 'name', 'type', 'value', 'desc']  # 6 列，贴近真实配表宽度


def _detected(stats):
    """变更被显示出来了（无论归类成 modified 还是 add/remove 对）。"""
    return stats['modified'] + stats['added'] + stats['removed']


class TestChangesAreActuallyDetected:

    def test_leading_zero_change_is_reported(self, svc):
        """00123 -> 123 必须报出来。"""
        before = _xlsx([['1', '00123', 'A', 't', 'v', 'd']], WIDE)
        after = _xlsx([['1', '123', 'A', 't', 'v', 'd']], WIDE)
        stats = _summary(svc, before, after)
        assert stats['modified'] == 1, (
            f'00123 -> 123 没有被归类为 modified：{stats}\n'
            f'修前两侧都被读成整数 123，比较相等 → 平台显示「没有变更」，审核者看不到这条。'
        )

    def test_literal_null_becoming_empty_is_reported(self, svc):
        """文本 'NULL' -> 空单元格 必须被检出。"""
        before = _xlsx([['1', 'a', 'A', 't', 'NULL', 'd']], WIDE)
        after = _xlsx([['1', 'a', 'A', 't', '', 'd']], WIDE)
        stats = _summary(svc, before, after)
        assert stats['modified'] == 1, (
            f"'NULL' -> 空 没有被归类为 modified：{stats}\n"
            f"修前两侧都是 NaN（都判空）→ 真实变更被静默吞掉。"
        )

    def test_two_distinct_literals_are_reported(self, svc):
        """'null' -> 'None' 是两个不同取值，必须被检出。"""
        before = _xlsx([['1', 'a', 'A', 't', 'null', 'd']], WIDE)
        after = _xlsx([['1', 'a', 'A', 't', 'None', 'd']], WIDE)
        stats = _summary(svc, before, after)
        assert stats['modified'] == 1, f"'null' -> 'None' 没有被归类为 modified：{stats}"

    def test_whitespace_only_change_is_reported(self, svc):
        """'x' -> 'x ' 是真实变更（配表里尾随空格会改变解析结果）。"""
        before = _xlsx([['1', 'a', 'A', 't', 'x', 'd']], WIDE)
        after = _xlsx([['1', 'a', 'A', 't', 'x ', 'd']], WIDE)
        stats = _summary(svc, before, after)
        assert stats['modified'] == 1, f"'x' -> 'x ' 没有被归类为 modified：{stats}"

    @pytest.mark.parametrize('before_val,after_val', [
        ('00123', '123'), ('1.10', '1.1'), ('TRUE', 'true'),
        ('NULL', ''), ('null', 'None'), ('x', 'x '),
    ])
    def test_narrow_table_never_swallows_the_change(self, svc, before_val, after_val):
        """窄表（2 列）低于行配对阈值时也会被报成 add+remove —— 但**绝不为 0 变更**。

        这是比「归类正确」更重要的保证：漏审的代价远高于多显示一行。
        """
        before = _xlsx([[before_val, 'A']], ['value', 'name'])
        after = _xlsx([[after_val, 'A']], ['value', 'name'])
        stats = _summary(svc, before, after)
        assert _detected(stats) > 0, (
            f'{before_val!r} -> {after_val!r} 在窄表上报出了 0 变更：{stats}\n'
            f'这就是本次修复要消灭的形态 —— 有变更却显示「没有变更」。'
        )


class TestNoSpuriousChanges:
    """反向保险：不要为了保真凭空造出变更。"""

    def test_identical_files_report_nothing(self, svc):
        rows = [['00123', 'NULL', '1.10', 'A'], ['456', 'x', '2.0', 'B']]
        content = _xlsx(rows, ['code', 'value', 'ratio', 'name'])
        stats = _summary(svc, content, content)
        assert stats['modified'] == 0, f'同一份文件与自己比较却报出变更：{stats}'
        assert stats['added'] == 0 and stats['removed'] == 0, stats

    def test_plain_numeric_cells_display_stably(self, svc):
        """纯数字单元格读出来应仍是人看到的那个数（避免凭空多出 123 -> 123.0）。"""
        df = _read(svc, _xlsx([[123, 1.5]], ['a', 'b']))
        assert str(df['a'].iloc[0]) == '123', f"整数格读成 {df['a'].iloc[0]!r}"
        assert str(df['b'].iloc[0]) == '1.5', f"小数格读成 {df['b'].iloc[0]!r}"


# ---------------------------------------------------------------------------
# 四、比较层与展示层必须口径一致（否则会出现「看不出差异的变更行」）
# ---------------------------------------------------------------------------
#
# 这是最容易漏的一处：修了比较层、忘了展示层，结果反而更糟。
#
#   utils.diff_data_utils.format_cell_value 是展示层的对应物。它原先也把**文本**
#   'null'/'None'/'nan'/'undefined' 渲染成空串，并 strip 首尾空格。
#   两边口径不一致时会出现这种情形：
#       表里   'null'  ->  空
#       比较层 判「有变更」（对）
#       展示层 两边都渲染成空串
#       => 审核者看到一行「空 → 空」的变更行，完全不知道改了什么
#   这比漏报更难排查 —— 至少漏报时界面是干净的。
#
# 下面用「逐一枚举值对」的方式把不变量钉死：
#   只要比较层认为两个值不等，展示层就必须把它们渲染成不同的文本。

VALUE_PAIRS_EXPECTED_DIFFERENT = [
    ('00123', '123'),
    ('1.10', '1.1'),
    ('TRUE', 'true'),
    ('NULL', ''),
    ('null', 'None'),
    ('x', 'x '),
    ('x', ' x'),
    ('x', 'x  '),
    ('', ' '),
    ('null', ''),
    ('nan', ''),
    ('None', ''),
    ('<NA>', ''),
]


class TestDisplayMatchesComparison:
    """展示层与比较层口径一致性。"""

    @pytest.mark.parametrize('before_val,after_val', VALUE_PAIRS_EXPECTED_DIFFERENT)
    def test_detected_change_is_visually_distinct(self, svc, before_val, after_val):
        from utils.diff_data_utils import format_cell_value

        assert svc._values_equal(before_val, after_val) is False, (
            f'前提不成立：比较层认为 {before_val!r} 与 {after_val!r} 相等，'
            f'这个值对不该出现在本参数化列表里'
        )
        rendered_before = format_cell_value(before_val)
        rendered_after = format_cell_value(after_val)
        assert rendered_before != rendered_after, (
            f'{before_val!r} -> {after_val!r}：比较层报了变更，展示层却把两边渲染成同一个字符串 '
            f'{rendered_before!r}。\n'
            f'后果：审核者看到一行「看不出差异」的变更行，无法判断改了什么 —— '
            f'比漏报更难排查。'
        )

    def test_blank_renders_as_empty(self):
        """反向保险：真正的空值仍必须渲染为空串（不能显示成 'None'/'nan'）。"""
        from utils.diff_data_utils import format_cell_value

        assert format_cell_value(None) == ''
        assert format_cell_value(float('nan')) == ''
        assert format_cell_value('') == ''

    def test_cell_css_preserves_whitespace(self):
        """`.excel-cell` 必须保留空格，否则上面那条一致性保证在浏览器里失效。

        CSS 源码级断言：`white-space: nowrap/normal` 会让 HTML 折叠连续空格与首尾空格，
        于是 `'  x  '` 与 `'x'` 在屏幕上完全一样 —— 单元测试看不到这一层，只能在此钉住。
        """
        path = os.path.join(PROJECT_ROOT, 'static', 'css', 'excel-diff-new.css')
        with open(path, encoding='utf-8') as fh:
            source = fh.read()
        start = source.find('.excel-diff-table .excel-cell {')
        assert start != -1, '找不到 .excel-diff-table .excel-cell 规则'
        rule = source[start:source.find('}', start)]
        assert 'white-space: pre-wrap' in rule or 'white-space: pre' in rule, (
            '.excel-cell 没有用 white-space: pre-wrap/pre。\n'
            '否则 HTML 会折叠空格，`\'  x  \'` 与 `\'x\'` 渲染成一样的文本 —— '
            '比较层报的变更在界面上看不出来。\n'
            f'当前规则：\n{rule}'
        )
        assert 'white-space: nowrap' not in rule, (
            '.excel-cell 又回到了 nowrap：空格会被折叠，且超长值会被 ellipsis 静默截断'
        )

    def test_long_value_is_not_silently_clipped(self):
        """长值不能只有「截断」一条路 —— overflow:hidden + nowrap + ellipsis 会静默隐藏内容。

        配表里几 KB 的参数串是常态；看不到全文的审核等于没审核。
        """
        path = os.path.join(PROJECT_ROOT, 'static', 'css', 'excel-diff-new.css')
        with open(path, encoding='utf-8') as fh:
            source = fh.read()
        start = source.find('.excel-diff-table .excel-cell {')
        rule = source[start:source.find('}', start)]
        assert 'text-overflow: ellipsis' not in rule or 'overflow: hidden' not in rule, (
            '.excel-cell 同时用了 overflow:hidden 与 text-overflow:ellipsis，'
            '超长单元格内容会被截断且无任何途径查看全文'
        )


# ---------------------------------------------------------------------------
# 五、禁止伪造 diff（比漏审更危险：让审核者对不存在的变更签字）
# ---------------------------------------------------------------------------
class TestNoMockDiffPayload:
    """`commit_diff_logic.get_mock_diff_data` 返回写死的假变更。

    实测其内容为 `{'A':'ID5','B':'New Item','C':'新增项目'}`（表）或
    `function oldFunction() { ... return "old"; }`（代码），并在
    `services/commit_diff_logic.py:849`（"无法获取真实diff数据"分支）与
    `:853`（异常兜底）被**真实返回给调用方**。若这条数据流到页面，
    审核者会对着一份根本不存在于仓库的变更点「确认」。
    正确做法是同文件 `:624` 的 `_build_diff_error_data`（返回 type='error'）。
    """

    def test_mock_diff_data_is_gone(self):
        from services import commit_diff_logic
        assert not hasattr(commit_diff_logic, 'get_mock_diff_data'), (
            'get_mock_diff_data 仍然存在。它返回写死的假 diff，'
            '会让人对着不存在的变更签字 —— 必须删掉并改用 _build_diff_error_data。'
        )

    def test_no_code_returns_mock_payload(self):
        """源码级断言：杜绝有人再把「无法获取真实diff」退回成模拟数据。"""
        path = os.path.join(PROJECT_ROOT, 'services', 'commit_diff_logic.py')
        with open(path, encoding='utf-8') as fh:
            offenders = [(n, line.strip()) for n, line in enumerate(fh, 1)
                         if 'mock' in line.lower() and not line.lstrip().startswith('#')]
        assert not offenders, (
            'commit_diff_logic.py 里仍引用 mock 数据：\n'
            + '\n'.join('  :%d %s' % (n, t) for n, t in offenders)
        )

    def test_error_payload_builder_is_available(self):
        """兜底应当返回 type='error'，让前端显示「无法获取差异」而不是假数据。"""
        from services.commit_diff_logic import _build_diff_error_data
        payload = _build_diff_error_data(
            type('C', (), {'path': 'a/b.xlsx', 'commit_id': 'abc'})(), '取不到差异')
        assert payload['type'] == 'error', payload
        assert payload['message'], '错误 payload 必须带可读信息'
