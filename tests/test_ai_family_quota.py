# -*- coding: utf-8 -*-
"""家族共享池（`subagent.FamilyQuota`）：串行滚动、名义额保底、富余流向重分片。

## 为什么测行为不测数字

run 41 的病根是「S1 剩 18 次、S3 剩 14 次没处花，S2 撞满 50+10 双顶降级」—— 每代理
各一份额度时，重分片的富余永远没有出路。这里用注入的假引擎逐段**记录编排层发给每个成员
的当片上限与任务书**，钉住滚动的三条性质（富余滚动 / 名义额保底 / 池尽如实给 0），
数字本身由 `FamilyQuota` 的纯函数测试单独钉。
"""
from __future__ import annotations

import dataclasses

from services.ai.engine import (
    STATUS_SUCCEEDED,
    EngineLimits,
    EngineOutcome,
    RoundRecord,
)
from services.ai.family_ledger import AssignedFile, mandatory_request_floor
from services.ai.subagent import (
    FamilyPlan,
    FamilyQuota,
    MemberPlan,
    attach_seed,
    plan_family,
    run_family,
)
from tests.test_ai_engine import _loaded, _scope
from tests.test_ai_subagent_family import CHANGE_SUMMARY, LIMITS


class _ScriptedEngine:
    """假引擎：记下每次调用的当片上限与任务书，按脚本回**实耗**（索取/轮次）。

    引擎与 `ContextTools` 一行不动是这次改造的前提：编排层发的 `limits.max_tool_requests`
    就是当片上限，引擎只认它。假引擎在这里替引擎把「这一段实际花了多少」报回编排层
    （真实引擎回的是 `requests_used` 与 `len(rounds)`，见 `run_family` 的扣账注释）。
    脚本只写分片与汇总（对账轮不开就是这几段），每段花完才轮到下一段。
    """

    def __init__(self, *spend: tuple[int, int]):
        self.calls: list[dict] = []
        self.script = list(spend) or [(0, 0)]

    def __call__(self, **kwargs):
        self.calls.append(
            {
                "limits": kwargs["limits"],
                "task": kwargs["task_message"],
                "seed": tuple(dict(item) for item in (kwargs.get("seed_messages") or ())),
            }
        )
        if len(self.script) > 1:
            requests_used, rounds = self.script.pop(0)
        else:
            requests_used, rounds = self.script[0]
        return EngineOutcome(
            status=STATUS_SUCCEEDED,
            report_markdown="# 变更理解\n\n改了道具表。\n",
            requests_used=requests_used,
            rounds=tuple(
                RoundRecord(index=number + 1, status="final")
                for number in range(rounds)
            ),
        )


def _plan(count: int = 3, *, verify: bool = False):
    plan = plan_family(mode="weekly", enabled=True, count=count, limits=LIMITS)
    assert plan is not None
    if verify:
        plan = dataclasses.replace(plan, verify=True)
    return attach_seed(plan, loaded=_loaded(), change_summary=CHANGE_SUMMARY)


def _run_family(engine, plan):
    return run_family(
        client=object(),
        provider=None,
        plan=plan,
        loaded=_loaded(),
        scope=_scope(),
        change_summary=CHANGE_SUMMARY,
        run_analysis_fn=engine,
    )


def _cap_of(engine: _ScriptedEngine, index: int) -> int:
    return engine.calls[index]["limits"].max_tool_requests


# ---------------------------------------------------------------------------
# 一、编排层的滚动（行为）
# ---------------------------------------------------------------------------


