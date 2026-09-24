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
    DEGRADE_MARKDOWN,
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
from services.ai.protocol import AnalysisPayload, Anomaly, CandidateDisposition, DroppedItem
from services.ai.rules import KIND_ANOMALY_CAP
from services.ai.subagent import (
    CANDIDATE_MAX_ITEMS_PER_MEMBER,
    MEMBER_BUDGET_PERCENT,
    ROLE_SUBAGENT,
    ROLE_SYNTHESIS,
    Candidate,
    MemberOutcome,
    _percent_of,
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
    _markdown,
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
        # 候选血缘（AI-P0-06）。**必须在这里显式传**：`_anomaly` 那份基线字典里没有它，
        # 漏传会让「带血缘的用例」静默退化成「没交回编号」。
        source_candidate_ids=tuple(overrides.get("source_candidate_ids") or ()),
    )


class TestTheFamilyProducesOneResult:
    def test_it_is_a_single_outcome_with_per_member_rounds(self):
        client = FlakyClient(_final(_anomaly()))
        plan = _plan(2)
        result = run_family(client=client, provider=FakeProvider(), plan=plan, **_args())

        # 2 个成员 + 1 次汇总，各一轮
        assert len(result.outcome.rounds) == 3
        assert [item.agent for item in result.outcome.rounds] == ["S1", "S2", "汇总"]
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

        assert "没能交回结论的分片" in task and "S2" in task
        assert "上游 502" in task

    def test_a_failed_synthesis_preserves_successful_shards_as_a_degraded_report(self):
        """汇总失败不能把已经完成的分片与其 token 成本一起抹掉。

        平台只能把它标成降级结果，并明确说没有完成跨分片汇总；不得伪装成完整报告。
        """
        client = FlakyClient(_final(_anomaly()), fail_on=3)
        result = run_family(
            client=client, provider=FakeProvider(), plan=_plan(2), **_args()
        )

        assert result.outcome.status == STATUS_DEGRADED
        assert result.outcome.degradation == DEGRADE_SUBAGENT
        assert "汇总失败后的分片保全报告" in result.outcome.report_markdown
        assert "未完成跨分片" in result.outcome.report_markdown
        assert "上游 502" in result.outcome.report_markdown
        assert result.outcome.anomalies, "分片已经交回的结构化结论必须保留"

    def test_a_failed_synthesis_with_no_usable_shard_still_fails(self):
        class AlwaysFails:
            def complete(self, messages, *, temperature=None):
                raise RuntimeError("上游 502")

        result = run_family(
            client=AlwaysFails(), provider=FakeProvider(), plan=_plan(2), **_args()
        )

        assert result.outcome.status == STATUS_FAILED
        assert result.outcome.report_markdown == ""


