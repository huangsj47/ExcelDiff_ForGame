# -*- coding: utf-8 -*-
"""子代理模式跑起来之后：账怎么合、谁没跑成、候选有没有被丢掉。

## 这一组盯的是**最危险的失真**

报告看起来完全正常，只是少了一条 —— 没有任何人会去数。三种来路都要被钉住：

1. 一个成员**失败**了（网络、协议、轮次耗尽）→ 它负责的维度必须写成信息缺口，
   而不是「没报出问题 = 没问题」；
2. 一个成员被**跳过**（预算不足早停）→ 同上，且要写明是谁决定的、还剩多少；
3. 主代理**漏掉**某条候选 → 平台自己查出来（`reconcile_candidates`），追加进报告的
   「信息缺口（平台补充）」，并记进 `dropped` 的账。

另外两件本模块的性质：一家子只产出**一个** `EngineOutcome`（落库那一层只认一个），
以及**深度 1**（成员手里没有任何「再派子代理」的能力）。
"""
from __future__ import annotations

import pytest

from services.ai.engine import (
    DEGRADE_CONTEXT,
    DEGRADE_ROUNDS,
    DEGRADE_SUBAGENT,
    STATUS_DEGRADED,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    EngineOutcome,
    RoundRecord,
)
from services.ai.engine import failed as engine_failed
from services.ai.llm_client import ChatResult
from services.ai.protocol import Anomaly, DroppedItem
from services.ai.subagent import (
    CANDIDATE_MAX_ITEMS_PER_MEMBER,
    ROLE_SUBAGENT,
    ROLE_SYNTHESIS,
    Candidate,
    MemberOutcome,
    aggregate_outcomes,
    attach_seed,
    build_synthesis_task,
    plan_family,
    reconcile_candidates,
    run_family,
)
from tests.test_ai_engine import (
    COMMIT,
    TABLE,
    FakeProvider,
    _anomaly,
    _final,
    _loaded,
    _requests,
    _scope,
)

LIMITS = __import__("services.ai.engine", fromlist=["EngineLimits"]).EngineLimits(
    max_rounds=8, max_tool_requests=20
)
CHANGE_SUMMARY = "本次变更共 1 个提交、2 个文件。\n"


class FlakyClient:
    """第 `fail_on` 次调用抛异常，其余按脚本回答。用来演「某个成员没跑成」。"""

    def __init__(self, *replies: str, fail_on: int = 0):
        self._replies = list(replies)
        self._fail_on = fail_on
        self.calls = 0

    def complete(self, messages, *, temperature=None):
        self.calls += 1
        if self.calls == self._fail_on:
            raise RuntimeError("上游 502")
        index = min(self.calls - 1, len(self._replies) - 1)
        return ChatResult(
            text=self._replies[index], model="fake", prompt_tokens=10, completion_tokens=5
        )


def _plan(count: int = 3):
    plan = plan_family(mode="weekly", enabled=True, count=count, limits=LIMITS)
    assert plan is not None
    return attach_seed(plan, loaded=_loaded(), change_summary=CHANGE_SUMMARY)


def _args():
    return {"loaded": _loaded(), "scope": _scope(), "change_summary": CHANGE_SUMMARY}


def _anomaly_obj(**overrides) -> Anomaly:
    data = _anomaly(**overrides)
    return Anomaly(
        title=data["title"], category=data["category"], severity=data["severity"],
        confidence=data["confidence"], evidence=tuple(data["evidence"]),
        commit=data["commit"], file_path=data["file_path"], impact=data["impact"],
        suggestion=data["suggestion"],
    )