class TestTheRollingOrchestration:
    def test_a_thrifty_first_shard_lends_its_surplus_forward(self):
        """S1 只用 5 次 → S2 的当片上限超过名义额（run 41 的 18+14 次富余这次有了出路）。

        账：池 = 3×20 = 60；S1 用 5 剩 55；S2 = 名义 20 +（55 − 20 −（后面 1 片 20 +
        汇总下限 2））= 20 + 13 = 33。
        """
        plan = _plan(3)
        nominal = plan.quota.requests_nominal
        engine = _ScriptedEngine((5, 1), (nominal, 1), (nominal, 1))

        _run_family(engine, plan)

        assert _cap_of(engine, 0) == nominal, "第一片在没人花钱之前只能拿名义额"
        assert _cap_of(engine, 1) == 33, (
            f"S2 的上限没吃到 S1 省下来的富余：S1 用 5/名义 {nominal}，S2 应拿 33，"
            f"实际 {_cap_of(engine, 1)}"
        )

    def test_a_greedy_first_shard_does_not_starve_the_rest(self):
        """S1 把自己的上限全部用掉：后面每片仍拿得到名义额（预留先保它们）。"""
        plan = _plan(3)
        nominal = plan.quota.requests_nominal
        engine = _ScriptedEngine((nominal, 1), (nominal, 1), (nominal, 1))

        _run_family(engine, plan)

        assert _cap_of(engine, 1) == nominal, "S1 花光名义额后 S2 被压到了名义额以下"
        assert _cap_of(engine, 2) == nominal, "S3 被压到了名义额以下"

    def test_a_skipped_shard_lends_its_whole_entitlement(self):
        """被预算闸门跳过的成员不扣池：它的名义额整份滚给后面的分片。

        账：S1 跳过不花钱 → 池剩 60；S2 = 20 +（60 − 20 −（后面 1 片 20 + 下限 2））
        = 20 + 18 = 38。
        """
        plan = _plan(3)
        nominal = plan.quota.requests_nominal
        engine = _ScriptedEngine((nominal, 1), (nominal, 1))

        run_family(
            client=object(),
            provider=None,
            plan=plan,
            loaded=_loaded(),
            scope=_scope(),
            change_summary=CHANGE_SUMMARY,
            run_analysis_fn=engine,
            should_skip=lambda member, spent: "预算不足" if member.label == "S1" else "",
        )

        assert len(engine.calls) == 3, "S1 被跳过后还有 2 个分片 + 1 次汇总要跑"
        assert _cap_of(engine, 0) == 2 * nominal - 2, (
            f"被跳过的 S1 的名义额没有滚给 S2：S2 拿到 {_cap_of(engine, 0)}，"
            f"按「2×名义 − 汇总下限」应是 {2 * nominal - 2}"
        )

    def test_the_task_book_carries_the_pool_account(self):
        """当片上限与全家已用写进成员私有任务书；共享前缀一个字节不带它们。"""
        plan = _plan(3)
        nominal = plan.quota.requests_nominal
        engine = _ScriptedEngine((3, 1), (nominal, 1), (nominal, 1))

        _run_family(engine, plan)

        s2_task = engine.calls[1]["task"]
        assert "你这一段的上限是" in s2_task, "任务书里没有当片上限"
        assert "已用 3 次索取" in s2_task, "任务书里没有「前面已用多少」"
        assert f"名义额 {nominal} 次" in s2_task, "任务书里没有名义额（富余的对照）"
        # 共享前缀只说池（家族常量），随成员变化的数字绝不进它 —— 进了缓存就全 miss。
        # 「已用」两个字本身可以是常量文案（「已用的次数写在你的任务书里」），要防的是
        # **数字**泄进去：具体成员的已用量、当片上限都不许出现在前缀里。
        seed_texts = {
            tuple(item["content"] for item in call["seed"]) for call in engine.calls
        }
        assert len(seed_texts) == 1, "成员之间的共享前缀不一致 —— prompt cache 全 miss"
        for seed in seed_texts:
            assert "你这一段的上限是" not in seed[1], "当片上限泄进了共享前缀"
            assert "已用 3 次" not in seed[1], "全家已用的具体数字泄进了共享前缀"
            assert f"全家共可索取 {plan.quota.requests_pool} 次" in seed[1], (
                "共享前缀里没有池总量 —— 成员不知道自己在共享池里跑"
            )

    def test_synthesis_gets_the_rest_and_never_starves(self):
        """汇总拿池内剩余；池被分片花光时**保下限、可越池**（不许被饿死）。"""
        plan = _plan(3)
        nominal = plan.quota.requests_nominal
        engine = _ScriptedEngine((nominal, 1), (nominal, 1), (nominal, 1), (0, 0))

        _run_family(engine, plan)

        assert _cap_of(engine, 3) == 2, "池空了汇总仍要保下限（可越池）"

    def test_the_pool_exhaustion_is_honest_at_zero(self):
        """池尽时对账轮如实拿 0 —— 引擎会按它自己的口径降级，平台不虚发额度。"""
        plan = _plan(3, verify=True)
        nominal = plan.quota.requests_nominal
        engine = _ScriptedEngine((nominal, 1), (nominal, 1), (nominal, 1), (0, 0), (0, 0))

        _run_family(engine, plan)

        assert _cap_of(engine, 3) == 2, "汇总先拿保底下限"
        assert _cap_of(engine, 4) == 0, "对账轮不保下限：池尽如实给 0"


