# -*- coding: utf-8 -*-
"""AI 分析的说明文档与网页帮助必须与代码同源。

## 为什么值得有这一组

AI 分析这个功能，**「说明」本身就是它的接口**：报告有哪几章、门槛过滤掉什么、
它看不到什么。这些说法一旦与代码漂移，读文档的人会按错误的心智模型去用它 ——
而漂移**不会报错**，只会让人在错误的地方找答案（"为什么没有异常清单？"
"为什么报告说没问题？"）。

这里钉三类最易漂移的事实：

1. **报告章节**是运行期契约（`skill_contract.REPORT_SECTIONS`，改 SKILL.md 就必须同步改）。
2. **配置默认值**：文档里的表是给人看的，代码里的常量是生效的，两边只能有一份事实。
3. **帮助页的「看不到什么」那一节还在**。它是这个功能最重要的诚实声明（没有它，
   「报告没报」会被读成「没问题」），也最容易被后来者当成啰嗦删掉。
"""
from __future__ import annotations

import os
import re

from models.ai_analysis.project_config import (
    DEFAULT_MAX_ANALYSIS_ROUNDS,
    DEFAULT_MAX_ANOMALIES_PER_RUN,
    DEFAULT_MAX_FILES_PER_RUN,
    DEFAULT_MAX_TOOL_REQUESTS,
    DEFAULT_PROMPT_CHAR_BUDGET,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_WEEKLY_INTERVAL_MINUTES,
    MAX_ANALYSIS_ROUNDS_RANGE,
    MAX_ANOMALIES_PER_RUN_RANGE,
    MAX_FILES_PER_RUN_RANGE,
    MAX_TOOL_REQUESTS_RANGE,
    PROMPT_CHAR_BUDGET_RANGE,
    REQUEST_TIMEOUT_RANGE,
    WEEKLY_INTERVAL_RANGE,
)
from services.ai.skill_contract import DIMENSION_IDS, REPORT_SECTIONS

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELP_PAGE = os.path.join(PROJECT_ROOT, "templates", "help.html")
AI_DOC = os.path.join(PROJECT_ROOT, "docs", "AI分析使用说明.md")
README = os.path.join(PROJECT_ROOT, "README.md")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


# ==========================================================================
# 一、报告章节：契约改了，两份文档都得跟上
# ==========================================================================


def test_the_doc_lists_every_report_section():
    """报告章节是运行期契约。文档少写一章，用户就会以为那章不存在。"""
    doc = _read(AI_DOC)
    missing = [name for name in REPORT_SECTIONS if name not in doc]
    assert not missing, f"说明文档漏了报告章节：{missing}"


def test_the_help_page_lists_every_report_section():
    help_html = _read(HELP_PAGE)
    missing = [name for name in REPORT_SECTIONS if name not in help_html]
    assert not missing, f"帮助页漏了报告章节：{missing}"


def test_the_doc_names_every_dimension_id():
    """说明文档要写出**每个检查维度**的 id。

    文档里写 id 而不是只写中文名，是为了让这条断言能精确比对：中文名与 id 各写一套，
    加维度时漏改一边，读文档的人就会按一份缺了维度的清单去理解报告。
    """
    doc = _read(AI_DOC)
    missing = [dimension for dimension in DIMENSION_IDS if dimension not in doc]
    assert not missing, f"说明文档漏了检查维度：{missing}"


def test_the_help_page_tells_qa_about_coupling_analysis():
    """耦合分析是给 QA 看的重点，帮助页不能只字不提。

    它决定了 QA 拿到报告后会不会去关注「成对改动只改了一半」这类结论，
    以及看到「待确认的耦合点」时知不知道那是要人工确认另一端。
    """
    help_html = _read(HELP_PAGE)
    assert "模块之间的耦合" in help_html
    assert "只改了一边" in help_html
    assert "待确认的耦合点" in help_html, "没有说清「读不到另一端」时报告会怎么写"


# ==========================================================================
# 二、配置默认值：文档里的表不能与常量漂移
# ==========================================================================