class TestTheFamilyProducesOneResult:
    def test_it_is_a_single_outcome_with_per_member_rounds(self):
        client = FlakyClient(_final(_anomaly()))
        plan = _plan(2)
        result = run_family(client=client, provider=FakeProvider(), plan=plan, **_args())

        # 2 个成员 + 1 次汇总，各一轮
        assert len(result.outcome.rounds) == 3
        assert [item.agent for item in result.outcome.rounds] == ["S1", "S2", ""]
        assert [item.agent_round for item in result.outcome.rounds] == [1, 1, 1], (
            "每个成员内部的轮次都从 1 开始（界面显示「S1 · 第 1/4 轮」用它）"
        )
        assert [item.index for item in result.outcome.rounds] == [1, 2, 3], (
            "round_index 是家族内全局递增的 —— trace 的唯一约束靠它"
        )

    def test_the_tokens_are_the_family_total(self):
        client = FlakyClient(_final())
        result = run_family(
            client=client, provider=FakeProvider(), plan=_plan(2), **_args()
        )

        # 假 client 每次报 10 + 5，一家子 3 次调用。
        assert result.outcome.prompt_tokens == 30
        assert result.outcome.completion_tokens == 15
        assert result.outcome.duration_ms >= 0

    def test_a_member_that_never_reports_cache_tokens_poisons_the_total(self):
        """**任一成员没上报 → 整家 `None`**（与引擎同一条口径）。

        只把报了的那几个加起来会得出一个偏高、且看起来完全正常的命中率。
        """
        steps = (
            MemberOutcome(
                plan=_plan(2).members[0],
                outcome=EngineOutcome(status=STATUS_SUCCEEDED, cache_read_tokens=900),
            ),
            MemberOutcome(
                plan=_plan(2).members[1],
                outcome=EngineOutcome(status=STATUS_SUCCEEDED, cache_read_tokens=None),
            ),
        )

        result = aggregate_outcomes(
            synthesis=EngineOutcome(status=STATUS_SUCCEEDED), steps=steps
        )

        assert result.cache_read_tokens is None, "有一家没上报却算出了一个命中数"

    def test_a_member_that_never_reports_tokens_poisons_the_total_too(self):
        """输入 / 输出与缓存那两个字段是**同一句话**：任一成员没上报，整家就是 `None`。

        这里以前是 `sum(step.outcome.prompt_tokens ...)` —— `None` 会被当成 0 加进去。
        子代理模式下一次分析有 n+1 次模型调用，少算一次就让「这次花了多少」偏小；
        而偏小的数看起来完全正常，没有人会去怀疑它（费用那一栏会据此算出一个确定的金额）。
        """
        steps = (
            MemberOutcome(
                plan=_plan(2).members[0],
                outcome=EngineOutcome(status=STATUS_SUCCEEDED, prompt_tokens=900, completion_tokens=100),
            ),
            MemberOutcome(
                plan=_plan(2).members[1],
                outcome=EngineOutcome(status=STATUS_SUCCEEDED, prompt_tokens=None, completion_tokens=None),
            ),
        )

        result = aggregate_outcomes(
            synthesis=EngineOutcome(status=STATUS_SUCCEEDED), steps=steps
        )

        assert result.prompt_tokens is None, "有一个成员没上报，总账却给了个数"
        assert result.completion_tokens is None

    def test_the_refused_requests_are_merged_across_members(self):
        """「额度用尽没轮到的那几块」要**跨成员合起来**。

        这一家子有几个成员，缺的那几块可能分别来自不同成员 —— 只看汇总那一个的账，
        报告末尾就只会说「有 N 个请求没执行」，说不出是哪几个。
        """
        steps = (
            MemberOutcome(
                plan=_plan(2).members[0],
                outcome=EngineOutcome(
                    status=STATUS_DEGRADED,
                    refused_requests=("config/道具表.xlsx",),
                ),
            ),
            MemberOutcome(
                plan=_plan(2).members[1],
                outcome=EngineOutcome(
                    status=STATUS_DEGRADED,
                    refused_requests=("引用扫描 CfgRewardMode", "config/奖励表.xlsx"),
                ),
            ),
        )

        result = aggregate_outcomes(
            synthesis=EngineOutcome(status=STATUS_SUCCEEDED), steps=steps
        )

        assert result.refused_requests == (
            "config/道具表.xlsx",
            "引用扫描 CfgRewardMode",
            "config/奖励表.xlsx",
        )

    def test_the_tool_stats_are_summed_per_kind(self):
        steps = (
            MemberOutcome(
                plan=_plan(2).members[0],
                outcome=EngineOutcome(
                    status=STATUS_SUCCEEDED,
                    tool_stats={"file_diff": {"executions": 2, "failed": 0}},
                ),
            ),
            MemberOutcome(
                plan=_plan(2).members[1],
                outcome=EngineOutcome(
                    status=STATUS_SUCCEEDED,
                    tool_stats={"file_diff": {"executions": 3, "failed": 1}},
                ),
            ),
        )

        result = aggregate_outcomes(
            synthesis=EngineOutcome(status=STATUS_SUCCEEDED), steps=steps
        )

        assert result.tool_stats["file_diff"] == {"executions": 5, "failed": 1}


