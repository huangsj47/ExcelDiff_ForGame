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
