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
    DEFAULT_SUBAGENT_COUNT,
    DEFAULT_SUBAGENT_ENABLED,
    DEFAULT_SUBAGENT_VERIFY,
    DEFAULT_WEEKLY_INTERVAL_MINUTES,
    MAX_ANALYSIS_ROUNDS_RANGE,
    MAX_ANOMALIES_PER_RUN_RANGE,
    MAX_FILES_PER_RUN_RANGE,
    MAX_TOOL_REQUESTS_RANGE,
    PROMPT_CHAR_BUDGET_RANGE,
    REQUEST_TIMEOUT_RANGE,
    SUBAGENT_COUNT_RANGE,
    WEEKLY_INTERVAL_RANGE,
)
from services.ai.skill_contract import DIMENSION_IDS, REPORT_SECTIONS

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELP_PAGE = os.path.join(PROJECT_ROOT, "templates", "help.html")
AI_DOC = os.path.join(PROJECT_ROOT, "docs", "AI分析使用说明.md")
README = os.path.join(PROJECT_ROOT, "README.md")
# 人工处置面板。帮助页里写的那句「去哪儿点」必须指着它，所以两条一起断言。
ANOMALY_PANEL_JS = os.path.join(PROJECT_ROOT, "static", "js", "ai_anomaly_disposition.js")


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
    # 子代理模式（services/ai/subagent.py）。**开关那一行不在这个表里**（它没有数字），
    # 但「数量」这一行必须有 —— 它同时钉住默认值 3 与范围 1~6。
    "子代理数量": (DEFAULT_SUBAGENT_COUNT, SUBAGENT_COUNT_RANGE),
}


# 开关类的配置项：文档那一行的默认值是「开 / 关」，代码里是 `True` / `False`。
# 不放进上面那张数字表（那张表按 \`1~6\` 这样的范围列拼行），但**同样要钉**：
# 文档说「默认关」而代码是 `True`，读文档的人会以为这个功能不上线就生效。
_DOCUMENTED_SWITCHES = {
    "子代理模式（仅周版本）": DEFAULT_SUBAGENT_ENABLED,
    "对账轮（找反证，仅周版本）": DEFAULT_SUBAGENT_VERIFY,
}


def test_the_documented_switches_match_the_code():
    """开关的默认值：文档写「关」而代码是 `True` 是最危险的一种漂移。

    它不会报错，只会让用户在一次「我什么都没开」的分析里发现账单翻了几倍 ——
    或者反过来，以为某个功能默认就有。
    """
    doc = _read(AI_DOC)

    for label, default in _DOCUMENTED_SWITCHES.items():
        word = "开" if default else "关"
        row = f"| {label} | {word} | 开/关 |"
        assert row in doc, f"说明文档里的配置表与代码不一致，期望这一行：{row}"


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


def test_the_help_page_points_at_the_disposition_entry_that_really_exists():
    """人工处置的入口：**界面里真的有**，帮助页指的就是它。

    ## 这条此前是反的，值得记一笔

    上一版这里断言的是「帮助页必须写着『界面还没有入口』」—— 而那是个**假事实**：
    写入路径（`services/ai/anomaly_disposition.py` + 三个接口）与界面
    （`static/js/ai_anomaly_disposition.js`，挂在「历次结论」弹层里）都已经做完了。
    它一直没有红，因为它断言的是**那句话本身**，而不是那句话依赖的前提。

    这类守卫比没有更糟：它把一句过期的话钉得比事实还牢 —— 删掉那句话，
    红的是守卫而不是现实。所以现在**两边一起断言**：界面侧那个入口得真的在，
    帮助页写的得是它。
    """
    help_html = _read(HELP_PAGE)
    panel_js = _read(ANOMALY_PANEL_JS)

    # 界面侧：挂载点与三个动作都还在（它是「历次结论」弹层里那份结构化结论清单）。
    assert "aiAnomalyPanel" in panel_js, "处置面板的挂载点没了"
    for action in ("确认", "忽略", "撤销"):
        assert action in panel_js, f"处置面板里没有「{action}」这个动作"

    # 帮助页侧：写的是「去哪儿点」，不是「没有入口」。
    assert "界面还没有入口" not in help_html, (
        "帮助页还在说人工处置「界面还没有入口」—— 这已经不成立了，"
        "入口在「历次结论」弹层、报告正文下方那份结构化结论清单上"
    )
    assert "历次结论" in help_html, "帮助页没有把用户指到真实入口"
    for label in ("待确认", "已确认", "已忽略"):
        assert label in help_html, f"帮助页没有提到处置状态「{label}」"


