# -*- coding: utf-8 -*-
"""表体渲染共享模块（`static/js/excel_diff_table.js`）的四项能力。

## 这个文件守什么

表体渲染从四份拷贝合并成一份实现的同时，加上了四件事：

1. **单行表头**：每列一格，格子里上小字列字母（A/B/C…）、下面字段名。
   原来是提交页两行表头、周版本页一行没有字母 —— 同一张表在两个页面长得不一样。
   列字母必须按**原始列序**给：隐藏了中间的列，剩下的列不能重新编号。
2. **分段标题带计数**：`变更行数据：3 行 · 4 处单元格`。计数要跟着筛选结果变。
   前缀「标签：」不能动 —— 有测试按正则 `colspan="N"[^>]*>变更行数据：` 卡，
   所以计数只能挂在标签**之后**。
3. **工具栏**：按列筛选 + 只看变更列 + 隐藏本页空列。三个都要在**同一个视图**里
   一起算：列集合由两个开关决定，行集合由筛选决定，计数按最终渲染出来的行数报。
4. **大表分批路径仍然走共享实现**（提交页 >100 行）：分批切的是「渲染计划」的
   单元，不是原始行 —— 否则每一批都会重新分段、段标题重复出现。这里直接断言
   「分批拼出来的表体和一次性拼出来的**逐字节相同**」。

断言跑的是**真的模块**（按浏览器的方式加载：给一个 window，模块只往外挂
`ExcelDiffTable`），不是复刻实现。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = shutil.which('node')
SHARED_MODULE = os.path.join(PROJECT_ROOT, 'static', 'js', 'excel_diff_table.js')
COMMIT_TEMPLATE = os.path.join(PROJECT_ROOT, 'templates', 'commit_diff.html')

HARNESS = '''
const window = {};
window.formatCellValue = function (value) {
    if (value === null || value === undefined) return '';
    if (typeof value === 'number' && isNaN(value)) return '';
    return String(value);
};
%s
const T = window.ExcelDiffTable;
const request = JSON.parse(process.argv[2]);
const sheet = request.sheet;
const out = {};

// 容器只是个状态载体：模块把「筛选词 / 开关」按容器保存，重渲染后还在
function makeContainer(state) {
    const container = {addEventListener: function () {}};
    T.mountSheetTable(container, sheet, {sheetName: 'S1', noticesHtml: ''});
    if (state) {
        T.setViewState(container, state);
    }
    return container;
}

if (request.op === 'render') {
    const container = makeContainer(request.state);
    out.html = T.renderSheetTable(sheet, {sheetName: 'S1', container: container});
    const state = T.getViewState(container);
    // 只回传视图本身：state 里还挂着 container，直接序列化会循环
    out.state = {filter: state.filter, column: state.column,
                 onlyChanged: state.onlyChanged, hideEmpty: state.hideEmpty};
}
if (request.op === 'filter') {
    const container = makeContainer(request.state);
    // 直接用**真实**的 view（不是在这里照抄一遍 buildView 的算法）：列集合怎么算、
    // 哪些行要渲染，都由实现说了算 —— 抄一遍就会与实现漂移。
    const view = T.resolveView(sheet, {sheetName: 'S1', container: container});
    out.rows = view.matched.map(function (r) { return r.row_number; });
    out.columns = {indexes: view.visibleIndexes, hiddenCount: view.hiddenCount};
    out.columnNames = view.visibleHeaders;
    out.html = T.renderSheetTable(sheet, {sheetName: 'S1', container: container});
}
if (request.op === 'batch') {
    const headers = sheet.headers || [];
    const units = T.buildRowRenderPlan(sheet.rows || [], headers);
    const cut = Math.max(1, Math.floor(units.length / 2));
    out.oneShot = T.tableBodyHtml(units, headers, {});
    out.batched = T.tableBodyHtml(units.slice(0, cut), headers, {}) +
                  T.tableBodyHtml(units.slice(cut), headers, {});
    out.headOpen = T.tableHeadHtml(sheet, {});
    out.full = T.renderSheetTable(sheet, {});
    out.unitKinds = units.map(function (u) { return u.row ? 'row' : 'group'; });
}
if (request.op === 'batchview') {
    // 提交页 >100 行那条路径的写法：从 resolveView 取「本帧的列集合与渲染计划」，
    // 再按单元分批。（上面的 'batch' 是直接 buildRowRenderPlan，看不到表头块。）
    const container = makeContainer(request.state);
    const view = T.resolveView(sheet, {container: container});
    const units = view.units;
    const cut = Math.max(1, Math.floor(units.length / 2));
    out.oneShot = T.tableBodyHtml(units, view.visibleHeaders, {});
    out.batched = T.tableBodyHtml(units.slice(0, cut), view.visibleHeaders, {}) +
                  T.tableBodyHtml(units.slice(cut), view.visibleHeaders, {});
    out.unitKinds = units.map(function (u) {
        return u.row ? 'row' : (u.note !== undefined ? 'note' : 'group');
    });
    out.visibleHeaders = view.visibleHeaders;
    out.full = T.renderSheetTable(sheet, {container: container});
}
process.stdout.write(JSON.stringify(out));
'''


def _run(request):
    if NODE is None:
        pytest.skip('本机没有 node，跳过（这是唯一能真跑共享模块的方式）')
    with open(SHARED_MODULE, encoding='utf-8') as handle:
        source = handle.read()
    script = HARNESS % source
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as handle:
        handle.write(script)
        path = handle.name
    try:
        proc = subprocess.run([NODE, path, json.dumps(request)],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, f'node 跑失败：{proc.stderr[:800]}'
        return json.loads(proc.stdout)
    finally:
        os.unlink(path)


# 一张有代表性的表：整行新增 / 整行删除 / 两处逐格修改；
# 其中 `空列` 在**所有**行里都是空的（「隐藏本页空列」的靶子）。
SHEET = {
    'headers': ['id', '名字', '备注', '空列'],
    'rows': [
        {'row_number': 2, 'status': 'added',
         'data': {'id': '100', '名字': 'A', '备注': '', '空列': ''}},
        {'row_number': 3, 'status': 'removed',
         'data': {'id': '5', '名字': 'E', '备注': '', '空列': ''}},
        {'row_number': 5, 'status': 'modified',
         'data': {'id': '7', '名字': 'B', '备注': 'x', '空列': ''},
         'cell_changes': [{'column': '备注', 'old_value': 'old, 1', 'new_value': 'new, 2'}]},
        {'row_number': 9, 'status': 'modified',
         'data': {'id': '8', '名字': 'ZZ', '备注': 'y', '空列': ''},
         'cell_changes': [{'column': '名字', 'old_value': 'C', 'new_value': 'ZZ'}]},
    ],
}


def _render(state=None):
    return _run({'op': 'render', 'sheet': SHEET, 'state': state})


def _filter(state):
    return _run({'op': 'filter', 'sheet': SHEET, 'state': state})


# --------------------------------------------------------------------------
# (a) 单行表头 + 列字母降级为小字
# --------------------------------------------------------------------------


class TestSingleRowHeader:
    def test_the_head_is_one_row_with_letter_and_field_name(self):
        html = _render()['html']
        thead = re.search(r'<thead>.*?</thead>', html, re.S)
        assert thead, '渲染结果里没有表头'
        assert thead.group(0).count('<tr') == 1, (
            '表头还是两行 —— 字母与字段名分成两条 tr 的写法已经合并成一行了')
        assert 'excel-header-row' not in html and 'excel-field-row' not in html, (
            '两行表头的 class 还在（excel-header-row / excel-field-row）')

        assert '<th scope="col" class="excel-row-header">行号</th>' in html, (
            '行号列表头不再是 `<th scope="col" class="excel-row-header">行号</th>`')

        # 列字母降级成小字、字段名照旧；被「隐藏本页空列」去掉的列不在表头里
        letters = re.findall(r'<div class="excel-column-id">([^<]*)</div>', html)
        names = re.findall(r'<div class="excel-bold-text">([^<]*)</div>', html)
        assert letters == ['A', 'B', 'C'], f'列字母是 {letters}，应为 A/B/C'
        assert names == ['id', '名字', '备注'], f'字段名是 {names}'

    def test_the_field_name_is_repeated_in_the_title_attribute(self):
        """列宽被压窄时字段名会被省略号截掉，title 是唯一的兜底。"""
        html = _render()['html']
        assert 'title="名字"' in html and 'title="id"' in html, '表头没有把字段名放进 title'

    def test_column_letters_keep_the_original_column_order(self):
        """隐藏中间的列之后，剩下的列仍然报它在 Excel 里的真实列号。"""
        sheet = {
            'headers': ['a', 'b', 'c', 'd'],
            'rows': [
                {'row_number': 2, 'status': 'added',
                 'data': {'a': '1', 'b': '', 'c': '3', 'd': '4'}},
            ],
        }
        html = _run({'op': 'render', 'sheet': sheet})['html']
        letters = re.findall(r'<div class="excel-column-id">([^<]*)</div>', html)
        assert letters == ['A', 'C', 'D'], (
            f'列字母是 {letters} —— 该列在 Excel 里是 C 列，隐藏 B 列不能让它变成 B')

    def test_untrusted_headers_are_escaped(self):
        """表头来自 Excel 第一行（不可信）：title 属性与文本都要转义。"""
        nasty = '<img src=x onerror="window.__qa=1">'
        sheet = {
            'headers': [nasty, 'a"b'],
            'rows': [{'row_number': 2, 'status': 'added', 'data': {nasty: 'v', 'a"b': 'w'}}],
        }
        html = _run({'op': 'render', 'sheet': sheet})['html']
        assert not re.search(r'<img', html), '表头里的 <img> 被当成了真标签'
        assert '&lt;img src=x onerror=&quot;window.__qa=1&quot;&gt;' in html, (
            '表头文本没有被转义')
        assert 'title="a&quot;b"' in html, 'title 属性里的 `"` 逃出了属性'


# --------------------------------------------------------------------------
# (b) 分段标题带计数
# --------------------------------------------------------------------------


class TestSectionHeaderCounts:
    def test_counts_follow_the_rendered_rows(self):
        html = _render()['html']
        # 删除 1 行、新增 1 行、变更 2 行 2 处单元格
        assert re.search(r'变更行数据：<span class="excel-group-header__count">2 行 · 2 处单元格</span>', html), (
            '变更段没有给出「行数 · 单元格处数」')
        assert re.search(r'删除行数据：<span class="excel-group-header__count">1 行</span>', html), (
            '删除段应只给行数')
        assert re.search(r'新增行数据：<span class="excel-group-header__count">1 行</span>', html), (
            '新增段应只给行数')

    def test_the_label_still_starts_the_cell(self):
        """计数只能挂在标签之后 —— 前缀「标签：」是别处的正则锚点。"""
        html = _render()['html']
        for label in ('删除行数据', '新增行数据', '变更行数据'):
            match = re.search(r'colspan="(\d+)"[^>]*>' + label + '：', html)
            assert match, f'「{label}：」不再是段标题的开头'

    def test_counts_shrink_with_the_filter(self):
        """计数要跟着筛选结果变，否则「3 行」里有一半是看不见的行。"""
        html = _filter({'filter': 'new, 2'})['html']
        assert '变更行数据：' in html
        assert re.search(r'变更行数据：<span class="excel-group-header__count">1 行 · 1 处单元格</span>', html), (
            '筛选之后计数没有跟着变')
        assert '删除行数据' not in html and '新增行数据' not in html, (
            '没有命中行的段连标题一起消失了，这里却还在')


# --------------------------------------------------------------------------
# (c) 工具栏：按列筛选 / 只看变更列 / 隐藏本页空列
# --------------------------------------------------------------------------


class TestToolbar:
    def test_the_dom_contract(self):
        html = _render()['html']
        for cls in ('excel-diff-toolbar', 'excel-diff-toolbar__search', 'excel-diff-search-input',
                    'excel-diff-search-column', 'excel-diff-search-clear', 'excel-diff-match-count',
                    'excel-diff-toolbar__views', 'excel-diff-toggle', 'excel-diff-hidden-note'):
            assert cls in html, f'工具栏缺少 {cls}'
        assert '<option value="">全部列</option>' in html, '下拉框缺少「全部列」'
        # 每个列名一个 option（含被隐藏的列：隐藏是视图，不是数据）
        for name in SHEET['headers']:
            assert f'<option value="{name}"' in html, f'下拉框里没有列 {name}'
        assert html.index('excel-diff-toolbar') < html.index('excel-table-wrapper'), (
            '工具栏应该在表格上方')

    def test_toggle_defaults(self):
        html = _render()['html']
        assert 'data-toggle="only-changed" aria-pressed="false"' in html, '「只看变更列」默认应是关闭'
        assert 'data-toggle="hide-empty" aria-pressed="true"' in html, '「隐藏本页空列」默认应是开启'
        assert '已隐藏 1 列' in html, '没有报出被隐藏的空列数（本例是 `空列` 这一列）'

    def test_no_hidden_columns_clears_the_note(self):
        html = _render({'hideEmpty': False})['html']
        assert 'class="excel-diff-hidden-note" role="status"></span>' in html, (
            '没有隐藏任何列时，隐藏说明应该是空的')

    def test_filter_is_case_insensitive_and_matches_the_raw_value(self):
        # `A` 是新增行里的名字，用**小写**筛选词也要命中（大小写不敏感）
        assert _filter({'filter': 'a'})['rows'] == [2]
        # `ZZ` 是修改行的新值（表体上印着），用小写筛选词反向再验一次
        assert _filter({'filter': 'zz'})['rows'] == [9], (
            '修改行的**新值**在表体上印着，必须能搜到')
        assert _filter({'filter': 'old, 1'})['rows'] == [5], (
            '修改行的**旧值**同样印在表体上，必须能搜到')

    def test_filter_limited_to_one_column(self):
        # `备注` 里只有 x / y / new, 2；`名字` 里是 A/E/B/C/D
        assert _filter({'filter': 'x'})['rows'] == [5]
        assert _filter({'filter': 'x', 'column': '名字'})['rows'] == [], (
            '限定列之后不该再去比别的列')
        assert _filter({'filter': 'A', 'column': '名字'})['rows'] == [2]

    def test_filter_does_not_trim_the_needle(self):
        """与展示层同一口径：表里怎么写就怎么比，替用户吃掉空格只会搜不到。"""
        assert _filter({'filter': 'zz'})['rows'] == [9]
        assert _filter({'filter': 'zz '})['rows'] == [], '筛选词被 trim 了'

    def test_no_match_shows_the_note_with_the_needle(self):
        result = _filter({'filter': 'zzz'})
        html = result['html']
        assert 'excel-diff-empty-result' in html, '一行都不剩时没有给出说明'
        assert '没有匹配「zzz」的行' in html, '说明里没有回显用户输入的筛选词'
        assert '命中 0 行' in html, '命中数没有报到 0'
        assert 'excel-row-group-header' not in html, '0 命中时还在渲染段标题'
        assert not re.search(r'class="excel-row-number', html), '0 命中时还在渲染数据行'

    def test_match_count_reflects_the_filter(self):
        assert '命中 1 行' in _filter({'filter': 'new, 2'})['html']
        # `1` 命中两行（新增行的 id=100、修改行的旧值 `old, 1`）
        assert _filter({'filter': '1'})['rows'] == [2, 5]
        assert '命中 2 行' in _filter({'filter': '1'})['html']
        assert '命中 1 行' in _filter({'filter': '1', 'column': 'id'})['html'], (
            '命中数没有跟着限定列变化')

    def test_match_count_is_empty_without_a_filter(self):
        html = _render()['html']
        assert 'class="excel-diff-match-count" role="status" aria-live="polite"></span>' in html, (
            '没有筛选时不该冒出一个「命中 0 行」')

    def test_only_changed_columns_keeps_changed_columns_and_row_numbers(self):
        """口径：单元格级变更的列 ∪ 整行增删行里有内容的列；行号列永远保留。

        行号列不是数据列（它单独一格、不参与筛选与隐藏），所以「保留」在这里表现为
        「它不受两个开关影响」——`resolveVisibleColumns` 只决定数据列。
        """
        result = _filter({'onlyChanged': True, 'hideEmpty': False})
        assert result['columnNames'] == ['id', '名字', '备注'], (
            f'只看变更列之后留下的是 {result["columnNames"]}：'
            f'`备注`/`名字` 有单元格级变更，`id` 出现在整行增删的行里，`空列` 哪里都没有内容')
        assert result['html'].count('<th scope="col" class="excel-row-header">行号</th>') == 1, (
            '行号列被「只看变更列」弄丢了')

    def test_hiding_empty_columns_uses_the_rows_being_rendered(self):
        """「本页空列」= 本次要渲染的这些行里所有单元格都为空的列。"""
        # 全表：`空列` 四行全空 → 隐藏
        assert _filter({'hideEmpty': True})['columnNames'] == ['id', '名字', '备注']
        # 只渲染新增行（备注也是空的）→ 连 `备注` 一起隐藏
        assert _filter({'hideEmpty': True, 'filter': '100', 'column': 'id'})['columnNames'] == ['id', '名字']

    def test_a_deleted_column_is_never_hidden_as_empty(self):
        """**整列被删除的列不许被「隐藏本页空列」去掉。**

        线上实例（用户反馈）：`AA随机类型` / `AA随机类型.1` 两列在本版本被删掉，打开
        「隐藏本页空列」后它们整列消失，于是**看不到这次改动**。

        形态是这样的（见 services/diff_service.py 的 `_detailed_dataframe_comparison`）：
        被删掉的列仍然留在表里（否则旧值没地方显示），但当前版本那一份被
        `reindex(fill_value='')` 填成了空串 —— 于是 `row.data[列]` 全是空，
        值只存在于 `cell_changes` 的 `old_value` 里。空列判据只看 `data` 的话，
        这一列「看起来」就是空的。

        口径：**改前、改后都没有内容**才算空列。一侧为空不是空。
        """
        sheet = {
            'headers': ['id', '名字', 'AA随机类型'],
            'rows': [
                # 被删除的列：旧值在 cell_changes 里，data 那侧是空串
                {'row_number': 2, 'status': 'modified', 'data': {'id': '1', '名字': '剑', 'AA随机类型': ''},
                 'cell_changes': [{'column': 'AA随机类型', 'old_value': '类型甲', 'new_value': ''}]},
                {'row_number': 3, 'status': 'modified', 'data': {'id': '2', '名字': '盾', 'AA随机类型': ''},
                 'cell_changes': [{'column': 'AA随机类型', 'old_value': '类型乙', 'new_value': ''}]},
                # 真正没内容的列：改前改后都空
                {'row_number': 4, 'status': 'modified', 'data': {'id': '3', '名字': '弓', 'AA随机类型': ''},
                 'cell_changes': [{'column': 'AA随机类型', 'old_value': '', 'new_value': ''}]},
            ],
        }
        result = _run({'op': 'filter', 'sheet': sheet, 'state': {'hideEmpty': True}})
        assert 'AA随机类型' in result['columnNames'], (
            f'整列被删除的列被当成空列隐藏了：{result["columnNames"]}。'
            f'它的旧值在 cell_changes.old_value 里，不在 row.data 里')
        assert '类型甲' in result['html'], '被删除列的内容没有渲染出来'
        # 反向自检：这次**没有**任何列该被隐藏（三列的改前/改后都有内容），
        # 所以 hiddenCount 必须是 0 —— 否则说明还有别的列被误伤。
        assert result['columns']['hiddenCount'] == 0, result['columns']

    def test_a_column_cleared_by_the_change_is_kept(self):
        """**修改后变空的列**同样要留着（没有列级变更那种信号可依赖）。

        与「整列删除」不同，这一列两侧都存在（`header_changes` 里什么都不会有），
        只是每一行的值都被清空了。老判据只看 `row.data`（= 改后）也会把它当空列去掉。
        """
        sheet = {
            'headers': ['id', '旧备注'],
            'rows': [
                {'row_number': 2, 'status': 'modified', 'data': {'id': '1', '旧备注': ''},
                 'cell_changes': [{'column': '旧备注', 'old_value': '待清理', 'new_value': ''}]},
                {'row_number': 3, 'status': 'added', 'data': {'id': '2', '旧备注': ''}},
            ],
        }
        result = _run({'op': 'filter', 'sheet': sheet, 'state': {'hideEmpty': True}})
        assert '旧备注' in result['columnNames'], result['columnNames']
        assert '待清理' in result['html'], '被清空那一列的原值没有渲染出来'

    def test_a_truly_empty_column_is_still_hidden(self):
        """反向自检：两侧都没内容的列**必须**仍然被隐藏 —— 这条规矩不能被放宽成
        「隐藏空列不再隐藏任何东西」。"""
        sheet = {
            'headers': ['id', '全空'],
            'rows': [
                {'row_number': 2, 'status': 'modified', 'data': {'id': '1', '全空': ''},
                 'cell_changes': [{'column': 'id', 'old_value': '0', 'new_value': '1'}]},
                {'row_number': 3, 'status': 'modified', 'data': {'id': '2', '全空': ''},
                 'cell_changes': [{'column': 'id', 'old_value': '1', 'new_value': '2'}]},
            ],
        }
        result = _run({'op': 'filter', 'sheet': sheet, 'state': {'hideEmpty': True}})
        assert result['columnNames'] == ['id'], result['columnNames']
        assert result['columns']['hiddenCount'] == 1, result['columns']

    def test_a_literal_null_text_is_not_an_empty_cell(self):
        """文本 `null` 不是空 —— 与展示层同一条口径（见 test_excel_literal_fidelity.py）。"""
        sheet = {
            'headers': ['a', 'b'],
            'rows': [
                {'row_number': 2, 'status': 'added', 'data': {'a': 'null', 'b': 'x'}},
                {'row_number': 3, 'status': 'added', 'data': {'a': None, 'b': 'y'}},
            ],
        }
        html = _run({'op': 'render', 'sheet': sheet})['html']
        assert 'title="a"' in html, '文本 null 被当成了空列'
        assert 'class="excel-diff-hidden-note" role="status"></span>' in html, (
            '两列都有内容，隐藏说明应该是空的')
        assert re.search(r'class="excel-cell-inner">null<', html), '文本 null 没有渲染出来'


# --------------------------------------------------------------------------
# (d) 大表分批路径仍然走共享实现
# --------------------------------------------------------------------------


class TestBatchingStillUsesTheSharedImplementation:
    def test_splitting_across_batches_changes_nothing(self):
        """分批切的是「渲染计划」的单元：分批拼出来的表体与一次性拼出来的逐字节相同。

        如果按原始行切，每一批都会重新分段，段标题会重复出现（`变更行数据：`
        出现 N 次），这条就会红。
        """
        result = _run({'op': 'batch', 'sheet': SHEET})
        assert result['batched'] == result['oneShot'], (
            '分批渲染的表体与一次性渲染不一致 —— 段标题可能重复了，或者行被切坏了')
        assert result['unitKinds'].count('group') == 3, (
            f'渲染计划里的单元是 {result["unitKinds"]}，三段各应有一个段标题')

    def test_the_batched_paths_head_is_the_same_head(self):
        """分批路径的表头与一次性渲染的表头必须是同一段 HTML（同一次实现）。"""
        result = _run({'op': 'batch', 'sheet': SHEET})
        head = re.search(r'<thead>.*?</thead>', result['full'], re.S).group(0)
        assert re.search(r'<thead>.*?</thead>', result['headOpen'], re.S).group(0) == head, (
            '分批路径与一次性渲染的表头不一样 —— 两条路径又各自长出了一份表头')
        assert result['headOpen'].endswith('<tbody>'), (
            '分批路径的开场部分要以 <tbody> 开标签结尾，否则每一批无处可加')
        assert result['headOpen'].startswith('<div class="excel-table-wrapper"><table class="excel-diff-table">')

    def test_the_commit_page_still_batches_through_the_module(self):
        """提交页 >100 行的路径：仍在分批，且分批用的是共享实现。"""
        with open(COMMIT_TEMPLATE, encoding='utf-8') as handle:
            source = handle.read()
        assert 'ExcelDiffTable.tableHeadHtml(' in source, '分批路径没有用共享实现的表头'
        assert 'ExcelDiffTable.tableBodyHtml(' in source, '分批路径没有用共享实现的行渲染'
        assert 'ExcelDiffTable.resolveView(' in source, (
            '分批路径没有从共享实现取「本帧的列集合与渲染计划」—— 它自己又算了一份')
        assert re.search(r'innerHTML \+= batchHtml', source), '分批追加的写法不见了（一次性塞进去会卡）'
        assert re.search(r'setTimeout\(renderNextBatch', source), (
            '分批之间不再让出主线程 —— 大表会把页面卡住')


# --------------------------------------------------------------------------
# (e) 表头块（物理第 2..N 行）：单独的 HeaderRowsBlock
# --------------------------------------------------------------------------


def _sheet_with_header_rows(header_rows, rows=None, headers=None):
    sheet = {
        'headers': headers or ['id', '名字', '备注'],
        'rows': rows if rows is not None else [
            {'row_number': 5, 'status': 'modified', 'data': {'id': '1', '名字': 'B', '备注': 'y'},
             'cell_changes': [{'column': '名字', 'old_value': 'A', 'new_value': 'B'}]},
        ],
        'header_rows': header_rows,
        'header_stats': {'total_rows_current': 2, 'total_rows_previous': 2,
                         'added': 0, 'removed': 0, 'modified': 0},
    }
    return sheet


HEADER_ROWS_CLEAN = [
    {'row_number': 2, 'status': 'unchanged', 'data': {'id': '编号', '名字': '名称', '备注': '备注'}},
    {'row_number': 3, 'status': 'unchanged', 'data': {'id': 'ID', '名字': 'Name', '备注': 'Desc'}},
]

HEADER_ROWS_CHANGED = [
    {'row_number': 2, 'status': 'modified', 'data': {'id': '编号', '名字': '名字', '备注': '备注'},
     'cell_changes': [{'column': '名字', 'old_value': '名称', 'new_value': '名字'}]},
    {'row_number': 3, 'status': 'unchanged', 'data': {'id': 'ID', '名字': 'Name', '备注': 'Desc'}},
]


class TestHeaderRowsBlock:
    def test_a_changed_header_row_is_rendered_above_the_data_sections(self):
        sheet = _sheet_with_header_rows(HEADER_ROWS_CHANGED)
        html = _run({'op': 'render', 'sheet': sheet})['html']

        assert '表头行数据：' in html, '改了表头行，却没有任何「表头行数据」这一段'
        assert html.index('表头行数据：') < html.index('变更行数据：'), (
            '表头块排在了数据段后面 —— 它是这张表最上面的一段')
        # 改动行照旧是「改前 / 改后」两行 + 单元格高亮（复用同一套行渲染）
        assert 'excel-row-modified-old' in html and 'excel-row-modified-new' in html
        assert '名称' in html and '名字' in html, '表头行的改前/改后取值没有渲染出来'
        # 没改动的那一行也铺开（有改动时给出上下文），且标注是第几行
        assert re.search(r'class="excel-row-number[^"]*">3<', html), '表头第 3 行没有渲染'

    def test_a_clean_header_block_collapses_to_one_line(self):
        """表头块没改动时只留一行说明：它每张表都常驻，铺开会把数据行挤下去。"""
        sheet = _sheet_with_header_rows(HEADER_ROWS_CLEAN)
        html = _run({'op': 'render', 'sheet': sheet})['html']

        assert '表头 2 行（第 2–3 行） · 无改动' in html, (
            f'没有改动时没有给出表头块的说明：{html[:400]}')
        assert '表头行数据：' in html, '表头块的段标题不见了（看不到这个配置生效了）'
        assert 'excel-row-unchanged' not in html, '没有改动却把表头行逐行铺开了'

    def test_without_the_configuration_nothing_changes(self):
        """没配「表头行数」的仓库（载荷里没有 header_rows）：渲染结果与改前逐字相同。"""
        sheet = {
            'headers': ['id', '名字'],
            'rows': [{'row_number': 2, 'status': 'added', 'data': {'id': '1', '名字': 'A'}}],
        }
        html = _run({'op': 'render', 'sheet': sheet})['html']
        assert '表头行数据' not in html and 'excel-row-note' not in html

    def test_a_header_only_change_is_not_reported_as_no_changes(self):
        """**只改表头**时 rows 是空的 —— 不能显示成「该工作表没有变更行」。"""
        sheet = _sheet_with_header_rows(HEADER_ROWS_CHANGED, rows=[])
        html = _run({'op': 'render', 'sheet': sheet})['html']

        assert '该工作表没有变更行' not in html, (
            '这次提交只改了表头，页面却说没有变更行 —— 评审者会直接放过')
        assert '表头行数据：' in html
        assert 'excel-row-modified-old' in html, '表头行的改动没有渲染出来'

    def test_the_header_block_is_not_filtered_away(self):
        """表头块是这张表的上下文（与 <thead> 同一性质），不受搜索筛选影响。

        筛选词只命中一行数据（`B`），表头块的两行照旧全在 —— 包括不匹配的那一行
        （第 3 行的 `ID`/`Name`）：它给的是「改动落在哪一行表头」的上下文。
        """
        sheet = _sheet_with_header_rows(HEADER_ROWS_CHANGED)
        html = _run({'op': 'render', 'sheet': sheet, 'state': {'filter': 'B'}})['html']
        assert '表头行数据：' in html, '筛掉数据行之后表头块也被筛掉了'
        assert re.search(r'class="excel-row-number[^"]*">3<', html), (
            '表头第 3 行（不匹配筛选词）不见了 —— 表头块不该跟着筛选走')

    def test_a_filter_that_matches_nothing_still_says_so(self):
        """反向：筛选到一行不剩时给的是「没有匹配的行」说明，而不是一张只剩表头的表。

        （表头块此时一起让位 —— 用户显式筛掉了所有数据行，这时「一张像表格的东西」
        比一句明确的说明更容易被误读成「有结果」。）
        """
        sheet = _sheet_with_header_rows(HEADER_ROWS_CHANGED)
        html = _run({'op': 'render', 'sheet': sheet,
                     'state': {'filter': 'zzz-nothing'}})['html']
        assert '没有匹配「zzz-nothing」的行' in html
        assert '表头行数据：' not in html

    def test_a_column_that_only_the_header_block_fills_is_not_hidden(self):
        """某一列只有表头里有内容时，它不能被「隐藏本页空列」藏掉 ——
        那条表头变更正是这次提交唯一要看的东西。"""
        sheet = _sheet_with_header_rows(
            [{'row_number': 2, 'status': 'modified',
              'data': {'id': '编号', '名字': '名称', '备注': 'notes'},
              'cell_changes': [{'column': '备注', 'old_value': '', 'new_value': 'notes'}]}],
            rows=[{'row_number': 5, 'status': 'modified', 'data': {'id': '1', '名字': 'B', '备注': ''},
                   'cell_changes': [{'column': '名字', 'old_value': 'A', 'new_value': 'B'}]}],
        )
        result = _run({'op': 'filter', 'sheet': sheet, 'state': {'hideEmpty': True}})
        assert '备注' in result['columnNames'], (
            f'只有表头里有内容的列被当成空列隐藏了：{result["columnNames"]}')
        assert 'notes' in result['html'], '表头那一列的新值没有渲染出来'

    def test_the_batched_path_keeps_the_header_block_once(self):
        """分批路径（提交页 >100 行）与一次性渲染的表体逐字节相同，表头块只出现一次。"""
        sheet = _sheet_with_header_rows(HEADER_ROWS_CHANGED, rows=SHEET['rows'])
        result = _run({'op': 'batchview', 'sheet': sheet})
        assert result['batched'] == result['oneShot'], (
            '分批渲染与一次性渲染不一致 —— 表头块可能被每一批都渲染了一遍')
        assert result['oneShot'].count('表头行数据：') == 1, (
            f'表头块的段标题出现了 {result["oneShot"].count("表头行数据：")} 次')
        assert result['unitKinds'][0] == 'group', (
            f'渲染计划的第一个单元不是表头块的段标题：{result["unitKinds"]}')


# --------------------------------------------------------------------------
# 事件绑定：不用内联 onclick（表名/单元格都是不可信数据）
# --------------------------------------------------------------------------


def test_events_are_delegated_and_never_inline():
    with open(SHARED_MODULE, encoding='utf-8') as handle:
        source = handle.read()
    assert "addEventListener('input'" in source and "addEventListener('click'" in source, (
        '工具栏事件没有走 addEventListener')
    assert not re.search(r'<[^>]*\son[a-z]+\s*=', source), (
        '共享模块拼出的 HTML 里出现了内联事件属性 —— innerHTML 里的 on* 会被编译执行')
