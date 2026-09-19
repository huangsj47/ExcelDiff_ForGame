# -*- coding: utf-8 -*-
"""AI 抽屉的「思考过程 / 完整结论」两个标签：三份模板的 DOM 必须**逐字相同**。

## 为什么要逐字比对

三份模板的 AI 抽屉块是逐字复制的（既有事实，`tests/test_ai_drawer_css_sync.py` 钉着
CSS 段、`tests/test_ai_usage_fragment_sync.py` 钉着「本次消耗」那一段）。这次新增的
标签条 + 思考面板同样落在三份里，所以用**同一套做法**再钉一次：按定界注释抠出片段，
三份逐字比。

只改一处的症状不是报错，而是**某一份模板上少一个标签、或者两个面板同时显示**——
用户只会觉得「这个页面怪怪的」，不会想到去看另外两份。

## 另外三条一起守

1. **`.ai-analysis-output` 还在**：那是「完整结论」面板本身，`set*Report` 写
   `textContent`、`render*` 摘 `is-empty`（`tests/test_ai_report_markdown.py` 与
   `tests/test_ai_report_is_left_aligned.py` 钉着）。把正文框换成新元素 = 一次静默回归。
2. **两个面板把 `aria-labelledby` 指回标签**：面板那一侧的反向声明是这个 tabs 结构里
   唯一的方向（标签上**刻意不写** `aria-controls`，因为正文框的 id 三份不同）。
3. **图标是 `fas` 且带文字标签**：这一页引的图标字体只有 FontAwesome
   （见 `tests/test_ai_*.py` 的既有口径），写成 `bi bi-*` 就是一个不存在的图标；
   只给图标不给字，读屏用户听到的是空按钮。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

TEMPLATES = (
    "templates/commit_diff_new.html",
    "templates/weekly_version_diff.html",
    "templates/merged_project_view.html",
)

# 片段的两端（定界注释本身也在片段里）。用注释而不是行号定位：行号会随无关改动漂移。
START = "<!-- AI 抽屉的两个标签：思考过程 / 完整结论。"
END = "<!-- AI 抽屉的两个标签 结束（以上这一段三份模板逐字相同）。 -->"

# 「完整结论」那个面板就是各页原有的正文框 —— id 三份里只有两种，逐份点名。
OUTPUT_IDS = {
    "templates/commit_diff_new.html": "aiAnalysisOutput",
    "templates/weekly_version_diff.html": "weeklyAiOutput",
    "templates/merged_project_view.html": "weeklyAiOutput",
}

# 两个模块三份都要引：标签的状态机与逐轮渲染器各只有一份实现。
SHARED_MODULES = ("js/ai_drawer_tabs.js", "js/ai_think_log.js")


def _source(name: str) -> str:
    return (PROJECT_ROOT / name).read_text(encoding="utf-8")


def _fragment(source: str) -> str:
    start = source.index(START)
    end = source.index(END, start) + len(END)
    return source[start:end]


def _markup(source: str) -> str:
    """剥掉 HTML 注释再断言**结构**。

    这一段片段的注释里逐字写着「不写 aria-controls」这种话 —— 不剥注释，
    断言 `aria-controls not in fragment` 会因为注释自身而红（假失败），
    而反过来，注释里写着一个标签名也会让 `count(...) == N` 变成假通过。
    """
    return re.sub(r"<!--.*?-->", " ", _fragment(source), flags=re.S)


def _tab_init(source: str) -> str:
    """`AiDrawerTabs.init({...})` 那一次调用（跨行的整段）。"""
    start = source.index("AiDrawerTabs.init(")
    end = source.index("});", start) + len("});")
    return source[start:end]


@pytest.mark.parametrize("name", TEMPLATES)
def test_the_fragment_is_present(name):
    source = _source(name)
    assert START in source, f"{name} 里没有标签那一段（抽屉里就没有两个标签）"
    assert END in source, f"{name} 里没有那一段的结束定界注释"


def test_the_three_fragments_are_byte_identical():
    """**核心回归**：三份逐字相同。改一处、忘另外两处 → 这里红。"""
    fragments = {name: _fragment(_source(name)) for name in TEMPLATES}
    reference = fragments[TEMPLATES[0]]
    for name, fragment in fragments.items():
        if fragment == reference:
            continue
        pytest.fail(
            f"{name} 的标签片段与 {TEMPLATES[0]} 不一致。\n"
            "三份模板的抽屉 DOM 是逐字复制的，改动请三处一起改。\n"
            f"--- {TEMPLATES[0]} ---\n{reference}\n--- {name} ---\n{fragment}"
        )


@pytest.mark.parametrize("name", TEMPLATES)
def test_the_tabs_are_a_real_tablist(name):
    """`role=tab` / `aria-selected` / `role=tabpanel` —— 少一样读屏就读不出来。"""
    markup = _markup(_source(name))
    assert markup.count('role="tablist"') == 1
    assert markup.count('role="tab"') == 2
    assert markup.count('role="tabpanel"') == 1, "思考面板那一个（正文框在片段之外）"
    assert markup.count('aria-selected=') == 2
    # 初始态：完整结论被选中、思考面板藏着（服务端渲染出来就该是这个样子）。
    assert 'id="aiDrawerTabReport" role="tab"' in markup
    # 漫游 tabindex：组内只有一个是可 Tab 到的（否则按 Tab 要按两次才越过标签条）。
    assert markup.count('tabindex="0"') == 2, "两个标签里一个 + 思考面板本身"
    assert markup.count('tabindex="-1"') == 1


@pytest.mark.parametrize("name", TEMPLATES)
def test_the_report_panel_is_still_the_original_output_box(name):
    """正文框没有被换掉，而且它自己就是「完整结论」那个 panel。"""
    source = _source(name)
    output_id = OUTPUT_IDS[name]
    assert f'class="ai-analysis-output is-empty" id="{output_id}"' in source
    assert f'id="{output_id}" role="tabpanel"' in source
    assert 'aria-labelledby="aiDrawerTabReport"' in source


@pytest.mark.parametrize("name", TEMPLATES)
def test_each_panel_points_back_at_its_tab(name):
    markup = _markup(_source(name))
    assert 'aria-labelledby="aiDrawerTabThink"' in markup
    # 标签上刻意不写 `aria-controls`：正文框的 id 三份不同，写死一个会指错。
    assert "aria-controls" not in markup


@pytest.mark.parametrize("name", TEMPLATES)
def test_the_icons_are_fontawesome_and_labelled(name):
    markup = _markup(_source(name))
    assert "bi bi-" not in markup, "这一页只有 FontAwesome，bi-* 是不存在的图标"
    assert markup.count('aria-hidden="true"') == 2
    assert "思考过程" in markup and "完整结论" in markup


@pytest.mark.parametrize("name", TEMPLATES)
def test_both_modules_are_loaded(name):
    source = _source(name)
    for module in SHARED_MODULES:
        assert f"filename='{module}'" in source, f"{name} 没有引 {module}"


@pytest.mark.parametrize("name", TEMPLATES)
def test_the_init_call_passes_this_page_s_report_panel(name):
    """`reportPanel` 必须逐份传对：传成另一份的 id，标签就切不动正文框。"""
    source = _source(name)
    init = _tab_init(source)
    assert OUTPUT_IDS[name] in init, f"{name} 的 AiDrawerTabs.init 没有传自己的正文框 id"
    assert "onShowThink" in init, "切到「思考过程」时要顺手去取落库的逐轮明细"


def _function_source(source: str, name: str) -> str:
    """按大括号配对抠出一个函数声明（与 test_ai_report_is_left_aligned.py 同一做法）。"""
    start = source.index(f"function {name}(")
    depth = 0
    for index in range(source.index("{", start), len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"function {name}( 的大括号没有闭合")


@pytest.mark.parametrize("name", TEMPLATES)
def test_the_wiring_never_sits_in_the_sandboxed_report_functions(name):
    """新接线不许出现在 `set*Report` / `render*` / `append*` / `schedule*` 里。

    那四个函数被 `tests/test_ai_report_is_left_aligned.py` 抠进一个**裸 node 沙箱**
    （只有一个极简的假 DOM），里面引用 `AiDrawerTabs` / `AiThinkLog` 会直接
    `ReferenceError` —— 而那条测试是这个仓库最要紧的一条排版回归。
    """
    source = _source(name)
    pairs = (
        ("setAiReport", ("renderAi", "appendAiLine", "scheduleAi")),
        ("setWeeklyAiReport", ("renderWeeklyAi", "appendWeeklyAiLine", "scheduleWeeklyAi")),
    )
    for head, others in pairs:
        if f"function {head}(" not in source:
            continue
        for func in (head, *others):
            body = _function_source(source, func)
            for global_name in ("AiDrawerTabs", "AiThinkLog"):
                assert global_name not in body, (
                    f"{name} 的 {func}() 里引用了 {global_name} —— 裸 node 沙箱会 ReferenceError"
                )