class TestAFailedMemberIsAGap:
    def test_a_transport_failure_names_the_dimensions_nobody_looked_at(self):
        plan = _plan(2)
        # 第 2 次调用 = S2 的第一轮（S1 先跑）。
        client = FlakyClient(_final(), fail_on=2)

        result = run_family(client=client, provider=FakeProvider(), plan=plan, **_args())

        assert result.outcome.degradation == DEGRADE_SUBAGENT
        assert "S2" in result.outcome.report_markdown
        assert "上游 502" in result.outcome.report_markdown, (
            "取不到的原因要原话带出来（读的人才知道去查什么）"
        )
        for name in plan.members[1].dimensions:
            assert name in result.outcome.report_markdown, name
        assert "信息缺口（平台补充）" in result.outcome.report_markdown

    def test_it_is_recorded_in_the_subagents_block(self):
        plan = _plan(2)
        result = run_family(
            client=FlakyClient(_final(), fail_on=2),
            provider=FakeProvider(),
            plan=plan,
            **_args(),
        )

        blocks = {item["label"]: item for item in result.outcome.subagents}
        assert blocks["S2"]["status"] == STATUS_FAILED
        assert blocks["S2"]["error"], "失败原因要落到面板上"
        assert blocks["S1"]["status"] == STATUS_SUCCEEDED

    def test_the_synthesis_is_told_about_it(self):
        plan = _plan(2)
        result = run_family(
            client=FlakyClient(_final(), fail_on=2),
            provider=FakeProvider(),
            plan=plan,
            **_args(),
        )
        steps = result.steps

        task = build_synthesis_task(plan, steps)

        assert "没有跑成的分片" in task and "S2" in task
        assert "上游 502" in task

    def test_a_failed_synthesis_makes_the_family_fail(self):
        """汇总没跑成 = 这一家子没有报告。不许把某个成员的半份报告当成结论。"""
        client = FlakyClient(_final(), fail_on=3)
        result = run_family(
            client=client, provider=FakeProvider(), plan=_plan(2), **_args()
        )

        assert result.outcome.status == STATUS_FAILED
        assert result.outcome.report_markdown == ""


class TestASkippedMemberIsAGap:
    def test_the_skip_reason_reaches_the_report_and_the_block(self):
        plan = _plan(3)

        def should_skip(member, tokens):
            assert tokens >= 0, "判据要拿到「本次已消耗」才判得准"
            return "剩余预算不足（还有 1200 tokens）" if member.index == 3 else ""

        result = run_family(
            client=FlakyClient(_final()),
            provider=FakeProvider(),
            plan=plan,
            should_skip=should_skip,
            **_args(),
        )

        assert len(result.steps) == 4, "跳过的成员也要在 steps 里（否则它就不存在了）"
        skipped = [step for step in result.steps if step.skipped_reason]
        assert [step.plan.label for step in skipped] == ["S3"]
        assert "未运行" in result.outcome.report_markdown
        assert "剩余预算不足" in result.outcome.report_markdown
        assert result.outcome.subagent_skipped, "要有一份「谁没跑」的名单"
        assert result.outcome.degradation == DEGRADE_SUBAGENT

    def test_it_does_not_run_the_engine_for_the_skipped_member(self):
        plan = _plan(3)
        client = FlakyClient(_final())

        run_family(
            client=client,
            provider=FakeProvider(),
            plan=plan,
            should_skip=lambda member, tokens: "预算不足" if member.index == 3 else "",
            **_args(),
        )

        # 2 个成员 + 1 次汇总 = 3 次调用（跳过的那一个没有花钱）
        assert client.calls == 3


