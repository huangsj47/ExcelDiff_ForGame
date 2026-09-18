# -*- coding: utf-8 -*-
"""Excel diff 的单元格宽度上限（300px）必须真的生效。

## 为什么这件事需要测试，而不是写一行 `max-width` 就完了

`max-width` 作用在 `display: table-cell` 上，在 `table-layout: auto` 下**浏览器不采纳**：

    实测（Chrome，无头渲染）：给 `.excel-cell` 写 max-width: 300px 之后，
    D 列的实际宽度仍是 357px；连一个只有 8 个字的短单元格也是 357px ——
    列宽完全由表格 `width: 100%` 的剩余空间分配决定，与 `max-width` 无关。

也就是说，「单元格最长 300px、内容超过才换行」这条要求，只写 `max-width` 是
**不会兑现**的：表现要么是不换行（列被内容撑开），要么是被表格挤着提前换行。

真正生效的做法是在 td 里放一个块级容器（`.excel-cell-inner`）来承担上限 ——
它的 `max-width` 会参与列宽的内在尺寸计算。本文件钉住的就是：

* 每一个拼单元格的地方都带上了这层容器（漏一处，那一类单元格就没有上限）；
* 上限声明在**内层容器**上，而不是只写在 td 上；
* 内层容器显式清掉了 `border`（见下）；
* 行高容得下高亮块（否则色块会盖住上下一行的字）。

## 同一页里的字面量：高亮不能改写单元格原文

单元格的宽度上限是为「内容超过 300px 才换行」服务的，而这件事的前提是
**格子里显示的就是文件里写的**。`format_cell_value`（服务端）已经把这条写进契约
（`utils/diff_data_utils.py`：不做任何改写、不 strip），客户端的高亮函数是它的下游，
也必须守同一条 —— 见 `TestTheHighlightKeepsTheCellTextLiteral`。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 所有会拼出 `.excel-cell` 的地方：**表体渲染的共享实现**（三个 diff 页面的表体都由它
# 渲染）、服务端 partial 与 `static/js/diff-handlers.js`。漏掉任何一个，那条路径上的
# 单元格就没有宽度上限。
#
# 2026 结构重构：三个模板（commit_diff / weekly_version_full_diff / merge_diff，含合并页
# 按提交展开的容器路径）不再自己拼单元格，而是调 `static/js/excel_diff_table.js`；所以
# 单元格的实现从三个模板换成了那一个文件 —— 断言的目标文件换了，但断言本身没变：
# 「每个拼单元格的地方都要有且只有一个内层容器」。模板侧改由
# `TEST_TEMPLATES_MUST_NOT_BUILD_CELLS` 反向兜住（谁在模板里又抄一份，这里就会红）。
CELL_SOURCES = (
    'static/js/excel_diff_table.js',
    'templates/diff_partials/excel_diff.html',
    'static/js/diff-handlers.js',
)
# 表体已经迁到共享实现的那几个模板：它们不该再出现 `<td class="excel-cell…">`
TEST_TEMPLATES_MUST_NOT_BUILD_CELLS = (
    'templates/commit_diff.html',
    'templates/weekly_version_full_diff.html',
    'templates/merge_diff.html',
)
EXCEL_CSS = 'static/css/excel-diff-new.css'
# 通配边框规则所在文件（最后加载，带 !important）
SCROLL_FIX_CSS = 'static/css/excel-scroll-fix.css'
DIFF_HANDLERS = 'static/js/diff-handlers.js'

TD_RE = re.compile(r'<td class="excel-cell[^"]*"')
WRAPPER = 'excel-cell-inner'
# 线上要求的上限
EXPECTED_MAX_WIDTH = '300px'


def _read(rel):
    with open(os.path.join(PROJECT_ROOT, rel), encoding='utf-8') as handle:
        return handle.read()


def _rule(css, selector):
    """抠出某个选择器（精确到 `{` 前的那段文本）的声明块，**含收尾的 `}`**。

    注意：注释里也会出现成对的大括号（本文件引用了那条通配边框规则），
    所以这里只从选择器之后开始找第一个 `}` —— 那些注释都在选择器之前。
    """
    start = css.find(selector + ' {')
    assert start != -1, f'找不到规则 {selector}'
    end = css.index('}', start)
    return css[start:end + 1]


def _decls(block):
    body = block[block.index('{') + 1:block.rindex('}')]
    body = re.sub(r'/\*.*?\*/', '', body, flags=re.S)
    return {name.strip(): ' '.join(value.split()).replace('!important', '').strip()
            for name, value in re.findall(r'([a-z-]+)\s*:\s*([^;]+)', body)}


class TestEveryCellCarriesTheWrapper:
    @pytest.mark.parametrize('source', CELL_SOURCES)
    def test_each_cell_wraps_its_content(self, source):
        """每个 `<td class="excel-cell…">` 都要有且只有一个内层容器。

        漏掉一处 = 那一类单元格（新增行 / 删除行 / 改前 / 改后 / 未变更）没有宽度
        上限 —— 表现是「这一列的宽度由它撑开」，而其它列看起来又是对的，
        很容易以为上限已经生效了。
        """
        text = _read(source)
        cells = len(TD_RE.findall(text))
        wrappers = text.count(WRAPPER)
        assert cells, f'{source} 里找不到 .excel-cell 单元格 —— 用例失效了'
        assert cells == wrappers, (
            f'{source}：{cells} 个单元格，但只有 {wrappers} 个 {WRAPPER} 容器 —— '
            f'少掉的那些单元格没有宽度上限'
        )

    @pytest.mark.parametrize('source', TEST_TEMPLATES_MUST_NOT_BUILD_CELLS)
    def test_the_delegating_templates_do_not_build_cells(self, source):
        """已经改用共享实现的模板里不许再有 `.excel-cell` 单元格。

        反向的那一条：上面只要求「出现单元格的地方都带内层容器」，
        如果某个模板悄悄抄回一份没带内层容器的单元格，上面那条扫不到它 ——
        它的文件已经不在 CELL_SOURCES 里了。这里按「谁在模板里拼单元格」兜住：
        这种抄一份回来的写法会让那条路径重新失去宽度上限，而它与共享实现
        会各自演化（这正是本次重构要消掉的形态）。
        """
        text = _read(source)
        found = TD_RE.findall(text)
        assert not found, (
            f'{source} 又在自己拼单元格了：{found} —— 表体渲染只该有一份实现'
            f'（static/js/excel_diff_table.js），它的单元格才带内层容器上限'
        )


class TestTheCapIsOnTheWrapper:
    def test_the_inner_wrapper_declares_the_cap(self):
        """上限必须声明在内层容器上。

        这条同时是「别把上限挪回 td」的守卫：td 上的 max-width 在 auto 布局下无效，
        挪回去等于悄悄取消了这个需求。
        """
        css = _read(EXCEL_CSS)
        block = _rule(css, '.excel-diff-table .excel-cell > .excel-cell-inner')
        decls = _decls(block)
        assert decls.get('max-width') == EXPECTED_MAX_WIDTH, (
            f'内层容器的 max-width 是 {decls.get("max-width")!r}，应为 {EXPECTED_MAX_WIDTH}'
        )
        assert decls.get('display') == 'block', (
            '内层容器必须是块级：只有块级盒的 max-width 才会参与列宽的内在尺寸计算'
        )

    def test_the_wrapper_clears_the_catch_all_border(self):
        """内层容器必须显式 `border: 0`。

        excel-scroll-fix.css 末尾有一条通配规则
        （`.excel-table-wrapper *, .excel-diff-table * { border-width: 1px !important;
        border-style: solid }`），它会给这层容器也套上 1px 边框 —— 整张表每个格子
        都会多出一圈线，而且行内/块级盒的边框都会影响尺寸。高特异性 + `border: 0`
        才能压住它。
        """
        assert 'border-width: 1px !important' in _read(SCROLL_FIX_CSS), (
            '通配边框规则不见了 —— 这条用例的前提变了，请重新确认内层容器是否还需要清 border'
        )
        decls = _decls(_rule(_read(EXCEL_CSS), '.excel-diff-table .excel-cell > .excel-cell-inner'))
        assert decls.get('border') == '0', (
            f'内层容器的 border 是 {decls.get("border")!r} —— 通配规则会给每个格子套一圈 1px 灰框'
        )

    def test_the_reason_the_cell_itself_cannot_hold_the_cap_is_recorded(self):
        """代码里要写清楚「为什么不能只写 `.excel-cell { max-width }`」。

        这一段注释是防止后人「简化」掉内层容器的唯一线索：把上限挪回 td 之后，
        页面看起来正常（不报错、测试也不红），只是那条需求悄悄失效了。
        """
        css = _read(EXCEL_CSS)
        assert 'table-layout' in css and 'table-cell' in css, (
            'excel-diff-new.css 里没有说明「max-width 在 table-cell 上不生效」，'
            '后人会把内层容器当成多余的嵌套删掉'
        )


class TestTheLineHeightLeavesRoomForTheHighlight:
    """行高必须容得下高亮块，否则色块会盖住上下一行的字（线上「显示不全」的另一半）。

    高亮是行内元素，它的背景盒高度 = 内容区（约 1.2em）+ 上下 padding。内容区与
    行框（line-height）之间若不够，背景盒就会溢出到相邻行上。这里按最保守的
    1.2em 估算内容区，要求行高 >= 内容区 + 上下 padding。
    """

    CONTENT_AREA_RATIO = 1.2

    def _measure(self):
        css = _read(EXCEL_CSS)
        cell = _decls(_rule(css, '.excel-diff-table .excel-cell'))
        # 单元格的 font-size 来自 `.excel-diff-table td`（.excel-cell 自己不声明字号）
        td = _decls(_rule(css, '.excel-diff-table td'))
        font_px = float(re.search(r'([\d.]+)px', td['font-size']).group(1))
        line_height = cell['line-height']
        if line_height.endswith('px'):
            line_px = float(line_height[:-2])
        else:
            line_px = float(line_height) * font_px
        # 高亮的上下 padding：从高亮规则里取（excel-scroll-fix.css 最后加载，是生效值）
        highlight = _decls(_rule(_read(SCROLL_FIX_CSS), '.excel-diff-table .excel-text-bg-old'))
        pad = highlight.get('padding', '0')
        parts = pad.replace('px', '').split()
        pad_v = float(parts[0]) if parts else 0.0
        return font_px, line_px, pad_v

    def test_the_line_box_fits_the_highlight_block(self):
        font_px, line_px, pad_v = self._measure()
        needed = font_px * self.CONTENT_AREA_RATIO + 2 * pad_v
        assert line_px >= needed, (
            f'行高 {line_px}px 装不下高亮块：内容区约 {font_px * self.CONTENT_AREA_RATIO:.1f}px '
            f'+ 上下 padding {2 * pad_v}px = {needed:.1f}px。'
            '色块会溢出到相邻行，把上下一行的字盖住（线上「被颜色色块挡住了」）。'
            f'调大 .excel-cell 的 line-height，或调小高亮的上下 padding。'
        )


# ==========================================================================
# 高亮不能改写单元格原文
# ==========================================================================

# 被测实现所在文件。`diff-handlers.js` 里的三个函数是**客户端渲染路径唯一的一份**
# （合并页与周版本页的 `highlightDifferences` 在脚本顺序上都先加载，会被它覆盖；
# 只有提交页 388 行那份自己的定义更靠前 —— 见文件末尾的说明）。
_HIGHLIGHT_FUNCS = (
    'splitKeepingSeparators',
    'highlightParameterList',
    'highlightBracketParameterList',
    'escapeHtml',
)

_NODE_PREAMBLE = r'''
// escapeHtml 走的是 document.createElement('div').textContent → innerHTML，
// 所以只需要一个「按浏览器规则转义文本」的最小 DOM 桩：转 `& < >`，**不转引号**。
const document = {
    createElement() {
        const el = { textContent: '' };
        Object.defineProperty(el, 'innerHTML', {
            get() {
                return el.textContent
                    .replace(/&/g, '&amp;')
                    .replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;');
            },
        });
        return el;
    },
};

// 把高亮 HTML 还原成**肉眼看到的文本**：剥掉 span 标签、反转义。
// 高亮只该决定「哪几个字带底色」，不该让看到的字变样。
function visibleText(html) {
    return String(html)
        .replace(/<[^>]+>/g, '')
        .replace(/&lt;/g, '<')
        .replace(/&gt;/g, '>')
        .replace(/&quot;/g, '"')
        .replace(/&#39;/g, "'")
        .replace(/&amp;/g, '&');
}

function highlightCount(html) {
    return (String(html).match(/<span class="excel-text-bg-(old|new)"/g) || []).length;
}
'''

_NODE_CASES = r'''
%(sources)s

const cases = %(cases)s;
const out = cases.map((item) => {
    const oldHtml = highlightParameterList(item.old, item.new, 'old');
    const newHtml = highlightParameterList(item.old, item.new, 'new');
    return {
        oldHtml: oldHtml,
        newHtml: newHtml,
        oldVisible: visibleText(oldHtml),
        newVisible: visibleText(newHtml),
        oldHighlights: highlightCount(oldHtml),
        newHighlights: highlightCount(newHtml),
    };
});
process.stdout.write(JSON.stringify(out));
'''


def _extract_top_level(name):
    """从 `diff-handlers.js` 里抠出一个**顶层** function 声明。

    这里不能用「数大括号」的常规办法：`highlightBracketParameterList` 里的
    正则字面量 `/{([^,}]+),([^}]+)}/g` 本身就带 `{` `}`，会把配对数算歪
    （`\\{` 被当成 +1、两个 `[^}]` 和一个 `\\}` 各被当成 -1，净差 -3）。
    所以改按列 0 的 `function ` 行切片 —— 这个文件里的顶层函数都从行首开始。
    """
    text = _read(DIFF_HANDLERS)
    match = re.search(r'^function %s\s*\(' % re.escape(name), text, re.M)
    assert match, f'{DIFF_HANDLERS}：找不到顶层函数 {name}'
    following = re.search(r'^function ', text[match.end():], re.M)
    end = match.end() + following.start() if following else len(text)
    return text[match.start():end].rstrip() + '\n'


def _build_script(cases):
    sources = '\n\n'.join(_extract_top_level(name) for name in _HIGHLIGHT_FUNCS)
    body = _NODE_CASES % {
        'sources': sources,
        'cases': json.dumps(cases, ensure_ascii=False),
    }
    return _NODE_PREAMBLE + body


def _run_highlight(cases):
    node = shutil.which('node')
    if not node:
        pytest.skip('node 不可用，跳过 JS 层验证')
    proc = subprocess.run(
        [node, '-'],
        input=_build_script(cases),
        capture_output=True,
        text=True,
        encoding='utf-8',
        timeout=60,
    )
    assert proc.returncode == 0, (
        '跑真实高亮函数时 Node 报错：\n'
        f'STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}'
    )
    return json.loads(proc.stdout)


# 每一行是一组真实会出现在单元格里的改前/改后值，外加两个期望：
#   changes：这一对是否应该**看得见**（原文不同 → 渲染出来也必须不同）
#   highlights：改前 / 改后 各自至少要出现几处高亮
_HIGHLIGHT_CASES = (
    # 线上 5978 的那一格：分隔符**后面**的空格必须留着。
    # 旧实现 split(',') → map(trim) → join(',') 之后显示成「玩家带动作,打断技能」，
    # 与文件里写的不一致（这格恰好只有前段变化，后段的空格被吃掉看不出来）。
    dict(old='玩家带动作, 打断技能', new='玩家带动作, 打断技能2',
         changes=True, old_highlights=1, new_highlights=1),
    # 线上 5966 的回归：只有最后一段变了，前缀 `13,39` 不能被卷进高亮。
    dict(old='13,39,10000', new='13,39,10001',
         changes=True, old_highlights=1, new_highlights=1),
    # **只差一个空格**的一对。旧实现两边 trim 之后完全相同：
    # 既有变更却一处都不高亮，显示出来还一模一样 —— 审核者无法判断改了什么。
    dict(old='a,b', new='a, b', changes=True, old_highlights=1, new_highlights=1),
    # 其它分隔符同样原样保留（`;` `|` 各走一遍切分路径）。
    dict(old='x;y;z', new='x;y;Z', changes=True, old_highlights=1, new_highlights=1),
    dict(old='a | b | c', new='a | b | d', changes=True, old_highlights=1, new_highlights=1),
    # `&` 既是分隔符又是 HTML 实体起始符：显示要还原成 `&`，但拼 HTML 时必须转义。
    dict(old='玩家带动作 & 打断技能', new='玩家带动作 & 打断技能2',
         changes=True, old_highlights=1, new_highlights=1),
    # 多于一段变化 → 整格高亮（碎片化高亮不便于阅读），原文仍要原样。
    dict(old='a,b,c', new='x,y,z', changes=True, old_highlights=1, new_highlights=1),
    # 值本身就是不可信内容（`{key,<img …>}` 那条既有用例的输入形态）。
    dict(old='k,<img src=x onerror="window.__qa=1">', new='k,<img src=x onerror="window.__qa=2">',
         changes=True, old_highlights=1, new_highlights=1),
    # 大括号参数对：括号里的空格、以及参数对之间的 `, ` 都要留着。
    dict(old='{a, b}, {c, d}', new='{a, b}, {c, e}',
         changes=True, old_highlights=1, new_highlights=1),
    # 大括号参数对只差一个尾空格 —— 与第 3 条同一缺陷的括号版本。
    dict(old='{id, 100 }', new='{id, 100}', changes=True, old_highlights=1, new_highlights=1),
    # 只有一个值变：只有那个值带底色，键与括号保持原样。
    dict(old='{a,1},{b,2}', new='{a,1},{b,3}',
         changes=True, old_highlights=1, new_highlights=1),
    # 整对都被替换（键也不同）→ 整对高亮。
    dict(old='{key,1}', new='{other,1}', changes=True, old_highlights=1, new_highlights=1),
)


class TestTheHighlightKeepsTheCellTextLiteral:
    """高亮只能决定「哪几个字带底色」，**不能改写看到的字**。

    ## 缺陷形态

    `highlightParameterList` 早先这样切分：

        oldValue.split(sep).map(p => p.trim()).join(sep)

    `trim()` 把分隔符**后面**的空格吃掉，`join(sep)` 再用裸分隔符拼回去。文件里写
    `玩家带动作, 打断技能`，页面上显示成 `玩家带动作,打断技能`。两个后果：

    1. **显示与文件不一致** —— 审核者看到的不是被审的东西；
    2. **变更被藏起来** —— 只差空格的两条记录（`a,b` 与 `a, b`）trim 之后一模一样，
       于是「有变更」却一处都不高亮，加了 (1) 之后两边还长得完全相同。
       这正是服务端 `format_cell_value` 的契约（`utils/diff_data_utils.py`：
       「表里怎么写就怎么显示，不做任何改写、不 strip」）要防的那类问题，
       只不过发生在客户端。大括号分支是同一条缺陷的第二个出口
       （`{a, b}, {c, d}` 被重拼成 `{a,b},{c,d}`）。

    ## 这里跑的是真函数

    把 `splitKeepingSeparators` / `highlightParameterList` /
    `highlightBracketParameterList` / `escapeHtml` 从 `diff-handlers.js` 里抠出来，
    放进 node 跑（只桩一个按浏览器规则转义文本的 `document`）。断言两件事：

    * **字面量保真**：把输出剥掉 span、反转义之后，必须**逐字符等于**输入；
    * **该看得见的变化看得见**：原文不同的一对，渲染结果也必须不同，且该高亮的有高亮。
      第二条是防「为了保真干脆不高亮」——那样第 1 条会全绿，缺陷却还在。
    """

    def test_the_visible_text_equals_the_cell_text(self, rendered):
        problems = []
        for case, result in zip(_HIGHLIGHT_CASES, rendered):
            for side in ('old', 'new'):
                if result[f'{side}Visible'] != case[side]:
                    problems.append(
                        f'{case["old"]!r} → {case["new"]!r} 的{side}值：'
                        f'显示成 {result[f"{side}Visible"]!r}，'
                        f'原文是 {case[side]!r}（被改写了）'
                    )
        assert not problems, (
            '高亮改写了单元格原文 —— 审核者看到的不是文件里写的内容：\n  ' + '\n  '.join(problems))

    def test_a_change_that_is_only_whitespace_is_still_visible(self, rendered):
        """只差空格的变更必须**看得见**：两边渲染结果不同，且都带高亮。

        这条拦的是「用 trim 把差异抹平」：旧实现在 `a,b` 与 `a, b` 上输出完全相同的
        HTML，页面上一处高亮都没有 —— 后端说这一格变了，界面却告诉审核者没变。
        """
        problems = []
        for case, result in zip(_HIGHLIGHT_CASES, rendered):
            if not case['changes']:
                continue
            if result['oldHtml'] == result['newHtml']:
                problems.append(
                    f'{case["old"]!r} 与 {case["new"]!r} 渲染结果完全相同 —— '
                    f'这一格确实变了，页面上却看不出来'
                )
            for side in ('old', 'new'):
                if result[f'{side}Highlights'] < case[f'{side}_highlights']:
                    problems.append(
                        f'{case["old"]!r} → {case["new"]!r} 的{side}值只有 '
                        f'{result[f"{side}Highlights"]} 处高亮，'
                        f'至少要 {case[f"{side}_highlights"]} 处'
                    )
        assert not problems, '\n  '.join(problems)

    def test_the_separator_and_the_space_after_it_survive(self, rendered):
        """线上 5978 那一格：`玩家带动作, 打断技能` 里的 `, ` 必须原样出现。

        单拎出来是为了让失败信息直接指向线上现象 —— 上面两条若是被改坏了，
        报错里翻不到这一格的原文。
        """
        result = _run_highlight([dict(old='玩家带动作, 打断技能', new='玩家带动作, 打断技能2')])[0]
        assert result['oldVisible'] == '玩家带动作, 打断技能', (
            f'被改成了 {result["oldVisible"]!r} —— 分隔符后面的空格没了'
        )

    def test_the_untrusted_cell_value_is_still_escaped(self, rendered):
        """保真是「看得见的文本」的保真，不是「原样拼进 HTML」。

        改写成按匹配位置拼接之后，最容易踩的坑就是把原文直接塞进 innerHTML ——
        那会把不可信的单元格内容变成标签。断言输出里**只有我们自己生成的 span**，
        没有别的标签（`&lt;img` 这样的文本不在检查范围内：它正是转义正确的结果）。
        """
        foreign_tag = re.compile(r'<(?!/?span\b)[a-zA-Z!/]')
        problems = []
        for case, result in zip(_HIGHLIGHT_CASES, rendered):
            for side in ('old', 'new'):
                html = result[f'{side}Html']
                found = foreign_tag.search(html)
                if found:
                    problems.append(f'{case[side]!r} 拼出了真实标签 {found.group(0)!r}：{html}')
        assert not problems, '\n  '.join(problems)


@pytest.fixture(scope='module')
def rendered():
    """真跑一遍高亮函数（整张用例表），多个断言共用同一份结果。"""
    return _run_highlight([dict(old=c['old'], new=c['new']) for c in _HIGHLIGHT_CASES])
