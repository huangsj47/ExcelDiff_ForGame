# -*- coding: utf-8 -*-
"""对账轮（`subagent_verify`，「找反证」）—— 阶段 3 的那一次独立复核。

## 这一组盯的是什么

对账轮是**用户主动打开、额外付一次模型调用**买来的东西。它有两种最糟的失败形态，
两种都不会报错：

1. **静默消失**：那一轮压根没跑（预算不足、上游出错），而结果里一切正常 ——
   用户以为自己买到了复核，实际上没有。所以「开了但没跑成」必须落成
   `DEGRADE_VERIFY` + 报告里的一句点名，与「没开」严格分开。
2. **污染异常清单**：它跑在汇总**之后**，汇总不可能采纳一份还没产生的结论。
   把它报出的东西塞进候选池，只会给**每一次**运行都记上几条「找不到去向」的假账。

另外两件要钉住的性质：它跑在汇总之后（核对的对象就是那份报告）、它用同一份共享前缀
（省钱的机制对它同样成立）、以及**不开时就一次模型调用都不多花**。
"""
from __future__ import annotations

import pytest

from services.ai.engine import (
    DEGRADATION_LABELS,
    DEGRADE_VERIFY,
    STATUS_DEGRADED,
    STATUS_SUCCEEDED,
    EngineLimits,
)
from services.ai.llm_client import ChatResult
from services.ai.protocol import Anomaly
from services.ai.subagent import (
    DEFAULT_VERIFY_ITEMS,
    MAX_VERIFY_ITEMS,
    ROLE_VERIFY,
    VERIFY_LABEL,
    build_verify_task,
    plan_family,
    run_family,
    verify_section,
)
from services.ai.verdict import RULING_TITLE
from tests.test_ai_engine import (
    FakeProvider,
    _anomaly,
    _final,
    _loaded,
    _scope,
)
from tests.test_ai_subagent_family import CHANGE_SUMMARY, FlakyClient

LIMITS = EngineLimits(max_rounds=8, max_tool_requests=20)

VERIFY_REPLY = _final(
    report="# 对账\n\n第 1 条：未找到反证（查了 config/[30]道具表_CfgItem.xlsx 与 build/lua/CfgItem.lua）。\n"
)


class RecordingClient(FlakyClient):
    """把每次调用收到的消息记下来 —— 「它用的是不是同一份共享前缀」只能这样验。"""

    def __init__(self, *replies: str, fail_on: int = 0):
        super().__init__(*replies, fail_on=fail_on)
        self.seen: list[list] = []

    def complete(self, messages, *, temperature=None):
        self.seen.append([dict(item) for item in messages])
        return super().complete(messages, temperature=temperature)


def _plan(count: int = 2, *, verify: bool = True, verify_items: int = 0):
    plan = plan_family(
        mode="weekly", enabled=True, count=count, limits=LIMITS,
        verify=verify, verify_items=verify_items,
    )
    assert plan is not None
    from services.ai.subagent import attach_seed

    return attach_seed(plan, loaded=_loaded(), change_summary=CHANGE_SUMMARY)


def _args():
    return {"loaded": _loaded(), "scope": _scope(), "change_summary": CHANGE_SUMMARY}


def _verify_steps(result):
    return [step for step in result.steps if step.plan.role == ROLE_VERIFY]


class TestThePlanCarriesTheSwitch:
    def test_the_default_is_off(self):
        """默认关是这一条功能的唯一安全默认值（它是一次额外的模型调用）。"""
        assert _plan(verify=False).verify is False

    def test_turning_it_on_is_kept(self):
        assert _plan(verify=True).verify is True

    def test_the_item_count_is_clamped(self):
        assert _plan(verify=True, verify_items=0).verify_items == DEFAULT_VERIFY_ITEMS
        # 负值 / 非数字都退回默认，而不是变成「核对 0 条」（那等于开着却什么都没核）。
        assert _plan(verify=True, verify_items=-3).verify_items == DEFAULT_VERIFY_ITEMS
        assert _plan(verify=True, verify_items="不是数字").verify_items == DEFAULT_VERIFY_ITEMS
        # 要得再多也有上限：对账要的是深度，不是把整份报告重读一遍。
        assert _plan(verify=True, verify_items=99).verify_items == MAX_VERIFY_ITEMS

    def test_it_is_attached_to_the_subagent_mode(self):
        """没开子代理模式时 `plan_family` 返回 `None` —— 对账轮**没有**独立入口。

        单代理那条路的报告本来就没有「几个分片各自的结论」需要交叉核对，而这条功能的
        价值正在于此。所以它在配置界面上也写着「要先打开子代理模式」。
        """
        assert plan_family(mode="weekly", enabled=False, count=3, limits=LIMITS, verify=True) is None