class TestTheCandidatesAndTheReconciliation:
    def _candidate(self, label: str = "S1", index: int = 1, **overrides) -> Candidate:
        return Candidate(
            member_label=label, index=index, anomaly=_anomaly_obj(**overrides)
        )

    def test_a_candidate_missing_from_the_report_is_recorded(self):
        candidate = self._candidate(title="【道具】ID 被删除但生成文件仍在")
        synthesis = EngineOutcome(
            status=STATUS_SUCCEEDED,
            report_markdown="# 变更理解\n\n一切正常。\n",
        )

        text, dropped = reconcile_candidates((candidate,), synthesis)

        assert "信息缺口（平台补充）" in text
        assert "[S1-1]" in text
        assert dropped and dropped[0].kind == "subagent"
        assert "[S1-1]" in dropped[0].detail

    def test_a_candidate_the_report_names_is_adopted(self):
        """模型采纳时把编号带进证据里 —— 这是平台能核对的第一手。"""
        candidate = self._candidate()
        synthesis = EngineOutcome(
            status=STATUS_SUCCEEDED,
            report_markdown=f"# 风险评估\n\n[S1-1] 确认存在，见 {TABLE}。\n",
        )

        text, dropped = reconcile_candidates((candidate,), synthesis)

        assert text == "" and dropped == ()

    def test_a_candidate_on_the_same_file_counts_as_adopted(self):
        """模型经常复述内容而不带编号 —— 同文件的条目算它进了报告。"""
        candidate = self._candidate()
        synthesis = EngineOutcome(
            status=STATUS_SUCCEEDED,
            report_markdown="# 风险评估\n\n改写了那条。\n",
            anomalies=(_anomaly_obj(title="另一个说法", file_path=TABLE),),
        )

        text, dropped = reconcile_candidates((candidate,), synthesis)

        assert text == "" and dropped == ()

    def test_a_two_digit_neighbour_does_not_make_a_one_digit_id_look_adopted(self):
        """**编号必须整段匹配，不能当子串。**

        同一个分片的候选编号到 `S1-30`（`CANDIDATE_MAX_ITEMS_PER_MEMBER`），所以
        `"S1-2" in report` 会被报告里的 `[S1-25]` 命中 —— 一条**真的没被汇总进去**的
        候选就此不报。方向恰好是最危险的那个：`reconcile_candidates` 存在的唯一理由
        就是「报告看起来完全正常，只是少了一条，而没有任何人会去数」。
        """
        one_digit = self._candidate(index=2, title="【角色属性表】id=7007 被删除后复用")
        two_digit = self._candidate(index=25, title="【刷怪】权重列被移出但入口还在")
        synthesis = EngineOutcome(
            status=STATUS_SUCCEEDED,
            report_markdown="# 风险评估\n\n[S1-25] 确认存在。\n",
        )

        text, dropped = reconcile_candidates((one_digit, two_digit), synthesis)

        assert [item.index for item in dropped] == [2], (
            f"采纳了 S1-25 却把 S1-2 也算成采纳了：{[item.detail for item in dropped]}"
        )
        assert "[S1-2]" in text and "[S1-25]" not in text.split("S1-25")[0][-200:]

    def test_the_id_matches_with_or_without_brackets(self):
        """模型写成裸编号（不带方括号）时也算引用到了 —— 否则会凭空多出一堆假缺口。"""
        candidate = self._candidate()
        synthesis = EngineOutcome(
            status=STATUS_SUCCEEDED,
            report_markdown="# 风险评估\n\nS1-1 这条确认存在。\n",
        )

        text, dropped = reconcile_candidates((candidate,), synthesis)

        assert text == "" and dropped == ()

    def test_the_same_file_written_differently_still_counts_as_adopted(self):
        """**路径要比对归一化后的形态，不能逐字相等。**

        候选的文件名来自分片模型、最终报告的文件名来自汇总模型，两边写法常常不同
        （`./` 前缀、反斜杠、首尾引号）。逐字相等会让**已经采纳**的候选被报成
        「找不到去向」—— 假缺口不是无害的：它把一条已经进了报告的结论说成没被汇总，
        读的人只能再去核一遍，而且这个数字会虚高。
        """
        candidate = self._candidate(file_path="config/60_skill/角色属性表.xlsx")
        synthesis = EngineOutcome(
            status=STATUS_SUCCEEDED,
            report_markdown="# 风险评估\n\n复述了那条。\n",
            anomalies=(
                _anomaly_obj(title="另一个说法", file_path="./config\\60_skill\\角色属性表.xlsx"),
            ),
        )

        text, dropped = reconcile_candidates((candidate,), synthesis)

        assert text == "" and dropped == (), (
            f"同一个文件的不同写法被当成了两个文件：{[item.detail for item in dropped]}"
        )

    def test_the_wording_only_claims_what_was_checked(self):
        """不能写成「模型把它丢了」—— 平台查的是「报告里有没有它」。"""
        candidate = self._candidate()
        text, _ = reconcile_candidates(
            (candidate,), EngineOutcome(status=STATUS_SUCCEEDED, report_markdown="")
        )

        assert "既没有引用这个编号，也没有同一个文件的条目" in text
        assert "没有被汇总进去" in text

    def test_the_gap_text_says_it_is_the_platforms_own_check(self):
        candidate = self._candidate()
        text, _ = reconcile_candidates(
            (candidate,), EngineOutcome(status=STATUS_SUCCEEDED, report_markdown="")
        )

        assert "不是模型的自我说明" in text, (
            "读的人必须知道这条是平台按记录核对出来的，不是模型的自述"
        )

    def test_an_omitted_candidate_lands_in_dropped_accounting(self):
        client = FlakyClient(
            # S1 报一条；S2 什么都没报；汇总既没引用编号、也没提同一个文件。
            # （三次调用按顺序取：S1 / S2 / 汇总。桩会重复最后一条，所以三条都要给。）
            _final(_anomaly(title="【道具】只有 S1 发现了这个")),
            _final(),
            _final(),
        )
        result = run_family(
            client=client, provider=FakeProvider(), plan=_plan(2), **_args()
        )

        assert any(item.kind == "subagent" for item in result.outcome.dropped), (
            "被漏掉的候选没有记账 —— 「为什么少了一条」就永远查不出来"
        )
        assert result.outcome.degradation == DEGRADE_SUBAGENT
        assert result.outcome.status == STATUS_DEGRADED
        assert "信息缺口（平台补充）" in result.outcome.report_markdown

    def test_the_candidates_fed_to_the_synthesis_are_pre_threshold(self):
        """喂给汇总的是**过门槛之前**那批。

        成员先按门槛/封顶裁一遍再汇总，「N 个成员各报 12 条、最后只剩 10 条」会把
        跨维度的发现静默吃掉。
        """
        payload_anomalies = tuple(
            _anomaly_obj(title=f"【道具】第 {index} 条") for index in range(1, 4)
        )
        outcome = EngineOutcome(
            status=STATUS_SUCCEEDED,
            payload=__import__(
                "services.ai.protocol", fromlist=["AnalysisPayload"]
            ).AnalysisPayload(status="final", anomalies=payload_anomalies),
            # 过门槛之后只剩一条（模拟门槛裁掉了两条）
            anomalies=(payload_anomalies[0],),
        )
        from services.ai.subagent import _candidates_of

        candidates = _candidates_of(_plan(2).members[0], outcome)

        assert len(candidates) == 3, "按 outcome.anomalies 抽的话只剩 1 条，那两条就没了"

    def test_the_candidate_list_is_capped_but_says_so(self):
        anomalies = tuple(
            _anomaly_obj(title=f"第 {index} 条") for index in range(CANDIDATE_MAX_ITEMS_PER_MEMBER + 5)
        )
        outcome = EngineOutcome(
            status=STATUS_SUCCEEDED,
            payload=__import__(
                "services.ai.protocol", fromlist=["AnalysisPayload"]
            ).AnalysisPayload(status="final", anomalies=anomalies),
        )
        plan = _plan(2)
        from services.ai.subagent import _candidates_of, _render_candidates

        # 候选要经过 `_candidates_of`（它才是封顶的那一处）—— 直接构造 MemberOutcome
        # 会得到「一条候选都没有」，测的就不是封顶了。
        step = MemberOutcome(
            plan=plan.members[0],
            outcome=outcome,
            candidates=_candidates_of(plan.members[0], outcome),
            candidate_total=len(anomalies),
        )

        text = _render_candidates(plan, (step,))

        assert "因篇幅未列出" in text, "封顶了却不说，读的人以为它只报了这么多"


