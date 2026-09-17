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
import json
import math
import os
import re
import shutil
import subprocess
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

# 会影响单元格渲染的样式来源：全局 CSS + 各模板内联 <style>（有几页把单元格样式
# 写在自己页面里，只查 static/css 会漏）。
_STYLE_SCAN_EXCLUDE = ('bootstrap', 'font-awesome', 'fontawesome')


def _css_and_inline_style_paths():
    paths = []
    css_dir = os.path.join(PROJECT_ROOT, 'static', 'css')
    for name in sorted(os.listdir(css_dir)):
        if name.endswith('.css'):
            paths.append(os.path.join(css_dir, name))
    for root, _dirs, names in os.walk(os.path.join(PROJECT_ROOT, 'templates')):
        for name in sorted(names):
            if name.endswith('.html'):
                paths.append(os.path.join(root, name))
    return paths


_DECL_RE = re.compile(r'([a-z-]+)\s*:\s*([^;}]+)', re.I)


def _white_space_rules(source):
    """产出 `(选择器, 值, 行号)`：靠**向前找未闭合的 `{`** 定位所属规则。

    不能用「逐条 `选择器 { 声明 }`」的正则去配：声明体里会有注释，注释里又会写
    大括号（本仓库的 CSS 注释经常贴示例规则），正则会被配歪 —— 上一版就是因此
    漏掉了真实存在的那条 `white-space: normal`。这里先把注释替换成等长空格再定位，
    保证行号与原文一致。
    """
    masked = re.sub(r'/\*.*?\*/', lambda m: ' ' * len(m.group(0)), source, flags=re.S)
    out = []
    for match in re.finditer(r'white-space\s*:\s*([^;}]+)', masked):
        depth = 0
        open_brace = None
        for i in range(match.start(), -1, -1):
            char = masked[i]
            if char == '}':
                depth += 1
            elif char == '{':
                if depth == 0:
                    open_brace = i
                    break
                depth -= 1
        if open_brace is None:
            continue
        j = open_brace - 1
        while j >= 0 and masked[j] not in '}{;':
            j -= 1
        selector = ' '.join(masked[j + 1:open_brace].split())
        line_no = masked.count('\n', 0, match.start()) + 1
        out.append((selector, match.group(1).strip(), line_no))
    return out


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

    def test_no_rule_reintroduces_whitespace_collapsing_on_cells(self):
        """**每一条**能命中单元格的规则都必须 pre-wrap —— 覆盖掉一条就等于没修。

        上一条只查了 `.excel-diff-table .excel-cell { … }` 这一条规则，而这正是它漏掉
        真实缺陷的原因：`excel-diff-new.css` 里另有一条

            .excel-cell.modified-column { white-space: normal; }

        特异性 (0,2,0) 与它**相同**、位置又更靠后，于是这条在层叠里赢，而它命中的
        正是「改前 / 改后」这两个最需要看清差异的单元格。后果是后端报出
        `'   x   '` → `'x'`、`'a  b'` → `'a b'` 这类变更时，
        HTML 把连续空格折成一个、把首尾空格整个去掉，两行渲染出来**一模一样**：
        实测墨迹宽度分别是 7.2px/21.6px（普通单元格是 50.4px/28.8px）。

        所以这里改成扫**全部**声明了 `white-space` 且选择器能命中单元格的规则，
        要求它们一致取 `pre-wrap` 值（`pre` / `pre-wrap` / `break-spaces` 都算）。
        任何一条把值拉回 `normal`/`nowrap` 都会被这条用例拦下。
        """
        allow = ('pre', 'pre-wrap', 'break-spaces')
        offenders = []
        for path in _css_and_inline_style_paths():
            with open(path, encoding='utf-8') as fh:
                source = fh.read()
            for selector, value, line_no in _white_space_rules(source):
                # 只看能命中单元格的规则。`.excel-diff-table td` 这种靠元素名命中的
                # 规则特异性更低、压不过 `.excel-cell`，不在此列。
                if not re.search(r'\.excel-cell|\.modified-column', selector):
                    continue
                if value.split()[0].lower() in allow:
                    continue
                offenders.append(
                    f'{os.path.relpath(path, PROJECT_ROOT)}:{line_no} '
                    f'{selector} -> white-space: {value}'
                )
        assert not offenders, (
            '这些规则会把单元格里的连续空格与首尾空格折叠掉，'
            '让「只差空格」的变更在界面上变成看不出差异：\n  ' + '\n  '.join(offenders) +
            '\n（比较层按字面量比对，`\'  x  \'` 与 `\'x\'` 会被报成变更；'
            '展示层必须让审核者看得见这个差别）'
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


# ---------------------------------------------------------------------------
# 六、客户端展示层：同一份契约，四份拷贝
# ---------------------------------------------------------------------------
#
# 上面第四节跑的是**服务端**展示层 `format_cell_value`。客户端还有四份
# `formatCellValue`（提交页 / 合并页 / 周版本页 / diff-handlers.js），渲染的是
# **同一批单元格**。历史上其中三份停留在旧口径：
#
#     const strValue = String(value).trim();
#     if (strValue.toLowerCase() === 'nan' || ... 'null' || ... 'undefined'
#         || ... 'none' || strValue === '') { return ''; }
#
# 也就是把「文本 null/nan/undefined/none」折叠成空串、并 strip 首尾空格 ——
# 正是 1.9.0 在服务端已经修掉的那两个改写。后果同样具体：
#
#     表里  'null' -> 空      比较层判「有变更」（对）
#                             客户端两边都渲染成空串
#                            => 一行「空 → 空」的变更，无法判断改了什么
#     表里  '  x  ' -> 'x'    比较层判「有变更」（对）
#                             客户端 trim 之后两边一模一样
#                            => 一行「看不出差异」的变更
#
# 周版本页那一份当时口径是对的，但它**内联做了 `<`/`>` 转义**，而调用方有的再转一遍
# （`&lt;` 变 `&amp;lt;`，页面上显示字面 `&lt;`）、有的直接裸插（连 `&` 都没转义，
# 文件里写的 `&lt;` 被浏览器解码成 `<`）—— 同一个字面量在同一张表的不同行状态显示
# 不同，且总有一边是错的。
#
# 所以这里钉两条：
#   1. 四份拷贝对同一批值必须给出**逐字相同**的结果（防再次漂移）；
#   2. 展示层**只做展示** —— 不改写原文，也不做 HTML 转义（转义是调用方拼 HTML
#      那一刻的事，做进函数里就必然与调用方重复或互相漏掉）。

# 客户端四份拷贝：(相对路径, 抠出函数用的正则)。
# 正则同时认两种写法 —— `function formatCellValue(…)` 声明，以及
# `window.formatCellValue = function (…)` 赋值（周版本页历史上用的是后者）。
CLIENT_FORMATTERS = (
    ('static/js/diff-handlers.js', r'function formatCellValue\s*\('),
    ('templates/commit_diff.html', r'function formatCellValue\s*\('),
    ('templates/merge_diff.html', r'function formatCellValue\s*\('),
    ('templates/weekly_version_full_diff.html',
     r'(?:function\s+formatCellValue|window\.formatCellValue\s*=\s*function)\s*\('),
)

def _probe_key(value):
    """探针在结果表里的查表键。

    不能拿探针值本身当 key：`float('nan') != float('nan')`，`tuple.index` 根本找不到它。
    """
    if isinstance(value, float) and math.isnan(value):
        return '<nan>'
    return repr(value)


# 探针值。`None` 对应 JSON null（载荷里真正的空值），`''` 是空单元格
# （实测读取层 dtype=str + keep_default_na=False 读出的就是空字符串）。
_CLIENT_PROBE_BASE = (
    None, '', '   ',
    'null', 'NULL', 'None', 'none', 'nan', 'NaN', 'undefined', '<NA>',
    '  x  ', ' x', 'x ', 'a  b',
    '0', '1.10', '00123', 'TRUE',
    'a<b', 'a&amp;b', 'a"b',
)
# 再并入第四节那张值对表里出现过的**每个**值：下面有一条「比较层判不等 ⇒ 客户端也
# 必须渲染成不同文本」的同构断言，要拿同一批值去查表，少一个就会 KeyError。
CLIENT_VALUE_PROBES = _CLIENT_PROBE_BASE + tuple(
    value
    for value in (v for pair in VALUE_PAIRS_EXPECTED_DIFFERENT for v in pair)
    if _probe_key(value) not in {_probe_key(probe) for probe in _CLIENT_PROBE_BASE}
)


def _probe_results(values):
    """`{查表键: 渲染结果}`。"""
    return {_probe_key(probe): rendered
            for probe, rendered in zip(CLIENT_VALUE_PROBES, values)}


def _is_real_blank(value):
    """真正的空值 —— 与 `format_cell_value` 的口径一致（None / '' / NaN 数字）。"""
    if value is None or value == '':
        return True
    return isinstance(value, float) and math.isnan(value)


def _extract_function(path, pattern):
    """按大括号配对抠出一个函数（测试要跑的是真实现，不是复刻）。"""
    with open(path, encoding='utf-8') as fh:
        text = fh.read()
    match = re.search(pattern, text)
    assert match, f'{path}：找不到匹配 {pattern} 的函数'
    depth = 0
    for index in range(match.start(), len(text)):
        if text[index] == '{':
            depth += 1
        elif text[index] == '}':
            depth -= 1
            if depth == 0:
                return text[match.start():index + 1]
    raise AssertionError(f'{path}：大括号不配对')


def _run_client_formatters(probes):
    """把四份真实现放进 node 跑（都是纯函数，不需要 DOM），返回 {路径: [结果…]}。"""
    node = shutil.which('node')
    if not node:
        pytest.skip('node 不可用，跳过 JS 层验证')

    blocks, calls, names = [], [], []
    for index, (rel, pattern) in enumerate(CLIENT_FORMATTERS):
        source = _extract_function(os.path.join(PROJECT_ROOT, rel), pattern)
        alias = f'fmt{index}'
        if source.lstrip().startswith('window.'):
            # `window.formatCellValue = function …` 是赋值表达式，不能当声明直接用
            blocks.append(f'const window = {{}};\n{source}\nconst {alias} = window.formatCellValue;')
        else:
            blocks.append(f'{source}\nconst {alias} = formatCellValue;')
        calls.append(alias)
        names.append(rel)

    script = '\n\n'.join(blocks) + '\n' + (
        'const probes = %s;\n' % json.dumps(list(probes)) +
        'const out = {};\n' +
        ''.join(f'out[{json.dumps(name)}] = probes.map((v) => {call}(v));\n'
                for name, call in zip(names, calls)) +
        'process.stdout.write(JSON.stringify(out));\n'
    )
    proc = subprocess.run([node, '-'], input=script, capture_output=True,
                          text=True, encoding='utf-8', timeout=60)
    assert proc.returncode == 0, (
        '跑客户端 formatCellValue 时 Node 报错：\n'
        f'STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}'
    )
    return json.loads(proc.stdout)


@pytest.fixture(scope='module')
def client_formatters():
    return _run_client_formatters(CLIENT_VALUE_PROBES)


class TestClientDisplayLayerKeepsLiterals:
    """客户端四份 `formatCellValue` 必须与比较层同一口径。"""

    def test_all_four_copies_agree(self, client_formatters):
        """四份拷贝对同一批值必须逐字一致 —— 否则同一个单元格在不同页面显示不同。"""
        reference_name = CLIENT_FORMATTERS[0][0]
        reference = client_formatters[reference_name]
        problems = []
        for rel in list(client_formatters)[1:]:
            for probe, expected, actual in zip(CLIENT_VALUE_PROBES,
                                               reference, client_formatters[rel]):
                if expected != actual:
                    problems.append(
                        f'{probe!r}: {reference_name} 给 {expected!r}，{rel} 给 {actual!r}')
        assert not problems, (
            '四份客户端 formatCellValue 口径已经漂移（同一份配表的同一个单元格'
            '在不同页面会显示成不同的东西）：\n  ' + '\n  '.join(problems)
        )

    def test_a_literal_that_is_a_real_value_is_not_rendered_blank(self, client_formatters):
        """文本 `null` / `none` / `nan` / `undefined` / `<NA>` 都是**取值**，不能渲染成空。

        比较层（`_normalize_value`）认为它们与空不等、会报变更；展示层若把它们折叠成
        空串，审核者看到的就是一行「空 → 空」的变更行 —— 无法判断改了什么。
        """
        problems = []
        for rel, values in client_formatters.items():
            by_probe = _probe_results(values)
            for probe in CLIENT_VALUE_PROBES:
                if _is_real_blank(probe) or probe == '   ':
                    continue
                if by_probe[_probe_key(probe)] == '':
                    problems.append(f'{rel}：{probe!r} 被渲染成空串')
        assert not problems, (
            '客户端把真实的取值渲染成了空串 —— 后端报的变更在界面上看不见：\n  '
            + '\n  '.join(problems)
        )

    def test_whitespace_is_not_trimmed(self, client_formatters):
        """首尾空格与连续空格都要原样留下（呈现由 CSS 的 pre-wrap 负责）。"""
        problems = []
        for rel, values in client_formatters.items():
            by_probe = _probe_results(values)
            for probe in ('  x  ', ' x', 'x ', 'a  b', '   '):
                if by_probe[_probe_key(probe)] != probe:
                    problems.append(
                        f'{rel}：{probe!r} 被渲染成 {by_probe[_probe_key(probe)]!r}')
        assert not problems, (
            '客户端 trim 了单元格值 —— 只差空格的变更会渲染成两边一模一样：\n  '
            + '\n  '.join(problems)
        )

    def test_real_blanks_still_render_as_empty(self, client_formatters):
        """反向保险：真正的空值仍必须渲染成空串（不能变成 'null'/'None'）。"""
        problems = []
        for rel, values in client_formatters.items():
            by_probe = _probe_results(values)
            for probe in CLIENT_VALUE_PROBES:
                if not _is_real_blank(probe):
                    continue
                rendered = by_probe[_probe_key(probe)]
                if rendered != '':
                    problems.append(f'{rel}：{probe!r} 渲染成 {rendered!r}，应为空串')
        assert not problems, '\n  '.join(problems)

    def test_escaping_is_the_callers_job_not_the_formatters(self, client_formatters):
        """`formatCellValue` 不能自己转义 HTML。

        转义做进函数里，调用方就必然要么重复转（`&lt;` 变 `&amp;lt;`，页面上显示字面
        `&lt;`）、要么漏转（文件里写的 `&lt;` 被浏览器解码成 `<`）—— 周版本页历史上
        两种情况同时存在，同一个字面量在变更行与新增行显示不同。转义必须只发生在
        拼 HTML 的那一刻，由调用方用 escapeHtml / escapeHtmlAttribute 各做一次。
        """
        problems = []
        for rel, values in client_formatters.items():
            by_probe = _probe_results(values)
            for probe in ('a<b', 'a&amp;b', 'a"b'):
                rendered = by_probe[_probe_key(probe)]
                if rendered != probe:
                    problems.append(f'{rel}：{probe!r} 被改写成 {rendered!r}')
        assert not problems, (
            'formatCellValue 里做了 HTML 转义 —— 转义必须由调用方在拼 HTML 时做一次：\n  '
            + '\n  '.join(problems)
        )

    def test_every_change_the_comparison_layer_reports_is_visible_here(
            self, svc, client_formatters):
        """与服务端那条同构：比较层判不等 ⇒ 客户端也必须渲染成不同文本。

        用的是**同一张值对表**。其中 `('null','')`、`('nan','')`、`('x','x ')`
        正是旧客户端实现会抹平的那几组。
        """
        problems = []
        for before_val, after_val in VALUE_PAIRS_EXPECTED_DIFFERENT:
            if svc._values_equal(before_val, after_val) is not False:
                continue
            for rel, values in client_formatters.items():
                by_probe = _probe_results(values)
                rendered_before = by_probe[_probe_key(before_val)]
                rendered_after = by_probe[_probe_key(after_val)]
                if rendered_before == rendered_after:
                    problems.append(
                        f'{rel}：{before_val!r} -> {after_val!r} 两边都渲染成 '
                        f'{rendered_before!r}'
                    )
        assert not problems, (
            '比较层报了变更，客户端展示层却把两边渲染成同一个字符串：\n  '
            + '\n  '.join(problems)
        )


# 合并页的单元格拼装函数。它比另外两页多一层「要不要显示文本块」的判断，
# 而那层判断里**又抄了一遍** NA 黑名单 —— 见下面那条用例。
MERGE_TEMPLATE = 'templates/merge_diff.html'


def _node_visible(html):
    """把一段高亮 HTML 还原成肉眼看到的文本（剥标签 + 反转义）。"""
    text = re.sub(r'<[^>]+>', '', html)
    for entity, char in (('&lt;', '<'), ('&gt;', '>'), ('&quot;', '"'),
                         ('&#39;', "'"), ('&amp;', '&')):
        text = text.replace(entity, char)
    return text


def _run_merge_modified_row(cases):
    """跑 merge_diff.html 里**真的** `createModifiedRowForContainer`。"""
    node = shutil.which('node')
    if not node:
        pytest.skip('node 不可用，跳过 JS 层验证')
    path = os.path.join(PROJECT_ROOT, MERGE_TEMPLATE)
    sources = [
        _extract_function(path, r'function formatCellValue\s*\('),
        _extract_function(path, r'function escapeHtml\s*\('),
        _extract_function(path, r'function createModifiedRowForContainer\s*\('),
    ]
    script = (
        'const document = { createElement() { const el = { textContent: "" };\n'
        '  Object.defineProperty(el, "innerHTML", { get() { return el.textContent\n'
        '    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); } });\n'
        '  return el; } };\n'
        + '\n\n'.join(sources) + '\n'
        + 'const cases = %s;\n' % json.dumps(cases) +
        'process.stdout.write(JSON.stringify(cases.map((c) => '
        'createModifiedRowForContainer({row_number: 5, cell_changes: '
        '[{column: "c", old_value: c.old, new_value: c.new}]}, ["c"], 0))));\n'
    )
    proc = subprocess.run([node, '-'], input=script, capture_output=True,
                          text=True, encoding='utf-8', timeout=60)
    assert proc.returncode == 0, (
        '跑合并页 createModifiedRowForContainer 时 Node 报错：\n'
        f'STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}'
    )
    return json.loads(proc.stdout)


MERGE_NA_CASES = (
    # 文本 null → x：旧实现把改前渲染成空白
    dict(old='null', new='x', must_show='null'),
    # 文本 nan → x：同上
    dict(old='nan', new='x', must_show='nan'),
    # 只由空白组成的取值 → x：旧实现的 trim() 把它当空
    dict(old='   ', new='x', must_show='   '),
    # 反向保险：真正的空值（JSON null → formatCellValue → ''）仍不显示文本块
    dict(old=None, new='x', must_show=None),
)


@pytest.fixture(scope='module')
def merge_na_rendered():
    """四种取值各跑一遍合并页真实的「修改行」渲染，返回每格可见文本。

    放模块作用域（而不是类里的实例方法 fixture）是因为类作用域 + `self`
    在 pytest 里已废弃：fixture 只跑一次、测试每条却拿到新实例。
    """
    return [_node_visible(html)
            for html in _run_merge_modified_row(
                [dict(old=c['old'], new=c['new']) for c in MERGE_NA_CASES])]


class TestMergePageDoesNotReapplyTheNaBlacklist:
    """合并页判空只能靠 `formatCellValue` 的结果，不能再叠一层字面量黑名单。

    `templates/merge_diff.html` 的修改行单元格是这样拼的（修复前）：

        // 对于null/nan/空值，不显示文本块
        if (oldValue && oldValue.trim() !== '' && oldValue.toLowerCase() !== 'null'
            && oldValue.toLowerCase() !== 'nan') { …文本块… } else { html += ''; }

    这是 `formatCellValue` 旧口径（把文本 null/nan 折成空串、并 trim）的**重复实现**。
    旧口径下它永远是死代码；而 `formatCellValue` 改成「表里怎么写就怎么显示」之后，
    它就变成了**真的黑名单**：文本 `null` / `nan`、以及只由空白组成的取值都被渲染成
    空白，可比较层认为它们与空不等、会报变更 —— 审核者看到一行「空 → 空」，
    无法判断改了什么。这正是本文件开头说的「最坏的失败方式」。

    这里跑真函数，断言这些取值必须被渲染出来（旧实现在这几组上渲染为空）。
    """

    CASES = MERGE_NA_CASES

    def test_literals_and_whitespace_values_are_rendered(self, merge_na_rendered):
        problems = []
        for case, visible in zip(self.CASES, merge_na_rendered):
            expected = case['must_show']
            if expected is None:
                if 'null' in visible:
                    problems.append(
                        f'真正的空值被渲染出了文本：{visible!r}')
                continue
            if expected not in visible:
                problems.append(
                    f'改前值 {case["old"]!r} 没有出现在渲染结果里（得到 {visible!r}）—— '
                    f'比较层认为它与 {case["new"]!r} 不等、会报变更，'
                    f'界面上却看不到改前是什么'
                )
        assert not problems, '\n  '.join(problems)


class TestNoTemplateReappliesTheNaBlacklist:
    """源码级兜底：`toLowerCase() === 'null'/'nan'/'none'/'undefined'` 这类判空不得再出现。

    它可以出现在任何地方（模板内联 JS、static/js），而且是**静默**的：多一层黑名单
    只会让某些取值不显示，不会报错。合并页那两处就是这么来的（抄了一份旧口径）。
    这里扫全仓的模板与 JS，拦住「在展示层再叠一份 NA 黑名单」这个形态。
    """

    BLACKLIST = re.compile(
        r"""toLowerCase\(\)\s*[!=]==?\s*['"](?:null|nan|none|undefined|<na>)['"]""",
        re.I)

    def test_no_source_reintroduces_the_blacklist(self):
        offenders = []
        roots = [os.path.join(PROJECT_ROOT, 'templates'),
                 os.path.join(PROJECT_ROOT, 'static', 'js')]
        for root in roots:
            for dirpath, _dirs, names in os.walk(root):
                for name in sorted(names):
                    if not name.endswith(('.html', '.js')) or '.min.' in name:
                        continue
                    full = os.path.join(dirpath, name)
                    with open(full, encoding='utf-8') as fh:
                        for line_no, line in enumerate(fh, 1):
                            # 注释里提到这个形态不算（本仓库的注释大量引用被修掉的写法）
                            if line.lstrip().startswith('//') or line.lstrip().startswith('*'):
                                continue
                            if self.BLACKLIST.search(line):
                                offenders.append(
                                    f'{os.path.relpath(full, PROJECT_ROOT)}:{line_no} '
                                    f'{line.strip()}')
        assert not offenders, (
            '展示层又叠了一层 NA 文本黑名单 —— 比较层认为文本 null/nan/none/undefined '
            '与空**不等**、会报变更，这里把它们判成空就会渲染出「空 → 空」的变更行：\n  '
            + '\n  '.join(offenders) +
            '\n（判空只能靠 format_cell_value / formatCellValue 的结果）'
        )


# ---------------------------------------------------------------------------
# 七、兜底渲染器：模板渲染失败时也不能改写内容、更不能拼出不转义的 HTML
# ---------------------------------------------------------------------------
class TestSimpleFallbackRenderer:
    """`excel_html_cache_service._generate_simple_excel_html` 是模板抛异常时的兜底。

    它是**拼字符串**而不是走 Jinja，所以自动转义帮不上忙。原实现直接
    `f'<td>{value}</td>'` 把单元格值、表名、列名原样插进 HTML：

    * 单元格里写 `<img src=x onerror=…>` 会被 innerHTML 当标签执行；
    * 写 `<b>` 这类普通标记会让表头/正文错位、文字被吃掉；
    * 且它**不经过** `format_cell_value`，同一个单元格在正常渲染与兜底渲染下
      显示口径不同 —— 兜底本来就是「主路径坏了」的时候才走，再显示成另一个样子
      只会让审核者更难判断。
    """

    @staticmethod
    def _render(payload):
        from services.excel_html_cache_service import ExcelHtmlCacheService
        return ExcelHtmlCacheService.__new__(ExcelHtmlCacheService)\
            ._generate_simple_excel_html(payload)

    def _payload(self, cell_value):
        return {
            'file_path': 'config/a<b>.xlsx',
            'summary': {'added': 1, 'removed': 0, 'modified': 1},
            'sheets': {'S<heet>': {
                'headers': ['h<img>'],
                'rows': [{'status': 'modified', 'row_number': 2,
                          'data': {'h<img>': cell_value}}],
            }},
        }

    def test_untrusted_values_are_escaped(self):
        html = self._render(self._payload('a<img src=x onerror="window.__qa=1">b'))
        # 兜底渲染器自己会生成 div/table/thead/tr/th/td/h3/h4/span，除此之外不该有
        # 任何标签 —— 多出来的就是被审核内容拼成的。
        allowed = r'div|table|thead|tbody|tr|th|td|h3|h4|span'
        foreign = re.search(r'<(?!/?(?:%s)\b)[a-zA-Z!/]' % allowed, html)
        assert not foreign, (
            f'兜底渲染器把不可信内容拼成了真实标签 {foreign.group(0)!r} —— '
            f'单元格/表名来自被审核的 Excel：\n{html}'
        )
        assert '<heet>' not in html, html

    @pytest.mark.parametrize('value', ['null', 'nan', 'None', '0', '00123', '1.10'])
    def test_literals_survive_the_fallback(self, value):
        """兜底路径的展示口径必须与主路径一致（`format_cell_value`）。"""
        html = self._render(self._payload(value))
        assert f'<td>{value}</td>' in html, (
            f'兜底渲染器把 {value!r} 显示成了别的东西：\n{html}'
        )

    def test_real_blanks_still_render_as_empty(self):
        html = self._render(self._payload(None))
        assert '<td></td>' in html, html
