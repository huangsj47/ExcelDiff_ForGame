# -*- coding: utf-8 -*-
"""列变更提示要**精简**：一行摘要 + 可展开的明细，明细一个字都不能少。

## 为什么改

线上反馈：一次提交动了 9 列，正文上方就铺出这样一行 ——

    列变更（9）：新增列 室外总威胁值；新增列 室内总威胁值；删除列 基础阈值；
    删除列 哨站刷怪随机池ID；删除列 补怪逻辑随机池ID；…（还有 4 条）

它占掉三四行高度、把真正的行变更挤出首屏，而且「新增列 X；删除列 Y；…」这种句子
读起来是一团。要求是**给个数量即可**。

但「只给数量」也不行：列改名/增删列是评审者要核对的改动本身（`header_changes` 这条
链就是为了让列变更不再隐形才建的，见 tests/test_column_identity_and_header_changes.py）。
所以取中间形态：摘要里给数量与分类（`新增 2 · 删除 6 · 改名 1`），明细收进
`<details>`，点开就是原来那一串 —— 不占地方，也没丢信息。

## 两条渲染路径都要改

`headerChangesHtml`（三张页面的 JS 拷贝，逐字节一致）与服务端
`templates/diff_partials/excel_diff.html` 的 Jinja 版本。两份实现各写一遍，
所以这里同时钉**形态**与**口径**（同一个输入，两条路径给出的数字与分类必须一样）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from html import unescape

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = shutil.which('node')
COMMIT_TEMPLATE = os.path.join(PROJECT_ROOT, 'templates', 'commit_diff.html')
WEEKLY_TEMPLATE = os.path.join(PROJECT_ROOT, 'templates', 'weekly_version_full_diff.html')
MERGE_TEMPLATE = os.path.join(PROJECT_ROOT, 'templates', 'merge_diff.html')
TEMPLATES = (COMMIT_TEMPLATE, WEEKLY_TEMPLATE, MERGE_TEMPLATE)

# 线上那一次的形态：2 个新增 + 6 个删除 + 1 个改名
CHANGES = (
    [{'change': 'added', 'column': '室外总威胁值', 'column_index': 6},
     {'change': 'added', 'column': '室内总威胁值', 'column_index': 7}]
    + [{'change': 'removed', 'column': f'删除{i}', 'old_name': f'删除{i}', 'column_index': 3 + i}
       for i in range(6)]
    + [{'change': 'renamed', 'column': '新名', 'old_name': '旧名', 'new_name': '新名',
        'column_index': 12}]
)

SHEET = {'headers': ['a', 'b'], 'rows': [], 'header_changes': CHANGES}


# --------------------------------------------------------------------------
#  JS 侧（三张页面共用同一份实现）
# --------------------------------------------------------------------------

HARNESS = """
const request = JSON.parse(process.argv[2]);
process.stdout.write(JSON.stringify({html: headerChangesHtml(request.sheet)}));
"""


def _extract(path, name):
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


def _render_js(template, sheet):
    if NODE is None:
        pytest.skip('本机没有 node，跳过（这是唯一能真跑模板 JS 的方式）')
    script = 'function escapeHtml(v){return String(v === undefined ? "" : v)' \
             '.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")' \
             '.replace(/"/g, "&quot;");}\n' + _extract(template, 'headerChangesHtml') + HARNESS
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as handle:
        handle.write(script)
        temp_path = handle.name
    try:
        proc = subprocess.run([NODE, temp_path, json.dumps({'sheet': sheet})],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, f'node 跑失败：{proc.stderr[:500]}'
        return json.loads(proc.stdout)['html']
    finally:
        os.unlink(temp_path)


# --------------------------------------------------------------------------
#  服务端 partial 侧
# --------------------------------------------------------------------------

def _render_partial(sheet):
    from jinja2 import Environment, FileSystemLoader

    def letter(index):
        result = ''
        while index >= 0:
            result = chr(65 + index % 26) + result
            index = index // 26 - 1
        return result

    env = Environment(
        loader=FileSystemLoader(os.path.join(PROJECT_ROOT, 'templates')), autoescape=True)
    env.filters['excel_column_letter'] = letter
    env.filters['format_cell_value'] = lambda value: '' if value is None else str(value)
    payload = {'type': 'excel', 'sheets': {'S': dict(sheet, has_changes=True)}}
    html = env.get_template('diff_partials/excel_diff.html').render(
        diff_data=payload, excel_data=payload)
    match = re.search(r'<details class="alert alert-info.*?</details>', html, re.S)
    assert match, f'列变更提示没有渲染：{html[:300]}'
    return match.group(0)


def _visible(markup):
    """去掉标签、还原实体之后看得见的文字。"""
    return unescape(re.sub(r'<[^>]+>', '', markup))


# --------------------------------------------------------------------------
#  形态：摘要给数量，明细在折叠块里
# --------------------------------------------------------------------------

class TestTheNoticeIsCompact:
    @pytest.mark.parametrize('template', TEMPLATES)
    def test_the_summary_carries_the_count_and_the_breakdown(self, template):
        markup = _render_js(template, SHEET)
        summary = re.search(r'<summary>.*?</summary>', markup, re.S).group(0)
        text = _visible(summary)

        assert '列变更（9）' in text, f'摘要里没有数量：{text!r}'
        assert '新增 2' in text and '删除 6' in text and '改名 1' in text, (
            f'摘要里没有分类计数：{text!r}'
        )

    @pytest.mark.parametrize('template', TEMPLATES)
    def test_the_long_list_is_collapsed_but_not_lost(self, template):
        markup = _render_js(template, SHEET)

        assert markup.startswith('<details'), (
            f'提示不再是可折叠的：{markup[:80]!r} —— 明细又铺回正文上方了'
        )
        assert 'excel-header-changes__list' in markup, '明细没有自己的容器'
        # 摘要之外的部分（= 折叠区）里，每一条都还在
        body = markup.split('</summary>', 1)[1]
        for item in ('新增列 室外总威胁值', '新增列 室内总威胁值', '删除列 删除5',
                     '列名 旧名 → 新名'):
            assert item in _visible(body), f'折叠区里少了「{item}」'

    @pytest.mark.parametrize('template', TEMPLATES)
    def test_the_summary_text_is_escaped(self, template):
        """列名来自被审核的 Excel（不可信）—— 摘要与明细都要转义。"""
        sheet = {'headers': [], 'rows': [],
                 'header_changes': [{'change': 'added', 'column': '<img src=x>',
                                     'column_index': 1}]}
        markup = _render_js(template, sheet)
        assert '<img' not in markup, f'列名没转义：{markup!r}'
        assert '&lt;img' in markup

    def test_no_notice_without_header_changes(self):
        for template in TEMPLATES:
            assert _render_js(template, {'headers': [], 'rows': []}) == ''


class TestBothRenderPathsAgreeOnTheNumbers:
    """两条路径各写一遍实现 —— 同一个输入，摘要口径必须一样。"""

    def test_the_server_side_summary_matches_the_client_side_one(self):
        client_summary = _visible(
            re.search(r'<summary>.*?</summary>', _render_js(WEEKLY_TEMPLATE, SHEET), re.S).group(0))
        server_summary = _visible(
            re.search(r'<summary>.*?</summary>', _render_partial(SHEET), re.S).group(0))

        for fragment in ('列变更（9）', '新增 2', '删除 6', '改名 1'):
            assert fragment in client_summary, f'前端摘要里少了「{fragment}」：{client_summary!r}'
            assert fragment in server_summary, (
                f'服务端摘要里少了「{fragment}」：{server_summary!r} —— '
                f'两条渲染路径的口径漂了'
            )

    def test_the_server_side_list_keeps_every_item(self):
        body = _render_partial(SHEET).split('</summary>', 1)[1]
        for item in ('新增列 室外总威胁值', '删除列 删除5', '列名 旧名 → 新名'):
            assert item in _visible(body), f'服务端折叠区里少了「{item}」'

    def test_the_server_side_is_collapsible_too(self):
        assert _render_partial(SHEET).startswith('<details')
