# -*- coding: utf-8 -*-
"""Excel diff 里「新增行 / 删除行 / 修改行」必须是同一套浅色底色。

## 线上反馈

新增行、删除行的底色太深（新增单元格是 #53d471 实底 + 黑色粗体、删除是 #f76571），
而修改行用的是浅色（新值行 #e8f5e8 / 旧值行 #ffebee）—— 同一张表里三套色，
深底还把「颜色即状态」之外的注意力全吸走。

## 现在的口径

三类共用**同一组 token**：

| 行 | 底色 | token |
|---|---|---|
| 新增行 / 新增单元格 | #e8f5e8 | `--color-success-bg-soft` |
| 删除行 / 删除单元格 | #ffebee | `--color-danger-bg-soft` |
| 修改行（旧值 / 新值） | #ffebee / #e8f5e8 | 同上两个 token |
| 行号格（深一档） | #d8f0da / #ffe0e4 | `--color-rownum-added-bg` / `--color-rownum-removed-bg` |

文字不再靠「深底 + 白字/黑粗体」补偿，改回深绿 #155724 / 深红 #721c24 常规字重 ——
与修改行的文字处理一致。

这组规则散落在三份 CSS（`diff-styles.css` / `excel-diff-new.css` /
`excel-scroll-fix.css`，后者最后加载才是最终生效值）与 `merge_diff.html` 的内联
`<style>` 里，改一处必须一起改，所以这里逐条钉住。
"""
from __future__ import annotations

import glob
import os
import re

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_ADDED = re.compile(r'excel-row-added|excel-cell\.excel-added|\.excel-added\b|excel-row-number\.excel-added')
_REMOVED = re.compile(r'excel-row-removed|excel-cell\.excel-removed|\.excel-removed\b|excel-row-number\.excel-removed')
_NOT_CLAUSE = re.compile(r':not\([^)]*\)')
_RULE = re.compile(r'([^{}]+)\{([^{}]*)\}', re.S)
_DECL = re.compile(r'(background(?:-color)?)\s*:\s*([^;]+)', re.I)
_BORDER_DECL = re.compile(r'border(?:-(?:top|right|bottom|left))?(?:-(?:color|width|style))?\s*:\s*([^;]+)', re.I)
# 行号格左侧那道状态色条是有意的行标记（不是单元格线框），单独放行
_ROW_NUMBER_ACCENT = re.compile(r'excel-row-number\.excel-(added|removed)')

# 允许的底色：同一套浅色 token，加两个「非状态色」（透明 / 白）
ALLOWED = (
    'var(--color-success-bg-soft', 'var(--color-danger-bg-soft',
    'var(--color-rownum-added-bg', 'var(--color-rownum-removed-bg',
    'transparent', '#fff', '#ffffff', 'none',
)
# 历史深色实底：出现即说明又有人把某一处改回深色
FORBIDDEN = ('#53d471', '#f76571', '#c62828', '#2e7d32', '#4caf50', '#ef5350', '#d32f2f', '#388e3c')
# 线框不许用的状态色（底色已经是浅色，线框再上状态色就是把每个格子框一圈）
FORBIDDEN_BORDER = ('#28a745', '#dc3545', '#f5c6cb', '#c3e6cb', '#53d471', '#f76571', '#c62828', '#2e7d32',
                    'var(--color-success', 'var(--color-danger')


def _sources():
    paths = sorted(glob.glob(os.path.join(PROJECT_ROOT, 'static', 'css', '*.css')))
    paths += sorted(glob.glob(os.path.join(PROJECT_ROOT, 'templates', '*.html')))
    return paths


def _added_removed_rules():
    """产出 (相对路径, 行号, 选择器, 规则体) —— 所有命中新增/删除行的规则。"""
    for path in _sources():
        with open(path, encoding='utf-8') as handle:
            text = handle.read()
        for match in _RULE.finditer(text):
            selector = _NOT_CLAUSE.sub('', match.group(1))
            if not (_ADDED.search(selector) or _REMOVED.search(selector)):
                continue
            line_no = text.count('\n', 0, match.start()) + 1
            yield (os.path.relpath(path, PROJECT_ROOT), line_no,
                   ' '.join(selector.split()), match.group(2))