# ---------------------------------------------------------------------------
# 二、FamilyQuota 的纯函数（数字）
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 四、对账轮的预留在真跑一遍时也成立（run 57：250 次在复核之前被吃光）
# ---------------------------------------------------------------------------


class TestTheVerifyReserveSurvivesARealRun:
    def _plan_with_verify(self, count: int = 3, verify_items: int = 3):
        plan = plan_family(
            mode="weekly", enabled=True, count=count, limits=LIMITS,
            verify=True, verify_items=verify_items,
        )
        assert plan is not None and plan.quota is not None
        return attach_seed(plan, loaded=_loaded(), change_summary=CHANGE_SUMMARY)

    def test_the_shards_spend_everything_they_can_and_the_verify_still_gets_its_cap(self):
        """分片**每一片都把自己那一段花满**（最凶的用法），对账轮仍拿到预留那一份。

        这是 run 57 的形状：分片按当片上限一路花到底、汇总再取池内剩余，复核拿到 0。
        断言落在**发给对账轮的 `limits`** 上 —— 只测 `verify_caps()` 的算术，
        证明不了 `run_family` 用的是它。
        """
        plan = self._plan_with_verify(count=3, verify_items=3)
        reserve = plan.quota.verify_requests
        assert reserve > 0

        # 每一段都按「上限」报实耗：S1/S2/S3 花满自己的当片上限，汇总也花满。
        engine = _ScriptedEngine((10_000, 1))  # 远大于上限 → 实耗被上限夹住
        _run_family(engine, plan)

        caps = [_cap_of(engine, index) for index in range(len(engine.calls))]
        # 最后一段是对账轮（分片 ×3 + 汇总 + 对账）
        assert len(caps) == 5, f"成员段数不对：{caps}"
        assert caps[-1] >= reserve, (
            f"分片把池花光之后，对账轮只拿到 {caps[-1]} 次（预留 {reserve} 次）——"
            "run 57 就是这么只裁了 20 条里的 3 条"
        )
        # 而分片那几段**没有**动用预留：它们的上限总和加上汇总的下限不超过池
        assert sum(caps[:4]) <= plan.quota.requests_pool, (
            "分片与汇总把池撑破了 —— 那正是预留被吃掉的形状"
        )

    def test_the_synthesis_is_not_handed_the_verify_reserve(self):
        """汇总的上限 = **池内剩余 − 对账预留**（那一份是留给复核的）。

        分片各只用 5 次（远低于名义额），于是池里剩一大截：这时候「汇总拿走多少」
        就看得出预留在不在里面 —— 改回 `max(剩余, 下限)` 时这里会多出 `reserve` 次。
        """
        plan = self._plan_with_verify(count=2, verify_items=1)
        reserve = plan.quota.verify_requests
        assert reserve > 0

        engine = _ScriptedEngine((5, 1))  # S1、S2 各用 5 次索取 1 轮
        _run_family(engine, plan)

        spent_by_shards = 10
        synthesis_cap = _cap_of(engine, 2)  # 0/1 是分片，2 是汇总
        assert synthesis_cap == plan.quota.requests_pool - spent_by_shards - reserve, (
            f"汇总拿到的上限里含（或不含）对账预留，与「池内剩余 − 预留」对不上："
            f"拿到的 {synthesis_cap}，池 {plan.quota.requests_pool}、"
            f"分片已用 {spent_by_shards}、预留 {reserve}"
        )

    def test_a_verify_round_warning_names_the_stage(self, monkeypatch):
        """池见底时日志里要有**阶段名 + 池剩余数**（原先只有引擎那句「额度用尽」）。

        断言挂在 `log_print` 上而不是 `capsys`：日志器在 import 时就持有了原始 stdout
        的引用，`capsys` 抓不到它写出去的内容（本仓库既有测试记着这一条）。
        """
        from services.ai import subagent

        messages: list[str] = []
        monkeypatch.setattr(
            subagent, "log_print", lambda *args, **kwargs: messages.append(str(args[0] or ""))
        )
        plan = self._plan_with_verify(count=3, verify_items=1)
        engine = _ScriptedEngine((10_000, 1))

        _run_family(engine, plan)

        joined = "\n".join(messages)
        assert "共享池已耗尽" in joined, f"池见底没有任何一句可查的话：{joined}"
        assert any(stage in joined for stage in ("S2", "S3", "汇总", "V1")), (
            f"告警里没有阶段名，读日志的人不知道是哪一步见底的：{joined}"
        )
        assert "剩余 0 次索取" in joined, f"告警没写池剩余数：{joined}"


