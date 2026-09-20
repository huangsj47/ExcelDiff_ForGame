# -*- coding: utf-8 -*-
"""AI 分析配置面板（摘要卡片 + 配置模态框）的重设计守卫。

界面在这一轮被重排过：摘要行从「一句 `·` 拼接的灰字」改成分段信息块，模态框从
「4 个分组从上往下平铺」改成「左分区导航 + 两栏栅格」，并补上了加载/失败/重试三态。

`tests/test_ai_config_template.py` 守的是**行为与无障碍契约**（id / label /
aria-describedby / Token 不回显 / 范围不写死 / aria-busy / 只读态），这一轮**一个字
都没改**。这里守的是契约管不到的那一半：视觉与状态反馈。每一条都对应一个真实缺陷形态。

## 为什么这些断言要写在这里，而不是靠人眼看一遍

模板要十来个上下文变量才渲染得出来，仓库里也没有浏览器。改错了的表现几乎都是
**静默**的：CSS 少一个 `}` 会让后面所有规则一起失效、重试按钮没接线就是「点了没反应」、
摘要里 `String(undefined)` 就是卡片上印一个 undefined —— 这些都不会抛异常，
也不会有任何测试变红，除非专门钉住。
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
TEMPLATE = 'templates/merged_project_view.html'
STYLE_CSS = 'static/css/style.css'
NODE = shutil.which('node')

# 切片锚点，与 test_ai_config_template.py 保持一致（那边是契约，这边是视觉）
MODAL_START = '<div class="card mb-4" id="aiConfigCard">'
MODAL_END = '{% if not repositories %}'
JS_START = '// AI 分析配置：页面摘要行 + 居中模态框'
JS_END = 'function normalizeRiskLevel(level) {'
# 本轮的样式段在页面 <style> 里的起止
PANEL_CSS_START = '/* ===================================================================\n       AI 分析配置面板'
PANEL_CSS_END = '/* ===== 响应式 ===== */'
# 模态框里该用栅格的字段容器
FIELD_CELL = 'col-12'


def _read(rel=TEMPLATE) -> str:
    with open(os.path.join(PROJECT_ROOT, rel), encoding='utf-8') as handle:
        return handle.read()


def _modal_html() -> str:
    text = _read()
    return text[text.index(MODAL_START):text.index(MODAL_END)]


def _panel_css() -> str:
    """本轮的样式段：设计 token 别名层 + 卡片/模态框/分区/示例的全部规则。

    只取这一段（而不是整块 <style>）是为了让下面「不许有裸十六进制」这类断言
    不会误伤页面原有的 `.page-header` / `.stats-*` 规则。
    """
    text = _read()
    style = text[text.index('<style>'):text.index('</style>')]
    assert PANEL_CSS_START in style, (
        '找不到 AI 分析配置面板的样式段 —— 这段是本轮新增的，被删掉或改了 banner 注释'
    )
    return style[style.index(PANEL_CSS_START):style.index(PANEL_CSS_END)]


def _panel_css_without_token_block() -> str:
    """去掉 `--ai-*` 定义块之后的样式。

    token 块里的十六进制是**兜底字面量**（`var(--color-border, #dee2e6)`），
    本来就该在那儿；把它排除掉，剩下的规则才该一律走 var()。
    """
    css = _panel_css()
    start = css.index('.ai-config-scope {')
    end = css.index('}', start) + 1
    return css[:start] + css[end:]


def _declarations(css: str) -> str:
    """只留声明，去掉注释。

    注释里**故意**写着反例与实测值（「#0d6efd on #fff 只有 3.65:1」「更不能写
    outline: none」），那些是要给人看的说明。把它们当成规则来断言，测试就会逼着
    后来的人删掉注释里的解释 —— 那是本末倒置。
    """
    return re.sub(r'/\*.*?\*/', '', css, flags=re.S)


def _extract(path, name):
    """按大括号配对抠出一个 function 声明 —— 下面要跑的是真实现，不是复刻。"""
    text = _read(path)
    match = re.search(r'function %s\s*\(' % re.escape(name), text)
    assert match, f'找不到 {name}'
    depth = 0
    for index in range(match.start(), len(text)):
        if text[index] == '{':
            depth += 1
        elif text[index] == '}':
            depth -= 1
            if depth == 0:
                return text[match.start():index + 1]
    raise AssertionError(f'{name} 的大括号不配对')


# 真跑 renderAiConfigSummary：给一个记录写入的假 DOM。
# 之前这个函数是 `summary.textContent = parts.join(' · ')`，拼错了肉眼还能发现；
# 改成 createElement 分段之后，写错的字段只会安静地少一格。
RENDER_HARNESS = '''
const nodes = {};
let seq = 0;
function mk(id) {
    if (nodes[id]) return nodes[id];
    const n = {
        id: id, textContent: '', className: '', children: [],
        classList: {
            _s: new Set(),
            add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
            toggle(c, on) { on ? this._s.add(c) : this._s.delete(c); },
            contains(c) { return this._s.has(c); }
        },
        appendChild(c) { this.children.push(c); }
    };
    nodes[id] = n;
    return n;
}
globalThis.document = { createElement: function () { return mk('el' + (seq++)); } };
function aiEl(id) { return mk(id); }
'''

RENDER_DRIVER = '''
const cases = JSON.parse(process.argv[2]);
const out = {};
for (const name of Object.keys(cases)) {
    for (const key of Object.keys(nodes)) delete nodes[key];
    renderAiConfigSummary(cases[name]);
    const summary = nodes['aiConfigSummary'];
    const status = nodes['aiConnectionStatus'];
    const badge = nodes['aiKeyStatusBadge'];
    out[name] = {
        badge: badge ? badge.textContent : null,
        badgeClass: badge ? badge.className : null,
        text: summary ? summary.textContent : null,
        pairs: summary
            ? summary.children.map(c => c.children.map(x => x.textContent).join(' = '))
            : [],
        status: status ? status.textContent : null
    };
}
process.stdout.write(JSON.stringify(out));
'''


def _render(payloads):
    if NODE is None:
        pytest.skip('本机没有 node，跳过（这是唯一能真跑摘要渲染的方式）')
    script = (RENDER_HARNESS
              + '\n'.join(_extract(TEMPLATE, name) for name in (
                  'setAiKeyStatusBadge', 'aiMetaItem', 'renderAiConfigSummary',
                  'describeMissingWeeklyAiConfig'))
              + RENDER_DRIVER)
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as handle:
        handle.write(script)
        temp_path = handle.name
    try:
        proc = subprocess.run([NODE, temp_path, json.dumps(payloads)],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, f'node 跑失败：{proc.stderr[:500]}'
        return json.loads(proc.stdout)
    finally:
        os.unlink(temp_path)


READY = {
    'api_base_url': 'https://api.example.com/v1', 'api_model': 'deepseek-v4-flash',
    'api_key': {'configured': True}, 'source': 'custom', 'endpoint_ready': True,
    'updated_at': '2026-09-17T10:22:31',
}
UNREADY = dict(READY, api_key={'configured': False}, endpoint_ready=False)
NEVER_CONFIGURED = {
    'api_base_url': '', 'api_model': '', 'api_key': {'configured': False},
    'source': 'openai', 'endpoint_ready': False,
}
# 后端在字段为空时可能回 null（resolved() 之外的路径），不能变成卡片上的 undefined
NULL_FIELDS = {
    'api_base_url': 'https://x/v1', 'api_model': None, 'api_key': None,
    'source': 'openai', 'endpoint_ready': False, 'updated_at': None,
}


# ==========================================================================
# 1. 样式段本身要立得住
# ==========================================================================


class TestThePanelCssIsWellFormed:
    def test_the_style_block_braces_are_balanced(self):
        """一个漏掉的 `}` 会让它**后面所有规则**一起失效。

        页面这块 <style> 里有 page-header / stats / 仓库列表 / AI 面板四段规则；
        少一个右括号时浏览器不报错、行为测试也不变红 —— 只有肉眼发现「整个页面样式变了」。
        """
        text = _read()
        style = text[text.index('<style>'):text.index('</style>')]
        stripped = re.sub(r'/\*.*?\*/', '', style, flags=re.S)
        assert stripped.count('{') == stripped.count('}'), (
            f"页面 <style> 大括号不配平：{stripped.count('{')} 个 {{ vs "
            f"{stripped.count('}')} 个 }}"
        )

    def test_the_panel_tokens_are_page_scoped(self):
        """`--ai-*` 只能定义在本页，不能进全局 style.css。

        一个页面的 token 一旦落到全局层就变成了跨页 API，却没有别的页面消费它 ——
        下次有人改它的值，改动面看起来是「改了个没人用的变量」。
        """
        assert '--ai-' in _panel_css(), '面板 token 别名层不见了'
        assert '--ai-' not in _read(STYLE_CSS), (
            'AI 面板的 token 被提到了全局 style.css —— 它是单页用的，应该留在本页'
        )

    def test_the_panel_rules_use_tokens_not_raw_hex(self):
        """颜色一律走 `var(--token, 兜底)`。

        仓库的 token 注释里记着实测 WCAG 对比度；在面板里另写一个裸十六进制，
        就等于绕过了那套实测值，而且以后调色时不会有人记得改它。
        （本页原有的 `.page-header` 等规则不在这一段里，不受这条约束。）
        """
        css = _declarations(_panel_css_without_token_block())
        # 先摘掉带十六进制兜底的 var()，剩下的十六进制才是「没用 token」
        without_fallbacks = re.sub(r'var\(--[\w-]+,\s*#[0-9a-fA-F]{3,8}\)', 'VAR', css)
        leftovers = re.findall(r'#[0-9a-fA-F]{3,8}\b', without_fallbacks)
        assert not leftovers, (
            f'面板样式里出现了不走 token 的颜色：{leftovers} —— '
            '要么用 var(--x, 字面量)，要么把它加进 --ai-* 别名层'
        )

    def test_the_panel_does_not_kill_the_focus_ring(self):
        """不许写 `outline: none`。

        style.css:1742 有一条全局 `:focus-visible` 焦点环（实测 5.84:1，满足
        WCAG 2.2 的 3:1）。面板里覆盖掉它，键盘用户就再也看不出焦点在哪，
        而鼠标用户完全无感 —— 是最容易漏掉的那类无障碍回归。
        """
        css = _declarations(_panel_css())
        offenders = re.findall(r'outline\s*:\s*(?:none|0)\b', css)
        assert not offenders, '面板样式里关掉了焦点轮廓'
        assert ':focus-visible' not in css, (
            '面板里重复定义焦点环 —— style.css 已有全局一条，重复定义只会互相打架'
        )

    def test_the_range_hint_has_a_real_rule(self):
        """`.ai-range-hint` 必须有真实样式定义。

        **这就是本轮发现的缺陷**：`applyAiFieldSchema` 会给整数字段的帮助文案尾部
        追加一个 `<span class="ai-range-hint">`（范围由 field_schema 下发），
        但这个类在 CSS 里从来没有定义过，只是碰巧继承了 `.form-text`。
        数字靠继承排版，列与列之间对不齐。
        """
        css = _panel_css()
        rule = re.search(r'\.ai-range-hint\s*\{([^}]*)\}', css)
        assert rule, '.ai-range-hint 没有样式定义（JS 会创建这个元素）'
        assert 'font-variant-numeric' in rule.group(1), (
            '范围提示没有用等宽数字 —— 不同位数之间会左右跳动'
        )


# ==========================================================================
# 2. 模态框结构
# ==========================================================================


class TestModalStructure:
    def test_the_rail_and_the_sections_match_one_to_one(self):
        """每个导航项的 `data-ai-section` 都要有同 id 的分组容器，数量也要相等。

        对不上的表现是**静默**的：点一下什么都不会发生（`aiEl` 返回 null，
        `initAiSectionNav` 里直接 return），控制台没有报错。
        """
        html = _modal_html()
        targets = re.findall(r'data-ai-section="([^"]+)"', html)
        assert len(targets) == 4, f'分区导航应有 4 项，实际 {len(targets)} 项：{targets}'
        for target in targets:
            assert f'id="{target}"' in html, f'导航项指向 {target}，但 markup 里没有这个 id'
        sections = re.findall(r'<fieldset[^>]*class="[^"]*ai-config-section[^"]*"[^>]*id="([^"]+)"', html)
        assert sorted(sections) == sorted(targets), (
            f'导航与分组对不上：导航 {sorted(targets)} vs 分组 {sorted(sections)}'
        )

    def test_the_actions_stay_outside_the_scrolling_body(self):
        """保存按钮必须在 `.modal-footer` 里（滚动区之外）。

        挪进 `.modal-body` 的表现：展开一个 G119 示例之后，用户要滚过四十行才能保存；
        而 `modal-dialog-scrollable` 的类名还在，看起来一切正常。
        """
        html = _modal_html()
        body_at = html.index('class="modal-body')
        footer_at = html.index('class="modal-footer')
        save_at = html.index('id="aiConfigSaveBtn"')
        assert body_at < footer_at < save_at, (
            '保存按钮跑到滚动区里了 —— 小屏上展开示例后要滚很久才够得着'
        )

    def test_the_example_panels_are_height_capped_and_scroll(self):
        """两段填写示例必须有高度上限并内部滚动。

        **这是本轮要修的观感问题之一**：原文是 `white-space: pre-wrap` 的纯文本墙，
        一展开就把模态框撑长，页脚被推下去。示例正文现在每份都是「通用形状 + G119 实例」
        两段（见 test_ai_config_template.py 里那一组位置用例），更长 —— 容器上的
        `max-height` / `overflow-y` / `pre-wrap` 就更是硬要求了。
        """
        rule = re.search(r'\.ai-example__body\s*\{([^}]*)\}', _panel_css())
        assert rule, '.ai-example__body 没有样式定义'
        body = rule.group(1)
        assert 'max-height' in body, '示例正文没有高度上限，展开后会把页脚推下去'
        assert re.search(r'overflow(?:-y)?\s*:\s*(auto|scroll)', body), '示例正文超出后不可滚动'
        assert 'pre-wrap' in body, (
            '示例是预排版文本，white-space 必须保留（原来写在内联 style 上）'
        )
        html = _modal_html()
        assert 'style="white-space: pre-wrap;"' not in html, (
            'white-space 又写回内联 style 了 —— 它应该在样式段里定义'
        )

    def test_the_example_body_fills_the_available_width(self):
        """示例正文不许用 `ch` 单位限制宽度。

        实测缺陷：这里曾写 `max-width: 68ch`。`ch` 是数字 "0" 的宽度
        （约 0.5em ≈ 6px），68ch ≈ 408px ≈ **34 个汉字**/行；而示例原文是按约
        45 字手工折行的。容器比原文窄 → 硬换行被二次折行，右侧空一大截、
        末尾留下「索、返程撤离。」这种半行。

        这条用例拦的是「有人又用 `ch` 给中文内容定宽」——`ch` 对 CJK 是错的单位，
        因为它量的是西文数字，不是汉字。
        """
        rule = re.search(r'\.ai-example__body\s*\{([^}]*)\}', _panel_css())
        assert rule, '.ai-example__body 没有样式定义'
        # 先去掉注释再断言：规则里那段解释「为什么不能用 ch」的注释本身会提到
        # max-width，按字面搜会把说明文字当成声明（这条断言第一版就这么误报过）。
        body = re.sub(r'/\*.*?\*/', '', rule.group(1), flags=re.S)
        assert not re.search(r'max-width\s*:', body), (
            '示例正文又设了 max-width（多半是 ch）—— 右侧会空出来'
        )

    def test_the_fields_use_a_grid_that_collapses_on_phones(self):
        """字段容器一律带 `col-12`，不许剩裸的 `col-md-*` / `col-6`。

        `modal-lg` 把对话框限死在 800px：视口 768~991px 时对话框只有约 500px，
        里面的 `col-md-6` 会算出 ~220px 一列 —— 「上下文索取上限（次）」这种
        十来个字的标签加一个数字输入框会被压到换行甚至裁切。
        用 `col-12 col-lg-6`（992px 起才分两栏）就不会。
        """
        html = _modal_html()
        offenders = re.findall(r'class="col-(?!12\b)(?:md|sm|lg|xl)?-?\d*[^"]*"', html)
        assert not offenders, (
            f'模态框里还有不带 col-12 的栅格容器：{offenders[:5]} —— '
            '窄屏下会挤成半列'
        )

    def test_each_field_keeps_its_label_help_and_error_in_one_cell(self):
        """每个字段的 label / 帮助文案 / 错误位置必须在**同一个栅格单元**里。

        帮助文案被挪到分组末尾时，`aria-describedby` 那条断言照样通过（读屏仍能念到），
        但视觉上它贴在别的字段下面 —— 用户以为自己填错了上面那一栏。
        这里用「label 到错误容器之间不再出现别的 label 或栅格边界」代替人眼检查。
        """
        html = _modal_html()
        fields = dict(re.findall(r"(\w+):\s*'([^']+)'",
                                 re.search(r'const AI_FIELD_DOM = \{(.*?)\n\};',
                                           _read(), re.S).group(1)))
        assert len(fields) >= 14, f'字段表只有 {len(fields)} 项，用例失效了'
        html_only = re.sub(r'\{\%.*?\%\}', '', html, flags=re.S)
        for dom_id in fields.values():
            label_at = html_only.index(f'for="{dom_id}"')
            help_at = html_only.index(f'id="{dom_id}Help"')
            error_at = html_only.index(f'id="{dom_id}Error"')
            assert label_at < help_at < error_at, f'{dom_id} 的 label/帮助/错误顺序不对'
            between = html_only[help_at:error_at]
            assert 'for="' not in between, (
                f'{dom_id} 的帮助文案与错误位置之间插进了别的字段 —— '
                '说明它们没待在同一个栅格单元里'
            )
            assert 'class="col-' not in between, (
                f'{dom_id} 的帮助文案与错误位置被栅格边界分开了'
            )


# ==========================================================================
# 3. 状态反馈：加载 / 失败 / 重试
# ==========================================================================


class TestLoadFailureIsVisibleAndRecoverable:
    """读配置失败必须**看得见**，而且能给用户一个重试的入口。

    原来失败只调 `setAiConfigFeedback(...)`，而那个元素在**模态框的页脚**里 ——
    请求是页面加载时就发的，用户不打开模态框，就只会看到摘要卡片上一行永远的
    「正在加载配置…」，分不清是在加载还是已经失败了，也没有任何东西可点。
    """

    def test_the_card_has_a_place_to_report_the_failure(self):
        html = _modal_html()
        tag = re.search(r'<div[^>]*id="aiConfigLoadError"[^>]*>', html)
        assert tag, '摘要卡片里没有失败提示容器'
        assert 'role="alert"' in tag.group(0), '读屏软件不会播报加载失败'
        assert 'd-none' in tag.group(0), '失败提示初始应该隐藏'

    def test_the_retry_button_exists_and_starts_hidden(self):
        html = _modal_html()
        tag = re.search(r'<button[^>]*id="aiConfigRetryBtn"[^>]*>', html)
        assert tag, '没有重试按钮'
        assert 'd-none' in tag.group(0), '重试按钮初始应该隐藏'
        assert re.search(r'>[^<]*重试', html[html.index('id="aiConfigRetryBtn"'):][:400]), (
            '重试按钮没有文字 —— 只有一个图标的话，读屏用户不知道它是干什么的'
        )

    def test_the_failure_path_really_uses_them(self):
        script = _read()[_read().index(JS_START):_read().index(JS_END)]
        body = script[script.index('async function refreshAiAnalysisConfig'):]
        body = body[:body.index('\n}\n')]
        assert "aiEl('aiConfigLoadError')" in body, '失败分支没有写卡片上的提示容器'
        assert "aiEl('aiConfigRetryBtn')" in body, '失败分支没有把重试按钮放出来'
        assert 'retryBtn.disabled = false' in body, '重试按钮没有被重新启用'
        assert "classList.remove('is-loading')" in body, (
            '失败后摘要行的加载态没有撤掉 —— 页面会永远停在「正在加载配置…」'
        )

    def test_the_retry_button_is_wired_outside_the_edit_permission_branch(self):
        """重试是只读 GET，只读用户也该用得上，别接进 `if (aiCanEdit)` 里。"""
        script = _read()[_read().index(JS_START):_read().index(JS_END)]
        body = script[script.index('function initAiConfig'):]
        assert 'initAiConfigRetry();' in body, '重试按钮没有接线'
        assert body.index('initAiConfigRetry();') < body.index('if (!aiCanEdit)'), (
            '重试按钮被接在了「可编辑」分支里 —— 只读用户点了不会有反应'
        )

    def test_the_retry_button_does_not_add_a_fourth_aria_busy(self):
        """重试按钮不许带 `aria-busy`。

        `test_every_async_button_reports_a_busy_state` 要求
        `setAttribute('aria-busy', 'true')` **恰好 3 次**（保存 / 测试连接 / 获取模型）。
        给重试按钮也加一个的话，那条断言会红，而它守的是「防重复提交」这个真问题。
        重试的忙碌态用 `disabled` + 卡片的 `.is-loading` 表达。
        """
        script = _read()[_read().index(JS_START):_read().index(JS_END)]
        assert script.count("setAttribute('aria-busy', 'true')") == 3
        body = script[script.index('function initAiConfigRetry'):]
        body = body[:body.index('\n}\n')]
        assert 'aria-busy' not in body, '重试按钮加了第 4 个 aria-busy'

    def test_the_loading_bar_has_a_reduced_motion_fallback(self):
        """加载进度条在「减少动态效果」下要有静态替代。

        style.css 的全局 reduced-motion 块会把 animation-iteration-count 压到 1，
        动画条于是停在起点（background-position: -40%）—— 那是一条看不见的条，
        等于加载态又没了指示。
        """
        css = _panel_css()
        assert 'prefers-reduced-motion' in css, '加载进度条没有动效偏好兜底'
        block = css[css.index('@media (prefers-reduced-motion'):]
        block = block[:block.index('\n    }')]
        assert 'animation: none' in block, '兜底块里没有关掉动画'
        assert 'background' in block, '兜底块里没有换成一条看得见的静态条'


# ==========================================================================
# 4. 摘要渲染：真跑一遍
# ==========================================================================


class TestTheSummaryRenderer:
    def test_it_splits_the_summary_into_labelled_pairs(self):
        """摘要行拆成分段信息块，而不是一句 `·` 拼起来的灰字。

        原来的写法把「来源 / 模型 / Token / 更新时间」塞进一行文本，信息之间没有
        视觉层次，也没法给模型名用等宽字体。
        """
        result = _render({'ready': READY})['ready']
        assert result['pairs'] == [
            '来源 = 自定义端点',
            '模型 = deepseek-v4-flash',
            'Token = 已配置',
            '最近更新 = 2026-09-17 10:22',
        ], f'摘要分段不对：{result["pairs"]}'
        assert result['text'] == '', '摘要容器里还留着拼接的整句文本'
        assert result['badge'] == '已就绪' and result['badgeClass'] == 'badge bg-success'

    def test_it_says_what_is_missing_when_not_ready(self):
        """未就绪时要点名缺的是哪一样，而不是让用户自己去比对上面几格。"""
        result = _render({'unready': UNREADY})['unready']
        assert result['badge'] == '待配置' and result['badgeClass'] == 'badge bg-warning'
        assert '还没就绪' in result['status'], f'没有给出未就绪结论：{result["status"]!r}'
        assert 'Token' in result['status'], (
            f'缺项清单没说是哪一样：{result["status"]!r}（后端与周版本抽屉都回「Token」）'
        )

    def test_a_ready_config_has_no_status_line(self):
        """已就绪时状态行必须是空的 —— 留着上一轮的文案就是过期的假信息。"""
        assert _render({'ready': READY})['ready']['status'] == ''

    def test_the_never_configured_state_gives_a_next_step(self):
        result = _render({'never': NEVER_CONFIGURED})['never']
        assert '尚未配置' in result['text'] and '点「修改配置」' in result['text']
        assert result['status'] == '', (
            '空状态里又补了一句「还没就绪：还缺…」—— 两句话说的是同一件事，是噪音'
        )
        assert result['pairs'] == [], '空状态不该再去渲染分段'

    def test_empty_values_never_render_as_undefined(self):
        """字段为 null 时显示「未配置」，不能把 undefined / null 印到卡片上。

        摘要改成用 `createElement` 逐格拼之后，写错的格子只会安静地少一格或者印出
        `String(undefined)`；这两种都不会抛异常。
        """
        result = _render({'nulls': NULL_FIELDS})['nulls']
        for pair in result['pairs']:
            assert 'undefined' not in pair and 'null' not in pair, (
                f'摘要里漏出了空值占位：{pair!r}'
            )
        assert '模型 = 未配置' in result['pairs'], f'空模型名没有兜底：{result["pairs"]}'
        assert 'Token = 未配置' in result['pairs'], f'空 Token 没有兜底：{result["pairs"]}'
        assert '最近更新' not in ' '.join(result['pairs']), '没有 updated_at 时不该出现这一格'

    def test_the_renderer_builds_nodes_instead_of_innerhtml(self):
        """摘要渲染不许用 innerHTML 拼字符串。

        本文件里确实有 `escapeHtml`，但它在另一段作用域里（AI 配置这段拿不到）——
        误以为有 escaper 而拼字符串，就是把服务端返回的模型名原样插进 DOM。
        """
        body = _extract(TEMPLATE, 'renderAiConfigSummary')
        assert 'innerHTML' not in body, '摘要渲染用了 innerHTML 拼字符串'
        assert 'textContent' in body, '摘要渲染没有用 textContent 写值'
