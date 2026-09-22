# -*- coding: utf-8 -*-
"""一轮的「证据」：面板上要能看出**这一轮到底看没看到东西**。

## 这条是被什么逼出来的（2026-09-19）

一次周版本分析的报告写着「未读到任何代码 diff」，而消耗面板上那一轮看起来一切正常：
`ai_analysis_trace` 只记了计数（索取 6 次、执行 6 条、丢弃 0 条），取数回来的正文一个字
都没落库。于是「这次为什么没读到」只能靠猜 —— 而三种可能（模型没要 / 取数失败 /
被预算拒了）在三列计数上**长得一模一样**。

最要命的是第二种：provider 取不到时给的是一句完整的话
（`[取数失败] xxx：平台读不到…**这不等于「没有改动」**`），它看着像内容、字数也不为 0。
在「条数 + 字符数」的口径下，一次失败的索取与一次真的读了一份 diff 完全无法区分。

所以这一组测试盯三件事：

1. 计数**一个不少**（面板上既有的读法不能因为加了明细而变化）；
2. 「取不到」要能被认出来（`failed` + 原因），而「确实没有内容」（`empty`）是另一回事；
3. 编码与解码只有一份实现，写进去的东西读得回来。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from services.ai.budget import TRUNCATION_SUFFIX, ContextItem
from services.ai.protocol import ContextRequest, DroppedItem
from services.ai.scope import AnalysisScope
from services.ai.trace_evidence import (
    FAILURE_NOTICE_PREFIXES,
    TRACE_LIST_MAX_ITEMS,
    TRACE_RESPONSE_MAX_CHARS,
    decode_evidence,
    encode_evidence,
    failure_notice,
    live_round_entry,
    summarize_dropped,
    summarize_executed,
    summarize_requests,
)

LUA = "code/qz_pub/battle/BattleMgr.lua"
COMMIT = "a" * 40


def _record(**overrides):
    base = {
        "request_count": 2, "item_count": 1, "refused_by_budget": 1, "truncated": 0,
        "requests": (ContextRequest("file_diff", COMMIT, LUA),),
        "executed": (ContextItem("file_diff", f"file_diff {LUA}", "代码差异：…\n+ 一行\n"),),
        "dropped": (DroppedItem("request", 1, "超出本次工具请求总预算（20 次），未执行", "file_diff x"),),
        "budget_notes": ("有 1 个上下文请求因超出本次索取额度而未执行。",),
        "response_text": '{"status": "need_more_context"}',
        "correction_hint": "",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestTheCountsSurvive:
    """加明细不能把原来的计数挤掉 —— 面板上的「索取 N 次」读的就是它们。"""

    def test_the_counts_are_still_there(self):
        columns = encode_evidence(_record())

        requests = json.loads(columns["requests_json"])
        executed = json.loads(columns["executed_json"])
        dropped = json.loads(columns["dropped_json"])

        assert requests["count"] == 2
        assert executed["items"] == 1
        assert dropped["refused_by_budget"] == 1 and dropped["truncated"] == 0

    def test_the_details_ride_along(self):
        columns = encode_evidence(_record())

        assert json.loads(columns["requests_json"])["items"][0]["path"] == LUA
        assert json.loads(columns["executed_json"])["details"][0]["chars"] > 0
        assert "未执行" in json.loads(columns["dropped_json"])["details"][0]["reason"]

    def test_find_references_keeps_the_query_in_trace(self):
        """引用扫描没有 commit/path，trace 必须靠 query 才能说明模型要查什么。"""
        columns = encode_evidence(_record(
            request_count=1,
            requests=(ContextRequest(type="find_references", query="CfgRewardMode"),),
        ))

        item = json.loads(columns["requests_json"])["items"][0]
        assert item["query"] == "CfgRewardMode"
        assert item["text"] == "find_references CfgRewardMode"

    def test_an_empty_round_writes_none_not_an_empty_object(self):
        """没有明细时写 `None`：`{}` 会被读成「有一轮，但它是空的」。"""
        columns = encode_evidence(_record(
            requests=(), executed=(), dropped=(), budget_notes=(), response_text="",
        ))

        assert columns["response_text"] is None
        assert columns["budget_notes"] is None
        assert columns["correction_hint"] is None
        # 计数仍在（它们是 `0`，不是「没有这一轮」）
        assert json.loads(columns["requests_json"]) == {"count": 2, "items": []}


class TestFailureVersusEmpty:
    """**这一组是这个模块存在的理由。**"""

    def test_the_providers_own_failure_lines_are_recognized(self):
        """拿 provider 真会给的几句话喂进来（不是我自己编的文案）。

        改文案时这条会红 —— 那正是它该做的事：认不出来就等于「取数失败」在面板上
        又变回了「读到了内容」。
        """
        from services.ai.platform_provider import (
            _render_agent_file_content,
            _render_agent_file_diff,
            render_diff_payload,
        )

        samples = [
            # Agent 说取不到（离线/超时/没绑节点），平台给模型的那一句
            _render_agent_file_diff(
                {"kind": "diff", "file_path": LUA, "content": ""}, path=LUA, commit=COMMIT
            ),
            # 平台本地读不到内容时的 error 载荷
            render_diff_payload(
                {"type": "error", "file_path": LUA, "message": "平台读不到 X 的内容"},
                path=LUA,
            ),
            # 配表**差异**解析失败
            render_diff_payload({"type": "excel", "file_path": LUA, "error": "坏文件"}, path=LUA),
            # 配表**正文**解析失败（`file_content` 读到一张读不了的表）
            _render_agent_file_content(
                {"kind": "excel", "file_path": LUA, "content": ""}, path=LUA
            ),
        ]

        for text in samples:
            notice = failure_notice(text)
            assert notice, f"认不出这句失败说明：{text!r}"
            item = ContextItem("file_diff", f"file_diff {LUA}", text)
            assert summarize_executed((item,))[0]["failed"] is True, text
            assert summarize_executed((item,))[0]["reason"], text

    def test_a_deleted_worksheet_is_a_conclusion_not_a_failure(self):
        """**`[配表]` 前缀不许进失败清单** —— 它被真结论共用。

        「该工作表已被删除」「本次没有可展示的差异」都是**内容**（模型据此写结论），
        不是「我们没读到」。按前缀识别区分不了这两类，所以配表正文解析失败换了专属前缀
        `[配表解析失败]`，而 `[配表]` 留在这里当真结论。
        """
        from services.ai.platform_provider import render_diff_payload

        deleted = render_diff_payload(
            {"type": "excel", "file_path": LUA, "sheets": {"S": {"operation": "deleted"}}},
            path=LUA,
        )
        assert deleted.startswith("配表差异"), deleted

        item = ContextItem("file_diff", f"file_diff {LUA}", deleted)
        assert summarize_executed((item,))[0]["failed"] is False, (
            f"「该表已被删除」被当成了取数失败：{deleted!r}"
        )
        assert failure_notice("[配表] config/a.xlsx：本次没有可展示的差异。") == ""

    def test_a_real_diff_is_not_marked_as_failed(self):
        text = f"代码差异：{LUA}\n@@ -1 +1 @@\n-旧\n+新\n"

        detail = summarize_executed((ContextItem("file_diff", LUA, text),))[0]

        assert detail["failed"] is False and detail["empty"] is False
        assert detail["reason"] == ""

    def test_an_explicitly_empty_answer_is_not_a_failure(self):
        """工具明确回「确实没有内容」时：`empty`，**不是** `failed`。

        合并这两者等于把「没有证据」写成「这里没问题」—— 而这个协议里
        `""` 与 `None` 的区别（没有内容 / 拿不到）就是为此存在的。
        """
        item = ContextItem("file_content", LUA, "", meta={"tool_empty": True})

        detail = summarize_executed((item,))[0]

        assert detail["empty"] is True
        assert detail["failed"] is False, f"「确实没有内容」被记成了取数失败：{detail}"

    def test_a_tool_level_failure_carries_its_reason(self):
        item = ContextItem(
            "file_diff", LUA, "读取失败或内容不可用",
            meta={"tool_failed": True, "reason": "provider 返回空值"},
        )

        detail = summarize_executed((item,))[0]

        assert detail["failed"] is True and detail["reason"] == "provider 返回空值"

    def test_the_search_failure_lines_are_recognized_too(self, monkeypatch):
        """`find_references` 的四句也要认得出来。

        认不出来的后果比别的工具更绕：面板上那一次的「失败」列是 0，而报告里那句
        「检索不到」看着像一条**结论**（「本批次没有别处引用」）—— 一次没搜成的检索
        被读成了「查过了，没有」。
        """
        import services.agent_file_content_dispatch as dispatch
        from services.ai import platform_provider as pp
        from tests.test_ai_engine import _loaded as loaded_skills

        scope = AnalysisScope.from_iterables(
            commits=(COMMIT,),
            paths_by_commit={COMMIT: [LUA]},
            readable_references=[],
        )
        provider = pp.PlatformContextProvider(
            loaded=loaded_skills(), scope=scope
        )
        monkeypatch.setattr(provider, "_repository_of", lambda pairs: SimpleNamespace(id=1))

        monkeypatch.setattr(pp, "is_agent_dispatch_mode", lambda: True)
        monkeypatch.setattr(
            dispatch,
            "request_references",
            lambda repository, *, query, entries, prefix="": {
                "status": "pending",
                "message": "Agent 当前离线",
            },
        )
        samples = [
            provider.find_references("target_id"),               # [检索还没回来]
            pp.PlatformContextProvider(loaded=loaded_skills()).find_references("x"),  # [检索不可用]
        ]

        for text in samples:
            notice = failure_notice(text)
            assert notice, f"认不出这句失败说明：{text!r}"
            item = ContextItem("find_references", "find_references target_id", text)
            assert summarize_executed((item,))[0]["failed"] is True, text

    def test_the_prefixes_are_the_ones_the_provider_emits(self):
        """前缀清单不许与两个发出方脱节：它们发的那几句必须都在清单里。

        扫源码而不是只看常量：清单里写着一个**已经不存在**的前缀是死代码（认不出来的
        那天才发现），而 provider 新加一句失败说明、清单没跟上，则会让「取数失败」
        在面板上又变回「读到了内容」。
        """
        import inspect

        import services.ai.context_tools as context_tools
        import services.ai.platform_provider as provider

        source = inspect.getsource(provider) + inspect.getsource(context_tools)
        for prefix in FAILURE_NOTICE_PREFIXES:
            assert f"{prefix}" in source, (
                f"{prefix} 已经不在 provider / context_tools 里了 —— 清单里留着它就是死代码"
            )


class TestTheRoundKeepsTheAnswer:
    def test_the_model_output_is_kept(self):
        columns = encode_evidence(_record())

        assert "need_more_context" in columns["response_text"]

    def test_a_long_answer_is_truncated_with_a_marker(self):
        columns = encode_evidence(_record(response_text="x" * (TRACE_RESPONSE_MAX_CHARS + 500)))

        assert len(columns["response_text"]) == TRACE_RESPONSE_MAX_CHARS
        assert columns["response_text"].endswith(TRUNCATION_SUFFIX), "截断要说出来"

    def test_the_correction_hint_is_kept(self):
        columns = encode_evidence(_record(correction_hint="你上一轮的返回不符合协议：…"))

        assert "不符合协议" in columns["correction_hint"]


class TestTheDetailsStayBounded:
    def test_at_most_the_list_cap_is_written(self):
        """一轮要了 200 个文件（越权或被诱导）时，trace 不该跟着涨到 200 条。"""
        requests = tuple(ContextRequest("file_diff", COMMIT, f"a/{i}.lua") for i in range(200))

        assert len(summarize_requests(requests)) == TRACE_LIST_MAX_ITEMS

    def test_a_long_dropped_detail_is_clipped(self):
        detail = summarize_dropped((DroppedItem("request", 0, "r" * 5000),))[0]

        assert len(detail["reason"]) <= 300


class TestEncodeDecodeRoundTrip:
    def test_what_was_written_comes_back(self):
        columns = encode_evidence(_record())

        # 造一行「像 AiAnalysisTrace 的东西」：解码只按属性名读。
        row = SimpleNamespace(
            requests_json=columns["requests_json"],
            executed_json=columns["executed_json"],
            dropped_json=columns["dropped_json"],
            response_text=columns["response_text"],
            budget_notes=columns["budget_notes"],
            correction_hint=columns["correction_hint"],
        )

        decoded = decode_evidence(row)

        assert decoded["requests"][0]["path"] == LUA
        assert decoded["executed"][0]["chars"] > 0
        assert decoded["dropped"][0]["reason"].endswith("未执行")
        assert "need_more_context" in decoded["response_text"]
        assert "超出本次索取额度" in decoded["budget_notes"]

    @pytest.mark.parametrize("raw", [None, "", "not json", "[]", '{"items": "不是列表"}'])
    def test_a_legacy_or_broken_row_reads_as_empty(self, raw):
        """老行（这一层之前写下的）只有计数，没有明细 —— 如实给空，而不是炸掉整页。"""
        row = SimpleNamespace(
            requests_json=raw, executed_json=raw, dropped_json=raw,
            response_text=None, budget_notes=None, correction_hint=None,
        )

        decoded = decode_evidence(row)

        assert decoded == {
            "requests": [], "executed": [], "dropped": [],
            "response_text": "", "budget_notes": "", "correction_hint": "",
        }


class TestTheTokenCountsStayUnknown:
    """实时那一条里的用量：**「不知道」必须一路原样带出去，不许在中途被写成 0。**

    面板上的「第 3/8 轮 · 本次已用 N tokens」读的就是这几个键。它们以前在这条路上被
    `int(... or 0)` 兜成整数，于是「这一轮调用失败、上游什么都没报」显示成「输入 0 tokens」——
    一次没跑成的调用被说成没花钱。四个用量字段是同一句话，缓存那两个一直是对的，
    输入输出曾经不是。
    """

    def test_a_round_that_did_not_report_stays_none(self):
        entry = live_round_entry(_record(prompt_tokens=None, completion_tokens=None))

        assert entry["tokens_input"] is None
        assert entry["tokens_output"] is None

    def test_a_round_that_really_reported_zero_is_zero(self):
        """反方向：确实报了 0 的不能被一起改成「不知道」—— 那会把真省下的钱说成没记账。"""
        entry = live_round_entry(_record(prompt_tokens=0, completion_tokens=0))

        assert entry["tokens_input"] == 0
        assert entry["tokens_output"] == 0

    def test_half_a_report_is_half_unknown(self):
        """只报一半时，报了的那个照实给（它不是错的），另一个如实说不知道。"""
        entry = live_round_entry(_record(prompt_tokens=1200, completion_tokens=None))

        assert entry["tokens_input"] == 1200
        assert entry["tokens_output"] is None

    @pytest.mark.parametrize("junk", ["x", -1, True, [1]])
    def test_junk_is_unknown_not_a_number(self, junk):
        """上游给的东西不一定是数字（网关会写字符串、也会写负数）。"""
        entry = live_round_entry(_record(prompt_tokens=junk, completion_tokens=junk))

        assert entry["tokens_input"] is None, junk
        assert entry["tokens_output"] is None, junk

    def test_a_round_object_without_the_fields_at_all_is_unknown(self):
        """`_record()` 的默认里根本没有 token 字段 —— 鸭子类型读不到就该是「不知道」。"""
        entry = live_round_entry(_record())

        assert entry["tokens_input"] is None
        assert entry["tokens_output"] is None