class TestFamilyQuotaMath:
    @staticmethod
    def _quota() -> FamilyQuota:
        """每个用例一份新账本 —— `FamilyQuota` 是可变对象，类属性会被跨用例攒账。"""
        return FamilyQuota(
            requests_pool=240,
            rounds_pool=50,
            requests_nominal=48,
            rounds_nominal=10,
        )

    def test_each_shard_gets_its_nominal_when_predecessors_spent_theirs(self):
        """前面每片都花光自己的名义额时，后面每片拿到的仍恰好是名义额。"""
        quota = self._quota()
        for position in range(5):
            cap_requests, cap_rounds = quota.shard_caps(4 - position)
            assert cap_requests == 48, f"第 {position} 片没拿到名义额：{cap_requests}"
            assert cap_rounds == 10, f"第 {position} 片轮次没拿到名义额：{cap_rounds}"
            quota.spend(48, 10)

    def test_surplus_flows_to_the_first_shard_that_needs_it(self):
        """前一片省下的额度滚给下一片（run 41 的 S1 用 32/48、S2 双顶 50/50 的对照）。"""
        quota = self._quota()
        quota.spend(32, 6)  # S1 只用 32（实测 run 41 的 S1）
        cap_requests, cap_rounds = quota.shard_caps(3)
        # 索取：剩 208；后面 3 片名义 144 + 汇总下限 2 → 滚 208−48−146 = 14。
        assert cap_requests == 48 + 14, cap_requests
        # 轮次：剩 44；后面 3 片名义 30 + 下限 2 → 滚 44−10−32 = 2。
        assert cap_rounds == 10 + 2, cap_rounds

    def test_the_last_shard_inherits_everything_left(self):
        """最后一片之后只剩汇总：池里所有没花掉的都归它（汇总保下限、可越池）。"""
        quota = self._quota()
        quota.spend(10, 2)
        quota.spend(10, 2)
        # 到最后一片时剩 240−20=220：名义 48 +（220−48−2）= 218；轮次剩 46：
        # 名义 10 +（46−10−2）= 44。
        cap_requests, cap_rounds = quota.shard_caps(0)
        assert cap_requests == 218, cap_requests
        assert cap_rounds == 44, cap_rounds

    def test_the_pool_never_gives_more_than_it_has(self):
        quota = self._quota()
        quota.spend(240, 50)  # 全花光
        assert quota.shard_caps(0) == (0, 0)
        assert quota.synthesis_caps() == (2, 2)  # 保下限、可越池
        assert quota.verify_caps() == (0, 0)

    def test_spend_is_by_actual_usage_not_nominal(self):
        """名义额是上限不是预扣：只有真用掉的才滚不出去（+2 格式重问轮不双记）。"""
        quota = self._quota()
        quota.spend(10, 3)
        assert quota.requests_left == 230
        assert quota.rounds_left == 47


# ---------------------------------------------------------------------------
# 三、账本的来源与兜底
# ---------------------------------------------------------------------------


def test_plan_family_builds_the_pool_from_actual_members():
    """维度比目标少、少开了成员时，池跟着缩 —— 池是「这几个成员能花的钱」。"""
    plan = plan_family(
        mode="weekly",
        enabled=True,
        count=5,
        limits=EngineLimits(max_rounds=8, max_tool_requests=20),
        dimensions=("a", "b", "c"),  # 3 个维度 → 最多 3 片
    )
    assert plan is not None and plan.count == 3
    assert plan.quota.requests_pool == 3 * plan.quota.requests_nominal
    assert plan.quota.rounds_pool == 3 * plan.quota.rounds_nominal


def test_a_manual_plan_without_quota_falls_back_to_nominal_pools():
    """手搓的 `FamilyPlan`（不带账本）退回「名义额即池」—— 行为等价旧口径。"""
    members = tuple(
        MemberPlan(index=i, label=f"S{i}", role="subagent", dimensions=("a",))
        for i in range(1, 3)
    )
    plan = FamilyPlan(
        count=2,
        members=members,
        synthesis=MemberPlan(index=3, label="", role="synthesis", dimensions=("a",)),
        limits=EngineLimits(max_rounds=10, max_tool_requests=30),
    )
    engine = _ScriptedEngine((30, 1), (30, 1), (0, 0))
    _run_family(engine, plan)
    # 没有账本时每片拿 limits 里的那个数（名义额即池，不滚动也不借贷）。
    assert _cap_of(engine, 0) == 30
    assert _cap_of(engine, 1) == 30
    assert _cap_of(engine, 2) == 2, "汇总仍保下限"


