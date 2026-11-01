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

DEFAULT_OUTPUT_TOKENS = 8192
RECOVERY_OUTPUT_TOKENS = 16384
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
                      (known_gateway_cap, usage.get("gateway_max_tokens"))
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
