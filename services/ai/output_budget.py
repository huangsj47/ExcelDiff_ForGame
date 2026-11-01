"""One source for output limits and the bounded recovery after a length stop.

Recovery raises only the platform default, once. It reserves the full replayed
prompt without assuming another cache hit; hidden reasoning shares the output
allowance, so no content/issue-count limit is used as a substitute.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from services.ai.pricing import equivalent_tokens

DEFAULT_OUTPUT_TOKENS = 65536
RECOVERY_OUTPUT_TOKENS = 131072
MAX_OUTPUT_RECOVERIES = 1


def output_cap_for(output_tokens: int | None) -> int:
    """Nominal plan cap; None retains the default conservative reservation."""
    return int(output_tokens) if output_tokens and int(output_tokens) > 0 else DEFAULT_OUTPUT_TOKENS


@dataclass(frozen=True)
class OutputRecoveryDecision:
    cap: int | None = None
    reason: str = ""
    estimated_call_tokens: int = 0


def suggest_output_recovery(
    *,
    usage: Mapping[str, Any],
    configured_cap: int | None,
    explicit_cap: bool = False,
    recovery_attempts: int = 0,
    budget: Any = None,
    prompt_chars: int = 0,
    known_gateway_cap: int | None = None,
    known_model_cap: int | None = None,
    cap_supported: bool = True,
    finalizing: bool = False,
) -> OutputRecoveryDecision:
    """Suggest a larger cap, or explain why another replay is not useful/safe.

    ``usage`` is the call already charged to ``budget``. ``prompt_chars`` is
    the complete next prompt, including history and correction instructions.
    The caller still runs its normal pre-call budget gate. Explicit user caps
    (even 8192), omitted wire caps and known gateway limits are never bypassed.
    """
    if str(usage.get("finish_reason") or "").strip().lower() != "length":
        return OutputRecoveryDecision(reason="output_not_truncated")
    if explicit_cap or configured_cap != DEFAULT_OUTPUT_TOKENS:
        return OutputRecoveryDecision(reason="explicit_output_limit")
    if recovery_attempts >= MAX_OUTPUT_RECOVERIES:
        return OutputRecoveryDecision(reason="output_recovery_exhausted")
    if not cap_supported or usage.get("max_tokens_adjustment") == "unsupported":
        return OutputRecoveryDecision(reason="gateway_output_limit_unsupported")
    current = usage.get("effective_max_tokens", configured_cap)
    if current is None or int(current) <= 0:
        return OutputRecoveryDecision(reason="wire_output_limit_unknown")
    current = int(current)
    cap = RECOVERY_OUTPUT_TOKENS
    gateway_limits = [int(value) for value in
                      (known_model_cap, known_gateway_cap, usage.get("gateway_max_tokens"))
                      if value is not None and int(value) > 0]
    if gateway_limits:
        cap = min(cap, *gateway_limits)
    # A previous call's measured token count can exceed our character estimate.
    # Never reserve less than either, and never speculate on another cache hit.
    prompt = max(0, int(prompt_chars), int(usage.get("prompt_tokens") or 0))
    weights = getattr(budget, "weights", None)
    total = max(0, int(getattr(budget, "total_token_budget", 0) or 0))
    if total:
        reserve = 0 if finalizing else max(0, int(getattr(budget, "report_reserve_tokens", 0) or 0))
        remaining = total - max(0, int(getattr(budget, "spent_tokens", 0) or 0)) - reserve
        output_weight = weights.output if weights is not None else 1.0
        if remaining < prompt or not math.isfinite(output_weight) or output_weight < 0:
            return OutputRecoveryDecision(reason="output_recovery_budget_exhausted")
        if output_weight > 0:
            cap = min(cap, int((remaining - prompt) // output_weight))
    if cap <= current:
        return OutputRecoveryDecision(reason="no_larger_affordable_output_limit")
    estimated = equivalent_tokens(input_tokens=prompt, output_tokens=cap, weights=weights)
    return OutputRecoveryDecision(cap=cap, reason="length_stop_default_cap_recovery",
                                  estimated_call_tokens=estimated)


def cap_for_model_window(configured_cap, declared_window):
    """Keep the declared input/output window usable; unknown windows do not invent a limit."""
    from services.ai.budget import COMPACT_AT_RATIO, resolve_context_window

    window, assumed = resolve_context_window(declared_window)
    if configured_cap is None or assumed:
        return configured_cap, ""
    available = max(1, int(window * (1 - COMPACT_AT_RATIO)))
    effective = min(int(configured_cap), available)
    note = (f"单次输出上限按端点声明的 {window:,} token 窗口从 {configured_cap:,} "
            f"调整至 {effective:,} tokens，为提示词保留空间。") if effective < configured_cap else ""
    return effective, note


def model_output_ceiling(declared_window):
    from services.ai.budget import COMPACT_AT_RATIO, resolve_context_window

    window, assumed = resolve_context_window(declared_window)
    return None if assumed else max(1, int(window * (1 - COMPACT_AT_RATIO)))


def respect_frozen_default(limits, plan):
    """Queued old plans retain their measured cap/reserves; new jobs use the new default."""
    from dataclasses import replace

    from services.ai.auto_sizing import PLAN_VERSION

    frozen = int(plan.thresholds.get("output_tokens") or 0)
    if plan.plan_version == PLAN_VERSION or limits.max_output_tokens_explicit or not frozen:
        return limits
    if limits.max_output_tokens is None:
        return limits
    return replace(limits, max_output_tokens=min(limits.max_output_tokens, frozen),
                   max_output_tokens_explicit=True)