class TestTheMandatoryListHasItsOwnQuota:
    """必读清单的保底额度（P1b）：**分到的文件要读得起**。

    分工是平台按 `(仓库, 路径, 提交)` 定下来的，而取证此前只靠模型自主 —— 一片分到 60 个
    文件、名义额只有 48 次索取时，「把它们各读一次」这件事在额度上根本做不到，任务书里
    那句「先都取一次 diff」就是一句空话。这一档补的是那个差。

    判据落在**当片上限**上，不落在 `_cap` 的算术细节上：算术由下面两条纯函数用例钉。
    """

    @staticmethod
    def _files(count: int) -> tuple:
        return tuple(
            AssignedFile(repository_id=1, commit=f"{index:040x}", path=f"code/m{index}.lua")
            for index in range(count)
        )

    def test_the_floor_is_the_part_the_nominal_cannot_cover(self):
        """名义额 48、清单 60 条 ⇒ 保底 12；清单 40 条 ⇒ 0（行为与从前逐字节相同）。"""
        long_member = MemberPlan(
            index=1, label="S1", role="subagent", dimensions=("a",), assigned_files=self._files(60)
        )
        short_member = MemberPlan(
            index=1, label="S1", role="subagent", dimensions=("a",), assigned_files=self._files(40)
        )
        assert mandatory_request_floor(long_member, 48) == 12
        assert mandatory_request_floor(short_member, 48) == 0
        # 没接线（老计划 / 汇总那一次）不占池子。
        assert mandatory_request_floor(MemberPlan(1, "S1", "subagent", ("a",)), 48) == 0

    def test_a_shard_gets_its_floor_even_when_the_pool_is_tight(self):
        """池紧到快没富余时，保底仍然给足（**这正是它要管的那一档**）。

        池 200、名义 48、后面 3 片名义 144 + 汇总下限 2：不加保底只剩 6 次富余 → 54，
        读不完 60 个文件；加了保底就是 60。池**宽**时两者一样（保底只是把富余少算 12，
        而算式里保底又加回来了）—— 所以这一档不会让宽裕的批次少拿。
        """
        quota = FamilyQuota(
            requests_pool=200, rounds_pool=50, requests_nominal=48, rounds_nominal=10
        )
        assert quota.shard_caps(3)[0] == 54, "不加保底：名义 48 + 富余 6"
        assert quota.shard_caps(3, mandatory_floor=12)[0] == 60

    def test_a_shard_without_a_mandatory_list_is_untouched(self):
        """保底为 0（名义额够读 / 没接线）时，算式与加这一档之前**逐字相同**。"""
        quota = FamilyQuota(
            requests_pool=240, rounds_pool=50, requests_nominal=48, rounds_nominal=10
        )
        assert quota.shard_caps(3, mandatory_floor=0) == quota.shard_caps(3)
        assert quota.shard_caps(3, mandatory_floor=0, later_mandatory_floor=0) == (94, 18)

    def test_the_floor_of_the_later_shards_is_held_back(self):
        """后面几片的保底先从富余里扣掉 —— 否则它们的那一份会被当前这一片花掉。"""
        quota = FamilyQuota(
            requests_pool=240, rounds_pool=50, requests_nominal=48, rounds_nominal=10
        )
        assert quota.shard_caps(3)[0] == 94, "富余应当是 46（240−48−3×48−2）"
        assert quota.shard_caps(3, later_mandatory_floor=36)[0] == 58

    def test_no_shard_is_starved_below_its_own_mandatory_list(self):
        """四片各 60 个必读文件、池 260（恰好够 4×60 + 汇总下限 2）。

        不加保底时这一串会这样走：S1 名义 48 + 富余 66 = 114 全花掉 → 后面每片只剩名义额
        48 → **最后一片拿 48，读不完它的 60 个文件**。保底把「后面几片各自要读多少」提前
        扣下，于是每片都拿得到自己那一份（实测 run 58 的 20 个补偿项正是这样漏掉的）。
        """
        quota = FamilyQuota(
            requests_pool=260, rounds_pool=50, requests_nominal=48, rounds_nominal=10
        )
        caps = []
        for position in range(4):
            members_after = 4 - (position + 1)
            floor = 12  # 每片 60 个文件 − 名义额 48
            cap, _ = quota.shard_caps(
                members_after,
                mandatory_floor=floor,
                later_mandatory_floor=members_after * floor,
            )
            caps.append(cap)
            quota.spend(cap, 10)
        assert caps == [78, 60, 60, 60], caps
        assert all(cap >= 60 for cap in caps), f"有分片读不完必读清单：{caps}"