class TestTheRoundRunsAfterTheSynthesis:
    def test_it_runs_once_and_last(self):
        client = RecordingClient(
            _final(_anomaly()),          # S1
            _final(_anomaly()),          # S2
            _final(_anomaly()),          # 汇总
            VERIFY_REPLY,                # 对账轮
        )
        result = run_family(client=client, provider=FakeProvider(), plan=_plan(2), **_args())

        assert client.calls == 4, "2 个分片 + 1 次汇总 + 1 次对账"
        assert [step.plan.role for step in result.steps] == [
            "subagent", "subagent", "synthesis", ROLE_VERIFY,
        ], "对账轮必须排在汇总之后 —— 它核对的就是汇总出来的那份报告"

    def test_it_shares_the_seed_prefix(self):
        """对账轮用的是**同一份共享前缀**（system + 整份变更清单）—— 所以它也吃缓存。

        前缀一变，缓存整个失效，而**不会报错**，只会悄悄贵一倍。所以这里逐字节比。
        """
        client = RecordingClient(
            _final(_anomaly()), _final(_anomaly()), _final(_anomaly()), VERIFY_REPLY,
        )
        run_family(client=client, provider=FakeProvider(), plan=_plan(2), **_args())

        first_two = [tuple(item["content"] for item in sent[:2]) for sent in client.seen]
        assert len(set(first_two)) == 1, "各成员（含对账轮）的前两条消息不再逐字节相同"
        # 而它私有的那一条**确实**不同：不同的话就对账轮的任务书没发出去。
        assert client.seen[-1][2]["content"] != client.seen[0][2]["content"]

    def test_the_task_names_the_synthesis_conclusions(self):
        client = RecordingClient(
            _final(_anomaly(title="【道具】ID 被删除但生成文件仍在")),
            _final(),
            _final(_anomaly(title="【道具】ID 被删除但生成文件仍在")),
            VERIFY_REPLY,
        )
        run_family(client=client, provider=FakeProvider(), plan=_plan(2), **_args())

        task = client.seen[-1][2]["content"]
        assert "找反证" in task
        assert "【道具】ID 被删除但生成文件仍在" in task, (
            "任务书里没有列出要核对的那几条 —— 对账轮无从下手"
        )

    def test_turning_it_off_spends_nothing_extra(self):
        client = RecordingClient(_final(_anomaly()))
        result = run_family(
            client=client, provider=FakeProvider(), plan=_plan(2, verify=False), **_args()
        )

        assert client.calls == 3, "关着的时候不许有第 4 次调用"
        assert _verify_steps(result) == []