class TestAMemberThatRanButDidNotHandBackConclusions:
    """**跑完了、结论没交回来**也是一种缺口 —— 而且是最容易被读成「没问题」的那种。

    实测（2026-09-20 那次）：S3 跑满 5 轮、最后一轮给的是 markdown 正文而不是协议 JSON，
    平台按 markdown 降级保存 —— 它那一路的**结构化结论是 0 条**，而报告末尾一个字的缺口
    都没有。它负责的三个维度（`code_logic`/`version_branch`/`process`）在清单里看起来是
    「看过、没问题」，实际是「结论没回来」。

    这里的 S2 **两次**都给 markdown：写完报告之后平台会再问一次「把上一条原样转成 JSON」
    （见 `engine.build_markdown_reemit_hint`），它仍然不转 —— 于是才落到下面这些缺口上。
    只给一次 markdown 的话，那个成员会**真的交回结构化结论**，这一整类缺口就不会出现了。
    """

    def _run_with_a_broken_member(self):
        client = FlakyClient(
            _final(_anomaly()),                      # S1 正常
            _markdown("改了道具表，风险中等。"),        # S2 没按协议出 JSON
            _markdown("改了道具表，风险中等。"),        # S2 收到「原样转成 JSON」后仍然不转
            _final(_anomaly()),                      # 汇总
            _final(_anomaly()),                      # 对账轮
        )
        return run_family(client=client, provider=FakeProvider(), plan=_plan(2), **_args())

    def _gap_section(self) -> str:
        report = self._run_with_a_broken_member().outcome.report_markdown
        return report.split("信息缺口（平台补充）")[-1]

    def test_it_names_the_member_and_its_dimensions(self):
        tail = self._gap_section()

        assert "S2" in tail, tail
        assert "结论没有按协议交回" in tail, tail
        assert "没有结构化结论进入清单" in tail, tail

    def test_it_does_not_claim_nobody_looked(self):
        """它**看过了**，只是结论没交回来 —— 写「没有人看过」是错的。

        两种缺口的处置不一样：这个要人去翻它降级保存的正文，那个要人重新安排一次分析。
        """
        tail = self._gap_section()

        assert "没有人看过" not in tail, tail
        assert "不要当成「没有问题」" in tail, tail

    def test_the_run_keeps_the_specific_reason(self):
        """降级理由要留住「模型没按协议出 JSON」这条能对症的，别被笼统的「有分片没跑成」盖掉。

        `subagent_gap` 的措辞是「有分片没有跑成、或它报出的结论没有进入最终报告」——
        套上去之后，「模型没按协议出 JSON」这个可以直接对症的原因就看不见了。
        """
        assert self._run_with_a_broken_member().outcome.degradation == DEGRADE_MARKDOWN


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

    def _synthesis(self, *anomalies, report: str = "# 变更理解\n\n改了道具表。\n") -> EngineOutcome:
        return EngineOutcome(
            status=STATUS_SUCCEEDED, report_markdown=report, anomalies=tuple(anomalies)
        )

    # ------------------------------------------------------------------
    # 血缘：对得上、对不上
    # ------------------------------------------------------------------

    def test_a_rewritten_title_still_maps(self):
        """**标题改写仍映射**：判据是编号，不是措辞。

        分片报的是「【道具】ID 被删除但生成文件仍在」，汇总把它写成「【道具表】主键被删、
        生成文件成了孤儿」—— 逐字比标题的时代这一条就是一条假缺口。
        """
        candidate = self._candidate(index=3, title="【道具】ID 被删除但生成文件仍在")
        adopted = _anomaly_obj(
            title="【道具表】主键被删、生成文件成了孤儿",
            source_candidate_ids=("S1-3",),
        )

        text, dropped = reconcile_candidates((candidate,), self._synthesis(adopted))

        assert (text, dropped) == ("", ()), (
            f"编号对上了却被报成缺口：{[item.detail for item in dropped]}"
        )

    def test_two_candidates_merged_into_one_finding(self):
        """两条候选合成一条：那条结论把两个编号都带上，两条都算有去向。"""
        first = self._candidate(index=1, title="【道具】ID 被删除但生成文件仍在")
        second = self._candidate(
            index=2, title="【道具】生成文件里 1001 还在", file_path="build/lua/CfgItem.lua"
        )
        merged = _anomaly_obj(
            title="【道具】主键 1001 被删、生成文件与老存档都还引用它",
            source_candidate_ids=("S1-1", "S1-2"),
        )

        text, dropped = reconcile_candidates((first, second), self._synthesis(merged))

        assert (text, dropped) == ("", ()), (
            f"多对一的血缘没被认出来：{[item.detail for item in dropped]}"
        )

    def test_one_candidate_split_into_two_findings(self):
        """一条候选拆成两条：两条都带同一个编号，仍算有去向（不许报成缺了一条）。"""
        candidate = self._candidate(index=5, title="【道具】ID 被删除但生成文件仍在")
        split_a = _anomaly_obj(title="【道具】ID 1001 被删除", source_candidate_ids=("S1-5",))
        split_b = _anomaly_obj(
            title="【道具】生成文件仍引用 1001",
            file_path="build/lua/CfgItem.lua",
            source_candidate_ids=("S1-5",),
        )

        text, dropped = reconcile_candidates(
            (candidate,), self._synthesis(split_a, split_b)
        )

        assert (text, dropped) == ("", ()), (
            f"一对多的血缘没被认出来：{[item.detail for item in dropped]}"
        )

    def test_a_candidate_nobody_claims_is_still_a_gap(self):
        """**反向对照**：真缺口照旧吵闹 —— 汇总交回了别的编号，就是没交回这一条。"""
        landed = self._candidate(index=1, title="【道具】ID 被删除但生成文件仍在")
        orphan = self._candidate(
            index=7,
            title="【掉落】备份不再深拷贝",
            file_path="code/qz_pub/core/scene/aoi/pack/PackDropObjSnapshotMod.lua",
        )
        adopted = _anomaly_obj(
            title="【道具】主键被删、生成文件成了孤儿", source_candidate_ids=("S1-1",)
        )

        text, dropped = reconcile_candidates(
            (landed, orphan), self._synthesis(adopted)
        )

        assert [item.index for item in dropped] == [7], (
            f"真缺口没记账：{[item.detail for item in dropped]}"
        )
        assert dropped[0].kind == "subagent"
        assert "[S1-7]" in text
        assert "[S1-1]" not in text, "采纳的那条不该出现在缺口名单里"

    def test_explicit_rejection_is_a_settled_disposition_not_a_gap(self):
        candidate = self._candidate(index=7, title="【掉落】备份不再深拷贝")
        synthesis = self._synthesis()
        synthesis = __import__("dataclasses").replace(
            synthesis,
            payload=AnalysisPayload(
                status="final",
                report_markdown=synthesis.report_markdown,
                dimensions=(),
                candidate_dispositions=(
                    CandidateDisposition(
                        candidate_id="S1-7",
                        status="rejected",
                        reason="与 S2-2 是同一根因，后者证据更完整",
                    ),
                ),
            ),
        )

        text, dropped = reconcile_candidates((candidate,), synthesis)

        assert dropped == ()
        assert "已明确拒绝" in text
        assert "与 S2-2 是同一根因" in text

    def test_a_candidate_the_synthesis_never_handed_back_a_id_for(self):
        """标题与文件都相同，**只要没有编号声明就不算采纳**。

        这是刻意的取舍：同文件/同标题这两手都是**从自然语言反推血缘**，它们产生的假缺口
        正是这一批缺陷（run 20 的 S3-3/F5）。判据换成显式编号之后，平台不再替汇总猜
        「这条大概就是那条」—— 猜错的代价是静默的（真的缺口被说成有去向）。
        """
        candidate = self._candidate(index=1, title="【道具】ID 被删除但生成文件仍在")
        # 同一份汇总里**有**一条带血缘的结论（另一条候选），所以这一条不算「一条编号都
        # 没交回」——它是真缺口。
        same_file = _anomaly_obj(
            title="【道具】ID 被删除但生成文件仍在", source_candidate_ids=("S1-9",)
        )

        text, dropped = reconcile_candidates((candidate,), self._synthesis(same_file))

        assert [item.index for item in dropped] == [1]
        assert "[S1-1]" in text

    # ------------------------------------------------------------------
    # 一条编号都没交回：说一次，不是 N 次
    # ------------------------------------------------------------------

    def test_no_lineage_at_all_is_said_once_instead_of_n_times(self):
        """汇总一条编号都没交回 → **不报 N 条假缺口**，如实说一句「无法按编号对账」。

        这正是 run 20 的成因：模型没按 schema 交回血缘，平台却按启发式逐条报了
        「找不到去向」，那 4 条假缺口又是 `subagent_gap` 降级的**唯一**触发源。
        """
        candidates = tuple(
            self._candidate(index=index, title=f"【道具】第 {index} 条")
            for index in range(1, 5)
        )
        synthesis = self._synthesis(
            _anomaly_obj(title="【道具】汇总自己写的一条", file_path="build/lua/CfgItem.lua")
        )

        text, dropped = reconcile_candidates(candidates, synthesis)

        assert dropped == (), "没有血缘时逐条报缺口 = 四条假缺口 + 一次假降级"
        assert text.count("没有交回候选血缘") == 1, f"要说只一次：\n{text}"
        assert "无法按编号对账" in text
        assert "4" in text, "账上几条候选要说出来"

    def test_a_run_with_no_lineage_does_not_degrade(self):
        """上一条的端到端后果：**不降级**（这个降级以前是假缺口撑起来的）。"""
        candidate = self._candidate(index=1, title="【道具】ID 被删除但生成文件仍在")
        plan = _plan(2)
        steps = tuple(
            MemberOutcome(plan=member, outcome=EngineOutcome(status=STATUS_SUCCEEDED))
            for member in plan.members
        )

        outcome = aggregate_outcomes(
            synthesis=self._synthesis(),
            steps=steps,
            candidates=(candidate,),
        )

        assert outcome.degradation != DEGRADE_SUBAGENT
        assert outcome.status == STATUS_SUCCEEDED

    # ------------------------------------------------------------------
    # run 20 那一幕
    # ------------------------------------------------------------------

    def test_the_run_20_s3_3_to_f5_case_no_longer_triggers_a_degradation(self):
        """run 20：`S3-3` 的文件是 `ProtoScsGas.lua`，最终结论 `F5` 的文件是
        `ProtoCGas.lua`（**同名协议拆在两个文件里**），标题措辞也不同
        （「表参改为两个定长标量」vs「Lt→LIC」），而且 `F5` 的 `evidence_refs` 为空。

        旧的三手一条都挂 → 报成「找不到去向」→ 4 条假缺口 → `subagent_gap` 降级。
        显式血缘把这条路径整个删掉：对上的是**编号**。
        """
        candidate = self._candidate(
            label="S3",
            index=3,
            title="【协议】通用蓝图 SyncNodeStateChange 报文结构被替换：表参改为两个定长标量",
            file_path="code/qz_pub/protocols/ProtoScsGas.lua",
        )
        adopted = _anomaly_obj(
            title="【协议】ProtoCGas 的 Lt 改为 LIC，通用蓝图报文结构被就地替换",
            file_path="code/qz_pub/protocols/ProtoCGas.lua",
            evidence=["提交 f0724d7d：ProtoCGas.lua 的字段定义整段被换掉"],
            source_candidate_ids=("S3-3",),
        )
        plan = _plan(3)
        steps = tuple(
            MemberOutcome(plan=member, outcome=EngineOutcome(status=STATUS_SUCCEEDED))
            for member in plan.members
        )

        text, dropped = reconcile_candidates((candidate,), self._synthesis(adopted))

        assert (text, dropped) == ("", ()), (
            f"S3-3 又被报成找不到去向：{[item.detail for item in dropped]}"
        )

        outcome = aggregate_outcomes(
            synthesis=self._synthesis(adopted), steps=steps, candidates=(candidate,)
        )

        assert outcome.degradation != DEGRADE_SUBAGENT, "假缺口把整次 run 判成了降级"
        assert outcome.status == STATUS_SUCCEEDED
        assert "信息缺口（平台补充）" not in outcome.report_markdown, (
            "没对上编号的那条候选被写进了报告的信息缺口"
        )

    # ------------------------------------------------------------------
    # 待复核（deferred）：有去向的主动处置，不是缺口
    # ------------------------------------------------------------------

    def _deferred_synthesis(self, *anomalies) -> EngineOutcome:
        """汇总带着机器可读的处置：把 S1-1 标成待复核、理由写明。"""
        return EngineOutcome(
            status=STATUS_SUCCEEDED,
            report_markdown="# 变更理解\n\n改了道具表。\n",
            anomalies=tuple(anomalies),
            payload=AnalysisPayload(
                status="final",
                report_markdown="# 变更理解\n\n改了道具表。\n",
                candidate_dispositions=(
                    CandidateDisposition(
                        candidate_id="S1-1",
                        status="deferred",
                        reason="find_references 只覆盖本批次 238/1199 个文件，不能定级",
                    ),
                ),
            ),
        )

    def test_a_deferred_candidate_is_not_a_shard_gap(self):
        """run 40 的形态：五个代理全部 succeeded、零真缺口，只有汇总标记的待复核。

        待复核有去向、有理由、报告里另有一节逐条交代 —— 它与「汇总一声不响地丢了
        某条发现」不是一回事，不许触发 `subagent_gap` 降级（那条标签写的是「有分片
        没有跑成」，一句与事实相反的话）。
        """
        candidate = self._candidate(index=1, title="【道具】ID 被删除但生成文件仍在")
        plan = _plan(2)
        steps = tuple(
            MemberOutcome(plan=member, outcome=EngineOutcome(status=STATUS_SUCCEEDED))
            for member in plan.members
        )

        outcome = aggregate_outcomes(
            synthesis=self._deferred_synthesis(), steps=steps, candidates=(candidate,)
        )

        assert outcome.degradation != DEGRADE_SUBAGENT, "待复核把整次 run 判成了降级"
        assert outcome.status == STATUS_SUCCEEDED
        # **但账要照记**：读的人得知道有这条候选、它的去向与理由是什么。
        deferred = [item for item in outcome.dropped if item.kind == "deferred"]
        assert [item.reason for item in deferred] == ["汇总明确标记为待复核"]
        assert deferred[0].detail.startswith("[S1-1]"), "要写出候选编号"
        assert "待复核" in outcome.report_markdown, "报告里必须还有那一节逐条交代"

    def test_a_real_gap_still_degrades(self):
        """反向对照：真缺口（汇总没声明任何来源、也没给处置）照旧触发降级。

        分支要证明是活的 —— 上一条「不降级」不许顺手把这一条也抹平。
        """
        candidate = self._candidate(index=1, title="【道具】ID 被删除但生成文件仍在")
        # 汇总只声明了 S1-2 的血缘；S1-1 既没进清单、也没给处置 → 真缺口。
        adopted = _anomaly_obj(
            title="【道具】汇总自己新写的一条", source_candidate_ids=("S1-2",)
        )
        plan = _plan(2)
        steps = tuple(
            MemberOutcome(plan=member, outcome=EngineOutcome(status=STATUS_SUCCEEDED))
            for member in plan.members
        )

        outcome = aggregate_outcomes(
            synthesis=self._synthesis(adopted), steps=steps, candidates=(candidate,)
        )

        assert outcome.degradation == DEGRADE_SUBAGENT, "真缺口必须是降级"
        assert any(item.kind == "subagent" for item in outcome.dropped)

    # ------------------------------------------------------------------
    # 与复核裁决的接续（撤销 / 降级 / 转人工核验）
    # ------------------------------------------------------------------

    def test_a_retracted_candidate_is_not_missing(self):
        """被复核撤销的候选**不是遗漏**：它的去向是「已撤销」，平台如实说。"""
        from services.ai.verdict import (
            VERDICT_RETRACTED,
            VerifyVerdict,
            reduce_findings,
        )

        base = _anomaly_obj(
            title="【协议】通用蓝图报文结构被替换",
            file_path="code/qz_pub/protocols/ProtoScsGas.lua",
            source_candidate_ids=("S3-3",),
        )
        reduction = reduce_findings(
            [base],
            verdicts=(
                VerifyVerdict(
                    finding_id="F1",
                    verdict=VERDICT_RETRACTED,
                    reason="客户端与服务端已同步",
                    evidence_refs=("ProtoScsGas.lua:9",),
                ),
            ),
        )
        candidate = self._candidate(
            label="S3", index=3, title="【协议】通用蓝图报文结构被替换",
            file_path="code/qz_pub/protocols/ProtoScsGas.lua",
        )

        text, dropped = reconcile_candidates(
            (candidate,), self._synthesis(), reduction=reduction
        )

        assert dropped == (), "撤销不是缺口"
        assert "[S3-3]" in text and "撤销" in text
        assert "[F1]" in text, "要说清它对应哪条结论"

    def test_the_sources_are_persisted_on_the_finding_row(self):
        """血缘一路带到 `Reduction`：`FindingRow.source_candidate_ids` 与落库载荷都要有。"""
        from services.ai.verdict import reduce_findings

        base = _anomaly_obj(
            title="【协议】通用蓝图报文结构被替换", source_candidate_ids=("S3-3",)
        )

        reduction = reduce_findings([base])

        assert reduction.rows[0].source_candidate_ids == ("S3-3",)
        assert reduction.claimed_candidate_ids == frozenset({"S3-3"})
        assert reduction.as_dict()["rows"][0]["source_candidate_ids"] == ["S3-3"]

    # ------------------------------------------------------------------
    # 措辞
    # ------------------------------------------------------------------

    def test_the_wording_only_claims_what_was_checked(self):
        """不能写成「模型把它丢了」—— 平台查的是「汇总有没有交回这个编号」。

        多写一手就变成平台没做过的保证（例如「整份报告里都没有这个文件」）；少写一手，
        读的人会以为某类采纳方式平台看不见，又跑去人工核一遍已经核过的条目。
        """
        candidate = self._candidate()
        orphan = reconcile_candidates(
            (candidate,),
            self._synthesis(_anomaly_obj(title="别的", source_candidate_ids=("S1-9",))),
        )[0]
        no_lineage = reconcile_candidates(
            (candidate,), self._synthesis(_anomaly_obj(title="别的"))
        )[0]

        assert "没有任何一条声明来源于这个编号" in orphan
        assert "既没有引用这个编号" not in orphan, "编号当子串找过了 —— 只比集合，不比文本"
        assert "不是模型的自我说明" in orphan
        assert "不是模型的自我说明" in no_lineage
        assert "没有提到这个文件" not in orphan, "平台不再按文件名核对，这句话是假承诺"

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

    现在每个成员拿配置值的 `MEMBER_BUDGET_PERCENT`%（向上取整）——它是一条**下限**，
    与成员数无关。

    **这里的期望值一律从 `MEMBER_BUDGET_PERCENT` 算出来，不写死某个百分比。** 原先有两条
    把 70 的算术结果（28 / 18 / 7）直接写进断言，改百分比时它们会红 —— 但红的原因是
    「数字变了」，而不是「规则坏了」；照着新数字改一遍，测试就重新变绿，什么也没保住。
    真正要钉住的是「按配置值算、与成员数无关」，那是下面几条在管的事。
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
    def test_every_member_gets_the_configured_share_regardless_of_the_size(self, size):
        expected = _percent_of(40, MEMBER_BUDGET_PERCENT)
        plan = self._plan_with(40, size)
        assert plan.limits.max_tool_requests == expected, (
            f"{size} 个分片时每个成员拿到 {plan.limits.max_tool_requests} 次 —— "
            f"配置值 40 的 {MEMBER_BUDGET_PERCENT}% 是 {expected} 次"
        )

    def test_the_allowance_does_not_shrink_as_members_are_added(self):
        """**这条就是原规则的病**：分片越多，每个分片看得越少。"""
        small = self._plan_with(40, 2).limits.max_tool_requests
        large = self._plan_with(40, 6).limits.max_tool_requests
        assert large == small, (
            f"2 个分片时每人 {small} 次、6 个分片时每人 {large} 次 —— 加人反而看得更少"
        )

    def test_the_allowance_is_a_share_of_the_setting_not_a_division_of_it(self):
        """额度是「配置值 × 比例」，**不是**「配置值 ÷ 成员数」。

        这条与上面那条不重复：上面证明「不随成员数变」，这条证明「变的是配置值本身」——
        一个把额度写成常量（比如永远给 8 次）的实现能过上面那条，过不了这条。

        顺带钉住取整方向：25 的 70% 是 17.5，取 17 就低于「至少 N%」了，所以向上取整。
        （`_percent_of` 自身的取整行为另有一条纯函数用例。）
        """
        expected = _percent_of(25, MEMBER_BUDGET_PERCENT)
        assert self._plan_with(25, 3).limits.max_tool_requests == expected
        assert self._plan_with(80, 3).limits.max_tool_requests == _percent_of(
            80, MEMBER_BUDGET_PERCENT
        )
        if MEMBER_BUDGET_PERCENT < 100:
            # 比例小于 100 时，「略小于配置值」与「被成员数除过」必须能分辨出来。
            assert expected > 25 // 4, "额度看起来像是又按成员数平分了一次"

    def test_a_tiny_allowance_is_never_rounded_down_to_zero(self):
        """配 1 次就还是 1 次：向上取整保证「N%」不会把小额度抹成 0。

        （`MIN_MEMBER_TOOL_REQUESTS` 那道兜底在这条规则下几乎不会触发 —— 它留着是
        给「以后把百分比调小」用的，不是这里的判据。）
        """
        assert self._plan_with(1, 3).limits.max_tool_requests == 1

    def test_the_round_budget_gets_the_same_floor(self):
        """轮次也是「设置的额度」，不能一边给足索取次数、一边把轮次砍成一半。"""
        plan = self._plan_with(40, 3, rounds=10)
        assert plan.limits.max_rounds == _percent_of(10, MEMBER_BUDGET_PERCENT), (
            plan.limits.max_rounds
        )

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
