# -*- coding: utf-8 -*-
"""工作表级变更（整张表被新增 / 被删除）必须看得见，且不能被说成「没有变更」。

## 线上反馈（同一个文件的三处症状）

`config/奖励模式_CfgRewardMode.xlsx` 在一周里被建了删、删了建（git 侧的操作序列
是 A→D→A→M→M，最终**存在**），于是：

1. **周版本列表把它标成红色的「删除文件」** —— 旧口径是「操作序列里出现过 D」，
   而它的 diff 页比对窗口首末两版，显示的是新增内容。列表与页面互相打脸。
   （口径已改成跟最终状态走：见 `resolve_primary_operation`。）
2. **提交页 5974（删除空表）/ 5975（新增空表）什么都看不到**：载荷在表上带了
   `operation` / `message`（「工作表 "Sheet1" 已被删除」），但两张表的渲染器
   从不输出它。删掉的正好是一张空表时 rows 为空，页面于是只剩一张没有列的
   空表格 + 一句「平台未检测出变更」—— 与载荷说的正好相反。
3. **周版本单文件页整片空白**：`hasChanges` 只看内容行，一份「只有工作表级
   增删、没有内容行变更」的载荷（线上 `90_新手关卡流程.xlsx`、
   `91_专项教学关.xlsx` 就是）里一张「有变更」的表都没有，选中表的名字保持
   null，正文区于是不渲染任何东西。

## 这些测试各钉什么

* `TestNoticeIsIdenticalInBothTemplates`：两个模板里的同名实现必须逐字节一致
  （它们是独立演化的两份拷贝，历史上就漂移过）。
* `TestSheetOperationNotice`：用 node 真跑两边的实现 —— 有 operation 就有提示、
  文案取载荷的 message、缺 message 时有兜底、表名/文案里的 HTML 被转义。
* `TestWeeklySheetSelection`：没有一张表「有变更」时也必须选中一张（回归 3），
  URL 指定的表优先、其次第一张有变更的。
* `TestSheetOperationCounts`：`sheetHasChanges` 认工作表级增删（回归 3 的
  根因）；`countSheetOperations` 让提交页不再说「平台未检测出变更」（回归 2）。
* `TestInlineErrorPayloadShowsReason`：后端取不到差异时的 message/detail 必须
  透给评审者（与代码 diff 的 error 分支同一口径）。
"""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = shutil.which('node')

COMMIT_TEMPLATE = os.path.join(PROJECT_ROOT, 'templates', 'commit_diff.html')
WEEKLY_TEMPLATE = os.path.join(PROJECT_ROOT, 'templates', 'weekly_version_full_diff.html')
MERGE_TEMPLATE = os.path.join(PROJECT_ROOT, 'templates', 'merge_diff.html')

# 三张渲染 Excel diff 的页面（提交页 / 周版本单文件页 / 多版本合并页）。
# 表级提示这几段是三份拷贝，独立演化过 —— 所以要求逐字节一致。
TEMPLATES = (COMMIT_TEMPLATE, WEEKLY_TEMPLATE, MERGE_TEMPLATE)
SHARED_FUNCS = ('sheetHasChanges', 'sheetOperationHtml', 'headerChangesHtml', 'sheetNoticesHtml')
# 选中哪张表的规则（周版本页与合并页各有，提交页有自己更复杂的一套）
PICKER = 'pickActiveSheet'
PICKER_TEMPLATES = (WEEKLY_TEMPLATE, MERGE_TEMPLATE)
# 转义函数各页一份、实现不同（有的走 textContent → innerHTML、有的是纯字符串替换），
# 所以不能要求逐字节一致；跑模板 JS 时统一用周版本页那份纯函数。
ESCAPER = 'escapeHtml'
# 提交页的计数函数（判断「平台未检测出变更」能不能说）
COMMIT_ONLY_FUNCS = ('countSheetOperations', 'countHeaderChanges')


def _extract(path, name):
    """按大括号配对抠出一个 function 声明（测试要跑的是真实现，不是复刻）。"""
    with open(path, encoding='utf-8') as handle:
        text = handle.read()
    match = re.search(r'function %s\s*\(' % re.escape(name), text)
    assert match, f'{os.path.basename(path)}：找不到 {name}'
    depth = 0
    for index in range(match.start(), len(text)):
        if text[index] == '{':
            depth += 1
        elif text[index] == '}':
            depth -= 1
            if depth == 0:
                return text[match.start():index + 1]
    raise AssertionError(f'{os.path.basename(path)}：{name} 的大括号不配对')