# 界面标签 → (常量, 取值范围常量)。标签按 `docs/AI分析使用说明.md` 的表格写法。
_DOCUMENTED_DEFAULTS = {
    "分析间隔（分钟）": (DEFAULT_WEEKLY_INTERVAL_MINUTES, WEEKLY_INTERVAL_RANGE),
    "清单过长时的取样上限": (DEFAULT_MAX_FILES_PER_RUN, MAX_FILES_PER_RUN_RANGE),
    "最大分析轮次": (DEFAULT_MAX_ANALYSIS_ROUNDS, MAX_ANALYSIS_ROUNDS_RANGE),
    "上下文索取上限": (DEFAULT_MAX_TOOL_REQUESTS, MAX_TOOL_REQUESTS_RANGE),
    "提示词字符预算": (DEFAULT_PROMPT_CHAR_BUDGET, PROMPT_CHAR_BUDGET_RANGE),
    "单次请求超时（秒）": (DEFAULT_REQUEST_TIMEOUT_SECONDS, REQUEST_TIMEOUT_RANGE),
    "单次异常上限": (DEFAULT_MAX_ANOMALIES_PER_RUN, MAX_ANOMALIES_PER_RUN_RANGE),
}


def test_the_documented_defaults_and_ranges_match_the_code():
    """逐个断言「标签 | 默认值 | 范围」这一整行。

    只断言数字出现在文档里是不够的：`20` 这种值在正文里到处都是，改了默认值
    而文档没改时，弱断言照样绿。所以这里按表格单元格精确匹配。
    """
    doc = _read(AI_DOC)
    for label, (value, bounds) in _DOCUMENTED_DEFAULTS.items():
        low, high = bounds
        row = f"| {label} | {value} | {low}~{high} |"
        assert row in doc, f"说明文档里的配置表与代码不一致，期望这一行：{row}"


# ==========================================================================
# 三、最容易被删掉的那一节
# ==========================================================================


def test_the_help_page_keeps_the_honest_limits_section():
    """「它看不到什么」+「没报不等于没问题」必须留在帮助页里。

    这两句是整篇帮助里**信息量最大**的部分：没有它们，「报告里没提」会被读成
    「这里没问题」。它们也最像「啰嗦」而被人顺手删掉，所以钉住。
    """
    help_html = _read(HELP_PAGE)

    assert "它看不到什么" in help_html
    assert "不等于" in help_html, "没有写清「没报 ≠ 没问题」"
    assert "报告是辅助" in help_html, "没有写清报告不是验收结论"


def test_the_help_page_says_the_disposition_ui_does_not_exist_yet():
    """人工处置（待确认/已确认/已忽略）**界面上没有入口**，这一点必须写明。

    数据层与设计语义都已具备（含「文件再变会重新出现」），但界面没做。
    不写清楚，用户会在页面上反复找一个不存在的按钮。
    """
    help_html = _read(HELP_PAGE)
    assert "界面还没有入口" in help_html
    for label in ("待确认", "已确认", "已忽略"):
        assert label in help_html, f"帮助页没有提到处置状态「{label}」"


# ==========================================================================
# 四、帮助页自身的结构
# ==========================================================================


def test_every_help_tab_points_at_a_real_section():
    """tab 与 section 必须一一对应。

    改名或删节时最容易留下的就是「点了没反应」的空 tab —— 页面不报错，
    只是内容不出来，所以只能这样钉。
    """
    help_html = _read(HELP_PAGE)
    tabs = re.findall(r'data-section="([^"]+)"', help_html)
    sections = re.findall(r'<section class="help-section" id="([^"]+)"', help_html)

    assert tabs, "帮助页没有 tab"
    assert set(tabs) == set(sections), (
        f"tab 与 section 对不上：只在 tab 里 {set(tabs) - set(sections)}，"
        f"只在正文里 {set(sections) - set(tabs)}"
    )
    assert len(tabs) == len(set(tabs)), "有重复的 tab"
    # 顺序也要一致：tab 的顺序就是正文的顺序，错位会让人以为点错了
    assert tabs == sections, f"tab 顺序与正文顺序不一致：{tabs} vs {sections}"


def test_the_help_page_has_an_ai_section_with_a_stable_anchor():
    """AI 那一节要能被深链到（`/help#section-ai`），所以 id 不能随便改。"""
    help_html = _read(HELP_PAGE)
    assert 'id="section-ai"' in help_html
    assert '<span>AI 分析</span>' in help_html


# ==========================================================================
# 五、README 是入口
# ==========================================================================


def test_the_readme_links_the_ai_doc():
    readme = _read(README)
    assert "AI分析使用说明.md" in readme, "README 没有链到 AI 说明文档"
    assert os.path.exists(AI_DOC), "README 链的文档不存在"


def test_the_readme_lists_ai_analysis_as_a_feature():
    """README 的「核心功能」是这个平台的门面，AI 分析不能漏在外面。"""
    readme = _read(README)
    features = readme.split("## 核心功能（重点）")[1].split("## ")[0]
    assert "AI 变更风险分析" in features, "README 的核心功能里没有 AI 分析"