class TestTheDegradationIsTheWorstOne:
    @pytest.mark.parametrize(
        "member_code,expected",
        [(DEGRADE_ROUNDS, DEGRADE_ROUNDS), (DEGRADE_CONTEXT, DEGRADE_CONTEXT)],
    )
    def test_a_members_degradation_carries_over(self, member_code, expected):
        plan = _plan(2)
        steps = (
            MemberOutcome(
                plan=plan.members[0],
                outcome=EngineOutcome(status=STATUS_DEGRADED, degradation=member_code),
            ),
            MemberOutcome(
                plan=plan.members[1], outcome=EngineOutcome(status=STATUS_SUCCEEDED)
            ),
        )

        result = aggregate_outcomes(
            synthesis=EngineOutcome(status=STATUS_SUCCEEDED), steps=steps
        )

        assert result.degradation == expected

    def test_a_coverage_gap_outweighs_a_soft_degradation(self):
        """「这块没人看过」比「这块看过了但看得不够」重 —— 排序是刻意的。"""
        plan = _plan(2)
        steps = (
            MemberOutcome(
                plan=plan.members[0],
                outcome=EngineOutcome(status=STATUS_DEGRADED, degradation=DEGRADE_CONTEXT),
            ),
            MemberOutcome(plan=plan.members[1], skipped_reason="预算不足"),
        )

        result = aggregate_outcomes(
            synthesis=EngineOutcome(status=STATUS_SUCCEEDED), steps=steps
        )

        assert result.degradation == DEGRADE_SUBAGENT
        assert result.status == STATUS_DEGRADED

    def test_an_all_clean_family_is_succeeded(self):
        plan = _plan(2)
        steps = tuple(
            MemberOutcome(plan=member, outcome=EngineOutcome(status=STATUS_SUCCEEDED))
            for member in plan.members
        )

        result = aggregate_outcomes(
            synthesis=EngineOutcome(status=STATUS_SUCCEEDED, report_markdown="# 变更理解\n"),
            steps=steps,
        )

        assert result.status == STATUS_SUCCEEDED
        assert result.degradation == ""


