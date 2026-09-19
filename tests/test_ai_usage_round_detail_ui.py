# -*- coding: utf-8 -*-
"""消耗面板的**逐轮明细**：这一轮到底看没看到东西。

## 这条是被什么逼出来的（2026-09-19）

一次周版本分析的报告写着「未读到任何代码 diff」，而面板上那一轮看起来一切正常 ——
逐轮表只有计数（索取 6 次、执行 6 条、丢弃 0 条）。三种可能（模型没要 / 取数失败 /
被预算拒了）在那三列上长得一模一样，而取不到时 provider 给的那句话
（`[取数失败] xxx：项目没有绑定 Agent 节点`）**看着像内容**、字数也不为 0。

后端把证据落库了（`services/ai/trace_evidence.py`），这一组守的是**它真的显示出来**：

1. 取不到的条目要带着原因出现在明细里（红字），而「工具明确回了没有内容」不是失败；
2. 老运行（没有明细）要如实说未采集，而不是显示成「什么都没要」；
3. 模型原文按 `textContent` 渲染（它是模型给的字符串，不能当 HTML 解析）；
4. 逐轮表多了一列之后，空态那一行的 `colSpan`、宽屏横向滚动、键盘可达都要跟上。

渲染是**用 node 真跑模板里那段脚本**（同一个 DOM stub 驱动，
`tests/test_ai_usage_budget_ui.py` 的 `_run_node`）—— 静态断言只看得见字符串，
而这里要断言的是「渲染出来的东西长什么样」。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.test_ai_usage_budget_ui import (
    _dashboard_script,
    _function_body,
    _function_source,
    _run_node,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = PROJECT_ROOT / "templates" / "ai_usage_dashboard.html"


def _html() -> str:
    return DASHBOARD.read_text(encoding="utf-8")


# 探针：把 `renderRoundDetail` **真跑**一遍，再把渲染出来的子树拍成 JSON。
#
# `renderRoundDetail` 在 IIFE 里，外面拿不到 —— 用 `_function_source` 把模板里逐字的
# 那段源码求值进同一个上下文（与 `fillPeriodSelect` 同一个做法）。
_ROUND_OUT = """function (A, sandbox, makeElement) {
    if (typeof sandbox.__probeRenderRoundDetail !== 'function') return null;
    function snapshot(node) {
        return {
            tag: node.tagName,
            className: node.className || '',
            text: node.textContent || '',
            hidden: !!node.hidden,
            colSpan: node.colSpan || 0,
            children: (node.children || []).map(snapshot)
        };
    }
    // 把所有文本拼起来便于断言「这句话出现了没有」，同时保留结构给「在哪一层」用。
    function flat(node, out) {
        out = out || [];
        out.push(node.tagName + '|' + (node.className || '') + '|' + (node.textContent || ''));
        (node.children || []).forEach(function (child) { flat(child, out); });
        return out;
    }
    return A.rounds.map(function (round) {
        const host = makeElement('div');
        sandbox.__probeRenderRoundDetail(host, round);
        return {tree: snapshot(host), flat: flat(host)};
    });
}"""

_ROUND_CASES = {
    "rounds": [
        # 0：一条取不到（失败）+ 一条正常 + 索取 + 丢弃 + 预算说明 + 纠正提示 + 原文
        {
            "round_index": 3,
            "requests": [{"text": "file_diff a1b2c3d4 code/a.lua"},
                         {"text": "file_content a1b2c3d4 code/b.lua lines=1180-1260"}],
            "executed": [
                {"label": "file_diff code/a.lua", "chars": 0, "failed": True, "empty": False,
                 "reason": "[取数失败] code/a.lua：项目没有绑定 Agent 节点", "truncated": False},
                {"label": "file_content code/b.lua", "chars": 8123, "failed": False,
                 "empty": False, "reason": "", "truncated": True},
            ],
            "dropped": [{"reason": "超出本次工具请求总预算（20 次），未执行",
                         "detail": "file_diff code/c.lua"}],
            "budget_notes": "有 1 个上下文请求因超出本次索取额度而未执行。",
            "correction_hint": "你上一轮的返回不符合协议：…",
            "response_text": '{"status": "need_more_context"}',
        },
        # 1：工具明确回了「确实没有内容」—— **不是**失败
        {
            "round_index": 4,
            "requests": [], "dropped": [], "budget_notes": "", "correction_hint": "",
            "response_text": "{}",
            "executed": [{"label": "file_content code/empty.lua", "chars": 0, "failed": False,
                          "empty": True, "reason": "", "truncated": False}],
        },
        # 2：老行（这一层之前写下的）：只有计数，没有任何明细
        {"round_index": 1, "response_text": "", "requests": [], "executed": [],
         "dropped": [], "budget_notes": "", "correction_hint": ""},
        # 3：模型原文里带着 HTML —— 必须原样当文本，不能解析
        {
            "round_index": 5, "requests": [], "executed": [], "dropped": [],
            "budget_notes": "", "correction_hint": "",
            "response_text": '<img src=x onerror="alert(1)"> & "引号"',
        },
        # 4：原文很长（后端已按 4000 字截断）→ 标题要说明
        {
            "round_index": 6, "requests": [], "executed": [], "dropped": [],
            "budget_notes": "", "correction_hint": "",
            "response_text": "x" * 4000,
        },
    ]
}


@pytest.fixture(scope="module")
def rendered() -> list:
    script = _dashboard_script()
    # `renderRoundDetail` 依赖同在这一层里的两个小工具，所以三个一起求值进上下文，
    # 末尾再把要调用的那个挂出来（`probe_name=""` 表示不自动加赋值前缀）。
    probe = "\n".join(
        _function_source(script, name) for name in ("roundBlock", "roundNote", "renderRoundDetail")
    ) + "\n__probeRenderRoundDetail = renderRoundDetail;"
    out = _run_node(script, _ROUND_CASES,
                    probe_source=probe, probe_name="",
                    out_source=_ROUND_OUT)
    assert out is not None, "探针没跑起来（renderRoundDetail 改名了？）"
    return out


class TestTheFailureIsVisible:
    """**这一组是这次改动的理由**：取数失败必须在明细里看得出来、带原因。"""

    def test_the_failed_item_says_why_and_is_marked(self, rendered):
        flat = rendered[0]["flat"]

        # 认到**那一条**上（标题行里也有「取不到」三个字，按关键字抓会抓错）。
        failed = [line for line in flat if "file_diff code/a.lua" in line]
        assert failed, f"取数失败的那一条没有出现在明细里：{flat}"
        assert "项目没有绑定 Agent 节点" in failed[0], f"没带上原因：{failed[0]}"
        assert "aiu-round-failed" in failed[0], (
            f"取不到的那一条没有用警示色标出来（读的人会当成正常拿到）：{failed[0]}"
        )

    def test_a_real_answer_is_not_marked_as_failed(self, rendered):
        flat = rendered[0]["flat"]

        ok = [line for line in flat if "file_content code/b.lua" in line]
        assert ok, flat
        assert "取不到" not in ok[0], f"正常拿到的那条被标成了取不到：{ok[0]}"
        assert "aiu-round-failed" not in ok[0], ok[0]
        assert "8,123 字" in ok[0], f"字数要显示：{ok[0]}"
        assert "已截断" in ok[0], f"截断要说明：{ok[0]}"

    def test_the_count_in_the_title_gives_the_ratio(self, rendered):
        flat = rendered[0]["flat"]

        title = [line for line in flat if "实际拿到" in line]
        assert title, flat
        assert "2 条" in title[0] and "1 条取不到" in title[0], title[0]

    def test_an_empty_answer_is_not_a_failure(self, rendered):
        """工具明确回「确实没有内容」时不许说成取不到 —— 两者在协议里是两件事。"""
        flat = rendered[1]["flat"]

        empty = [line for line in flat if "code/empty.lua" in line]
        assert empty, flat
        assert "确实没有内容" in empty[0], empty[0]
        assert "取不到" not in empty[0], f"「没有内容」被说成了「取不到」：{empty[0]}"
        assert "aiu-round-failed" not in empty[0], empty[0]

    def test_the_dropped_items_say_where_and_why(self, rendered):
        flat = rendered[0]["flat"]

        dropped = [line for line in flat if "未执行" in line]
        assert dropped, flat
        assert "code/c.lua" in dropped[0], f"丢了哪一条要说：{dropped[0]}"


class TestTheRestOfTheEvidenceShows:
    def test_the_requests_are_listed_verbatim(self, rendered):
        flat = rendered[0]["flat"]

        assert any("file_diff a1b2c3d4 code/a.lua" in line for line in flat), flat
        assert any("lines=1180-1260" in line for line in flat), (
            "模型点名的行窗口也要原样显示（它决定了它看到的是哪一段）"
        )
        assert any("模型点名索取（2 条）" in line for line in flat), flat

    def test_the_budget_note_and_the_correction_hint_show(self, rendered):
        flat = rendered[0]["flat"]

        assert any("超出本次索取额度" in line for line in flat), flat
        assert any("不符合协议" in line for line in flat), flat

    def test_a_legacy_round_says_it_was_not_collected(self, rendered):
        """老运行没有明细 —— 如实说「未采集」，不能说成「什么都没要」。"""
        flat = rendered[2]["flat"]

        assert any("未采集" in line for line in flat), flat
        assert not any("模型点名索取" in line for line in flat), (
            f"老行被渲染成了「什么都没有要」：{flat}"
        )

    def test_the_model_output_lands_in_a_pre_as_text(self, rendered):
        def walk(node):
            yield node
            for child in node["children"]:
                yield from walk(child)

        pres = [node for node in walk(rendered[3]["tree"]) if node["tag"] == "pre"]
        assert len(pres) == 1, f"模型原文要放在 <pre> 里：{rendered[3]['tree']}"
        assert pres[0]["text"] == '<img src=x onerror="alert(1)"> & "引号"', pres[0]
        assert pres[0]["children"] == [], (
            f"原文里有 HTML —— 它被当标记解析了（模型输出不能当 HTML）：{pres[0]}"
        )
        assert pres[0]["className"] == "aiu-round-raw", pres[0]

    def test_a_truncated_answer_says_so(self, rendered):
        flat = rendered[4]["flat"]

        assert any("4000 字截断" in line for line in flat), (
            f"原文被截断时要说出来（否则读的人以为模型只说了这些）：{flat[:6]}"
        )

    def test_the_pre_is_reachable_by_keyboard(self):
        """可滚动容器要拿得到焦点（与表格容器同一条要求，静态断言：stub 测不到）。"""
        body = _function_body(_dashboard_script(), "renderRoundDetail")

        assert "setAttribute('tabindex', '0')" in body, (
            "模型原文那个 <pre> 是可滚动的（max-height: 220px），键盘用户够不着下半段"
        )


class TestTheRoundTableGrewAColumn:
    """逐轮表多了一列「明细」，几处跟列数有关的地方都要跟上。"""

    def test_the_header_has_the_new_column(self):
        script = _dashboard_script()

        assert script.count('<th scope="col">明细</th>') == 1, "明细列的表头"
        # 列数只有一个来源（`roundColumns`），而它现在**随「这次分没分片」变**：
        # 分片那一列只在真的分了片时才加（见 `TestTheSliceColumn`）。
        assert "var roundColumns = hasSlices ? 11 : 10" in script, (
            "列数要有一个名字 —— 空态那一行的 colSpan 与它必须是一致的"
        )

    def test_the_empty_row_uses_the_same_column_count(self):
        script = _dashboard_script()

        start = script.index("if (!(payload.rounds || []).length) {")
        block = script[start:start + 300]
        assert "td.colSpan = roundColumns" in block, f"空态那一行的列数还是写死的：{block}"

    def test_every_round_gets_a_collapsed_detail_row(self):
        script = _dashboard_script()
        body = _function_body(script, "roundDetailCell")

        assert "detailRow.hidden = true" in body, "明细行默认要是收起的"
        assert "aria-expanded" in body, "展开状态要报给读屏"
        # 按需渲染：几十轮全渲染出来会让这个弹层又长又慢
        assert "renderRoundDetail(detailCell, round)" in body
        assert "detailCell.childNodes.length" in body, "少了「只渲染一次」的判断"
        assert "tbody.appendChild(actionCell.aiuDetailRow)" in script, (
            "明细行没有被挂进表里 —— 点了「明细」什么都不会出现"
        )

    def test_the_caption_says_what_the_detail_column_is(self):
        script = _dashboard_script()

        # caption 现在是拼出来的（分片那一列可加可不加），所以按它的**拼接片段**断言。
        start = script.index("var caption = '每一轮")
        block = script[start:script.index("id=\"aiuRoundTableCaption\">' + caption", start)]
        assert "明细" in block, (
            f"新列要说清它是什么，否则读的人只会看到一列表头写着「明细」：{block}"
        )
        # 那块区域的名字仍然指向一个真的存在的 id。
        assert "id=\"aiuRoundTableCaption\">' + caption + '<" in script, (
            "aria-labelledby 指的那个 caption 不存在"
        )


class TestTheRunToolTable:
    """这次运行**按工具类型**的账（页面级那张表没有「失败」列，这里要有）。"""

    def test_the_failed_column_exists_and_is_highlighted(self):
        script = _dashboard_script()
        body = _function_body(script, "runToolSection")

        assert "'failed'" in body, "没有失败列 —— 而它正是「取不到」的那个数"
        assert "aiu-round-failed" in body, "失败次数要一眼看得出来"

    def test_its_wrap_is_reachable_by_keyboard_and_named(self):
        script = _dashboard_script()
        body = _function_body(script, "runToolSection")

        assert "setAttribute('role', 'region')" in body, body
        assert "setAttribute('tabindex', '0')" in body, body
        assert "aiuRunToolTableCaption" in body, body
        # 区域名必须指向一个真的存在的 caption（同一句话只写一遍）
        assert re.search(r'<caption class="aiu-caption" id="aiuRunToolTableCaption"', script), (
            "aria-labelledby 指的那个 caption 不存在"
        )

    def test_it_is_only_built_when_there_is_something_to_show(self):
        script = _dashboard_script()

        assert "if (kinds.length) {" in script and "runToolSection(tools, kinds)" in script, (
            "没有工具统计时不该渲染出一张空表（老运行没有这一块）"
        )


class TestTheStylingFollowsTheHouseRules:
    def test_the_new_colors_go_through_tokens(self):
        """组件里不写裸 hex（`--aiu-danger` 那条注释里记着实测对比度）。"""
        html = _html()
        css = html[html.index(".aiu-round-actions"): html.index(".aiu-round-raw:focus-visible")]

        assert "var(--aiu-danger)" in css, "取不到的红要走令牌"
        assert not re.search(r"#[0-9a-fA-F]{3,6}", css), (
            f"新增样式里出现了裸 hex：{re.findall(r'#[0-9a-fA-F]{3,6}', css)}"
        )

    def test_the_detail_text_is_not_an_emoji_or_a_new_font_size(self):
        html = _html()
        css = html[html.index(".aiu-round-actions"): html.index(".aiu-round-raw:focus-visible")]

        # 字号刻度由 `tests/test_ai_usage_page_typography.py` 统一守；这里只确认新增段
        # 没有自带一档（那一条会红在那边，但先把「哪里来的」指出来）。
        assert set(re.findall(r"font-size:\s*([^;}]+)", css)) <= {"0.75rem", "1em"}, css
        assert not re.search(r"[\U0001F300-\U0001FAFF]", html), "页面里不许出现 emoji"


class TestTheSliceColumn:
    """子代理模式（services/ai/subagent.py）：这一次运行分了几片、哪一片没跑成。

    这一列的读数与轮次不同：`round_index` 在分片之间是**连着**的（一家子只落一条运行，
    见 `models/ai_analysis/trace.py`），所以「S1 的第 2 轮」只能靠 `agent` + `agent_round`
    说清楚。而分片表里**没跑成的分片也必须有一行** —— 少一行就等于把「这块没人看过」藏起来。
    """

    def test_the_column_only_exists_when_the_run_was_split(self):
        script = _dashboard_script()

        assert "var hasSlices = slices.length > 0" in script
        assert "(hasSlices ? '<th scope=\"col\">分片</th>' : '')" in script, (
            "分片列应当只在真的分了片时出现 —— 否则绝大多数运行会多一列全是「—」的列"
        )

    def test_the_round_row_names_the_slice_and_its_own_round(self):
        script = _dashboard_script()

        assert "(round.agent || '汇总')" in script, (
            "主代理自己那几轮的 `agent` 是空的，要写「汇总」而不是留一个空格"
        )
        assert "' · 第 ' + round.agent_round + ' 轮'" in script

    def test_the_slice_section_skips_nothing(self):
        script = _dashboard_script()
        body = _function_body(script, "runSubagentSection")

        # 状态与原因两列都要有：跳过（没花钱）与失败（花了没成）是两件事。
        assert "aiuSubagentStatus(item)" in body
        assert "item.skipped_reason || item.error" in body, (
            "没跑成的原因要显示出来（否则「未运行」旁边什么都没有）"
        )

    def test_a_not_run_slice_is_visually_marked(self):
        script = _dashboard_script()
        body = _function_body(script, "runSubagentSection")

        assert "status.className = 'aiu-round-failed'" in body, (
            "非成功的分片要一眼看得出来（它意味着那一块没人看过）"
        )

    def test_the_section_is_not_rendered_without_slices(self):
        script = _dashboard_script()

        assert "if (hasSlices) {" in script and "runSubagentSection(slices)" in script, (
            "没有分片数据时不该渲染一张空表（会让人以为分了片却一片都没跑）"
        )

    def test_the_caption_says_where_the_tokens_come_from(self):
        script = _dashboard_script()
        body = _function_body(script, "runSubagentSection")

        assert "加起来等于这次运行的合计" in body, (
            "分片的 token 与整次运行的关系要写清楚（否则两处数字看起来对不上就是 bug）"
        )
