# -*- coding: utf-8 -*-
"""三个页面的 AI 分析抽屉 CSS 必须逐字一致。

## 为什么钉这条

`.ai-drawer` / `.ai-analysis-output` 这段样式在三个模板里各有一份**逐字相同**的副本：

- `templates/weekly_version_diff.html`（周版本 diff 页，用户截图那一页）
- `templates/merged_project_view.html`（项目总览页，点风险标签打开同一个抽屉）
- `templates/commit_diff_new.html`（提交 diff 页）

线上反馈「右侧 AI 分析区域抽屉太小了，基本放不下多少文本」时，只改一份就会出现
「周版本页已经加宽到 720px、另外两个页面还是 460px」的口径不一致 ——
这正是本次的改动形态（宽度 / 字号 / 字体要三处同步）。

`tests/` 里原本没有任何用例碰过这段 CSS（`ai-drawer` / `460px` / `ai-analysis-output`
零命中），也就是说没有护栏：谁改了一份、忘了另外两份，CI 不会响。这里补上。

## 这里钉住什么

1. 三个模板里都能找到成对的分隔注释（开始 / 结束），且各只出现一次；
2. 分隔注释之间的 CSS **逐字相同**（换行、缩进、注释都算）；
3. 宽度仍写成相对值 `clamp(...)` —— 防止有人改回固定 px 却没同步说明。

CSS 里的分隔注释就是给人看的锚点：
`/* ===== AI 抽屉样式 开始 ===== */` … `/* ===== AI 抽屉样式 结束 ===== */`
"""
from __future__ import annotations

import os
import re

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES_DIR = os.path.join(PROJECT_ROOT, 'templates')

TARGETS = (
    'weekly_version_diff.html',
    'merged_project_view.html',
    'commit_diff_new.html',
)

START_MARK = '    /* ===== AI 抽屉样式 开始'
END_MARK = '    /* ===== AI 抽屉样式 结束 ===== */\n'


def _read(name):
    path = os.path.join(TEMPLATES_DIR, name)
    with open(path, encoding='utf-8') as handle:
        return handle.read()


def _read_style_css():
    path = os.path.join(PROJECT_ROOT, 'static', 'css', 'style.css')
    with open(path, encoding='utf-8') as handle:
        return handle.read()


def _drawer_css(name):
    """取出分隔注释之间的那段 CSS（含首尾注释本身）。"""
    text = _read(name)
    assert text.count(START_MARK) == 1, f'{name}：分隔注释「开始」应恰好出现一次'
    assert text.count(END_MARK) == 1, f'{name}：分隔注释「结束」应恰好出现一次'
    start = text.index(START_MARK)
    end = text.index(END_MARK, start) + len(END_MARK)
    assert start < end
    return text[start:end]


@pytest.mark.parametrize('name', TARGETS)
def test_each_template_has_the_drawer_block(name):
    block = _drawer_css(name)
    # 抽屉本体、遮罩、正文框这三个最关键的类必须都在块里
    for selector in ('.ai-drawer {', '.ai-drawer-overlay {', '.ai-analysis-output {'):
        assert selector in block, f'{name}：抽屉样式块里缺少 {selector}'


def test_the_three_copies_are_byte_identical():
    blocks = {name: _drawer_css(name) for name in TARGETS}
    reference_name = TARGETS[0]
    reference = blocks[reference_name]
    drifted = [name for name in TARGETS[1:] if blocks[name] != reference]
    assert not drifted, (
        'AI 抽屉 CSS 在三个模板里必须逐字一致，这些文件已经和 '
        f'{reference_name} 不一致：{"、".join(drifted)}。\n'
        '宽度 / 字号 / 字体这一组改动要三份同步改 —— 只改一份会让另一个页面的 '
        '抽屉还是旧样子（加宽前是 460px）。\n'
        "改法：把修好的那一段（含首尾的「AI 抽屉样式 开始 / 结束」注释）原样复制到其余模板。"
    )