class TestTheReportGetsTheSection:
    """AI-P1-01（2026-09-21）：对账轮的原文**不再进报告正文**，改为独立存档。

    改之前：报告 = 模型汇总稿 + 「复核裁决（平台）」+ 「对账结果（找反证）」原文 ——
    同一件事在报告里出现三遍（模型稿一遍、平台按裁决渲染一遍、对账轮原文一遍），
    而读的人还得自己辨认哪一句已经被裁决改掉。run 20 的正文里那份汇总稿、裁决节、
    对账轮原文分别从 0 / 13833 / 16981 字符处开始，就是这么来的。

    现在正文只留平台那几节规范结论，原文进结论载荷的 `verify_report_markdown`
    （存档，默认不渲染）。**原文一个字都不许丢**，下面几条钉的就是这个。
    """

    def _run(self):
        client = FlakyClient(
            _final(_anomaly()), _final(_anomaly()), _final(_anomaly()), VERIFY_REPLY
        )
        return run_family(client=client, provider=FakeProvider(), plan=_plan(2), **_args())

    def test_the_original_survives_as_an_archive_instead_of_the_body(self):
        outcome = self._run().outcome

        assert "## 对账结果（找反证）" not in outcome.report_markdown, (
            "对账轮原文又回到了报告正文里（AI-P1-01：正文只能有一份规范结论）"
        )
        archived = outcome.verify_report_markdown
        assert "## 对账结果（找反证）" in archived, "原文要有抬头，不能只剩光秃秃一段"
        assert "未找到反证" in archived, "模型写的那段要原样带进存档"
        # 抬头在前、正文在后（读者先知道这是什么，再读结论）。
        assert archived.index("## 对账结果（找反证）") < archived.index("未找到反证")

    def test_the_archived_section_keeps_its_headings_demoted(self):
        """对账轮那段**要降一级再存档**：它交回的是一整份报告（7 个一级标题一个不少）。

        实测那次：原样贴进 `## 对账结果（找反证）` 之后整份文档有 **15 个一级标题**
        （7 个各出现两次）。而契约是「固定 7 个一级标题、顺序固定」
        （`skill_contract.REPORT_SECTIONS`）。存档这一份同样降级 —— 它随时可能被渲染给
        人看（调试页），两处各写一份拼接迟早会漂移。
        """
        archived = self._run().outcome.verify_report_markdown

        h1 = [line for line in archived.split("\n") if line.startswith("# ")]
        assert "# 对账" not in h1, "对账轮那段没降级，存档里又出现一套一级标题"
        assert "## 对账" in archived, "降级过头了：它应当挂在二级标题下"
        assert len(h1) == len(set(h1)), f"一级标题重名：{h1}"

    def test_the_body_still_ends_with_the_gap_section(self):
        """信息缺口永远在最后：读的人一眼就能看到「哪些东西没看到」。

        （对账轮原文离开正文之后，这一条次序仍然成立 —— 正文里剩下的那几节里，
        它排最后。）
        """
        client = FlakyClient(
            _final(_anomaly()), _final(_anomaly()), _final(_anomaly()), VERIFY_REPLY,
            fail_on=1,
        )
        result = run_family(client=client, provider=FakeProvider(), plan=_plan(2), **_args())

        report = result.outcome.report_markdown
        assert "信息缺口（平台补充）" in report
        assert report.rstrip().endswith("不是模型的自我说明。"), (
            "信息缺口那一节不再收尾了"
        )

    def test_a_failed_synthesis_gets_no_section_and_no_round(self):
        """汇总都没跑成时**不跑对账轮**：它核对的就是那份报告，而报告不存在。

        跑下去只有两种结果，两种都比不跑糟：空转（任务书里只剩「主代理没报出任何结论」），
        或者让模型对着一份失败的运行凭空产出「对账结论」。往一份空报告后面追加一节也等于
        凭空造出一份看得见的报告。
        """
        client = FlakyClient(_final(), fail_on=3)
        result = run_family(client=client, provider=FakeProvider(), plan=_plan(2), **_args())

        assert client.calls == 3, "汇总失败之后不该再有第 4 次调用"
        assert _verify_steps(result) == []
        assert "## 对账结果" not in (result.outcome.report_markdown or "")

    def test_an_empty_body_renders_nothing(self):
        from services.ai.subagent import MemberOutcome, MemberPlan

        member = MemberPlan(index=4, label=VERIFY_LABEL, role=ROLE_VERIFY, dimensions=())
        assert verify_section(MemberOutcome(plan=member)) == ""


