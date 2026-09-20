# -*- coding: utf-8 -*-
"""子代理模式的**计划层**：谁负责哪几个维度、额度怎么摊、任务书里写了什么。

## 这一组盯的是什么

分工是**平台算出来的**，不是模型自己商量的。所以它能被断言到逐字：

1. 生效清单上的维度**一个不漏、一个不重**地分给 n 个成员（漏一个 = 那个维度没人看，
   而不报错）；
2. 同一份配置永远得到同一套分工（分法是算出来的、确定的，不是拍脑袋的）；
3. 不适用的情形（单提交 / 没开 / 只给一个成员）**返回 `None`**，走原来的单代理路径；
4. 额度取**家族常量**（所有成员一样）—— 它是「共享消息逐字节相同」的前提，
   见 `tests/test_ai_subagent_cache.py`；
5. 任务书必须同时说清「你的职责是这几个维度」与「你的读取范围是整批」
   —— 少说一句就会出两种具体的错（跨模块耦合永远发现不了 / 替别人兜底）。

## 分工从固定表改成「按相邻顺序均分」之后，这一组盯的东西变了

旧实现是一张写死九个 id 的表，且**查不到档位时静默退化成「一个成员全看完」**
（用户花了 (n+1) 倍的钱拿到单代理效果，而没有任何提示）。维度集合一旦项目化
（`LoadedSkills.dimensions`，项目可声明），那张表必然作废。所以这里现在盯三条**结构性质**
——任何一份清单都必须满足：每组非空、每组是清单里连续的一段、全部维度恰好出现一次；
另加一条「清单短于分片数时少开成员，而不是悄悄换一条路」。
"""
from __future__ import annotations

import pytest

from services.ai.engine import EngineLimits
from services.ai.skill_contract import DIMENSION_IDS
from services.ai.subagent import (
    MAX_SUBAGENTS,
    MEMBER_BUDGET_PERCENT,
    ROLE_SYNTHESIS,
    MemberOutcome,
    _percent_of,
    apply_dimensions,
    build_member_task,
    build_synthesis_task,
    group_dimensions,
    plan_family,
)

LIMITS = EngineLimits(max_rounds=8, max_tool_requests=20)

# 一张**明显不是平台默认**的清单：项目声明了自己的维度（非配表项目的样子）。
PROJECT_DIMENSIONS = (
    "performance",
    "protocol",
    "resource",
    "save_data",
    "code_logic",
    "release",
)

# 计划层用例扫过的分片数档位。**不再从 `GROUPINGS` 的键取**：那张表已经不存在了，
# 而「有哪些档位」本来就该由配置范围（1~MAX_SUBAGENTS）决定。
COUNTS = tuple(range(2, MAX_SUBAGENTS + 1))


def _plan(count: int = 3, *, mode: str = "weekly", enabled: bool = True, dimensions=None):
    kwargs = {} if dimensions is None else {"dimensions": dimensions}
    plan = plan_family(mode=mode, enabled=enabled, count=count, limits=LIMITS, **kwargs)
    assert plan is not None, "这一组的前提是「该开子代理」"
    return plan


