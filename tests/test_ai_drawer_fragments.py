# -*- coding: utf-8 -*-
"""「历次结论」那段弹层在三份抽屉模板里必须**逐字相同**。

## 为什么需要这个护栏

`commit_diff_new.html` / `weekly_version_diff.html` / `merged_project_view.html` 的
AI 抽屉块是**逐字复制**的（仓库既有事实），「历次结论」这个弹层同样落在三份里。
既有护栏只覆盖 CSS 段（`test_ai_drawer_css_sync.py`）与「本次消耗」那一段
（`test_ai_usage_fragment_sync.py`），这一段的形状又和它们都不一样：

* **位置**：必须在 `</aside>` **外面**。抽屉是 `position: fixed` + 内部滚动，
  Bootstrap 的 modal 挂在里面会被裁掉 —— 表现是「点了按钮什么都看不见」，
  而 DOM 里它明明在。
* **定界注释**：用注释而不是行号定位（行号会随无关改动漂移，注释是这段代码自己的一部分）。

另外钉住「按钮在抽屉里、弹层在抽屉外」这一对位置关系：按钮挪到抽屉外，用户就看不见它；
弹层挪进抽屉里，用户就看不见内容。
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

START = "<!-- 历次结论：这个目标跑过的每一次"
END = "<!-- 历次结论 结束（以上这一段三份模板逐字相同）。 -->"

# 每份模板 footer 里那个按钮的 id（前两份是各自的既有前缀，第三份沿用周版本那一套）。
BUTTON_IDS = {
    "templates/commit_diff_new.html": "aiHistoryBtn",
    "templates/weekly_version_diff.html": "weeklyAiHistoryBtn",
    "templates/merged_project_view.html": "weeklyAiHistoryBtn",
}


def _source(name: str) -> str:
    return (PROJECT_ROOT / name).read_text(encoding="utf-8")


def _fragment(source: str) -> str:
    start = source.index(START)
    end = source.index(END, start) + len(END)
    return source[start:end]


@pytest.mark.parametrize("name", TEMPLATES)
def test_the_fragment_is_present(name):
    source = _source(name)
    assert START in source, f"{name} 里没有历次结论那一段"
    assert END in source, f"{name} 里没有那段的分隔注释（三份逐字比对的锚点）"
    assert 'id="aiReportHistoryModal"' in source
    assert 'id="aiReportHistoryBody"' in source


def test_the_three_fragments_are_byte_identical():
    """**核心回归**：三份逐字相同。改一处、忘了另外两处 → 这里红。"""
    fragments = {name: _fragment(_source(name)) for name in TEMPLATES}
    reference_name = TEMPLATES[0]
    reference = fragments[reference_name]
    for name, fragment in fragments.items():
        if fragment == reference:
            continue
        for index, (left, right) in enumerate(zip(reference.splitlines(),
                                                  fragment.splitlines())):
            if left != right:
                pytest.fail(
                    f"{name} 的历次结论片段与 {reference_name} 不一致"
                    f"（第 {index + 1} 行）：\n  {reference_name}: {left}\n  {name}: {right}"
                )
        pytest.fail(
            f"{name} 的历次结论片段与 {reference_name} 行数不同"
            f"（{len(reference.splitlines())} vs {len(fragment.splitlines())}）"
        )


@pytest.mark.parametrize("name", TEMPLATES)
def test_the_modal_is_outside_the_drawer(name):
    """在抽屉里会被 `position: fixed` + `overflow` 裁掉 —— 表现是「点了没反应」。"""
    source = _source(name)
    assert source.index('id="aiReportHistoryModal"') > source.index("</aside>"), (
        f"{name} 的历次结论弹层在抽屉里，会被裁掉"
    )


@pytest.mark.parametrize("name", TEMPLATES)
def test_the_button_is_inside_the_drawer_footer(name):
    """按钮反过来要在抽屉里（用户拉开抽屉就该看到它）。"""
    source = _source(name)
    button_id = BUTTON_IDS[name]
    assert f'id="{button_id}"' in source, f"{name} 没有历次结论按钮"
    assert source.index(f'id="{button_id}"') < source.index("</aside>"), (
        f"{name} 的历次结论按钮不在抽屉里"
    )
    # 文案要有（图标是 aria-hidden 的，纯图标按钮对读屏是没有名字的）
    assert "历次结论" in _fragment_button(source, button_id)
    assert 'class="fas ' in _fragment_button(source, button_id), (
        "图标必须是 fas（base.html 只加载 FontAwesome）"
    )
    assert 'aria-hidden="true"' in _fragment_button(source, button_id)


def _fragment_button(source: str, button_id: str) -> str:
    """按钮那几行（从 `<button` 到 `</button>`）。"""
    start = source.rindex("<button", 0, source.index(f'id="{button_id}"'))
    end = source.index("</button>", start) + len("</button>")
    return source[start:end]


def test_the_modal_has_a_title_and_a_close_button():
    """弹层要有可访问的名字（`aria-labelledby` 指向标题）与关闭按钮 —— 没有关闭按钮的
    弹层会把用户困在里面。"""
    for name in TEMPLATES:
        source = _source(name)
        assert 'aria-labelledby="aiReportHistoryTitle"' in source
        assert 'id="aiReportHistoryTitle"' in source
        assert "历次结论" in source
        fragment = _fragment(source)
        assert 'data-bs-dismiss="modal"' in fragment, f"{name} 的弹层没有关闭按钮"
        assert 'class="btn-close"' in fragment


def test_the_modal_starts_with_a_loading_line():
    """打开之前的内容：`加载中...`。**不能是空白** —— 空白会被读成「这个目标没有历史」。"""
    for name in TEMPLATES:
        assert "加载中..." in _fragment(_source(name))


def test_the_report_renderer_is_loaded_once_from_the_base_layout():
    """弹层里的报告用的是与抽屉同一套渲染器（`static/js/ai-report-markdown.js`：
    先整体转义、再套白名单）。

    它由 `base.html` 引一次（抽屉里那份报告就靠它），三份模板**不许**再各引一遍 ——
    引两遍会让同一个文件被解析两次（而且两份的加载顺序决定了谁赢，这才是难查的地方）。
    """
    base = _source("templates/base.html")
    assert "ai-report-markdown.js" in base, "base.html 没有引报告渲染器，弹层里就只剩纯文本了"
    for name in TEMPLATES:
        source = _source(name)
        assert "<script" not in source or "ai-report-markdown.js" not in "".join(
            line for line in source.splitlines() if "<script" in line
        ), f"{name} 自己又引了一遍报告渲染器"
