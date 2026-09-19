# -*- coding: utf-8 -*-
"""子代理模式的**计划层**：谁负责哪几个维度、额度怎么摊、任务书里写了什么。

## 这一组盯的是什么

分工是**平台算出来的**，不是模型自己商量的。所以它能被断言到逐字：

1. 九个维度**一个不漏、一个不重**地分给 n 个成员（漏一个 = 那个维度没人看，而不报错）；
2. 同一份配置永远得到同一套分工（`GROUPINGS` 是写死的表，不是算出来的）；
3. 不适用的三种情形（单提交 / 没开 / 只给一个成员）**返回 `None`**，走原来的单代理路径；
4. 额度取**家族常量**（所有成员一样）—— 它是「共享消息逐字节相同」的前提，
   见 `tests/test_ai_subagent_cache.py`；
5. 任务书必须同时说清「你的职责是这几个维度」与「你的读取范围是整批」
   —— 少说一句就会出两种具体的错（跨模块耦合永远发现不了 / 替别人兜底）。
"""
from __future__ import annotations

import pytest

from services.ai.engine import EngineLimits
from services.ai.skill_contract import DIMENSION_IDS
from services.ai.subagent import (
    GROUPINGS,
    MAX_SUBAGENTS,
    ROLE_SYNTHESIS,
    MemberOutcome,
    build_member_task,
    build_synthesis_task,
    group_dimensions,
    plan_family,
)

LIMITS = EngineLimits(max_rounds=8, max_tool_requests=20)


def _plan(count: int = 3, *, mode: str = "weekly", enabled: bool = True):
    plan = plan_family(mode=mode, enabled=enabled, count=count, limits=LIMITS)
    assert plan is not None, "这一组的前提是「该开子代理」"
    return plan


class TestTheGroupsCoverEverything:
    @pytest.mark.parametrize("count", sorted(GROUPINGS))
    def test_every_dimension_is_assigned_exactly_once(self, count):
        flat = [name for group in group_dimensions(count) for name in group]

        assert sorted(flat) == sorted(DIMENSION_IDS), (
            f"{count} 个成员时分工没有覆盖全部维度或出现了重复：{group_dimensions(count)}"
        )

    def test_the_group_count_matches(self):
        for count in sorted(GROUPINGS):
            assert len(group_dimensions(count)) == count

    def test_it_is_deterministic(self):
        """同一份输入永远得到同一套分工 —— 报告里那句「本次由 S1/S2/S3 分工」才立得住。"""
        assert group_dimensions(3) == group_dimensions(3)
        assert _plan(3).members == _plan(3).members

    def test_related_dimensions_land_together(self):
        """`config_data` 与 `value_sanity` 都是「这一行/这个值自己说不说得通」，必须同组。

        分开的代价是两份报告各说一半：一个说「描述与取值打架」，一个说「量级不对」，
        而它们要一起看才知道是同一行。
        """
        for count in sorted(GROUPINGS):
            groups = group_dimensions(count)
            same = [group for group in groups if "config_data" in group]
            assert same, groups
            assert "value_sanity" in same[0], f"{count} 个成员时这两个维度被拆开了：{groups}"

    def test_out_of_range_counts_are_clamped_not_dropped(self):
        """超出范围时钳到边界 —— 无论怎么钳，九个维度都要有人管。"""
        assert group_dimensions(99) == group_dimensions(MAX_SUBAGENTS)
        assert group_dimensions(0) == group_dimensions(2)
        for group in (group_dimensions(99), group_dimensions(0)):
            assert sorted(name for item in group for name in item) == sorted(DIMENSION_IDS)


class TestWhenSubagentsDoNotApply:
    def test_a_commit_analysis_is_never_split(self):
        """单提交的规模本来就不需要分工 —— 拆了只会让「这一次提交改了什么」多绕一圈。"""
        assert plan_family(mode="commit", enabled=True, count=3, limits=LIMITS) is None

    def test_it_is_off_by_default(self):
        assert plan_family(mode="weekly", enabled=False, count=3, limits=LIMITS) is None

    @pytest.mark.parametrize("count", [0, 1, None])
    def test_a_single_member_degrades_to_the_plain_path(self, count):
        """一个成员就是原来的单代理，只会白白多花一次汇总的钱。"""
        assert plan_family(mode="weekly", enabled=True, count=count, limits=LIMITS) is None

    def test_the_count_is_capped(self):
        plan = _plan(99)

        assert len(plan.members) == MAX_SUBAGENTS


