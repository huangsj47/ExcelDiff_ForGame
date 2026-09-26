# -*- coding: utf-8 -*-
"""**记账口径按单价折算**：缓存命中与输出两档不再按未命中输入的原价记账。

## 这一组盯的是哪一种假绿

「折算过了」这句断言本身没有内容 —— 一个把倍率乘错的实现照样能让 `spent_tokens` 变个
数。所以判据落在**能与钱对上**上：折算出来的等效 token 数，必须等于费用面板按同一张
单价表算出的钱除以未命中单价（`pricing.estimate_cost`）。

真机数字（项目 1 的 run 3）就是这条判据的样本：账上 1,306,756，而实际只花了 ¥1.19
（命中率 84.6%）—— 按 2 元/M 折回来是 595k，两者的差正是缓存命中那 1,041,792 的折扣。

## 反向的一半同样要有

**没有价格表时口径必须逐字回到旧样子**（未命中 + 输出之和）。这条不钉住的话，一个
「无论如何都乘 0.5」的实现也能让上面那条通过，而那会把每一次运行的账面都改小一半。
"""
from __future__ import annotations

import dataclasses

from services.ai.auto_sizing import plan_analysis, single_run_guard
from services.ai.budget_gate import SingleRunBudget
from services.ai.engine import STATUS_SUCCEEDED, run_analysis
from services.ai.llm_client import ChatResult
from services.ai.pricing import (
    BudgetWeights,
    budget_weights,
    budget_weights_from_config,
    equivalent_tokens,
    estimate_cost,
    parse_price_table,
)
from services.ai.rules import RuleThresholds
from services.ai.subagent import _tokens_of
from tests.test_ai_analysis_plan import DIMENSIONS, _config_three
from tests.test_ai_engine import COMMIT, TABLE, FakeProvider, _final, _loaded, _requests, _scope
from tests.test_ai_subagent_family import CHANGE_SUMMARY

#: 一张「命中 1/10、输出 4 倍」的表（与真机那个网关同形）。
TABLE_JSON = (
    '{"version": "2026-09", "models": {"m-*": '
    '{"input": 2.0, "output": 8.0, "cache_read": 0.2}}}'
)

#: run 3 的真实用量（`GET /ai-analysis/runs/3/usage`）。
RUN3_INPUT, RUN3_OUTPUT, RUN3_CACHE_READ = 1_231_505, 75_251, 1_041_792
#: 那次运行的账按新口径应落在哪（未命中 189,713×1 + 缓存 1,041,792×0.1 + 输出 75,251×4）。
RUN3_EQUIVALENT = 594_896


def _weights(model: str = "m-flash") -> BudgetWeights:
    table, errors = parse_price_table(TABLE_JSON)
    assert table is not None and not errors, errors
    weights = budget_weights(model, table)
    assert weights is not None
    return weights


def _plan_with_weights(weights: BudgetWeights):
    """一份**真的由规划器算出来**的计划，外加那一份倍率（模拟落库再读回来）。"""
    plan = plan_analysis(_config_three(), 580_000, DIMENSIONS)
    return dataclasses.replace(
        plan, thresholds={**plan.thresholds, "budget_weights": weights.to_dict()}
    )


class UsageClient:
    """按脚本回答，并报出调用方指定的那一份用量（含缓存命中数）。"""

    def __init__(self, *replies: str, usage: dict | None = None):
        self._replies = list(replies)
        self._usage = dict(usage or {})
        self.calls: list[list[dict]] = []

    def complete(self, messages, **kwargs):
        self.calls.append([dict(item) for item in messages])
        index = min(len(self.calls) - 1, len(self._replies) - 1)
        return ChatResult(text=self._replies[index], model="m-flash", **self._usage)


def _run(client, *, single_run_budget=None, **overrides):
    kwargs = {
        "client": client,
        "provider": FakeProvider(),
        "loaded": _loaded(),
        "scope": _scope(),
        "change_summary": CHANGE_SUMMARY,
        "thresholds": RuleThresholds(),
    }
    kwargs.update(overrides)
    if single_run_budget is not None:
        kwargs["single_run_budget"] = single_run_budget
    return run_analysis(**kwargs)


# ==========================================================================
# 一、倍率从项目配置的单价表来（「1/10 就按 1/10，1/5 就按 1/5」）
# ==========================================================================


def test_the_weights_are_read_off_the_configured_prices():
    """倍率 = 各档单价 ÷ 未命中单价。**没有任何一个比例写在代码里。**"""
    weights = _weights()

    assert weights.hit == 0.1
    assert weights.output == 4.0
    assert "2.00" in weights.note and "0.20" in weights.note  # 那句话里带着出处

    # 换一张表（命中 1/5、输出 2 倍）→ 倍率跟着变，代码一个字没动。
    other, _ = parse_price_table(
        '{"version": "v", "models": {"m-flash": {"input": 1.0, "output": 2.0, "cache_read": 0.2}}}'
    )
    changed = budget_weights("m-flash", other)
    assert changed is not None and changed.hit == 0.2 and changed.output == 2.0