def test_drawer_width_stays_relative():
    """宽度必须是相对值：写死 px 会在小笔记本上横占过多。"""
    block = _drawer_css(TARGETS[0])
    assert 'width: clamp(' in block, (
        '抽屉宽度应当写成 clamp(...) 这类相对值（最小可读宽度 / 相对视口宽度 / 阅读行宽上限），'
        '而不是固定 px —— 固定 px 在窄屏上会把整屏横着占满，在宽屏上又定死了每行字数。'
    )
    assert 'width: 460px' not in block, '460px 是加宽前的旧宽度，正文只剩约 380px、一行 27 个汉字。'


# --------------------------------------------------------------------------
# `hidden` 压不压得住作者样式的 display
# --------------------------------------------------------------------------

_DISPLAY_RULE = re.compile(r"([^{}\n]+)\{([^}]*)\}", re.M)
_CLASS_ATTR = re.compile(r'class="([^"]+)"')


def _classes_with_author_display(css):
    """样式表里**自己设了 display**（且不是 none）的那些类选择器。

    `[hidden]{display:none}` 来自浏览器默认样式表，任何作者来源的 `display` 都会盖掉它
    （与谁更具体无关）。所以一个类只要自己设了 display，`hidden` 就对它失效。
    """
    found = set()
    for selector, body in _DISPLAY_RULE.findall(css):
        if re.search(r"(?m)^\s*display\s*:\s*(?!none)", body):
            for token in selector.split(','):
                token = token.strip()
                if token.startswith('.') and ' ' not in token and '[' not in token:
                    found.add(token[1:])
    return found


def _hidden_classes(name):
    """模板里**同时带 `hidden` 属性**的元素用到的类名。

    HTML 写法有两种（`class="x" hidden` 与 `hidden class="x"`），两种都要认。
    """
    text = _read(name)
    found = set()
    for match in _CLASS_ATTR.finditer(text):
        start, end = match.span()
        around = text[max(0, start - 40): min(len(text), end + 40)]
        if re.search(r'\bhidden\b', around):
            found.update(match.group(1).split())
    return found


def test_a_hidden_element_that_sets_its_own_display_is_actually_hidden():
    """**带 `hidden` 的元素，样式表必须有一条 `[hidden]` 规则把它压回去。**

    这个坑在本仓库踩过五次：`.ai-drawer-tab-dot`、`.ai-drawer-panel`、
    `.ai-analysis-output`、`.ai-drawer-footer .btn`、`.ai-drawer-footer a.btn`，
    外加后补的 `.ai-budget-banner` 与 `.ai-usage-line`。最后这两条漏了好几版，
    表现是**抽屉一打开就挂着一个 26px 的空红框、底部还有一条 17px 的空灰条**
    （不超预算、没有用量明细时也占着位置），看起来像内容没加载出来。

    判据：凡是「模板里带 `hidden` 属性、而样式表又给它设了 display」的类，
    必须有对应的 `[hidden]` 规则。静态断言只能钉住「规则在不在」——
    「浏览器算出来到底是不是 none」由 `scripts/shot_ai_drawer.py` 真渲染复核。
    """
    css = _read_style_css()
    risky = _classes_with_author_display(css)

    missing = []
    for name in TARGETS:
        for cls in sorted(_hidden_classes(name) & risky):
            if f'.{cls}[hidden]' not in css:
                missing.append(f'{name}: .{cls}')

    assert not missing, (
        '这些元素带 `hidden`、样式表却给它设了 display 而没有 `[hidden]` 覆盖，'
        '于是 `hidden` 完全不生效（作者来源的 display 盖掉浏览器默认的 `[hidden]`）：\n  '
        + '\n  '.join(sorted(set(missing)))
        + '\n改法：紧跟着那条规则补一句 `.<类名>[hidden] { display: none; }`，'
        '并写清它为什么必须显式写（参见隔壁几条的注释）。'
    )


# --------------------------------------------------------------------------
# 弹层与遮罩的层级：**必须成对抬**
# --------------------------------------------------------------------------

_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_Z_INDEX_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")