class TestDepthIsOne:
    def test_the_module_never_calls_itself(self):
        """**深度 1 是结构性的**：成员手里没有任何「再派子代理」的能力。

        扫源码而不是只看行为：真跑一遍只能证明「这次没递归」，而这里要保证的是
        「不可能递归」—— 一次调用 `run_family` 的地方多起来（比如以后有人在成员的
        任务书里加了什么），这条会红。
        """
        import inspect

        import services.ai.subagent as module

        source = inspect.getsource(module)
        # `run_family` / `run_family_with_seed` 之外，模块内部不得再调它们。
        assert source.count("run_family(") == source.count("def run_family(") + source.count(
            "run_family(plan=prepared"
        ), "模块内部出现了对 run_family 的递归调用"
        # 任务的构造里不许出现「你可以再分片」这类字样
        assert "再派" not in source and "子代理的子代理" not in source

    def test_the_plan_never_grants_a_member_more_than_the_cap(self):
        assert len(_plan(99).members) <= 6


class TestEachMemberKeepsMostOfTheConfiguredBudget:
    """**「设置的额度」是一个 agent 的额度，不是全家共享的一锅。**

    原先每个成员拿 `总额 // (成员数 + 1)`：20 次配 3 个分片 = 每人 5 次，而线上表现就是
    一个分片跑 3 次报「本轮上下文额度已用尽」、`references/test-scope-and-regression.md`
    读不到，报告里写成「因额度耗尽未读」。更别扭的是**分片数越多，每个分片反而看得越少**。

    现在每个成员拿配置值的 `MEMBER_BUDGET_PERCENT`%（默认 70%，向上取整）——「至少七成」
    是一条**下限**，与成员数无关。
    """

    def _limits(self, requests: int, rounds: int = 8):
        from services.ai.engine import EngineLimits

        return EngineLimits(max_rounds=rounds, max_tool_requests=requests)

    def _plan_with(self, requests: int, size: int, rounds: int = 8):
        plan = plan_family(
            mode="weekly", enabled=True, count=size, limits=self._limits(requests, rounds)
        )
        assert plan is not None
        return plan

    @pytest.mark.parametrize("size", [2, 3, 4, 6])
    def test_every_member_gets_at_least_seventy_percent_regardless_of_the_size(self, size):
        plan = self._plan_with(40, size)
        assert plan.limits.max_tool_requests >= 28, (
            f"{size} 个分片时每个成员只拿到 {plan.limits.max_tool_requests} 次 —— "
            "少于配置值 40 的 70%"
        )

    def test_the_allowance_does_not_shrink_as_members_are_added(self):
        """**这条就是原规则的病**：分片越多，每个分片看得越少。"""
        small = self._plan_with(40, 2).limits.max_tool_requests
        large = self._plan_with(40, 6).limits.max_tool_requests
        assert large == small, (
            f"2 个分片时每人 {small} 次、6 个分片时每人 {large} 次 —— 加人反而看得更少"
        )

    def test_it_rounds_up_so_the_floor_is_never_broken(self):
        """25 的 70% 是 17.5：取 17 就低于「至少七成」了，所以向上取整。"""
        assert self._plan_with(25, 3).limits.max_tool_requests == 18

    def test_a_tiny_allowance_is_never_rounded_down_to_zero(self):
        """配 1 次就还是 1 次：向上取整保证「七成」不会把小额度抹成 0。

        （`MIN_MEMBER_TOOL_REQUESTS` 那道兜底在 70% 这条规则下几乎不会触发 —— 它留着是
        给「以后把百分比调小」用的，不是这里的判据。）
        """
        assert self._plan_with(1, 3).limits.max_tool_requests == 1

    def test_the_round_budget_gets_the_same_floor(self):
        """轮次也是「设置的额度」，不能一边给七成才够的索取、一边把轮次砍成一半。"""
        plan = self._plan_with(40, 3, rounds=10)
        assert plan.limits.max_rounds == 7, plan.limits.max_rounds

    def test_a_configured_zero_is_never_pushed_back_up(self):
        """上限配 0 是一个明确的意思：这次分析一次上下文都不给。

        `MIN_MEMBER_TOOL_REQUESTS` 不许把它顶成 2 —— 报告里会按「配置就是 0」说
        （见 `prompt._budget_line`），顶上去会让那句话变成假话。
        """
        assert self._plan_with(0, 3).limits.max_tool_requests == 0

    def test_a_member_never_exceeds_the_configured_total(self):
        for requests in (0, 1, 2, 5, 40, 100):
            for size in (2, 3, 6):
                got = self._plan_with(requests, size).limits.max_tool_requests
                assert 0 <= got <= requests, (requests, size, got)

    def test_the_percent_helper_rounds_up_and_never_goes_negative(self):
        from services.ai.subagent import _percent_of

        assert _percent_of(40, 70) == 28
        assert _percent_of(25, 70) == 18  # 17.5 → 18
        assert _percent_of(1, 70) == 1
        assert _percent_of(0, 70) == 0
        assert _percent_of(-5, 70) == 0