def test_a_missing_cache_price_falls_back_to_the_miss_price():
    """没配命中价 → 命中那档按未命中价算（倍率 1.0），**不猜**一个折扣。

    与 `estimate_cost` 同一条口径（那里也会写一句「按未命中价计入，偏保守」）。
    猜一个折扣的代价是账面偏小 → 闸门放行一次本来该停的运行。
    """
    table, _ = parse_price_table('{"version": "v", "models": {"m-flash": {"input": 2.0, "output": 8.0}}}')

    weights = budget_weights("m-flash", table)

    assert weights is not None
    assert weights.hit == 1.0
    assert weights.output == 4.0


def test_no_usable_price_table_means_no_weights():
    """没有表 / 表里没有这个模型 / 数据坏了 → `None`（= 全 1.0 的旧口径）。"""
    assert budget_weights("m-flash", None) is None
    assert budget_weights_from_config({}) is None
    assert budget_weights_from_config({"api_model": "m-flash"}) is None  # 平台默认表是空的
    # 模型名对不上（`resolve_price` 不做「包含」匹配）→ 也不编一个倍率。
    assert budget_weights("whatever", parse_price_table(TABLE_JSON)[0]) is None
    # 落库那份数据坏了（倍率是字符串 / 负数）→ 退回 `None`，不抛。
    assert BudgetWeights.from_mapping({"hit": "abc"}) is None
    assert BudgetWeights.from_mapping({"hit": -1}) is None
    assert BudgetWeights.from_mapping(None) is None


# ==========================================================================
# 二、折算出来的数与**钱**对得上（唯一判据）
# ==========================================================================


def test_the_folded_figure_is_the_money_you_actually_spent():
    """**判据与费用面板同源**：等效 token = 费用 ÷ 未命中单价。

    这一条是这一组的地基：单看 `equivalent_tokens` 的返回值，一个乘错倍率的实现也能
    编出一个数；只有把钱折回来对上，才证明两处用的是同一份单价。
    """
    table, _ = parse_price_table(TABLE_JSON)
    weights = _weights()

    folded = equivalent_tokens(
        input_tokens=RUN3_INPUT,
        output_tokens=RUN3_OUTPUT,
        cache_read=RUN3_CACHE_READ,
        weights=weights,
    )
    cost = estimate_cost(
        "m-flash",
        tokens_input=RUN3_INPUT,
        tokens_output=RUN3_OUTPUT,
        cache_read=RUN3_CACHE_READ,
        table=table,
    )

    assert cost.amount is not None, cost.reason
    # ¥1.1897924 ÷ 2 元/M = 594,896.2 token —— 账上记整数，所以两边**除了不足一个 token
    # 的舍入**之外必须逐字相等。这是「两处同源」的判据：差一点点可以（舍入），差一个
    # 倍率不行。
    from decimal import Decimal

    assert int(round(cost.amount / Decimal("2.0") * Decimal(1_000_000))) == folded
    # run 3 的实际数字：账上 1,306,756，折算后是这个数。
    assert folded == RUN3_EQUIVALENT


def test_an_unreported_usage_is_estimated_with_the_output_weight():
    """**缺用量那一档也要折算**，而且两头都要对：

    * 提示词那一半**不许猜命中** —— 缺用量时连有没有命中都不知道，按未命中算（权重 1.0）；
    * 输出那一半用配置的输出上限 × 输出倍率 —— 只按 1.0 算会让这一档在缓存贵的模型上
      偏小得离谱。
    """
    from services.ai.auto_sizing import conservative_tokens_for

    weights = _weights()

    assert conservative_tokens_for(1_000, 100, weights) == 1_400  # 1,000×1 + 100×4
    assert conservative_tokens_for(1_000, 100, None) == 1_100  # 旧口径
    # 没配价格表时一个字都不许变（这是同一档的反向对照）。
    assert conservative_tokens_for(1_000, 100) == 1_100


def test_an_unreported_cache_count_is_charged_at_the_miss_price():
    """上游没报命中的那一次：**不许猜命中**（全按未命中算 = 偏保守）。"""
    weights = _weights()

    assert equivalent_tokens(input_tokens=1_000, output_tokens=0, cache_read=None, weights=weights) == 1_000
    # 命中数大于输入总数（上游自相矛盾）→ 按全部命中算，与 `estimate_cost` 同一条处理。
    assert equivalent_tokens(input_tokens=100, output_tokens=0, cache_read=999, weights=weights) == 10


# ==========================================================================
# 三、接线：计划上的倍率真的进了引擎的账
# ==========================================================================


def test_the_engine_books_the_folded_figure():
    """引擎按**折算后**的数记账，而不是上游报的输入 + 输出之和。"""
    weights = _weights()
    budget = SingleRunBudget.from_plan(_plan_with_weights(weights))
    client = UsageClient(
        _final(),
        usage={"prompt_tokens": 1_000, "completion_tokens": 100, "cache_read_tokens": 800},
    )

    outcome = _run(client, single_run_budget=budget)

    assert outcome.status == STATUS_SUCCEEDED
    # 未命中 200×1 + 命中 800×0.1 + 输出 100×4 = 680（不折算的话是 1,100）。
    assert budget.spent_tokens == 680, budget.spent_tokens
    assert budget.weighted is True
    # 落库那份用量仍然是**上游报的原值**（折算只影响预算这本账，不影响用量统计）。
    assert outcome.prompt_tokens == 1_000
    assert outcome.completion_tokens == 100


