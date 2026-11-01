"""Quality-first output allowance is independent from context and run ceilings."""
from services.ai.engine import EngineLimits
from services.ai.output_budget import DEFAULT_OUTPUT_TOKENS


def test_default_allows_eight_times_previous_output_without_changing_explicit_limits():
    assert DEFAULT_OUTPUT_TOKENS == 65536
    assert EngineLimits().max_output_tokens == 65536
    assert EngineLimits.from_config({"max_output_tokens": 4096}).max_output_tokens == 4096


def test_plan_reserves_the_same_larger_cap_as_real_calls():
    from services.ai.auto_sizing import plan_analysis
    plan = plan_analysis({"file_count": 1, "total_chars": 1000}, 600000, subagent_enabled=False)
    assert plan.thresholds["output_tokens"] == 65536
    assert plan.round_reserve_tokens >= 65536


def test_small_declared_model_window_leaves_room_for_the_prompt():
    from services.ai_analysis_service import _apply_model_window
    class Client:
        def model_contexts(self):
            return {"small": 32768}
    limits, note = _apply_model_window(Client(), {"api_model": "small"}, EngineLimits())
    assert limits.max_output_tokens < 32768
    assert limits.max_output_tokens > 8192
    assert "输出" in note


def test_length_recovery_remains_inside_declared_model_window():
    from services.ai.llm_client import ChatResult
    from services.ai_analysis_service import _apply_model_window
    from tests.test_ai_engine import ScriptedClient, _final, _run
    class Client(ScriptedClient):
        def model_contexts(self):
            return {"medium": 200000}
        def complete(self, messages, *, temperature=None, max_tokens=DEFAULT_OUTPUT_TOKENS):
            self.calls.append(max_tokens)
            return ChatResult(text="" if len(self.calls) == 1 else _final(), model="medium",
                              finish_reason="length" if len(self.calls) == 1 else "stop",
                              effective_max_tokens=max_tokens, prompt_tokens=100,
                              completion_tokens=10)
    client = Client()
    limits, _ = _apply_model_window(client, {"api_model": "medium"}, EngineLimits(max_rounds=3))
    outcome = _run(client, limits=limits)
    assert outcome.status == "succeeded"
    assert client.calls == [65536, 80000]


def test_queued_old_plan_retains_frozen_cap_and_budget_instead_of_silently_expanding():
    from types import SimpleNamespace

    from services.ai.output_budget import respect_frozen_default
    old = SimpleNamespace(plan_version="2026-11-01.2", thresholds={"output_tokens": 8192},
                          total_token_budget=100000, round_reserve_tokens=20000)
    limits = respect_frozen_default(EngineLimits(), old)
    assert limits.max_output_tokens == 8192
    assert limits.max_output_tokens_explicit
    assert old.total_token_budget == 100000 and old.round_reserve_tokens == 20000
