"""把用户配置转换成一次运行可见、可审计的预算计划。"""
from __future__ import annotations

from typing import Mapping

from services.ai.context_tools import DEFAULT_TOOL_LIMITS
from utils.content_window import CONTENT_MAX_CHARS

#: `file_content` 那一档「取数侧真正会给多少字」的键名。
#:
#: **这个键是必须的，不是装饰。** 正文在**取数侧**就按 `CONTENT_MAX_CHARS` 切好了
#: （`platform_provider` / `agent_file_content_reader` / `agent_file_content_dispatch`
#: 三处都读这个常量），所以 `file_content` 在加权预算下算出来的 30,333 是**给不到的** ——
#: 只报那个数，「单条上限」这一行就是一句谎话（用户按它去理解「为什么还是只有一万字」，
#: 永远找不到答案）。`context_tools.DEFAULT_TOOL_LIMITS` 上方那段注释解释过同一件事。
PROVIDER_MAX_CHARS_KEY = "file_content_provider_max_chars"


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def derive_tool_limits(
    *,
    prompt_char_budget: int,
    max_tool_requests: int,
    remaining_chars: int | None = None,
    remaining_requests: int | None = None,
) -> dict[str, int]:
    """按有效提示词预算推导单次取数上限，避免总预算变大但单条永远卡 11k。

    ## 两种口径：总预算（起跑前）与剩余（跑到一半）

    不给 `remaining_*` 时按**总预算**算 —— 那是起跑前的口径（计划、预估端点都用它）。
    给了就按**剩余**算：跑到第 5 轮时前面几轮花掉的额度不该再被算一遍，否则「剩余还
    很宽」的判断永远是错的（按总量算出来的单条上限在预算快耗尽时仍然那么大）。

    判据是「给没给」，不是「剩余是不是 0」：剩余真的为 0 时按 0 算出来的额度由下面的
    下限兜住 —— 静默退回总预算会让「这一轮已经没有余地了」这件事消失。

    ## `file_content` 的谎言与那个额外的键

    返回的 `file_content` 是**加权预算**允许的上限，而正文在取数侧硬夹在
    `CONTENT_MAX_CHARS`：大于它的那一部分永远不生效。所以这里额外给出
    `file_content_provider_max_chars`（见 `PROVIDER_MAX_CHARS_KEY`）—— 计划要能回答
    「模型实际最长得看到多少字」，而不只是「我们允许了多少」。

    `file_diff` / `read_reference` 不欠这个账：它们的正文由平台自己拼装，
    取数侧的 11,000 只是**旧的默认额度**，不是硬上限。
    """
    budget = max(
        0, int(remaining_chars if remaining_chars is not None else prompt_char_budget)
    )
    requests = max(
        1, int(remaining_requests if remaining_requests is not None else max_tool_requests)
    )
    share = int((budget * 0.65) / max(4, min(requests, 12)))
    content = _clamp(share, 11_000, 32_000)
    return {
        "commit_detail": _clamp(share, 6_000, 20_000),
        "file_diff": content,
        "file_content": content,
        "read_reference": content,
        "find_references": _clamp(share, 8_000, 20_000),
        PROVIDER_MAX_CHARS_KEY: min(content, CONTENT_MAX_CHARS),
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
    window_source: str = "",
    reserved_output_chars: int = 0,
) -> dict:
    """返回可直接落库/下发 UI 的预算事实，不做费用预测。

    `window_source` / `reserved_output_chars` 是 E3 要的两项：

    * **窗口来源**回答「生效预算这个数是哪来的」——「端点未声明、按默认值」与
      「端点声明了窗口」是两件事，计划里不写清就会被读成「平台问到了端点」；
    * **保留输出空间**回答「水位为什么只到 60%」——那 40% 是留给模型回复与估算误差的，
      它不是一个被丢掉的名额（`budget.COMPACT_AT_RATIO` 的注释）。

    两者默认空 / 0：老调用方（还没接这两项的那些）拿到的是「未说明」，而不是一个
    编出来的默认值 —— 「不知道」与「按默认窗口算的」在界面上是两句话。
    """
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
            "window_source": str(window_source or ""),
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
        "reserved_output": {"chars": max(0, int(reserved_output_chars))},
    }