def test_without_a_price_table_the_arithmetic_is_the_old_one():
    """反向的一半：**没有价格表时口径逐字回到旧样子**（未命中 + 输出之和）。"""
    budget = SingleRunBudget.from_plan(plan_analysis(_config_three(), 580_000, DIMENSIONS))
    client = UsageClient(
        _final(),
        usage={"prompt_tokens": 1_000, "completion_tokens": 100, "cache_read_tokens": 800},
    )

    outcome = _run(client, single_run_budget=budget)

    assert outcome.status == STATUS_SUCCEEDED
    assert budget.spent_tokens == 1_100, budget.spent_tokens
    assert budget.weighted is False
    assert single_run_guard(budget, spent_tokens=budget.spent_tokens)["weighted"] is False


def test_the_family_prices_the_same_call_the_same_way():
    """**家族那本账与引擎那本账对同一次调用必须同价。**

    家族用它判「这个成员还跑得起吗」（`should_skip`），引擎用它判「下一轮还发不发」。
    两处各算一套的话，会出现「成员被判跑得起、进去就被拦下」—— 而它不报错，只表现为
    额度忽然不够用。
    """
    weights = _weights()
    budget = SingleRunBudget.from_plan(_plan_with_weights(weights))
    client = UsageClient(
        _final(),
        usage={"prompt_tokens": 1_000, "completion_tokens": 100, "cache_read_tokens": 800},
    )

    outcome = _run(client, single_run_budget=budget)
    family_side = _tokens_of(outcome, output_cap=0, weights=weights)

    assert family_side == budget.spent_tokens == 680
    # 不给倍率时它退回旧口径 —— 判据确实来自那颗参数，不是巧合。
    assert _tokens_of(outcome, output_cap=0) == 1_100


def test_the_plan_round_trips_the_weights_to_the_reader():
    """倍率要能**落库再读回来**：运行侧读的是 `request_payload` 里那份计划。"""
    from services.ai.auto_sizing import AnalysisPlan

    weights = _weights()
    plan = _plan_with_weights(weights)

    restored = AnalysisPlan.from_dict(plan.to_dict())

    assert restored is not None
    assert restored.weights is not None
    assert restored.weights.hit == 0.1 and restored.weights.output == 4.0
    # 而它真的被账本读走了（不是只挂在计划上好看）。
    assert SingleRunBudget.from_plan(restored).weights == weights


def test_the_service_hands_the_weights_from_the_config_to_the_payload():
    """**接线**：项目配置 → 计划（`thresholds.budget_weights`）→ payload 里那份计划。

    「倍率算得对」与「它被挂到计划上了」是两件事：只测前者的话，一个把结果算完就扔掉的
    实现照样全绿（`services/ai_analysis_service` 那一步就是这么接的）。
    """
    from services.ai.analysis_plan import attach_plan

    payload = {"group": {"project_id": 1}}
    plan = attach_plan(
        payload,
        effective_budget={"user_chars": 580_000},
        dimensions=DIMENSIONS,
        budget_weights=budget_weights_from_config(
            {"api_model": "m-flash", "model_price_table": TABLE_JSON}
        ),
    )

    assert plan.thresholds["budget_weights"]["hit"] == 0.1
    assert payload["plan"]["thresholds"]["budget_weights"]["output"] == 4.0
    # 界面上那句「单次上限」也要说清楚运行中按折算记 —— 否则撞顶时看着像上限没生效。
    assert "按单价折算" in payload["plan"]["estimate"]["formula"]

    # 反向：没配价格表时**一个键都不加**（老 payload 读回来也不会多出这一项）。
    bare = {"group": {"project_id": 1}}
    bare_plan = attach_plan(
        bare,
        effective_budget={"user_chars": 580_000},
        dimensions=DIMENSIONS,
        budget_weights=budget_weights_from_config({}),
    )
    assert "budget_weights" not in bare_plan.thresholds
    assert "按单价折算" not in bare_plan.estimate["formula"]


def test_the_halt_reason_says_the_figure_is_folded():
    """改了记账口径就要**显式写出来** —— 否则读的人会拿折算值去和原价上限对不上。"""
    weights = _weights()

    blocked = single_run_guard(_plan_with_weights(weights), spent_tokens=999_999_999)

    assert blocked["blocked"] is True
    assert blocked["weighted"] is True
    assert "按单价折算" in blocked["reason"]
    # 没配价格表时不许说这句话（那是旧口径）。
    plain = single_run_guard(plan_analysis(_config_three(), 580_000, DIMENSIONS), spent_tokens=999_999_999)
    assert plain["weighted"] is False
    assert "按单价折算" not in plain["reason"]