_VAR_FALLBACK = re.compile(r"var\(\s*--[\w-]+\s*,\s*(-?\d+)\s*\)")


def _z_index_of(css, selector):
    """样式表里某个选择器的 `z-index`（**先剥注释**）。

    这个文件里「注释提到 z-index」不是假设而是事实：下面那条规则上面的注释就写着
    「抽屉是 position: fixed + z-index: 1210」。不剥注释的话，一条只为说明而写的
    `z-index: 1210` 会被当成真规则 —— 本仓库在静态断言上踩过这个坑。

    取值有两种写法：裸数字、以及 `var(--z-drawer, 1210)`。后者按**兜底值**算 ——
    抽屉的变量只在部分模板里声明，兜底值才是这三张页面上真正生效的那个。
    """
    body = _CSS_COMMENT.sub(" ", css)
    for sel, block in _Z_INDEX_RULE.findall(body):
        if sel.strip() != selector:
            continue
        found = re.search(r"z-index\s*:\s*([^;]+)", block)
        if not found:
            return None
        raw = found.group(1).strip()
        if re.fullmatch(r"-?\d+", raw):
            return int(raw)
        fallback = _VAR_FALLBACK.search(raw)
        return int(fallback.group(1)) if fallback else None
    return None


def test_the_modal_stays_above_its_own_backdrop():
    """**Bootstrap 的遮罩必须始终在弹层下面。**

    Bootstrap 5.1 把这一对写死成遮罩 1050 / 弹层 1055（差 10），而抽屉是 1210 ——
    所以抽屉页上弹层要抬，遮罩也得跟着抬。原来的写法只抬了 `#aiUsageModal` /
    `#aiReportHistoryModal` 两个 id，遮罩却是**全局**抬到 1290，于是：

    * 同一张页面上**别的**弹层（合并视图的 AI 配置 / 知识库、周版本的 diffModal）
      变成遮罩 1290 压住弹层 1055；
    * `style.css` 是全站加载的，**没有抽屉的 AI 消耗面板**也拿到了 1290 的遮罩 ——
      点「全量重置」弹出来的确认框整个被罩住，确认词都输不进去。

    真浏览器复核（无头 Chrome + 系统 Chrome，`elementFromPoint` 命中的是
    `.modal-backdrop`、Playwright 报 `backdrop intercepts pointer events`）之后定下口径：
    **成对抬**。这条用例钉的就是「成对」这两个字。

    判据是**相对关系**而不是具体数值：谁把遮罩抬到弹层之上，无论抬到多少都算错。
    """
    css = _read_style_css()
    modal = _z_index_of(css, ".modal")
    backdrop = _z_index_of(css, "body > .modal-backdrop")

    assert modal is not None, "找不到 `.modal` 的 z-index 规则（Bootstrap 自己是 1055）"
    assert backdrop is not None, "找不到 `body > .modal-backdrop` 的 z-index 规则"
    assert backdrop < modal, (
        f"遮罩（{backdrop}）不低于弹层（{modal}）—— 整页只剩一层灰，"
        "弹层看不见也点不动（AI 消耗页的「全量重置」就是这么坏的）"
    )


def test_the_modal_pair_is_above_the_drawer():
    """反方向：在**有抽屉**的页面上，这一对必须都高于抽屉。

    只满足「遮罩 < 弹层」是不够的：把两个都设成 Bootstrap 原样的 1050/1055，
    抽屉（1210）就会压住弹层右半边 —— 那正是这条规则当初存在的原因
    （历次结论里「导出这一份 md」贴着右边缘，被抽屉盖住）。
    """
    css = _read_style_css()
    drawer = _z_index_of(_read("weekly_version_diff.html"), ".ai-drawer")
    assert drawer is not None, "周版本页里找不到 `.ai-drawer` 的 z-index"

    assert _z_index_of(css, "body > .modal-backdrop") > drawer, "遮罩没高过抽屉"
    assert _z_index_of(css, ".modal") > drawer, "弹层没高过抽屉"
