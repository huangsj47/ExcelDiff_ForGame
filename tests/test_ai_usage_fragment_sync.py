# -*- coding: utf-8 -*-
"""三份抽屉模板里新加的「本次消耗」片段必须**逐字相同**。

## 为什么需要这个护栏

`commit_diff_new.html` / `weekly_version_diff.html` / `merged_project_view.html` 的
AI 抽屉块是**逐字复制**的（仓库既有事实：`tests/test_ai_drawer_css_sync.py` 钉着其中
的 CSS 段）。这次新增的「本次消耗一行 + 明细弹层」DOM 同样落在三份里。

既有护栏只覆盖 CSS 块，DOM/JS 没有护栏 —— 于是「只改一处」这种最可能发生的改动不会
被任何测试拦住，而它的症状是**某一份模板的文案与另外两份不一致**（例如只有它把
「未上报」写成 0%），用户只会觉得「这个页面怪怪的」，不会想到去看另外两份。

所以这里按 token 把那段 HTML 抠出来逐字比对。用**定界注释**而不是行号定位：
行号会随无关改动漂移，而注释是这段代码自己的一部分。
"""
from __future__ import annotations

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

TEMPLATES = (
    "templates/commit_diff_new.html",
    "templates/weekly_version_diff.html",
    "templates/merged_project_view.html",
)

# 片段的两端（注释本身也在片段里，所以定界符要写在它里面）。
START = "<!-- AI 用量：本次消耗一行 + 明细弹层。"
END = "<script src=\"{{ url_for('static', filename='js/ai_usage_line.js') }}\"></script>"


def _source(name: str) -> str:
    return (PROJECT_ROOT / name).read_text(encoding="utf-8")


def _fragment(source: str) -> str:
    start = source.index(START)
    end = source.index(END, start) + len(END)
    return source[start:end]


@pytest.mark.parametrize("name", TEMPLATES)
def test_the_fragment_is_present(name):
    source = _source(name)
    assert START in source, f"{name} 里没有那一段（打开 AI 抽屉就看不到本次消耗）"
    assert END in source, f"{name} 里没有引用 static/js/ai_usage_line.js"


def test_the_three_fragments_are_byte_identical():
    """**核心回归**：三份逐字相同。改一处、忘了另外两处 → 这里红。"""
    fragments = {name: _fragment(_source(name)) for name in TEMPLATES}
    reference_name = TEMPLATES[0]
    reference = fragments[reference_name]
    for name, fragment in fragments.items():
        if fragment == reference:
            continue
        # 逐行指到第一处不同，而不是只丢一句「不相等」——
        # 三份都是一百多行的片段，让人肉去找差异等于没有护栏。
        reference_lines = reference.splitlines()
        fragment_lines = fragment.splitlines()
        difference = next(
            (
                f"第 {index + 1} 行：\n  {reference_name}: {left!r}\n  {name}: {right!r}"
                for index, (left, right) in enumerate(zip(reference_lines, fragment_lines))
                if left != right
            ),
            f"行数不同：{reference_name}={len(reference_lines)}，{name}={len(fragment_lines)}",
        )
        pytest.fail(f"{name} 的片段与 {reference_name} 不一致：\n{difference}")


def test_the_fragment_sits_outside_the_drawer():
    """弹层不能放在抽屉 `<aside>` 里面。

    抽屉是 `position: fixed` + 内部滚动（`.ai-drawer-body { overflow-y: auto }`），
    Bootstrap 的 modal 挂在那种容器里会被裁掉 —— 点「明细」看起来什么也没发生。
    """
    for name in TEMPLATES:
        source = _source(name)
        fragment_at = source.index(START)
        drawer_at = source.index('id="weeklyAiDrawer"') if "weeklyAiDrawer" in source else source.index('id="aiDrawer"')
        drawer_end = source.index("</aside>", drawer_at)
        assert fragment_at > drawer_end, f"{name} 的用量片段落在抽屉内部，弹层会被裁掉"


def test_the_fragment_is_inside_the_content_block():
    """它得在 `{% block content %}` 里：放进 head 之类的位置 DOM 根本不会出现。"""
    for name in TEMPLATES:
        source = _source(name)
        assert source.index("{% block content %}") < source.index(START)
        scripts_block = source.find("{% block scripts %}")
        if scripts_block != -1:
            assert source.index(START) < scripts_block, (
                f"{name} 的片段落在 `{{% block scripts %}}` 之后"
            )


def test_the_fragment_uses_the_globally_available_icon_library():
    """图标用 FontAwesome（base.html 全局引入）。

    抽屉模板里有 `bi bi-*` 的写法，但 Bootstrap Icons 只有 weekly 那份引了 ——
    merged 那份用的是 `fas`。共享片段必须用三份都能渲染的那一套。
    """
    fragment = _fragment(_source(TEMPLATES[0]))
    assert 'class="fas ' in fragment, "片段里没有用 FontAwesome 图标"
    assert "bi bi-" not in fragment, (
        "片段用了 bootstrap-icons —— 它没有被 base.html 全局引入，"
        "在 merged_project_view 里会渲染成空白"
    )


def test_the_detail_button_is_a_button_with_a_text_label():
    """「明细」要有文字标签，不能只放一个图标（读屏与触屏都要能用）。"""
    fragment = _fragment(_source(TEMPLATES[0]))
    assert "<button" in fragment and "明细" in fragment
    assert 'aria-hidden="true"' in fragment, "图标没有对读屏隐藏，会被念成乱码"