class TestTheGroupsCoverEverything:
    @pytest.mark.parametrize("count", COUNTS)
    def test_every_dimension_is_assigned_exactly_once(self, count):
        flat = [name for group in group_dimensions(count) for name in group]

        assert sorted(flat) == sorted(DIMENSION_IDS), (
            f"{count} 个成员时分工没有覆盖全部维度或出现了重复：{group_dimensions(count)}"
        )

    def test_the_group_count_matches(self):
        for count in COUNTS:
            assert len(group_dimensions(count)) == count

    def test_it_is_deterministic(self):
        """同一份输入永远得到同一套分工 —— 报告里那句「本次由 S1/S2/S3 分工」才立得住。"""
        assert group_dimensions(3) == group_dimensions(3)
        assert _plan(3).members == _plan(3).members

    @pytest.mark.parametrize("count", COUNTS)
    def test_every_group_is_a_contiguous_block(self, count):
        """**相邻即相关**：每一组必须是清单里连续的一段，不允许交错取样。

        顺序是有意的（`skill_contract.DEFAULT_DIMENSION_SPECS` 上面那段解释了为什么），
        而「相邻地切」正是把这个顺序用起来的唯一方式：交错取样会把「相关的维度」拆到
        不同成员身上，组内反而成了随机切片。
        """
        groups = group_dimensions(count)
        flat = [name for group in groups for name in group]
        assert flat == list(DIMENSION_IDS), f"分组的顺序被打乱了：{groups}"

    @pytest.mark.parametrize("count", COUNTS)
    def test_no_member_is_left_without_dimensions(self, count):
        """空成员 = 白花一次整份提示词的钱，还拉低报告里「谁负责什么」的可读性。"""
        assert all(group for group in group_dimensions(count))

    @pytest.mark.parametrize("count", COUNTS)
    def test_a_project_declared_list_splits_the_same_way(self, count):
        """**旧实现是一张写死九个 id 的表**，项目声明了自己的清单之后它必然作废 ——
        而现在任何一份清单都能分，且分法与默认清单同一套规则。"""
        groups = group_dimensions(count, PROJECT_DIMENSIONS)

        assert len(groups) == count
        assert [name for group in groups for name in group] == list(PROJECT_DIMENSIONS)
        assert all(group for group in groups)

    def test_related_dimensions_stay_together_or_adjacent(self):
        """`config_data` 与 `value_sanity` 是同一件事的两个说法（这一行自洽 / 这个值合理）。

        旧实现用一张手工表保证它们永远同组（分开的代价是两份报告各说一半：一个说
        「描述与取值打架」，一个说「量级不对」，而它们要一起看才知道是同一行）。
        按相邻顺序均分之后，同组不再是必然的：**分片数恰好切在它们中间时会分到相邻两组**
        —— 这是这次改动的已知代价（顺序可声明，项目可以把它们排到不被切开的位置）。
        所以这里断的是**不交错**：要么同组，要么落在相邻的两组里。
        """
        for count in COUNTS:
            groups = list(group_dimensions(count))
            left = next(index for index, group in enumerate(groups) if "config_data" in group)
            right = next(index for index, group in enumerate(groups) if "value_sanity" in group)
            assert abs(left - right) <= 1, f"{count} 个成员时这两个维度被拆到了不相邻的两组：{groups}"

    def test_out_of_range_counts_are_clamped_not_dropped(self):
        """超出范围时钳到边界 —— 无论怎么钳，全部维度都要有人管。"""
        assert group_dimensions(99) == group_dimensions(MAX_SUBAGENTS)
        assert group_dimensions(0) == group_dimensions(2)
        for group in (group_dimensions(99), group_dimensions(0)):
            assert sorted(name for item in group for name in item) == sorted(DIMENSION_IDS)

    def test_a_short_list_opens_fewer_members_instead_of_degenerating(self):
        """**清单比配置的分片数短时少开几个成员，而不是退化成「一个成员全看完」。**

        旧实现查不到档位时会返回「一个成员看全部」，而调用方照样付 (n+1) 次模型调用的钱
        —— 用户花 7 倍的钱拿到单代理效果，报告里一个字都不提。现在：分片数按维度数收敛，
        而且**显式说清为什么**（`count_note` 进汇总任务书）。
        """
        plan = _plan(6, dimensions=PROJECT_DIMENSIONS[:3])

        assert plan.count == 3
        assert len(plan.members) == 3
        assert [m.dimensions for m in plan.members] == [("performance",), ("protocol",), ("resource",)]
        assert plan.count_note, "少了成员却不说原因 —— 那正是要消灭的静默退化"
        assert "3" in plan.count_note and "6" in plan.count_note
        assert plan.count_note in build_synthesis_task(plan, ())

    def test_a_one_dimension_list_does_not_open_subagents_at_all(self):
        """清单只有 1 个维度时**没有拆法**：开子代理等于白花一次汇总的钱。

        这一档返回 `None`（走单代理路径，不额外花钱），并写一行日志 —— 与旧实现那句
        「一个成员全看完」的区别是：这里**不多花任何钱**，且原因写在日志里可以查到。
        """
        assert plan_family(
            mode="weekly", enabled=True, count=3, limits=LIMITS, dimensions=("only_one",)
        ) is None

    def test_apply_dimensions_rebuilds_the_split_from_the_effective_list(self):
        """生产路径：`plan_family` 按默认清单算完，开跑前按**加载出来的**清单重算分工。

        两份清单不同时，成员与汇总那一次的维度都必须换成生效清单里的 —— 否则模型看到
        的清单（提示词里那份）与平台分工用的清单就是两份。
        """
        plan = _plan(3)
        assert plan.synthesis.dimensions == DIMENSION_IDS

        replanned = apply_dimensions(plan, PROJECT_DIMENSIONS)

        assert replanned.count == 3
        assert replanned.synthesis.dimensions == PROJECT_DIMENSIONS
        assert [name for m in replanned.members for name in m.dimensions] == list(
            PROJECT_DIMENSIONS
        )
        # 已经是同一份清单时原样返回：不制造无意义的差异（`replace` 会换掉对象身份，
        # 而下游有按 `plan` 判等的比较）。
        assert apply_dimensions(plan, DIMENSION_IDS) is plan


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

        # `plan.limits` 是**所有**成员（含汇总）用的那一份，所以它必须是**算出来的常量**、
        # 与成员数无关：每个成员拿配置值的 `MEMBER_BUDGET_PERCENT`%（默认 70%，向上取整）。
        assert plan.limits.max_tool_requests == _percent_of(20, MEMBER_BUDGET_PERCENT)
        assert plan.limits.max_rounds == _percent_of(8, MEMBER_BUDGET_PERCENT)

    def test_the_allowance_does_not_depend_on_the_member_count(self):
        """**「设置的额度」是一个 agent 的额度，不是全家共享的一锅。**

        原先按 `总额 ÷ (成员数 + 1)` 摊，于是分片开得越多、每个分片看得越少 ——
        与「多开几个分片来看得更全」正好相反。
        """
        allowances = {
            count: _plan(count).limits.max_tool_requests for count in (2, 3, 4, 6)
        }
        assert len(set(allowances.values())) == 1, allowances
        assert set(allowances.values()) == {_percent_of(20, MEMBER_BUDGET_PERCENT)}

    def test_a_tiny_quota_still_gives_each_member_something(self):
        plan = plan_family(
            mode="weekly", enabled=True, count=6,
            limits=EngineLimits(max_rounds=1, max_tool_requests=3),
        )

        assert plan is not None
        assert plan.limits.max_tool_requests >= 1, "不能变成 0 次索取"
        assert plan.limits.max_rounds >= 1

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
        """成员额度以配置值为基准，但**永远不超过**它（再小的配置也只是「不变」）。"""
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

    def test_it_keeps_the_output_protocol_and_every_dimension(self):
        plan = _plan(3)
        task = build_member_task(plan.members[0], plan)

        # 「分了工也要把**生效清单上的每一个**维度都留痕」——条数从计划里算，不写死。
        assert "都要留痕" in task, "分了工也要维度留痕（防只报自己那几块）"
        assert str(len(plan.synthesis.dimensions)) in task, (
            f"任务书里的维度条数与生效清单不一致：{task}"
        )
        assert "信息缺口" in task and "绝不许写成「没问题」" in task, (
            "取不到的内容不许写成「没问题」—— 这条纪律分片后更要紧"
        )

    def test_the_member_task_carries_no_quota_number_of_its_own(self):
        """成员任务书里**没有**它自己的额度数字 —— 这是有意的，不是漏写。

        原先这里写的是

            assert str(plan.limits.max_tool_requests) in task or "额度" in task

        析取的后半截把前半截废掉了：`"额度"` 确实在任务书里，但它出现在
        「但不要为了它们专门花索取额度」这句与额度数字无关的话里，而
        `str(plan.limits.max_tool_requests) in task` 实测为 False。所以整条断言的
        实际内容只有 `"额度" in task` —— 一份**假保证**，比没有断言更坏：
        下一个改额度的人会以为这里有网，而把成员额度低报成五分之一也照样绿。

        真实契约是反过来的：额度走**共享消息**那一句（「本次分析总共可索取 N 次」，
        必须逐字节相同，见 `tests/test_ai_subagent_cache.py`），成员任务书只列
        职责 / 权限 / 分工 / 输出协议。真正告诉模型「你能花多少」的那句话在
        **汇总**任务书里，由
        `TestTheSynthesisTask::test_it_states_the_family_quota_with_its_unit` 钉住。

        这里断**方向**：不写可以，写了就必须不小于真实限值 —— 哪天真要写进来，
        这条会红，写的人得来说明写的是哪个数、为什么。
        """
        plan = _plan(3)
        task = build_member_task(plan.members[0], plan)

        for limit in (plan.limits.max_tool_requests, plan.limits.max_rounds):
            assert f"{limit} 次" not in task and f"{limit} 轮" not in task, (
                f"任务书里出现了额度数字 {limit} —— 如果这是有意加的，"
                "请把这条用例改成「断言它不小于 plan.limits」并写明理由"
            )


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

    def test_it_states_the_family_quota_with_its_unit(self):
        """额度必须**写全**：「各 N 次索取、最多 M 轮」—— 数字与单位一起断言。

        原先这条挂在 `TestTheMemberTask` 上，写的是

            assert str(plan.limits.max_tool_requests) in task or "额度" in task

        析取的后半截把前半截废掉了：那个 `"额度"` 出现在成员任务书的
        「但不要为了它们专门花索取额度」这句**与额度数字无关**的话里，
        实测 `str(20) in task` 为 False —— 整条断言的实际内容只有 `"额度" in task`，
        把成员额度低报成 5 也照样通过。而且它断错了对象：**成员任务书里
        刻意不写成员自己的数字**（额度由引擎按家族统一执行，任务书里列的是
        职责 / 权限 / 分工 / 输出协议），这句明确告诉模型「你能花多少」的话
        在**汇总**的任务书里。

        低报的后果是具体的：模型据此决定「一次要完还是逐步逼近」，
        报小了它会过早收手。所以判据要落在**那一句话**上，而不是落在
        「文本里出现过这个数」或「文本里出现过额度二字」。
        """
        plan = _plan(3)
        task = build_synthesis_task(plan, self._steps(plan))

        assert f"各 {plan.limits.max_tool_requests} 次索取" in task, task
        assert f"最多 {plan.limits.max_rounds} 轮" in task, task

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
