"""把用户配置转换成一次运行可见、可审计的预算计划。"""
from __future__ import annotations

from typing import Mapping

from services.ai.context_tools import DEFAULT_TOOL_LIMITS


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def derive_tool_limits(*, prompt_char_budget: int, max_tool_requests: int) -> dict[str, int]:
    """按有效提示词预算推导单次取数上限，避免总预算变大但单条永远卡 11k。"""
    budget = max(0, int(prompt_char_budget))
    requests = max(1, int(max_tool_requests))
    share = int((budget * 0.65) / max(4, min(requests, 12)))
    content = _clamp(share, 11_000, 32_000)
    return {
        "commit_detail": _clamp(share, 6_000, 20_000),
        "file_diff": content,
        "file_content": content,
        "read_reference": content,
        "find_references": _clamp(share, 8_000, 20_000),
    }


def build_budget_plan(
    *,
    configured_prompt_chars: int,
    effective_prompt_chars: int,
    platform_chars: int,
    max_rounds: int,
    max_tool_requests: int,
    shard_count: int = 1,
    verify: bool = False,
    tool_limits: Mapping[str, int] | None = None,
    window_note: str = "",
) -> dict:
    """返回可直接落库/下发 UI 的预算事实，不做费用预测。"""
    shards = max(1, int(shard_count))
    family = shards > 1
    roles = shards + 1 + (1 if verify else 0) if family else 1
    limits = dict(tool_limits or DEFAULT_TOOL_LIMITS)
    configured = max(0, int(configured_prompt_chars))
    effective = max(0, int(effective_prompt_chars))
    return {
        "prompt_chars": {
            "configured": configured,
            "effective": effective,
            "platform_overhead": max(0, int(platform_chars)),
            "clamped": effective < configured,
            "reason": str(window_note or ""),
        },
        "per_role": {
            "max_rounds": max(0, int(max_rounds)),
            "max_tool_requests": max(0, int(max_tool_requests)),
        },
        "roles": {
            "count": roles,
            "shards": shards if family else 0,
            "synthesis": family,
            "verify": bool(verify and family),
        },
        "job_theoretical_max": {
            "rounds": roles * max(0, int(max_rounds)),
            "tool_requests": roles * max(0, int(max_tool_requests)),
        },
        "tool_limits": {key: max(0, int(value)) for key, value in limits.items()},
    }
