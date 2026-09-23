# -*- coding: utf-8 -*-
"""`services/ai/auto_sizing.py`：数值一栏收敛后的「预算 → 推导」那一层。

## 为什么值得有这一组

配置面收敛（2026-09-23）之后，分片数 / 每片索取 / 每片轮次 / 取样上限 / 每片异常
全部由这个模块推导 —— 用户看得到（预算计划的「推导依据」一行），但改不了。
它的数字直接决定一次分析的模型调用次数与能否装进窗口，所以三条性质必须钉死：

1. **校准复现实测**：默认预算下必须反解出 runs 38~41 实测出的那一档（≈50 次索取、
   10 轮、5 片）。公式改坏了（比如除数写错一位），最先变的是这里；
2. **自洽**：池 = 片 × 名义额、名义额 ≥ 下限、「片 × 每片异常 ≥ 全次上限」——
   这些是 `_engine_limits` 与 rules 层各自假设过的不变量，任何一边单方面改都会
   静默失效；
3. **两处调用同源**：运行侧（ai_analysis_service）与预估端点（ai_usage_service）
   必须调**同一个函数**，否则预估说 50、实际跑 40，用户永远找不到原因
   （`:1483 一类口径 bug 的教训`）。
"""
from __future__ import annotations

import pytest

from services.ai.auto_sizing import (
    ANOMALY_RUN_CAP,
    ITEM_CHARS,
    REQUESTS_CEILING_PER_SHARD,
    REQUESTS_FLOOR_PER_SHARD,
    ROUNDS_PER_SHARD,
    SHARD_TARGET,
    derive_family_sizing,
    sampling_cap_for,
)
from services.ai.budget_plan import build_budget_plan

# ==========================================================================
# 一、校准：默认预算必须复现 runs 38~41 反解出的那一档
# ==========================================================================


def test_the_default_budget_reproduces_the_measured_tier():
    """580k 生效额度（1M 窗口水位 60% 扣平台内置后的用户侧）+ 上千个文件 ——
    run 41 的真实工况。反解结果必须落在实测出的那一档：每片 ≈50 次索取。"""
    sizing = derive_family_sizing(effective_user_chars=580_000, file_count=1199)

    # (580,000 − 36,500 清单 − 6,000 基线) // 11,000 = 48 —— 实测 S1 用 32、S2 用满 50，
    # 48 次正是「S2 不再双顶降级、S1 仍有富余」的那一档。
    assert sizing.requests_per_shard == (
        (580_000 - 500 * 73 - 6_000) // ITEM_CHARS
    )
    assert 44 <= sizing.requests_per_shard <= 50, (
        "默认预算反解出的每片索取偏离实测档（runs 38~41 校准 ≈ 50），公式被改坏了吗？"
    )
    assert sizing.rounds_per_shard == ROUNDS_PER_SHARD == 10
    assert sizing.shard_count == SHARD_TARGET == 5
    assert sizing.sampling_cap == 500, "1199 个文件应被取样上限夹到 500"


def test_small_budgets_floor_instead_of_starving():
    """预算配得再小，每片也有下限 8 —— 低于它连「清单分诊 + 抽查几条」都做不完。"""
    sizing = derive_family_sizing(effective_user_chars=1_000, file_count=10)
    assert sizing.requests_per_shard == REQUESTS_FLOOR_PER_SHARD == 8
    assert sizing.sampling_cap == 200, "文件数低于下限时按下限"


def test_absurd_budgets_ceiling_instead_of_exploding():
    """预算配得离谱大时封顶 60：每片一次索取约 11k 字，60 次已远超水位，
    更大的数字只是个没有意义的数。"""
    sizing = derive_family_sizing(effective_user_chars=10_000_000, file_count=100)
    assert sizing.requests_per_shard == REQUESTS_CEILING_PER_SHARD == 60


def test_fewer_dimensions_means_fewer_shards():
    """项目声明了 3 个维度时只能开 3 片 —— 「推导说 5 片、实际开 3 片」会让
    预算计划的账对不上（auto_sizing docstring 的口径）。"""
    sizing = derive_family_sizing(
        effective_user_chars=580_000, file_count=1199, dimension_count=3
    )
    assert sizing.shard_count == 3
    # 每片异常随之重配：ceil(20×2/3) = 14 被夹到上限 12。
    assert sizing.anomalies_per_subagent == 12


# ==========================================================================
# 二、自洽：三边各自假设过的不变量
# ==========================================================================


