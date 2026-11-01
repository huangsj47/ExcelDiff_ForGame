"""Output recovery stays bounded and accounts for the whole replayed prompt."""
from types import SimpleNamespace

import pytest

from services.ai.budget_gate import SingleRunBudget
from services.ai.pricing import BudgetWeights


def recover(**overrides):
    from services.ai.output_budget import suggest_output_recovery
    args = dict(usage={"finish_reason": "length", "effective_max_tokens": 8192},
                configured_cap=8192, prompt_chars=1000)
    args.update(overrides)
    return suggest_output_recovery(**args)


def test_default_truncation_gets_one_larger_recovery_without_changing_global_default():
    from services.ai.auto_sizing import DEFAULT_OUTPUT_TOKENS
    decision = recover()
    assert decision.cap == 16384
    assert DEFAULT_OUTPUT_TOKENS == 8192
    assert decision.estimated_call_tokens == 17384
    assert recover(recovery_attempts=1).cap is None


@pytest.mark.parametrize("kwargs", [
    {"explicit_cap": True}, {"configured_cap": 4096}, {"configured_cap": None},
    {"cap_supported": False}, {"usage": {"finish_reason": "stop"}},
    {"usage": {"finish_reason": "length", "effective_max_tokens": None,
               "max_tokens_adjustment": "unsupported"}},
    {"known_gateway_cap": 8192},
])
def test_recovery_never_overrides_user_or_gateway_constraints(kwargs):
    assert recover(**kwargs).cap is None


def test_recovery_reserves_full_prompt_and_report_using_actual_usage_if_larger():
    budget = SingleRunBudget(total_token_budget=300000, spent_tokens=80000,
                             report_reserve_tokens=10000)
    usage = {"finish_reason": "length", "effective_max_tokens": 8192,
             "prompt_tokens": 199000, "reasoning_tokens": 8192}
    decision = recover(usage=usage, budget=budget, prompt_chars=1000)
    assert decision.cap == 11000
    assert decision.estimated_call_tokens == 210000
    budget.spent_tokens += 4000
    assert recover(usage=usage, budget=budget).cap is None


def test_recovery_respects_weighted_output_and_known_gateway_cap():
    budget = SingleRunBudget(total_token_budget=50000, weights=BudgetWeights(output=3))
    decision = recover(budget=budget, prompt_chars=2000, known_gateway_cap=12000)
    assert decision.cap == 12000
    assert decision.estimated_call_tokens == 38000


def test_missing_completion_usage_uses_actual_wire_cap():
    from services.ai.model_call import tokens_for_budget
    usage = {"prompt_tokens": 100, "completion_tokens": None,
             "effective_max_tokens": 16384}
    tokens, estimated = tokens_for_budget(usage, 0, 0, "ok",
                                          SimpleNamespace(max_output_tokens=8192))
    assert (tokens, estimated) == (16484, True)


def test_output_cap_usage_is_preserved_for_round_accounting():
    from services.ai.llm_client import ChatResult
    from services.ai.model_call import usage_of
    result = ChatResult(text="ok", requested_max_tokens=16384, effective_max_tokens=4096,
                        gateway_max_tokens=4096, max_tokens_adjustment="gateway_limit")
    usage = usage_of(result)
    assert usage["requested_max_tokens"] == 16384
    assert usage["effective_max_tokens"] == usage["gateway_max_tokens"] == 4096
    assert usage["max_tokens_adjustment"] == "gateway_limit"
