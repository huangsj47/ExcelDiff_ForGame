# -*- coding: utf-8 -*-
"""工作表标签的顺序与默认选中：四张页面只有一份实现，且**不打乱工作簿原序**。

## 为什么这一条值得一个测试文件

旧实现（提交页 `displayExcelDiff` 与合并页 `generateExcelTabsForContainer` 里**逐字
复制**的两份）在「有变更」这一组里排的是：

    if (a.name === 'Sheet1') return -1;
    if (b.name === 'Sheet1') return 1;
    return a.name.localeCompare(b.name);

表名恰好叫 `Sheet1` 的那个工作簿里它看起来是对的 —— 而那只对**一个**项目成立。
别的项目（表名形如 `0_常规属性`、`道具表`、`Sheet2`……）这一组就退化成**按表名
字母序重排**：工作簿原序被打乱。

后果不是报错，而是**人机对不上**：AI 侧 `lines` 参数的语义是「第几张表」，取值来自
`services/ai/platform_provider.py` 里 `book.sheetnames` 的**工作簿顺序**。页面标签
一旦按别的规则重排，模型说的「第 2 张表」和评审者点开的第 2 个标签就是两张不同的表。
这个错位不抛异常、不打日志，只会让「模型说的现象我在那张表里找不到」变成一个谁也
说不清的悬案 —— 所以它需要一条**会红**的用例，而不是靠人眼在页面上比对。

同一段代码里还有两处同类的「某个项目的私事」，但**处理方式不同**，这个不对称是有意的：

* `!sheet.name.includes('测试配置')` 只决定**默认打开哪一张表** —— 一个便利启发式。
  换项目这个中文子串不出现，过滤退化成**恒为空操作**（看起来在起作用、实际从来没
  生效过）。平台没有「跳过某类表」的配置位，所以现在是**没有过滤**，而不是「有一份
  悄悄失效的过滤」。顺带修掉一个真实缺陷：旧的提交页在「所有有变更的表都被过滤掉」
  时不给默认表名赋值，于是一个标签都不高亮、正文区什么都不渲染。
* `sheetName.includes('召唤物')` 决定**标签能不能点** —— 那限制的是「用户能做什么」。
  删掉它会让原本打不开的表变成可点，是产品行为变更，不能在「抽象化」的口号下顺手
  做掉。所以它**保留**了，只是换了接线方式：判定作为参数传给共享模块，调用点只留
  那个中文子串（正解是按项目/仓库配置传入，本次不做）。

## 这些用例怎么保证「真的在跑被改的那段代码」

排序与选中都收进了 `static/js/excel_sheet_order.js`，页面只是调它。所以：

1. **真跑共享模块 + 真跑页面的调用点**（node + DOM stub）。只把模块抠出来测是不够的：
   页面里到底调没调它、传的参数对不对，字符串断言说不清楚 —— 而「没调」正是这里最
   可能出的错（页面上什么都不会报，只是继续按旧规则排）。
2. **静态断言一律先剥注释**。本仓库的习惯是把**被去掉的那个写法原样写进说明注释**
   （这次就引了 `a.name === 'Sheet1'` 与 `includes('测试配置')`），不剥的话说明文字会
   让「已经改干净了」的断言为假、也会让「还在用旧写法」的断言为真 —— 两个方向都会
   骗人。剥注释的做法见 `_strip_comments`：先剥 HTML 注释 `<!-- -->`，再剥 JS 的
   `//` 与 `/* */`，**字符串字面量里的 `//` 不动**（`'http://…'` 那种）。
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

ORDER_MODULE = os.path.join(PROJECT_ROOT, 'static', 'js', 'excel_sheet_order.js')
COMMIT_TEMPLATE = os.path.join(PROJECT_ROOT, 'templates', 'commit_diff.html')
MERGE_TEMPLATE = os.path.join(PROJECT_ROOT, 'templates', 'merge_diff.html')
WEEKLY_TEMPLATE = os.path.join(PROJECT_ROOT, 'templates', 'weekly_version_full_diff.html')

# 用共享模块的四个调用点（提交页 / 合并页的两处标签生成 / 周版本单文件页）
CALL_SITES = (
    (COMMIT_TEMPLATE, 'function displayExcelDiff('),
    (MERGE_TEMPLATE, 'function generateExcelTabsForContainer('),
    (MERGE_TEMPLATE, 'function generateMergedExcelTabs('),
    (WEEKLY_TEMPLATE, 'window.generateWeeklyExcelTabs = function'),
)

# 页面自己的函数：变更判定与转义（都在模板里，不重写）。`escapeHtml` 统一用周版本页
# 那份纯字符串实现 —— 提交页那份走 DOM，node 里没有 document。
COMMON_FUNCS = ('sheetHasChanges',)
EXTRA_FUNCS = {
    COMMIT_TEMPLATE: ('escapeHtmlAttribute', 'countHeaderChanges', 'countSheetOperations',
                      'isSheetNotClickable'),
    MERGE_TEMPLATE: ('escapeHtmlAttribute', 'pickActiveSheet', 'isSheetNotClickable'),
    WEEKLY_TEMPLATE: ('pickActiveSheet',),
}

# 旧实现里那个「只对一个项目成立」的中文子串（默认选中的过滤，已移除）。
SKIP_TOKEN = '测试配置'
# 「不给点」那条规则里的中文子串。**它没被移除**：那是产品约束（改变用户能做什么），
# 不是便利启发式，所以判定留在两个调用点、由共享模块作为参数接收。
DISABLED_TOKEN = '召唤物'
DISABLED_PREDICATE = 'isSheetNotClickable'
# 带这条规则的两个调用点（周版本页在 HEAD 里没有，别给它加）。
DISABLED_CALL_SITES = (COMMIT_TEMPLATE, MERGE_TEMPLATE)


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as handle:
        return handle.read()


def _strip_comments(code: str) -> str:
    """剥掉 HTML 注释与 JS 注释，**字符串字面量与正则字面量里的 `//` 不动**。

    不做这一步的后果是双向的：本仓库的注释里会原样引用被禁掉的写法，
    于是「已经改干净」的断言会假绿、「还在用旧写法」的断言会假红。

    **正则字面量必须单独认**：本仓库的转义函数里有 `/"/g`、`/'/g` 这种写法，
    只认引号的话那两个引号会被当成「字符串开始了」，于是从这里往后整段代码都被
    当成字符串、注释一根都剥不掉 —— 这条断言会静默失效（第一版就是这么假绿的，
    靠「注释里那句话还在」才被发现）。判定沿用通行做法：`/` 前面是运算符/括号/逗号
    这类位置才是正则，前面是标识符或数字就是除号。
    """
    code = re.sub(r'<!--.*?-->', ' ', code, flags=re.S)
    out: list[str] = []
    index, length = 0, len(code)
    quote = ''
    # 正则字面量的起始判定用的「前一个非空白字符」。
    # **别把 `<` / `>` 放进来**：模板里到处是 `</div>`、`</script>`，`<` 后面那个
    # `/` 会被当成正则开头，然后一路扫到下一个 `/` 才收手 —— 中间整段代码被当成
    # 正则吞掉，注释剥不干净（第一版把 `<` 放进来了，于是脚本块开头的注释全留着）。
    previous = ''
    regex_allowed_after = '(,=:[!&|?{};+-*%~^\n'
    while index < length:
        char = code[index]
        if quote:
            out.append(char)
            if char == '\\' and quote != '`':
                if index + 1 < length:
                    out.append(code[index + 1])
                    index += 2
                    continue
            elif char == quote:
                quote = ''
            index += 1
            continue
        if char in '\'"`':
            quote = char
            out.append(char)
            previous = char
            index += 1
            continue
        if char == '/' and index + 1 < length and code[index + 1] == '/':
            while index < length and code[index] != '\n':
                index += 1
            continue
        if char == '/' and index + 1 < length and code[index + 1] == '*':
            index += 2
            while index + 1 < length and not (code[index] == '*' and code[index + 1] == '/'):
                index += 1
            index += 2
            continue
        if char == '/' and regex_allowed_after.find(previous) >= 0:
            index += 1
            in_class = False
            while index < length:
                inner = code[index]
                if inner == '\\':
                    index += 2
                    continue
                if inner == '[':
                    in_class = True
                elif inner == ']':
                    in_class = False
                elif inner == '/' and not in_class:
                    index += 1
                    break
                elif inner == '\n':
                    break
                index += 1
            out.append('/')
            previous = '/'
            continue
        out.append(char)
        if not char.isspace():
            previous = char
        index += 1
    return ''.join(out)


def _extract(path: str, name: str) -> str:
    """按大括号配对抠出一个 `function name(...) {}`（要跑的是真实现，不是复刻）。"""
    text = _read(path)
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


def _block_at(path: str, marker: str) -> str:
    """从 marker 之后的第一个 `{` 起配平抠出一整段（`fn = function(){}` 这种写法）。"""
    text = _read(path)
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


def _call_site_body(call_site) -> str:
    """一个调用点的函数源码（两种写法：`function f(` 与 `window.f = function`）。"""
    path, marker = call_site
    if marker.startswith('window.'):
        return _block_at(path, marker)
    return _extract(path, marker.split('(')[0].replace('function ', ''))


# ---------------------------------------------------------------------------
#  node 驱动：共享模块 + 页面里的调用点
# ---------------------------------------------------------------------------

# 模块自己的契约：三个方法一个都不能少 —— 少一个的表现是页面里
# `ExcelSheetOrder.xxx is not a function`，而那时标签已经渲染了一半。
MODULE_CONTRACT = '''
function hasChangesFlag(sheet) { return !!(sheet && sheet.hasChanges); }
const _analysis = ExcelSheetOrder.analyzeSheets(request.names, request.sheets, hasChangesFlag);
out.analysisOrder = _analysis.map(function (sheet) { return sheet.name; });
out.originalIndex = _analysis.map(function (sheet) { return sheet.originalIndex; });
const _ordered = ExcelSheetOrder.orderSheets(_analysis);
out.names = _ordered.map(function (sheet) { return sheet.name; });
// 入参数组的顺序**不该被 orderSheets 就地改掉**：调用方可能还要按原序用那一份。
out.analysisOrderAfter = _analysis.map(function (sheet) { return sheet.name; });
out.repeatable = ExcelSheetOrder.orderSheets(_ordered)
    .map(function (sheet) { return sheet.name; });
out.default = ExcelSheetOrder.defaultSheetName(_ordered);
out.emptyDefault = ExcelSheetOrder.defaultSheetName([]);
// 不传「不给点」判定时，一律都可点击 —— 模块自己不认识任何表名规则
out.notClickableWithoutPredicate = _ordered.map(function (sheet) { return sheet.notClickable; });
const _withPredicate = ExcelSheetOrder.analyzeSheets(
    request.names, request.sheets, hasChangesFlag,
    function (name) { return name === request.names[0]; }
);
out.notClickableWithPredicate = _withPredicate.map(function (sheet) { return sheet.notClickable; });
out.api = ['analyzeSheets', 'orderSheets', 'defaultSheetName']
    .filter(function (key) { return typeof ExcelSheetOrder[key] === 'function'; });
'''

# 提交页：真跑 displayExcelDiff。它把标签拼成 HTML 字符串、塞进容器（走一个
# setTimeout），所以 setTimeout 换成立即执行；正文渲染、进度条、点击绑定都打桩 ——
# 这些用例只看**标签的顺序与哪一张是激活的**。
COMMIT_HARNESS = '''
const request = JSON.parse(process.argv[2]);
const container = {innerHTML: ''};
const document = {getElementById: function () { return container; }};
function setTimeout(callback) { callback(); return 0; }
function bindExcelSheetTabClicks() {}
function updateLoadingProgress() {}
function renderSheetsProgressively(diffData, sheets) {
    out.renderedOrder = sheets.map(function (sheet) { return sheet.name; });
}
displayExcelDiff(request.diffData);
out.tabsHtml = container.innerHTML;
'''

# 合并页：真跑 generateExcelTabsForContainer。标签容器只需要 innerHTML 与
# querySelectorAll（返回空数组，点击绑定那一圈就自然跳过）。
MERGE_HARNESS = '''
const request = JSON.parse(process.argv[2]);
const tabsContainer = {innerHTML: '', querySelectorAll: function () { return []; }};
const container = {querySelector: function () { return tabsContainer; }};
generateExcelTabsForContainer(container, request.sheets);
out.tabsHtml = tabsContainer.innerHTML;
'''

# 周版本单文件页：真跑 generateWeeklyExcelTabs。它还会去显示激活表的内容，那一支要
# document.querySelector，打桩返回 null 即可（这些用例不关心正文）。
WEEKLY_HARNESS = '''
const request = JSON.parse(process.argv[2]);
const tabsContainer = {innerHTML: '', querySelectorAll: function () { return []; }};
const document = {querySelector: function () { return null; }};
function getSheetFromUrl() { return null; }
generateWeeklyExcelTabs(tabsContainer, request.sheets);
out.tabsHtml = tabsContainer.innerHTML;
'''

# 合并页的另一处标签生成：真跑 generateMergedExcelTabs。它用 DOM API 建标签
# （表名是不可信输入，拼 innerHTML 会被 `"` 逃出属性），所以这里给一个记录属性的
# 假元素，再把「建出来的标签」还原成能解析的 HTML 片段。
MERGED_HARNESS = '''
const request = JSON.parse(process.argv[2]);
const created = [];
const document = {
    createElement: function () {
        const element = {className: '', textContent: '', attributes: {},
                         setAttribute: function (key, value) { element.attributes[key] = value; },
                         addEventListener: function () {}};
        created.push(element);
        return element;
    }
};
const tabsContainer = {textContent: '', appendChild: function () {}};
generateMergedExcelTabs(tabsContainer, request.sheets);
out.tabsHtml = created.map(function (element) {
    return '<div class="' + element.className + '" data-sheet="'
        + element.attributes['data-sheet'] + '"></div>';
}).join('');
'''

HARNESSES = {
    'function displayExcelDiff(': COMMIT_HARNESS,
    'function generateExcelTabsForContainer(': MERGE_HARNESS,
    'function generateMergedExcelTabs(': MERGED_HARNESS,
    'window.generateWeeklyExcelTabs = function': WEEKLY_HARNESS,
}


def _run_node(script: str, payload: dict) -> dict:
    if NODE is None:
        pytest.skip('本机没有 node，跳过（这是唯一能真跑页面 JS 的方式）')
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as handle:
        handle.write(script)
        temp_path = handle.name
    try:
        proc = subprocess.run([NODE, temp_path, json.dumps(payload)],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, f'node 跑失败：{proc.stderr[:600]}'
        # 页面里的 `console.log` 与结果共用一个 stdout，所以结果取**最后一行**
        # （整个 stdout 直接喂给 json.loads 会「Expecting value: line 1 column 1」，
        #  那看起来像「脚本没输出」，其实是它前面还打了日志）。
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        assert lines, f'node 没有任何输出：{proc.stderr[:600]}'
        return json.loads(lines[-1])
    finally:
        os.unlink(temp_path)


def _run_module(names, sheets) -> dict:
    """只跑共享模块本身。`sheets` 直接给 `{hasChanges: bool}` 形状。"""
    script = '\n'.join([
        'var window = globalThis;',
        'const out = {};',
        _read(ORDER_MODULE),
        'const request = JSON.parse(process.argv[2]);',
        MODULE_CONTRACT,
        'process.stdout.write(JSON.stringify(out));',
    ])
    return _run_node(script, {'names': names, 'sheets': sheets})


def _run_call_site(call_site, payload: dict) -> dict:
    """真跑共享模块 + 页面里的那个调用点（node + DOM stub）。"""
    path, marker = call_site
    # `window` 用 node 的全局对象：浏览器里 `window.X = …` 会顺带建出一个全局 X，
    # 模板里两种写法都有。共享模块放在最前面 —— 与浏览器里的加载顺序相同（模块脚本
    # 先于内联脚本），也避免它被粘到上一段尾部（`window.f = function () {}` 后面紧跟
    # `(` 会被当成函数调用，而不是新语句 —— ASI 不在这里插分号）。
    script = '\n'.join([
        'var window = globalThis;',
        'const out = {};',
        _read(ORDER_MODULE),
        _extract(WEEKLY_TEMPLATE, 'escapeHtml'),
    ] + [_extract(path, name) for name in COMMON_FUNCS]
      + [_extract(path, name) for name in EXTRA_FUNCS[path]]
      + [_call_site_body(call_site), HARNESSES[marker],
         'process.stdout.write(JSON.stringify(out));'])
    return _run_node(script, payload)


def _payload_for(call_site, sheets) -> dict:
    """提交页的入口收的是整个 diffData，另外两页收的是 sheets。"""
    if call_site[0] == COMMIT_TEMPLATE:
        return {'diffData': {'sheets': sheets}}
    return {'sheets': sheets}


def _sheet(has_changes: bool) -> dict:
    """一张最小的表数据：有变更 = 有一行新增（`sheetHasChanges` 的口径）。"""
    if has_changes:
        return {'headers': ['id'], 'rows': [{'row_number': 2, 'status': 'added', 'data': {'id': '1'}}]}
    return {'headers': ['id'], 'rows': [{'row_number': 2, 'status': 'unchanged', 'data': {'id': '1'}}]}


def _tabs(tabs_html: str) -> list:
    """页面上的标签：`[(class 串, 表名)]`，**按实际顺序**。

    从 `data-sheet` 读表名而不是读文本 —— 文本被转义过。`\\s+` 而不是单个空格：
    周版本页那一段的 class 与 data-sheet 之间是一个换行。
    """
    return re.findall(r'<div class="([^"]*)"\s+data-sheet="([^"]*)"', tabs_html)


def _tab_names(tabs_html: str) -> list:
    return [name for _classes, name in _tabs(tabs_html)]


def _active_names(tabs_html: str) -> list:
    """带 `active` 的标签名。"""
    return [name for classes, name in _tabs(tabs_html)
            if re.search(r'(?:^|\s)active(?:\s|$)', classes)]


def _classes_of(tabs_html: str, name: str) -> str:
    for classes, tab_name in _tabs(tabs_html):
        if tab_name == name:
            return classes
    raise AssertionError(f'标签里没有 {name!r}：{_tab_names(tabs_html)}')


# 表名不是 `Sheet1` 的**工作簿原序**（线上另一个项目的真实形态：带序号的表名排在
# 中文表名之前）。关键点：字母序与工作簿序在这一组里**不一致** —— 旧的排序会翻过来。
WORKBOOK_ORDER_NO_SHEET1 = ('道具表', '0_常规属性', 'Sheet2')
CHANGES_NO_SHEET1 = (True, True, False)
# `Sheet1` 在工作簿里排第二：旧实现会给它 -1 优先级、把它提到最前。
WORKBOOK_ORDER_WITH_SHEET1 = ('A_表', 'Sheet1', 'B_表')


class TestTheOrderIsTheWorkbookOrder:
    """排序：有变更的在前，**组内保持工作簿原始顺序**。"""

    @pytest.mark.parametrize('call_site', CALL_SITES)
    def test_the_workbook_order_survives_without_a_sheet1(self, call_site):
        """表名里没有 `Sheet1` 时，组内顺序就是工作簿顺序。

        旧实现把「有变更」这一组按表名字母序重排 —— 这一组名字的顺序会**反过来**
        （`道具表` 排到 `0_常规属性` 后面），所以这条用例在旧实现上必红。
        """
        sheets = {}
        for name, flag in zip(WORKBOOK_ORDER_NO_SHEET1, CHANGES_NO_SHEET1):
            sheets[name] = _sheet(flag)
        result = _run_call_site(call_site, _payload_for(call_site, sheets))
        assert _tab_names(result['tabsHtml']) == list(WORKBOOK_ORDER_NO_SHEET1), (
            f'{os.path.basename(call_site[0])}：有变更的表被重排了 —— '
            'AI 说的「第 N 张表」用的是工作簿顺序，重排之后两边对不上'
        )

    @pytest.mark.parametrize('call_site', CALL_SITES)
    def test_sheet1_does_not_jump_the_queue(self, call_site):
        """`Sheet1` 不再插队：它排在**工作簿里它自己的位置**上。

        旧实现给它一个 -1 优先级，于是 `A_表, Sheet1` 会变成 `Sheet1, A_表` ——
        顺序被一个只对一个项目成立的名字改掉了。
        """
        sheets = {name: _sheet(True) for name in WORKBOOK_ORDER_WITH_SHEET1}
        result = _run_call_site(call_site, _payload_for(call_site, sheets))
        assert _tab_names(result['tabsHtml']) == list(WORKBOOK_ORDER_WITH_SHEET1), (
            f'{os.path.basename(call_site[0])}：`Sheet1` 又被提到最前面了'
        )

    @pytest.mark.parametrize('call_site', CALL_SITES)
    def test_changed_sheets_come_first(self, call_site):
        """有变更的在前这条不能因为改排序而丢掉。"""
        names = ('甲', '乙', '丙')
        sheets = {name: _sheet(flag) for name, flag in zip(names, (False, True, False))}
        result = _run_call_site(call_site, _payload_for(call_site, sheets))
        assert _tab_names(result['tabsHtml']) == ['乙', '甲', '丙']

    def test_the_module_keeps_its_three_methods(self):
        """模块的对外契约：少一个方法，页面上就是 `is not a function`。"""
        result = _run_module(['A'], {'A': {'hasChanges': True}})
        assert result['api'] == ['analyzeSheets', 'orderSheets', 'defaultSheetName'], (
            'ExcelSheetOrder 少了方法 —— 页面调不到它'
        )

    def test_the_helpers_do_not_mutate_their_input(self):
        """入参数组的顺序不许被就地改掉，且重复调用结果一致。"""
        names = ['A', 'B']
        sheets = {'A': {'hasChanges': False}, 'B': {'hasChanges': True}}
        result = _run_module(names, sheets)
        assert result['analysisOrder'] == names, 'analyzeSheets 没有按工作簿顺序返回'
        assert result['originalIndex'] == [0, 1], 'originalIndex 不是工作簿下标'
        assert result['names'] == ['B', 'A'], '有变更的没有排到前面'
        assert result['analysisOrderAfter'] == names, (
            'orderSheets 把入参数组就地排掉了 —— 调用方还要按原序用那一份'
        )
        assert result['repeatable'] == result['names'], '重复调用结果不一致'


class TestTheDefaultSheet:
    """默认打开哪一张：第一张有变更的表；一张都没有时第一张。"""

    @pytest.mark.parametrize('call_site', CALL_SITES)
    def test_the_default_is_the_first_changed_sheet_in_workbook_order(self, call_site):
        """那张「被跳过」的表也在工作簿里 —— 默认要落在**它**头上，而不是往后顺延。

        旧实现里那句 `!sheet.name.includes('测试配置')` 会把工作簿里的第一张有变更的
        表跳过去。换一个项目这个中文子串不出现，过滤就成了恒为空操作 —— 也就是说这条
        规则从来只在**一个**项目上有效，而它留下的印象是「平台有一套跳过规则」。
        """
        sheets = {SKIP_TOKEN: _sheet(True), '道具表': _sheet(True)}
        result = _run_call_site(call_site, _payload_for(call_site, sheets))
        assert _active_names(result['tabsHtml']) == [SKIP_TOKEN], (
            f'{os.path.basename(call_site[0])}：默认选中的不是工作簿里第一张有变更的表'
        )

    def test_the_commit_page_always_activates_exactly_one_tab(self):
        """**所有有变更的表都被过滤掉时也必须有一张是激活的**。

        旧实现在那一支里不给 `defaultActiveSheetName` 赋值，它于是保持 `undefined`：
        标签渲染出来一个都不高亮，正文区也没有一张表是激活的 —— 页面上看起来
        「什么都变了但什么都看不到」，没有任何报错。
        """
        sheets = {SKIP_TOKEN: _sheet(True), '测试配置_2': _sheet(True)}
        result = _run_call_site((COMMIT_TEMPLATE, 'function displayExcelDiff('),
                                {'diffData': {'sheets': sheets}})
        active = _active_names(result['tabsHtml'])
        assert len(active) == 1, (
            f'激活的标签有 {len(active)} 个（应为 1）：{active} —— '
            '一张都不激活时正文区什么都不渲染'
        )
        assert active == [SKIP_TOKEN]

    def test_a_payload_without_changes_still_activates_the_first_sheet(self):
        sheets = {name: _sheet(False) for name in ('甲', '乙')}
        result = _run_call_site((COMMIT_TEMPLATE, 'function displayExcelDiff('),
                                {'diffData': {'sheets': sheets}})
        assert _active_names(result['tabsHtml']) == ['甲']

    def test_no_sheets_at_all_is_not_a_crash(self):
        """空载荷不许抛 —— `defaultSheetName` 对空数组返回 null。"""
        assert _run_module([], {})['emptyDefault'] is None


class TestTheSkippedByDefaultRuleIsGone:
    """「默认选中哪一张」那条项目专属的过滤不许再留在页面的**代码**里。

    断言前先剥注释（见模块 docstring）：本仓库会把被去掉的写法原样写进说明注释，
    不剥的话这几条会因为「注释里还留着」而假红。

    **注意这条与下面那条的不对称是有意的**：`测试配置` 只影响「默认打开哪一张」
    （便利启发式，删掉严格更可预测），已移除；`召唤物` 限制的是「用户能做什么」
    （产品约束），**保留**，见 `TestTheSummonSheetStaysUnclickable`。
    """

    @pytest.mark.parametrize('template', [COMMIT_TEMPLATE, MERGE_TEMPLATE, WEEKLY_TEMPLATE])
    def test_the_filter_token_only_survives_in_comments(self, template):
        code = _strip_comments(_read(template))
        assert SKIP_TOKEN not in code, (
            f'{os.path.basename(template)} 的代码里还写死着「{SKIP_TOKEN}」—— '
            '换一个项目这个中文子串不出现，规则就成了恒为空操作的兜底'
        )

    def test_the_stripping_itself_works(self):
        """剥注释这一步要能失败：它坏了，上面那两条就会变成假绿。"""
        sample = '/* 说明：includes("测试配置") */\nvar a = 1; // 测试配置\n'
        assert SKIP_TOKEN not in _strip_comments(sample)
        assert _strip_comments('var url = "http://x";') == 'var url = "http://x";', (
            '字符串字面量里的 // 被当成注释了 —— 那样断言会漏掉真实的字符串'
        )

    def test_the_dead_branch_is_gone_from_the_commit_page(self):
        """`var treasureSheet;` 声明后从未赋值，紧跟的 `if` 永远进不去 —— 死代码。"""
        code = _strip_comments(_read(COMMIT_TEMPLATE))
        assert 'treasureSheet' not in code, (
            '提交页里那个永远进不去的分支又回来了（声明后从未赋值）'
        )


class TestTheSummonSheetStaysUnclickable:
    """「召唤物」表**不给点**：这是产品约束，必须原样留着，只是换了接线方式。

    为什么它和 `测试配置` 不一样（这条不对称是有意的）：

    * `测试配置` 只决定**默认打开哪一张表** —— 一个便利启发式，删掉严格更可预测，
      不会让人得出错误结论，所以移除；
    * 「召唤物」决定的是**用户能做什么** —— 删掉那一刻，原本打不开的表变成可点。
      那是产品行为变更，不该在「抽象化」的口号下顺手做掉。

    但**接线方式**可以（也应该）跟「默认选中」一起收干净：判定作为参数传给共享模块，
    调用点只保留那个中文子串本身 —— 这是这一层唯一还没抽象干净的东西，正解是按
    项目/仓库配置传入（本次不做）。

    下面这些**真跑页面里的调用点**（node + DOM stub），不是字符串断言。
    """

    SUMMON = DISABLED_TOKEN + '表'

    @staticmethod
    def _sheets(**flags):
        return {name: _sheet(flag) for name, flag in flags.items()}

    @pytest.mark.parametrize('call_site', [
        (COMMIT_TEMPLATE, 'function displayExcelDiff('),
        (MERGE_TEMPLATE, 'function generateExcelTabsForContainer('),
    ])
    def test_a_summon_sheet_is_not_clickable_even_with_changes(self, call_site):
        sheets = self._sheets(**{'普通表': True, self.SUMMON: True})
        result = _run_call_site(call_site, _payload_for(call_site, sheets))
        summon_classes = _classes_of(result['tabsHtml'], self.SUMMON)
        assert 'excel-tab-disabled' in summon_classes, (
            f'{os.path.basename(call_site[0])}：有变更的「{DISABLED_TOKEN}」表变成可点了 —— '
            '这条限制的是用户能做什么，属于产品行为'
        )
        assert 'excel-tab-summon-disabled' in summon_classes, (
            f'{os.path.basename(call_site[0])}：专属标记类不见了（CSS 里那条规则会变成死规则）'
        )
        # 同一次渲染里，普通的有变更表照旧可点、且不带专属标记
        normal_classes = _classes_of(result['tabsHtml'], '普通表')
        assert 'excel-tab-disabled' not in normal_classes, '普通的有变更表被误禁用了'
        assert 'excel-tab-summon-disabled' not in normal_classes, '专属标记打到了普通表上'

    @pytest.mark.parametrize('call_site', [
        (COMMIT_TEMPLATE, 'function displayExcelDiff('),
        (MERGE_TEMPLATE, 'function generateExcelTabsForContainer('),
    ])
    def test_a_summon_sheet_without_changes_keeps_the_badge_too(self, call_site):
        """没有变更时也带专属标记（原实现是「命中规则就打」，与有没有变更无关）。"""
        sheets = self._sheets(**{self.SUMMON: False})
        result = _run_call_site(call_site, _payload_for(call_site, sheets))
        assert 'excel-tab-summon-disabled' in _classes_of(result['tabsHtml'], self.SUMMON)

    @pytest.mark.parametrize('template', DISABLED_CALL_SITES)
    def test_the_predicate_is_byte_identical_in_both_call_sites(self, template):
        """两处判定逐字相同 —— 它们是同一段判定的拷贝，历史上就漂移过。"""
        bodies = {os.path.basename(path): _extract(path, DISABLED_PREDICATE)
                  for path in DISABLED_CALL_SITES}
        assert len(set(bodies.values())) == 1, (
            f'{os.path.basename(template)} 里的 {DISABLED_PREDICATE} 与另一处不一致了：'
            f'{{k: len(v) for k, v in bodies.items()}}'
        )

    @pytest.mark.parametrize('call_site', CALL_SITES)
    def test_only_the_disabled_call_sites_pass_the_predicate(self, call_site):
        """**周版本页不许加这条规则**（HEAD 里没有）：它只在提交页与合并页生效。

        传了判定的那两个调用点会把规则接上；没传的两个（合并页的
        `generateMergedExcelTabs`、周版本页）保持「都可点击」。
        """
        path, _marker = call_site
        body = _call_site_body(call_site)
        if path in DISABLED_CALL_SITES and _marker != 'function generateMergedExcelTabs(':
            assert DISABLED_PREDICATE in body, (
                f'{os.path.basename(path)}：{_marker} 没有把「不给点」的判定传进去 —— '
                '那条产品规则会静默失效'
            )
        else:
            assert DISABLED_PREDICATE not in body, (
                f'{os.path.basename(path)}：{_marker} 不该有这条规则（HEAD 里没有）—— '
                '别给周版本页 / 合并页的另一处标签生成加上'
            )

    def test_the_shared_module_knows_no_sheet_names(self):
        """共享模块里**一个中文子串都不许有**：判定必须由调用点传进来。"""
        code = _strip_comments(_read(ORDER_MODULE))
        for token in (DISABLED_TOKEN, SKIP_TOKEN):
            assert token not in code, (
                f'共享模块里写死了「{token}」—— 它必须是通用的，规则由调用点传进来'
            )

    def test_the_predicate_is_optional_and_defaults_to_all_clickable(self):
        """不传判定 = 都可点击。模块自己不认识任何表名规则。"""
        result = _run_module(['A', 'B'], {'A': {'hasChanges': True}, 'B': {'hasChanges': False}})
        assert result['notClickableWithoutPredicate'] == [False, False], (
            '不传「不给点」判定时模块自己禁用了某张表 —— 规则必须由调用点传进来'
        )
        assert result['notClickableWithPredicate'] == [True, False], (
            '传了判定却没有生效 —— 「不给点」这条会在页面上静默失效'
        )


class TestEveryCallSiteGoesThroughTheSharedModule:
    """四种「标签怎么排」的写法收成一份：页面只能调模块，不许自己再排一遍。"""

    @pytest.mark.parametrize('call_site', CALL_SITES)
    def test_the_call_site_uses_the_shared_module(self, call_site):
        body = _call_site_body(call_site)
        assert 'ExcelSheetOrder.orderSheets(' in body, (
            f'{os.path.basename(call_site[0])}：{call_site[1]} 没有走共享模块 —— '
            '排序口径会从这里开始再次漂移'
        )
        assert 'ExcelSheetOrder.analyzeSheets(' in body, (
            f'{os.path.basename(call_site[0])}：{call_site[1]} 自己摊了一份变更分析 —— '
            '`originalIndex` 是「原序」唯一的事实源，别处再算一份就会对不上'
        )

    @pytest.mark.parametrize('call_site', CALL_SITES)
    def test_no_call_site_sorts_by_name(self, call_site):
        code = _strip_comments(_call_site_body(call_site))
        assert 'localeCompare' not in code, (
            f'{os.path.basename(call_site[0])}：标签又按表名排序了 —— 工作簿原序会被打乱'
        )
        assert "'Sheet1'" not in code, (
            f'{os.path.basename(call_site[0])}：又出现了按表名 `Sheet1` 定优先级'
        )

    @pytest.mark.parametrize('template', [COMMIT_TEMPLATE, MERGE_TEMPLATE, WEEKLY_TEMPLATE])
    def test_the_module_is_loaded_before_the_inline_script(self, template):
        """模块必须在内联脚本**之前**加载 —— 否则页面里 `ExcelSheetOrder` 是 undefined。"""
        text = _read(template)
        module_at = text.index('js/excel_sheet_order.js')
        inline_at = text.index('<script>', module_at)
        assert module_at < inline_at, (
            f'{os.path.basename(template)}：共享模块排在内联脚本后面了'
        )