class TestTheLimitsAreFamilyConstants:
    """**共享消息逐字节相同**的前提：额度不能按成员微调。"""

    def test_every_member_shares_one_number(self):
        plan = _plan(3)

        # `plan.limits` 是**所有**成员（含汇总）用的那一份；这里断言它确实比整次分析的
        # 额度小（否则 n 个成员会把额度花成 n 倍），且不小于一个成员该有的下限。
        assert plan.limits.max_tool_requests < LIMITS.max_tool_requests
        assert plan.limits.max_tool_requests == LIMITS.max_tool_requests // 4
        assert plan.limits.max_rounds == LIMITS.max_rounds // 2

    def test_a_tiny_quota_still_gives_each_member_something(self):
        plan = plan_family(
            mode="weekly", enabled=True, count=6,
            limits=EngineLimits(max_rounds=1, max_tool_requests=3),
        )

        assert plan is not None
        assert plan.limits.max_tool_requests >= 2, "分摊后不能变成 0 次索取"
        assert plan.limits.max_rounds >= 2

    def test_the_floor_never_overrides_an_explicit_zero(self):
        """上限配成 0 = 「这次一次上下文都不给」，那个「下限 2」不许把它顶回去。

        顶回去的后果是配置说话不算数：项目里写着 0，周版本分析却每个分片各拿 2 次，
        而提示词还把 2 说成「本次分析总共可索取 2 次」—— 配置、提示词、用户的理解
        三样对不上。
        """
        plan = plan_family(
            mode="weekly", enabled=True, count=3,
            limits=EngineLimits(max_rounds=8, max_tool_requests=0),
        )

        assert plan is not None
        assert plan.limits.max_tool_requests == 0

    def test_a_member_never_gets_more_than_the_whole_run(self):
        """成员额度是「总额度 ÷ (n+1)」，再小的总额也只是**不变**，不许被抬高。"""
        plan = plan_family(
            mode="weekly", enabled=True, count=6,
            limits=EngineLimits(max_rounds=8, max_tool_requests=1),
        )

        assert plan is not None
        assert plan.limits.max_tool_requests == 1

    def test_the_synthesis_is_covered_by_the_same_numbers(self):
        plan = _plan(3)

        assert plan.synthesis.index == 4
        assert plan.count + 1 == plan.synthesis.index
        assert set(plan.synthesis.dimensions) == set(DIMENSION_IDS), (
            "汇总那一次的职责是**九个维度都要有交代**，不是只管某几组"
        )


class TestTheMemberTask:
    def test_it_names_the_dimensions_it_owns(self):
        plan = _plan(3)
        task = build_member_task(plan.members[1], plan)

        for name in plan.members[1].dimensions:
            assert name in task, f"任务书里没写它负责 {name}：{task}"
        assert plan.members[1].label in task

    def test_it_says_the_read_scope_is_the_whole_batch(self):
        """**这一条是这个功能最容易做错的地方。**

        子代理只被告知「你负责这几个维度」时，它会以为自己的视野也只有这几个维度，
        于是「协议改了、调用方没改」这类跨模块耦合永远发现不了 —— 而那正是分工要
        抓的东西。
        """
        for member in _plan(3).members:
            task = build_member_task(member, _plan(3))

            assert "可以读取本批次改动的任何一个文件" in task, task
            assert "不受上面的维度限制" in task or "不是你的视野边界" in task, task

    def test_it_names_the_other_members(self):
        plan = _plan(3)
        task = build_member_task(plan.members[0], plan)

        assert "S2" in task and "S3" in task, "没说别的成员负责什么，它会替别人兜底"
        assert "config_data" in task, "别的人维度也要写出来（避免重复劳动）"

    def test_it_keeps_the_output_protocol_and_the_nine_dimensions(self):
        task = build_member_task(_plan(3).members[0], _plan(3))

        assert "九个维度都要留痕" in task, "分了工也要九个维度都留痕（防只报自己那几块）"
        assert "信息缺口" in task and "绝不许写成「没问题」" in task, (
            "取不到的内容不许写成「没问题」—— 这条纪律分片后更要紧"
        )

    def test_the_task_does_not_understate_the_quota(self):
        plan = _plan(3)

        task = build_member_task(plan.members[0], plan)

        assert str(plan.limits.max_tool_requests) in task or "额度" in task, task


class TestTheSynthesisTask:
    def _steps(self, plan):
        return (
            MemberOutcome(plan=plan.members[0]),
            MemberOutcome(plan=plan.members[1]),
            MemberOutcome(
                plan=plan.members[2], skipped_reason="剩余预算不足（还有 1200 tokens）"
            ),
        )

    def test_it_lists_the_members_and_their_dimensions(self):
        plan = _plan(3)
        task = build_synthesis_task(plan, self._steps(plan))

        assert "S1" in task and "S2" in task, task
        assert "主代理" in task

    def test_it_demands_a_destination_for_every_candidate(self):
        plan = _plan(3)
        task = build_synthesis_task(plan, self._steps(plan))

        assert "每一条候选都要有去向" in task, task
        assert "不许一声不响地丢掉" in task, task

    def test_a_skipped_member_becomes_a_named_gap(self):
        """**跳过必须点名**：静默跳过等于把「没人看过」写成「没问题」。"""
        plan = _plan(3)
        task = build_synthesis_task(plan, self._steps(plan))

        assert "S3" in task and "未运行" in task, task
        assert "剩余预算不足" in task, "跳过它的原因要原样带出来"
        assert "没有任何人看过" in task, task
        assert "绝不能因为没人报出问题就当成「没问题」" in task, task


class TestSubagentModeOf:
    def test_without_subagents_it_is_none(self):
        from services.ai.engine import EngineOutcome
        from services.ai.subagent import subagent_mode_of

        assert subagent_mode_of(EngineOutcome(status="succeeded")) is None

    def test_it_counts_only_the_members(self):
        from services.ai.engine import EngineOutcome
        from services.ai.subagent import subagent_mode_of

        outcome = EngineOutcome(
            status="succeeded",
            subagents=(
                {"role": "subagent", "label": "S1"},
                {"role": "subagent", "label": "S2"},
                {"role": ROLE_SYNTHESIS, "label": "汇总"},
            ),
        )

        assert subagent_mode_of(outcome) == ("subagents", 2)