def _row_rules():
    for path, line_no, selector, body in _added_removed_rules():
        backgrounds = [value.strip() for _prop, value in _DECL.findall(body)]
        if not backgrounds:
            continue
        yield path, line_no, selector, backgrounds


def test_added_and_removed_rows_use_the_shared_light_palette():
    problems = []
    for path, line_no, selector, backgrounds in _row_rules():
        for value in backgrounds:
            lowered = value.lower()
            if any(bad in lowered for bad in FORBIDDEN):
                problems.append(f'{path}:{line_no} {selector}：底色 {value!r} 是深色实底 —— '
                                f'新增/删除行必须与修改行同用浅色 token')
            elif not any(lowered.startswith(ok) for ok in ALLOWED):
                problems.append(f'{path}:{line_no} {selector}：底色 {value!r} 不在共用浅色里 —— '
                                f'要么换成 --color-success-bg-soft / --color-danger-bg-soft，'
                                f'要么说明为什么它是第三种色')
    assert not problems, '新增/删除行的底色没有和修改行统一到同一套浅色：\n  ' + '\n  '.join(problems)


def test_added_and_removed_text_is_not_compensating_for_a_dark_fill():
    """深底时代的补偿写法（黑色粗体 / 白色文字）不该再出现。"""
    problems = []
    for path in _sources():
        with open(path, encoding='utf-8') as handle:
            text = handle.read()
        for match in _RULE.finditer(text):
            selector = _NOT_CLAUSE.sub('', match.group(1))
            if not (_ADDED.search(selector) or _REMOVED.search(selector)):
                continue
            body = match.group(2)
            line_no = text.count('\n', 0, match.start()) + 1
            if re.search(r'color\s*:\s*#(000|000000|fff|ffffff)\b', body, re.I) and \
                    re.search(r'background[^;]*var\(--color-(success|danger)-bg-soft', body):
                problems.append(f'{path}:{line_no} {" ".join(selector.split())}：'
                                f'浅底上不该再用黑/白字补偿（用 #155724 / #721c24，与修改行一致）')
    assert not problems, '浅色底配上深底时代的文字补偿写法：\n  ' + '\n  '.join(problems)


def test_added_and_removed_cell_borders_are_the_table_default():
    """新增/删除行的**线框**必须是表格默认色，不能跟着状态变色。

    线上 5969：新增行每个格子的格线都是深绿 #28a745，而表里其余部分是灰线框 ——
    底色已经表达了状态，线框再上色就是把整行框起来，评审者的视线被框到格线上。

    行号格左侧那道 4px 状态色条（`excel-row-number.excel-added` /
    `.excel-removed` 上的 border-left）是有意的行标记，不在此列。
    """
    problems = []
    for path, line_no, selector, body in _added_removed_rules():
        if _ROW_NUMBER_ACCENT.search(selector):
            continue
        for value in _BORDER_DECL.findall(body):
            lowered = value.strip().lower()
            if any(bad in lowered for bad in FORBIDDEN_BORDER):
                problems.append(f'{path}:{line_no} {selector}：线框 {value.strip()!r} 用了状态色 —— '
                                f'新增/删除行必须用表格默认线框 var(--color-border, #dee2e6)')
    assert not problems, '新增/删除行的线框跟着状态变色了：\n  ' + '\n  '.join(problems)


def test_the_palette_tokens_exist():
    """三类底色共用的 token 必须真的定义在 style.css 里（否则 var() 回退到字面量，
    下次改色又会漏掉某一份）。"""
    with open(os.path.join(PROJECT_ROOT, 'static', 'css', 'style.css'), encoding='utf-8') as handle:
        tokens = handle.read()
    missing = [name for name in ('--color-success-bg-soft', '--color-danger-bg-soft',
                                '--color-rownum-added-bg', '--color-rownum-removed-bg')
               if name + ':' not in tokens]
    assert not missing, f'style.css 里缺 token：{missing}'


if __name__ == '__main__':
    import pytest
    pytest.main([__file__, '-v'])