class TestAFailedRoundIsNotSilent:
    """**这一组是这个模块存在的理由之一。**"""

    def _run_failing(self):
        client = FlakyClient(
            _final(_anomaly()), _final(_anomaly()), _final(_anomaly()), VERIFY_REPLY, fail_on=4,
        )
        return run_family(client=client, provider=FakeProvider(), plan=_plan(2), **_args())

    def test_it_degrades_with_its_own_reason(self):
        result = self._run_failing()

        assert result.outcome.degradation == DEGRADE_VERIFY
        assert result.outcome.status == STATUS_DEGRADED
        assert DEGRADATION_LABELS[DEGRADE_VERIFY] in result.outcome.error_message

    def test_the_report_names_it(self):
        result = self._run_failing()

        report = result.outcome.report_markdown
        assert "对账轮（找反证）」**，而它没有跑成" in report
        assert "没有经过「找反证」这一道" in report

    def test_it_is_never_written_as_a_missing_dimension(self):
        """对账轮**没有负责的维度**：说「它负责的维度没人看过」是错的 —— 那是「有一块
        维度没人看过」，而这里只是「结论没经过复核」，两件事的处理方式完全不同。"""
        report = self._run_failing().outcome.report_markdown
        tail = report.split("信息缺口（平台补充）")[-1]

        assert "它没有跑成" in tail
        assert "它负责的维度" not in tail

    def test_a_skipped_round_is_flagged_too(self):
        """预算不足提前收工把它跳过了 —— 与「失败」同样要说，理由不同而已。"""
        # **只跳对账轮**：把所有成员都跳掉的话，降级会是「缺分片」（更重）—— 那条
        # 路径由别的用例覆盖，这里要单独看「只有对账轮没跑成」这一支。
        def skip(member, tokens):
            return "预算不足" if member.label == VERIFY_LABEL else ""

        client = FlakyClient(_final(_anomaly()))
        result = run_family(
            client=client, provider=FakeProvider(), plan=_plan(2), should_skip=skip, **_args()
        )

        assert result.outcome.degradation == DEGRADE_VERIFY
        step = _verify_steps(result)[0]
        assert step.skipped_reason and step.outcome is None
        assert "它没有跑成" in result.outcome.report_markdown

    def test_a_missing_verify_outranks_a_degraded_shard_but_loses_to_a_missing_shard(self):
        """降级取最重的那个：缺一个分片（有一块没人看过）比「没复核」重。"""
        from services.ai.subagent import MemberOutcome, MemberPlan, aggregate_outcomes

        shard = MemberPlan(index=1, label="S1", role="subagent", dimensions=("config_id",))
        verify = MemberPlan(index=3, label=VERIFY_LABEL, role=ROLE_VERIFY, dimensions=())
        synthesis = MemberPlan(index=2, label="", role="synthesis", dimensions=())

        def _synth(degradation=""):
            return _outcome(degradation)

        only_verify_missed = aggregate_outcomes(
            synthesis=_synth(),
            steps=[MemberOutcome(plan=shard, outcome=_outcome("")),
                   MemberOutcome(plan=synthesis, outcome=_synth()),
                   MemberOutcome(plan=verify, skipped_reason="预算不足")],
        )
        assert only_verify_missed.degradation == DEGRADE_VERIFY

        both = aggregate_outcomes(
            synthesis=_synth(),
            steps=[MemberOutcome(plan=shard, skipped_reason="预算不足"),
                   MemberOutcome(plan=synthesis, outcome=_synth()),
                   MemberOutcome(plan=verify, skipped_reason="预算不足")],
        )
        assert both.degradation == "subagent_gap", "缺一个分片必须盖过「没复核」"


class TestTheVerifyFindingsStayOutOfTheCandidatePool:
    def test_its_anomalies_are_not_counted_as_candidates(self):
        """对账轮跑在汇总**之后**，汇总不可能采纳一份还没产生的结论。

        把它塞进候选池，`reconcile_candidates` 会给**每一次**运行都记上几条
        「找不到去向」的假账 —— 那比没有对账更糟：它会让人开始不信这套账。
        """
        client = FlakyClient(
            _final(_anomaly()),
            _final(_anomaly()),
            _final(_anomaly()),
            _final(_anomaly(title="【流程】对账时新发现的问题")),
        )
        result = run_family(client=client, provider=FakeProvider(), plan=_plan(2), **_args())

        assert [step.candidates for step in result.steps if step.plan.role == ROLE_VERIFY] == [()]
        assert result.candidates
        assert all(
            "对账时新发现" not in candidate.anomaly.title for candidate in result.candidates
        )
        assert not [
            item for item in result.outcome.dropped if VERIFY_LABEL in item.detail
        ], "对账轮报出的东西不该被记成「找不到去向」的候选"
        # 它仍然照实记账：报了几条要看得见（面板上那一行）。
        verify_step = _verify_steps(result)[0]
        assert verify_step.candidate_total == 1

    def test_the_panel_row_says_what_it_is(self):
        client = FlakyClient(_final(_anomaly()), _final(_anomaly()), _final(_anomaly()), VERIFY_REPLY)
        result = run_family(client=client, provider=FakeProvider(), plan=_plan(2), **_args())

        row = result.outcome.subagents[-1]
        assert row["label"] == VERIFY_LABEL
        assert row["role"] == ROLE_VERIFY
        assert row["dimensions"] == [], "对账轮没有负责的维度（面板上由 role 说明它在干什么）"
        assert row["status"] == STATUS_SUCCEEDED