def test_the_docs_describe_the_exported_document_the_code_actually_builds():
    """「导出 md」这件事**两边都要说，而且说的是同一份东西**。

    这是最容易被读错的一处：拿到文件的人会把它当成「模型发现的所有问题」的清单，
    而它其实只是**达到门槛的那部分**（没达到门槛的只写在报告正文里）。文档写了、
    附录开头也得写；附录开头写了、代码里那句常量就得在。
    """
    from services.ai import report_document

    doc_text = _read(AI_DOC)
    help_html = _read(HELP_PAGE)

    for label, text in (("说明文档", doc_text), ("帮助页", help_html)):
        assert "导出 md" in text, f"{label}没有写「导出 md」这个入口"
    assert "报告原文" in doc_text and "异常清单" in doc_text
    assert "元信息表" in help_html and "报告原文" in help_html

    # 三段式的顺序与代码一致（元信息 → 原文 → 附录）
    sample = report_document.build_report_markdown(
        project_label="项目", target_label="提交 abc", report_text="# 变更理解\n正文",
        anomalies=[{"title": "一条"}],
    )
    assert sample.index("| 项目 |") < sample.index("# 变更理解") < sample.index(
        report_document.APPENDIX_TITLE
    )
    assert report_document.APPENDIX_INTRO in sample, "附录开头那句「不是全部」没了"

    # 「附录里有处置列」这件事：文档与代码必须同时成立。这一列是**导出那一刻**现查的
    # 人工处置状态（不是模型结论的一部分），所以同一份报告隔天再导可能不一样 ——
    # 文档必须说清这一点，否则两次导出拿到不同结果的人会以为平台在改历史。
    assert "「处置」这一列" in doc_text
    assert "导出那一刻" in doc_text
    assert "| 维度 | 处置 |" in sample
    assert report_document.APPENDIX_INTRO in sample, "附录开头那句「不是全部」没了"
    assert "导出这一刻" in report_document.APPENDIX_INTRO


def test_the_readme_mentions_the_export_and_the_two_tabs():
    """README 的功能清单是门面：抽屉里有什么，它得说到。"""
    readme = _read(README)
    features = readme.split("## 核心功能（重点）")[1].split("## ")[0]
    assert "两个标签" in features
    assert "导出" in features, "README 没提导出"
    assert "历次结论" in features, "README 没提「翻历次结论」"


def test_the_docs_describe_the_history_entry_the_code_serves():
    """「历次结论」是用户报的那个问题的解法（重新分析后看不到旧结论），
    三处说明都要有它，而且说的必须是**同一个窗口**（与保留策略同一天数）。"""
    from services.ai_analysis_service import ANALYSIS_CACHE_DAYS
    from services.ai_report_history_service import (
        DEFAULT_HISTORY_LIMIT,
        MAX_HISTORY_LIMIT,
    )

    doc_text = _read(AI_DOC)
    help_html = _read(HELP_PAGE)

    for label, text in (("说明文档", doc_text), ("帮助页", help_html)):
        assert "历次结论" in text, f"{label}没有写「历次结论」"
    assert "正在分析时也能点" in doc_text or "正在分析时也能点" in help_html
    # 窗口与保留策略同源：文档里写的那天数必须是代码里那个数。
    #
    # **不能只断 `str(ANALYSIS_CACHE_DAYS) in doc_text`**：那份文档里 `90` 还出现在
    # 「P90」（整表统计那一节）与「从 800 涨到 900」（历史对照那一节）里，所以把保留
    # 窗口从 90 天改成 30 天时，只要文档里还剩任何一个 `90`，这条断言照样绿 ——
    # 而「天数与代码同源」恰恰是这条用例存在的**唯一理由**。带上单位才是对着
    # **那一句话**断；文档里同一句还写着它与保留策略同源，一并钉住。
    window_sentence = f"默认 {ANALYSIS_CACHE_DAYS} 天"
    assert window_sentence in doc_text, (
        f"说明文档里的窗口天数与代码不一致：找不到「{window_sentence}」"
    )
    assert "与平台保留策略同一天数" in doc_text, (
        "文档没写明这个窗口与平台保留策略是同一个数 —— 两个数各写各的就会各自漂移"
    )
    assert DEFAULT_HISTORY_LIMIT <= MAX_HISTORY_LIMIT


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


# ==========================================================================
# 六、给用户看的话里不许出现他打不开的东西
# ==========================================================================


# 这些是**仓库里**的路径。帮助页的读者只有浏览器，没有仓库。
_DEAD_END_PATH_HINTS = ("docs/", "skills/", "scripts/", "README", "requirements")


def test_the_help_page_does_not_point_at_files_the_reader_cannot_open():
    """帮助页里的「详见 …」必须是读者能打开的东西。

    ## 这条是被用户发现的

    AI 那一节原来以「详见 `docs/AI分析使用说明.md`」结尾。写这句话的人手边就有仓库，
    而读它的人只有这个平台 —— 那句话对读者是一条死路，他点了打不开，也不知道该去哪儿问。

    这类话很容易被顺手写上去（「细节在文档里」在心里是对的），所以只能靠一条断言拦：
    **要讲的内容就写在页面上**，写不下就先精简再写；真要指向别处，只能是平台里的页面。

    注：`docs/` 这类路径**写给维护者看**是合理的（README、代码注释、docs 目录内部互相
    引用都不受这条约束）—— 这条只管用户界面的模板。
    """
    help_html = _read(HELP_PAGE)
    found = [hint for hint in _DEAD_END_PATH_HINTS if hint in help_html]
    assert not found, (
        f"帮助页里出现了读者打不开的仓库路径 {found} —— 把要讲的内容写进页面，"
        f"或者指向平台里真实存在的页面"
    )