class TestTheOutcomeBlocks:
    def test_every_step_gets_a_row_even_when_it_did_not_run(self):
        plan = _plan(2)
        result = run_family(
            client=FlakyClient(_final()),
            provider=FakeProvider(),
            plan=plan,
            should_skip=lambda member, tokens: "预算不足" if member.index == 2 else "",
            **_args(),
        )

        rows = result.outcome.subagents
        assert len(rows) == 3
        roles = [row["role"] for row in rows]
        assert roles.count(ROLE_SUBAGENT) == 2 and roles.count(ROLE_SYNTHESIS) == 1
        skipped = [row for row in rows if row["status"] == "skipped"]
        assert skipped and skipped[0]["skipped_reason"] == "预算不足"

    def test_the_rows_are_json_safe(self):
        import json

        result = run_family(
            client=FlakyClient(_final()), provider=FakeProvider(), plan=_plan(2), **_args()
        )

        json.dumps(result.outcome.to_dict(), ensure_ascii=False)


class TestTheEngineRejectsNothingUnexpected:
    def test_a_plain_engine_failure_is_carried_not_raised(self):
        """引擎不抛异常这条承诺，在子代理路径上也要成立（一家子更经不起崩）。"""
        plan = _plan(2)

        class Boom:
            def complete(self, messages, *, temperature=None):
                raise RuntimeError("连接被重置")

        result = run_family(client=Boom(), provider=FakeProvider(), plan=plan, **_args())

        assert result.outcome.status == STATUS_FAILED
        assert "连接被重置" in result.outcome.error_message