class TestTheTaskItself:
    def _task(self, *anomalies, items: int = 0):
        plan = _plan(2, verify=True, verify_items=items)
        return build_verify_task(plan, _outcome_engine(tuple(_anomaly_objs(anomalies))))

    def test_it_asks_for_counter_evidence_not_confirmation(self):
        task = self._task(_anomaly(severity="critical"))

        assert "找反证" in task
        assert "未找到反证" in task, "没有给「推翻不了」留一条出口 —— 模型会编一条反证"
        assert "不要重复确认" in task

    def test_it_takes_the_most_severe_first(self):
        """按与报告封顶同一套排序（`rank_anomalies`）取前 K 条，两套排序迟早会对不上。"""
        task = self._task(
            _anomaly(title="低危的一条", severity="low", confidence="low"),
            _anomaly(title="严重的一条", severity="critical", confidence="high"),
            items=1,
        )

        assert "严重的一条" in task
        assert "低危的一条" not in task, "只核对最严重的几条"

    def test_an_empty_report_still_asks_a_question(self):
        """一条都没报出来时**不是空转**：要它去判断「这次真的一条都没有」是否站得住。"""
        task = self._task()

        assert "没有报出任何达到门槛的结论" in task


# --------------------------------------------------------------------------
# 小工具：直接造 `EngineOutcome`（这几条测的是编排，不需要跑引擎）
# --------------------------------------------------------------------------


def _outcome(degradation: str = ""):
    from services.ai.engine import STATUS_SUCCEEDED as OK
    from services.ai.engine import EngineOutcome

    return EngineOutcome(status=OK, degradation=degradation, report_markdown="# 报告\n")


def _anomaly_objs(items):
    out = []
    for item in items:
        out.append(
            Anomaly(
                title=item["title"], category=item["category"], severity=item["severity"],
                confidence=item["confidence"], evidence=tuple(item["evidence"]),
                commit=item["commit"], file_path=item["file_path"], impact=item["impact"],
                suggestion=item["suggestion"],
            )
        )
    return out


def _outcome_engine(anomalies):
    from services.ai.engine import STATUS_SUCCEEDED as OK
    from services.ai.engine import EngineOutcome

    return EngineOutcome(status=OK, anomalies=tuple(anomalies), report_markdown="# 报告\n")


# ===========================================================================
# 报告正文的边界：模型草稿始终是正文主体（2026-09-23 起）
# ===========================================================================
# AI-P1-01 时期「有裁决节时草稿让位」的形态，用户实测后明确不要：报告要读的是模型的
# 整体汇总，复核只做跟在后面的标注。现在两条臂的准则是「**正文永远以草稿开头**」：
# 有裁决标注时它跟在草稿后面、且 `draft_markdown` 另存模型原稿（给外部读侧的存档）；
# 没有裁决标注时（默认配置 `DEFAULT_SUBAGENT_VERIFY = False`）平台那几节直接接在草稿
# 后面、**不另存**（草稿同时是正文的开头，再存一份就是同一段字节出现两次）。
# 「单一结论清单」的口径由落库层承担：异常表 / `final_findings` / 下一轮基线仍然只认
# `reduce_findings` 那一份。下面两条各钉一条臂，反转判据（把草稿又顶出正文）时两条都红。