@pytest.mark.parametrize(
    ("chars", "files", "dimensions"),
    [
        (580_000, 1199, 9),
        (120_000, 30, 9),       # 窗口被压到很小的工况
        (10_000_000, 3, 9),     # 预算离谱大
        (580_000, 50, 2),       # 维度极少
        (0, 0, 9),              # 全零输入不许炸
    ],
)
def test_the_invariants_hold_across_regimes(chars, files, dimensions):
    sizing = derive_family_sizing(
        effective_user_chars=chars, file_count=files, dimension_count=dimensions
    )

    # 池 = 片 × 名义额（预算计划的 family_pool 节与任务书里的「全家共可索取」按它写）。
    assert sizing.family_requests_pool == sizing.shard_count * sizing.requests_per_shard
    assert sizing.family_rounds_pool == sizing.shard_count * sizing.rounds_per_shard
    # 名义额在上下限内 —— 下限防饿死、上限防「算出几百次」。
    assert REQUESTS_FLOOR_PER_SHARD <= sizing.requests_per_shard <= REQUESTS_CEILING_PER_SHARD
    # 「分片 × 每片 ≥ 全次上限」仍然成立（_engine_limits 的既有理由），×2 是给
    # 汇总去重/门槛滤掉的那部分留的余量。
    assert sizing.shard_count * sizing.anomalies_per_subagent >= ANOMALY_RUN_CAP
    # 片数夹在 2~6 且不超过维度数。
    assert 2 <= sizing.shard_count <= 6
    assert sizing.shard_count <= max(2, dimensions)
    # 推导依据要带着「为什么是这个数」的关键数字，预算计划把它原样摆给用户。
    for token in (str(sizing.shard_count), str(sizing.requests_per_shard)):
        assert token in sizing.note


def test_sampling_cap_for_is_a_pure_clamp():
    """它单独暴露是因为用点在预算压窗口**之前**（build_weekly_payload 选清单），
    同一个公式只许有一份 —— 两处各算会漂移。"""
    assert sampling_cap_for(50) == 200
    assert sampling_cap_for(300) == 300
    assert sampling_cap_for(9999) == 500
    assert sampling_cap_for(0) == 200


# ==========================================================================
# 三、budget_plan 的 family_pool 节：账要跟着池口径走
# ==========================================================================


def test_build_budget_plan_reports_the_family_pool_when_given():
    """给了 family_pool 时：job_theoretical_max 改按「池 + 汇总保底」算，
    family_pool 节原样入账 —— 否则界面按 roles × 名义额高估整次分析的
    理论上限（roles × 名义额根本不是串行池的语义）。"""
    sizing = derive_family_sizing(effective_user_chars=580_000, file_count=1199)
    plan = build_budget_plan(
        configured_prompt_chars=560_000,
        effective_prompt_chars=580_000,
        platform_chars=20_000,
        max_rounds=sizing.rounds_per_shard,
        max_tool_requests=sizing.requests_per_shard,
        shard_count=sizing.shard_count,
        verify=True,
        family_pool={
            "requests_pool": sizing.family_requests_pool,
            "rounds_pool": sizing.family_rounds_pool,
            "requests_nominal": sizing.requests_per_shard,
            "rounds_nominal": sizing.rounds_per_shard,
            "synthesis_floor_requests": 2,
            "synthesis_floor_rounds": 2,
            "note": sizing.note,
        },
    )

    pool = plan["family_pool"]
    assert pool is not None
    assert pool["requests_pool"] == sizing.family_requests_pool
    assert pool["requests_nominal"] == sizing.requests_per_shard
    assert "串行滚动" in pool["note"]
    # 理论上限 = 池 + 汇总保底（汇总可越池，见 subagent.FamilyQuota）。
    assert plan["job_theoretical_max"]["tool_requests"] == sizing.family_requests_pool + 2
    assert plan["job_theoretical_max"]["rounds"] == sizing.family_rounds_pool + 2


def test_build_budget_plan_without_a_pool_is_byte_unchanged():
    """老调用方（单代理 / 还没接池的路径）不传 family_pool 时行为与从前一致，
    family_pool 节是 None 而不是一个编出来的空账。"""
    plan = build_budget_plan(
        configured_prompt_chars=560_000,
        effective_prompt_chars=560_000,
        platform_chars=20_000,
        max_rounds=8,
        max_tool_requests=40,
        shard_count=1,
        verify=False,
    )
    assert plan["family_pool"] is None
    assert plan["job_theoretical_max"] == {"rounds": 8, "tool_requests": 40}


# ==========================================================================
# 四、同源：预估端点与运行侧必须用同一个推导
# ==========================================================================


def test_the_estimate_endpoint_and_the_runner_share_one_derivation():
    """预估说 50、实际跑 40 这种分叉不会报错，只会让「确认框里的预计代价」变成
    谎话 —— 两边必须 import 同一个函数（services/ai/auto_sizing.py），谁改成
    自算一份，这条就红。"""
    import services.ai_analysis_service as runner
    import services.ai_usage_service as estimator

    assert runner.derive_family_sizing is estimator.derive_family_sizing