HARNESS = '''
const request = JSON.parse(process.argv[2]);
const out = {};
if (request.op === 'notices') {
    out.html = sheetNoticesHtml(request.sheet);
    out.operationOnly = sheetOperationHtml(request.sheet);
}
if (request.op === 'hasChanges') {
    out.value = sheetHasChanges(request.sheet);
}
if (request.op === 'active') {
    out.value = pickActiveSheet(request.sheets, request.urlSheet);
}
if (request.op === 'count') {
    out.value = countSheetOperations(request.diffData);
}
if (request.op === 'countHeaders') {
    out.value = countHeaderChanges(request.diffData);
}
process.stdout.write(JSON.stringify(out));
'''


def _run(path, request, funcs):
    if NODE is None:
        pytest.skip('本机没有 node，跳过（这是唯一能真跑模板 JS 的方式）')
    with open(path, encoding='utf-8') as handle:
        assert re.search(r'function %s\s*\(' % ESCAPER, handle.read()), (
            f'{os.path.basename(path)} 里没有 {ESCAPER} —— 提示文案就没人转义了'
        )
    # 转义函数用周版本页那份纯字符串实现（提交页那份走 DOM，node 里没有 document）
    script = '\n'.join([_extract(WEEKLY_TEMPLATE, ESCAPER)]
                       + [_extract(path, name) for name in funcs]) + HARNESS
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as handle:
        handle.write(script)
        temp_path = handle.name
    try:
        proc = subprocess.run([NODE, temp_path, json.dumps(request)],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, f'node 跑失败：{proc.stderr[:500]}'
        return json.loads(proc.stdout)
    finally:
        os.unlink(temp_path)


def _notices(path, sheet):
    return _run(path, {'op': 'notices', 'sheet': sheet}, SHARED_FUNCS)['html']


def _visible_text(markup):
    """提示块里**看得见**的文字（去掉标签、还原实体），用来断言文案。"""
    return html.unescape(re.sub(r'<[^>]+>', '', markup))


# 线上 5975 的形态：新增一张空表
EMPTY_ADDED_SHEET = {
    'headers': [], 'operation': 'added', 'message': '新增工作表 "Sheet1"',
    'rows': [], 'stats': {'added': 0, 'modified': 0, 'removed': 0},
}
# 线上 5974 的形态：删除一张空表
EMPTY_DELETED_SHEET = {
    'headers': [], 'operation': 'deleted', 'message': '工作表 "Sheet1" 已被删除',
    'rows': [], 'stats': {'added': 0, 'modified': 0, 'removed': 0},
}


class TestNoticeIsIdenticalInBothTemplates:
    @pytest.mark.parametrize('name', SHARED_FUNCS + (PICKER,))
    def test_shared_helpers_are_byte_identical(self, name):
        paths = PICKER_TEMPLATES if name == PICKER else TEMPLATES
        bodies = {os.path.basename(p): _extract(p, name) for p in paths}
        assert len(set(bodies.values())) == 1, (
            f'{name} 在几个模板里已经不一致了 —— 它们是同一段代码的拷贝，'
            f'改成一样再提交：{ {k: len(v) for k, v in bodies.items()} }'
        )

    # 真正拼 HTML 的那两个（sheetHasChanges 是判断函数，sheetNoticesHtml 只是拼接）
    TEXT_BUILDERS = ('sheetOperationHtml', 'headerChangesHtml')

    @pytest.mark.parametrize('name', TEXT_BUILDERS)
    @pytest.mark.parametrize('template', [COMMIT_TEMPLATE, WEEKLY_TEMPLATE])
    def test_dynamic_text_goes_through_the_escaper(self, template, name):
        body = _extract(template, name)
        assert 'escapeHtml(' in body, (
            f'{os.path.basename(template)} 的 {name} 直接拼了动态文本，没过 escapeHtml'
        )


# 正文渲染入口：三张页面各有一份，都必须把表级提示拼进去
RENDERER_MARKERS = {
    COMMIT_TEMPLATE: 'function sheetBodyHtml(',
    WEEKLY_TEMPLATE: 'window.showWeeklyExcelSheet = function',
    MERGE_TEMPLATE: 'function showMergedExcelSheet(',
}
# 标签生成函数（用同一套选中规则）
TAB_BUILDER_MARKERS = {
    WEEKLY_TEMPLATE: 'window.generateWeeklyExcelTabs = function',
    MERGE_TEMPLATE: 'function generateMergedExcelTabs',
}


def _block_at(path, marker):
    """从 marker 之后的第一个 { 起按大括号配对抠出一整段（用于 `fn = function(){}` 这种写法）。"""
    with open(path, encoding='utf-8') as handle:
        text = handle.read()
    start = text.index(marker)
    depth = 0
    for index in range(text.index('{', start), len(text)):
        if text[index] == '{':
            depth += 1
        elif text[index] == '}':
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise AssertionError(f'{os.path.basename(path)}：{marker} 的大括号不配对')


class TestRenderersCallTheSharedHelpers:
    """正文渲染入口必须真的调用这几个共用函数（只抠函数出来测，测不到调用点）。"""

    NOTICE_REF = re.compile(r'sheetNoticesHtml|notices', re.I)

    def _content_paths(self, template):
        """渲染函数里所有「往正文写东西」的出口（正常表 / 空表 / 无数据三种）。"""
        body = _block_at(template, RENDERER_MARKERS[template])
        exprs = re.findall(r'contentContainer\.innerHTML\s*=\s*[^;]+;', body, re.S)
        exprs += re.findall(r'return\s+[^;]+;', body, re.S)
        return [e for e in exprs
                if ('该工作表' in e or 'createExcelDiffTable' in e or 'tableHtml' in e or 'alert' in e)]

    @pytest.mark.parametrize('template', TEMPLATES)
    def test_every_content_path_carries_the_sheet_notices(self, template):
        paths = self._content_paths(template)
        assert len(paths) >= 2, (
            f'{os.path.basename(template)}：只找到 {len(paths)} 个正文出口 —— 用例失效了'
        )
        missing = [p for p in paths if not self.NOTICE_REF.search(p)]
        assert not missing, (
            f'{os.path.basename(template)}：正文出口没有带上表级提示 —— '
            '工作表级新增/删除（尤其删掉的是一张空表时）就没人看得见：\n  '
            + '\n  '.join(' '.join(m.split())[:110] for m in missing)
        )

    @pytest.mark.parametrize('template', PICKER_TEMPLATES)
    def test_the_tab_builder_uses_the_picker(self, template):
        body = _block_at(template, TAB_BUILDER_MARKERS[template])
        assert 'pickActiveSheet(' in body, (
            f'{os.path.basename(template)}：标签用自己的一套选中规则 —— '
            '正文用的却是 pickActiveSheet，两者一旦不一致就会「标签选中 A、正文是 B」'
        )


# 端到端：把线上 5974/5975 的真实载荷喂给正文渲染入口，看它到底渲出了什么
#
# 表体渲染现在由共享模块（static/js/excel_diff_table.js）负责，两个入口页都调它。
# 这里用一个返回 'TABLE' 的桩顶掉它：这些用例只关心「空表 / 没有净变更时页面说了
# 什么」，桩把「表格」变成一个可判定的标记，`'TABLE' not in rendered` 就是
# 「这条路径没有再去渲染表格」。
FULL_HARNESS = '''
const ExcelDiffTable = {
    renderSheetTable: function () { return 'TABLE'; },
    mountSheetTable: function (container) { container.innerHTML = 'TABLE'; },
    toolbarHtml: function () { return ''; },
    tableHeadHtml: function () { return ''; },
    tableBodyHtml: function () { return ''; },
    resolveView: function () { return {units: [], visibleHeaders: []}; }
};
function createExcelDiffTable(sheetName, sheetData) { return 'TABLE'; }
function getModifiedColumns(sheetData) { return []; }
function changedRowsGroupedHtml(rows, headers) { return ''; }
function escapeHtmlSafe(text) { return escapeHtml(text); }
const request = JSON.parse(process.argv[2]);
const container = { innerHTML: '' };
if (request.mode === 'weekly') {
    window.showWeeklyExcelSheet(container, request.sheetName, request.sheet);
} else {
    container.innerHTML = sheetBodyHtml(request.sheetName, request.sheet);
}
process.stdout.write(JSON.stringify({html: container.innerHTML}));
'''


def _run_full(template, sheet, mode):
    if NODE is None:
        pytest.skip('本机没有 node')
    marker = RENDERER_MARKERS[template]
    script = '\n'.join(['var window = {};', _extract(WEEKLY_TEMPLATE, ESCAPER)]
                       + [_extract(template, name) for name in SHARED_FUNCS]
                       + [_block_at(template, marker)]) + FULL_HARNESS
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as handle:
        handle.write(script)
        temp_path = handle.name
    try:
        proc = subprocess.run([NODE, temp_path, json.dumps({'mode': mode, 'sheetName': 'Sheet1', 'sheet': sheet})],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, f'node 跑失败：{proc.stderr[:600]}'
        return json.loads(proc.stdout)['html']
    finally:
        os.unlink(temp_path)


# 端到端（真模块版）：把一张**有内容**的表喂进正文渲染入口，走真的共享实现。
#
# 上面那个 FULL_HARNESS 用桩顶掉表格（只关心「空表时页面说了什么」）；这里反过来 ——
# 加载真的 static/js/excel_diff_table.js，断言三个模板的入口函数**确实**把数据交给了
# 它：表头、分段标题、行都出得来。这一条专治「入口函数改了调用点却没人发现」
# （模板里调错名字/传错参数不会有任何报错，只会渲染出空壳）。
REAL_MODULE_HARNESS = '''
window.formatCellValue = function (value) {
    if (value === null || value === undefined) return '';
    if (typeof value === 'number' && isNaN(value)) return '';
    return String(value);
};
const request = JSON.parse(process.argv[2]);
const container = {
    innerHTML: '',
    addEventListener: function () {},
    querySelector: function () { return null; },
    querySelectorAll: function () { return []; }
};
if (request.mode === 'weekly') {
    window.showWeeklyExcelSheet(container, request.sheetName, request.sheet);
} else if (request.mode === 'merge') {
    showMergedExcelSheet(container, request.sheetName, request.sheet);
} else {
    container.innerHTML = sheetBodyHtml(request.sheetName, request.sheet);
}
process.stdout.write(JSON.stringify({html: container.innerHTML}));
'''


# 入口函数自己调用的、也在同一个模板里的下一层函数（提交页的入口只是把活转交给它）
EXTRA_ENTRY_FUNCS = {
    COMMIT_TEMPLATE: ('createExcelDiffTable',),
}


def _run_real_module(template, sheet, mode):
    if NODE is None:
        pytest.skip('本机没有 node')
    with open(os.path.join(PROJECT_ROOT, 'static', 'js', 'excel_diff_table.js'),
              encoding='utf-8') as handle:
        module_source = handle.read()
    marker = RENDERER_MARKERS[template]
    # `window` 就用 node 的全局对象：浏览器里 `window.X = …` 会顺带建出一个全局 X，
    # 模板里既有 `window.ExcelDiffTable.mountSheetTable(...)` 也有裸名调用
    # （`ExcelDiffTable.renderSheetTable(...)`），用真全局两种写法才都能解析。
    # 共享模块放在最前面：与浏览器的加载顺序一致（模块脚本先于页面内联脚本），
    # 也避免它被粘到上一段的尾部（`window.f = function () {}` 后面紧跟 `(` 会被
    # 当成函数调用，而不是新语句 —— ASI 不在这里插分号）。
    script = '\n'.join(['var window = globalThis;', module_source, _extract(WEEKLY_TEMPLATE, ESCAPER)]
                       + [_extract(template, name) for name in SHARED_FUNCS]
                       + [_extract(template, name) for name in EXTRA_ENTRY_FUNCS.get(template, ())]
                       + [_block_at(template, marker)]) + REAL_MODULE_HARNESS
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as handle:
        handle.write(script)
        temp_path = handle.name
    try:
        proc = subprocess.run([NODE, temp_path,
                               json.dumps({'mode': mode, 'sheetName': 'Sheet1', 'sheet': sheet})],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, f'node 跑失败：{proc.stderr[:600]}'
        return json.loads(proc.stdout)['html']
    finally:
        os.unlink(temp_path)


# 一张有变更的表：整行删除 / 整行新增 / 逐格修改各一条
SHEET_WITH_CHANGES = {
    'headers': ['id', '名字'],
    'rows': [
        {'row_number': 2, 'status': 'added', 'data': {'id': '9', '名字': '新'}},
        {'row_number': 5, 'status': 'removed', 'data': {'id': '3', '名字': '删'}},
        {'row_number': 7, 'status': 'modified', 'data': {'id': '4', '名字': '旧名'},
         'cell_changes': [{'column': '名字', 'old_value': '旧名', 'new_value': '新名'}]},
    ],
}


class TestTheRealSheetsRenderThroughTheSharedModule:
    """三个模板的入口函数 + 真的共享模块：表头 / 分段 / 行都要出得来。"""

    @pytest.mark.parametrize('template,mode', [
        (COMMIT_TEMPLATE, 'commit'),
        (WEEKLY_TEMPLATE, 'weekly'),
        (MERGE_TEMPLATE, 'merge'),
    ])
    def test_a_sheet_with_changes_renders_a_real_table(self, template, mode):
        html = _run_real_module(template, SHEET_WITH_CHANGES, mode)
        name = os.path.basename(template)

        assert '<table class="excel-diff-table">' in html, f'{name}：没有渲染出表格'
        assert '<div class="excel-table-wrapper">' in html, f'{name}：表格没有包装容器'
        assert 'excel-diff-toolbar' in html, f'{name}：没有工具栏（筛选/开关）'
        # 单行表头：字段名在 excel-bold-text 里，列字母在 excel-column-id 里
        assert '<div class="excel-bold-text">名字</div>' in html, f'{name}：表头里没有字段名'
        assert '<div class="excel-column-id">A</div>' in html, f'{name}：表头里没有列字母'
        # 分段与行
        for label in ('删除行数据', '新增行数据', '变更行数据'):
            assert f'{label}：' in html, f'{name}：没有「{label}」这一段'
        assert '<tr class="excel-row-added">' in html, f'{name}：没有渲染新增行'
        assert '<tr class="excel-row-removed">' in html, f'{name}：没有渲染删除行'
        assert 'excel-row-modified-old' in html, f'{name}：没有渲染修改行的旧值行'

    @pytest.mark.parametrize('template,mode', [
        (COMMIT_TEMPLATE, 'commit'),
        (WEEKLY_TEMPLATE, 'weekly'),
        (MERGE_TEMPLATE, 'merge'),
    ])
    def test_the_sheet_notices_still_come_first(self, template, mode):
        """表级提示仍然在表格之前 —— 整张表被新增/删除时它就是全部内容。"""
        sheet = dict(SHEET_WITH_CHANGES, operation='deleted', message='工作表 "Sheet1" 已被删除')
        html = _run_real_module(template, sheet, mode)
        assert 'excel-sheet-operation' in html, f'{os.path.basename(template)}：表级提示没了'
        assert html.index('excel-sheet-operation') < html.index('excel-table-wrapper'), (
            f'{os.path.basename(template)}：表级提示跑到了表格后面')


class TestTheRealEmptySheetPayloadsRenderSomething:
    """线上 5974/5975 的载荷原样喂进正文渲染入口：

    旧实现在这两条上渲染出一张没有列的空表格（提交页还会加一句「平台未检测出
    变更」），评审者等于什么都看不到。现在必须看得出「这张表被删了 / 被加了」。
    """

    @pytest.mark.parametrize('template,mode', [
        (COMMIT_TEMPLATE, 'commit'),
        (WEEKLY_TEMPLATE, 'weekly'),
    ])
    @pytest.mark.parametrize('sheet', [EMPTY_ADDED_SHEET, EMPTY_DELETED_SHEET])
    def test_the_body_says_what_happened(self, template, mode, sheet):
        rendered = _run_full(template, sheet, mode)
        text = _visible_text(rendered)
        assert sheet['message'] in text, (
            f'线上载荷渲染成了 {text!r} —— 看不出这张表发生了什么'
        )
        assert '没有数据行' in text, (
            f'空表时没有给出「没有数据行」的说明：{text!r}'
        )
        assert 'TABLE' not in rendered, '空表不该再去渲染表格'


class TestSheetOperationNotice:
    """工作表级新增/删除必须在正文里说出来（哪怕这张表一行内容都没有）。"""

    @pytest.mark.parametrize('template', TEMPLATES)
    @pytest.mark.parametrize('sheet', [EMPTY_ADDED_SHEET, EMPTY_DELETED_SHEET])
    def test_the_operation_message_is_rendered(self, template, sheet):
        rendered = _notices(template, sheet)
        assert sheet['message'] in _visible_text(rendered), (
            f'工作表级变更没有渲染出来：载荷说 {sheet["message"]!r}，页面拿到了 {rendered!r}\n'
            '（线上 5974/5975 就是这样：载荷说了「工作表已被删除」，页面一片空白）'
        )
        assert 'excel-sheet-operation' in rendered, '提示块缺少可定位的 class'

    @pytest.mark.parametrize('template', TEMPLATES)
    def test_added_and_deleted_use_different_styles(self, template):
        added = _notices(template, EMPTY_ADDED_SHEET)
        deleted = _notices(template, EMPTY_DELETED_SHEET)
        assert 'alert-success' in added and 'alert-warning' in deleted, (
            f'新增/删除没有区分样式：added={added!r} deleted={deleted!r}'
        )

    @pytest.mark.parametrize('template', TEMPLATES)
    def test_a_sheet_without_an_operation_has_no_notice(self, template):
        plain = {'headers': ['A'], 'rows': [{'row_number': 2, 'status': 'added', 'data': {}}]}
        assert _notices(template, plain) == '', '普通工作表不该冒出工作表级提示'

    @pytest.mark.parametrize('template', TEMPLATES)
    def test_a_missing_message_still_says_something(self, template):
        sheet = dict(EMPTY_DELETED_SHEET)
        sheet.pop('message')
        rendered = _notices(template, sheet)
        assert 'excel-sheet-operation' in rendered and '已被删除' in _visible_text(rendered), (
            f'载荷没带 message 时提示为空：{rendered!r}'
        )

    @pytest.mark.parametrize('template', TEMPLATES)
    def test_the_message_is_escaped(self, template):
        """message 由后端拼表名而来，表名来自被审核的 Excel —— 必须转义。"""
        sheet = dict(EMPTY_DELETED_SHEET)
        sheet['message'] = '<script>alert(1)</script>'
        rendered = _notices(template, sheet)
        assert '<script>' not in rendered, f'工作表级提示没转义：{rendered!r}'
        assert '&lt;script&gt;' in rendered

    @pytest.mark.parametrize('template', TEMPLATES)
    def test_header_changes_are_rendered_too(self, template):
        """列级变更提示在周版本页里原先根本没有（只有提交页有）。"""
        sheet = {
            'headers': ['id', '新列'], 'rows': [],
            'header_changes': [{'change': 'added', 'column': '新列', 'column_index': 2}],
        }
        rendered = _notices(template, sheet)
        assert '列变更' in rendered and '新增列 新列' in _visible_text(rendered), (
            f'列级变更提示没有渲染：{rendered!r}'
        )


class TestWeeklySheetSelection:
    """没有一张表「有变更」时，也必须选中一张 —— 否则正文区一片空白。"""

    @staticmethod
    def _run(sheets, url_sheet=None, template=WEEKLY_TEMPLATE):
        return _run(template,
                    {'op': 'active', 'sheets': sheets, 'urlSheet': url_sheet},
                    (PICKER,))['value']

    @pytest.mark.parametrize('template', PICKER_TEMPLATES)
    def test_the_merge_page_also_picks_a_sheet(self, template):
        sheets = [{'name': '流程', 'hasChanges': False}]
        assert self._run(sheets, template=template) == '流程'

    def test_a_payload_without_row_changes_still_shows_a_sheet(self):
        """线上 90_新手关卡流程.xlsx / 91_专项教学关.xlsx 的形态。"""
        sheets = [
            {'name': '流程', 'hasChanges': False},
            {'name': '奖励', 'hasChanges': False},
        ]
        assert self._run(sheets) == '流程', (
            '一张「有变更」的表都没有时选中了空 —— 周版本正文区会整片空白'
        )

    def test_the_url_sheet_wins_when_it_has_changes(self):
        sheets = [{'name': 'A', 'hasChanges': True}, {'name': 'B', 'hasChanges': True}]
        assert self._run(sheets, 'B') == 'B'

    def test_the_url_sheet_is_ignored_when_it_has_no_changes(self):
        sheets = [{'name': 'A', 'hasChanges': False}, {'name': 'B', 'hasChanges': True}]
        assert self._run(sheets, 'A') == 'B'

    def test_no_sheets_is_null(self):
        assert self._run([]) is None


class TestSheetOperationCounts:
    """「有变更」与「平台未检测出变更」两句判断都要认工作表级增删。"""

    @pytest.mark.parametrize('template', TEMPLATES)
    @pytest.mark.parametrize('sheet,expected', [
        (EMPTY_DELETED_SHEET, True),
        (EMPTY_ADDED_SHEET, True),
        ({'headers': [], 'rows': []}, False),
        ({'headers': ['A'], 'rows': [{'row_number': 2, 'status': 'added', 'data': {}}]}, True),
        ({'headers': ['A'], 'rows': [], 'header_changes': [{'change': 'added', 'column': 'A'}]}, True),
        (None, False),
    ])
    def test_sheet_has_changes(self, template, sheet, expected):
        got = _run(template, {'op': 'hasChanges', 'sheet': sheet},
                   ('sheetHasChanges',))['value']
        assert got is expected, f'{sheet!r} 的「有变更」判定为 {got}，应为 {expected}'

    def test_the_tab_builder_uses_the_shared_has_changes_rule(self):
        """标签生成必须调用 weeklySheetHasChanges —— 别再内联一份只看内容行的判断。

        （这个坑线上踩过两次：`generateWeeklyExcelTabs` 里一份、它调用方
        `initWeeklyExcelDiff` 里又一份，两份口径不一致时页面就会出现
        「标签选中 A、正文却是 B」或整片空白。）
        """
        with open(WEEKLY_TEMPLATE, encoding='utf-8') as handle:
            text = handle.read()
        body = text[text.index('window.generateWeeklyExcelTabs = function'):]
        body = body[:body.index('// 显示激活的sheet内容')]
        assert 'sheetHasChanges(' in body, (
            '标签/默认表的选择没有走 sheetHasChanges —— 工作表级增删会被当成'
            '「没有变更」，周版本正文区又会退回一片空白'
        )
        assert 'row.status === ' not in body, (
            '标签生成里又内联了一份「只看内容行」的变更判断'
        )

    def test_the_weekly_init_does_not_recompute_the_active_sheet(self):
        """`initWeeklyExcelDiff` 不许再自己算一份「第一张有变更的表」。"""
        with open(WEEKLY_TEMPLATE, encoding='utf-8') as handle:
            text = handle.read()
        body = text[text.index('window.initWeeklyExcelDiff = function'):]
        body = body[:body.index('function loadDiffContent()')]
        assert 'row.status === ' not in body, (
            'initWeeklyExcelDiff 里又内联了一份变更判断 —— 它算出来的「第一张有变更的'
            '表」会和标签选中的表不一致（工作表级增删时还会算不出任何表，正文一片空白）'
        )

    def test_count_sheet_operations(self):
        diff_data = {'sheets': {
            'A': {'operation': 'added'}, 'B': {'operation': 'deleted'},
            'C': {}, 'D': {'operation': 'modified'},
        }}
        assert _run(COMMIT_TEMPLATE, {'op': 'count', 'diffData': diff_data},
                    ('countSheetOperations',))['value'] == 2

    def test_the_commit_page_does_not_claim_no_changes_when_a_sheet_was_added(self):
        """载荷里明明有工作表级变更时不许说「平台未检测出变更」。"""
        with open(COMMIT_TEMPLATE, encoding='utf-8') as handle:
            text = handle.read()
        block = text[text.index('var sheetOperationCount = countSheetOperations(diffData);'):]
        head = block[:block.index('} else {')]
        assert '平台未检测出变更' in head, '找不到「未检测出变更」的判断'
        assert 'sheetOperationCount === 0' in head, (
            '「平台未检测出变更」没有把工作表级变更算进去 —— 线上 5974/5975 就是这样'
        )
        assert '工作表级变更' in block[:block.index('// 有变更时显示正常统计信息')], (
            '只有工作表级变更时没有给出正确的说明'
        )


class TestInlineErrorPayloadShowsReason:
    """后端取不到真实差异时（type='error'），原因必须透给评审者。"""

    def test_the_message_is_shown_instead_of_a_generic_error(self):
        with open(COMMIT_TEMPLATE, encoding='utf-8') as handle:
            text = handle.read()
        start = text.index("console.log('No Excel sheets data found');")
        block = text[start:start + 1600]
        assert 'diffData.message' in block or 'diffData.error' in block, (
            '内联载荷的 error/message 被丢掉了（页面只会说「Excel数据格式错误」）'
        )
        assert 'escapeHtml(reason)' in block, '透出来的原因必须转义'
        assert 'detail' in block, 'detail 也要给出来'