class TestTheDefaultConfigKeepsTheModelTextAsTheOneCanonicalReport:
    """没有裁决节那条臂：模型交回的那份文本**就是**正文（而且不重复存一份）。"""

    DRAFT = "# 变更理解\n\n改了道具表，主键被删。\n"
    DRAFT_MARKER = "改了道具表，主键被删。"

    def _run(self):
        """默认配置（`verify=False`）跑一次**真实聚合**：两个分片 + 汇总，没有对账轮。"""
        client = FlakyClient(
            _final(_anomaly()),  # S1
            _final(_anomaly()),  # S2
            _final(_anomaly(), report=self.DRAFT),  # 汇总：模型交回的那份文本
        )
        return run_family(
            client=client, provider=FakeProvider(), plan=_plan(2, verify=False), **_args()
        )

    def test_the_model_text_is_the_one_canonical_report(self):
        outcome = self._run().outcome

        assert self.DRAFT_MARKER in outcome.report_markdown, (
            "默认配置下模型交回的结论散文不在正文里 —— 报告只剩平台那几节，"
            "而这一档没有裁决节来替代它"
        )
        assert outcome.draft_markdown == "", (
            "草稿既在正文又另存了一份：同一段字节在结论载荷里出现两次"
        )

        from services.ai.result_payload import result_payload

        payload = result_payload(outcome, {"summary": {}}, suppressed=frozenset())
        assert payload["report_markdown"] == outcome.report_markdown
        assert self.DRAFT_MARKER in payload["report_markdown"]
        assert payload["draft_markdown"] == ""

    def test_the_platform_sections_are_still_appended_to_it(self):
        """草稿留在正文 ≠ 平台不说话：平台那几节照样接在它后面（次序也不变）。"""
        report = self._run().outcome.report_markdown

        assert "## 信息缺口（平台补充）" in report, (
            "汇总没交回候选血缘时这一节必然出现；它不在说明平台那几节没接上"
        )
        assert report.index(self.DRAFT_MARKER) < report.index("## 信息缺口（平台补充）"), (
            "平台那几节没有排在模型正文之后"
        )


class TestTheDraftStaysAsTheBodyWhenThePlatformAnnotates:
    """另一条臂：**有**裁决标注时，草稿**仍然是正文主体**（2026-09-23 起），复核标注跟在
    它后面；`draft_markdown` 是给外部读侧保留的模型原稿存档（AI-P1-01 时期「裁决节取代
    正文」的形态用户实测后明确不要：报告要读的是整体汇总，反证只做标注）。"""

    def test_the_draft_is_the_body_and_the_annotation_follows(self):
        from tests.test_ai_verify_verdict import _critical_round, _run, _verdict_reply

        outcome = _run(
            _critical_round(
                _verdict_reply(
                    {
                        "finding_id": "F1",
                        "verdict": "retracted",
                        "reason": "同一提交里生成文件已经删掉了",
                        "evidence_refs": ["config/[30]道具表_CfgItem.xlsx 第 12 行"],
                    }
                )
            )
        ).outcome
        report = outcome.report_markdown

        assert report.startswith("# 变更理解"), (
            "这一条臂没走到「有裁决标注」—— 那它就没在验这条判据；"
            "同时也钉「草稿是正文开头」，不许标注节顶到它前面"
        )
        assert "改了道具表。" in report, (
            "有裁决标注了，草稿却不在正文里 —— 报告又只剩裁决那节了"
        )
        assert RULING_TITLE in report, "裁决标注节没有跟在草稿后面"
        assert report.index("改了道具表。") < report.index(RULING_TITLE), (
            "复核标注排到了草稿前面 —— 标注是标注，不是正文主体"
        )
        assert outcome.draft_markdown.startswith("# 变更理解"), (
            "模型原稿的存档没了 —— 外部读侧（API/SSE）可能只认这个键"
        )
        assert "改了道具表。" in outcome.draft_markdown, "存档要逐字保留模型的原文"
        assert report.index(RULING_TITLE) < report.index("## 信息缺口（平台补充）"), (
            "次序必须是 草稿 → 复核标注 → 平台补充节：标注节排到了信息缺口后面，"
            "读者会在「没看到什么」之后才看到「哪条被改判」"
        )


@pytest.mark.parametrize("count", [2, 3, 6])
def test_the_verify_step_is_only_ever_one(count):
    """无论分几个片，对账轮都只有一次 —— 它不是分片，是「再问一遍」的那一遍。"""
    client = FlakyClient(_final())
    result = run_family(
        client=client, provider=FakeProvider(), plan=_plan(count), **_args()
    )

    assert len(_verify_steps(result)) == 1
