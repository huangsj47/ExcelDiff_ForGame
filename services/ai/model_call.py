# -*- coding: utf-8 -*-
"""**一次模型调用**周边的三件小事：发什么、算不算被截断、这次调用往账上记多少。

## 为什么单独一个模块

`services/ai/engine.py` 顶着仓库 2000 行的 ERROR 闸门（`scripts/check_file_length.py`），
而下面这三个函数与「编排一轮怎么跑」无关 —— 它们只管**与端点交界的那一层**：
请求参数（`complete_kwargs`）、上游那句「你写超了」的读法（`output_limit_hit`）、
以及一次调用的账（`tokens_for_budget`）。搬出来之后引擎那边只剩调用点。

（同样的理由搬过几次：`round_message`、`round_hints`、`mid_round` 都是这么来的。
边界画法一样：**能独立说清一件事的整块才搬**，不为凑行数把一个函数切成两半。）

## `usage_of` 是**唯一**的读法

`ChatResult` 上那十几个字段只在 `usage_of` 里读一次，其余地方按名字取 —— 分散在三处读
`getattr(result, ...)`，漏掉哪一处都是「这一轮看着没花钱」。

## `tokens_for_budget` 与 `SingleRunBudget.note_usage` 的分工

这里算的是「这一个数是多少」，`budget_gate.SingleRunBudget` 负责把它加到账上并判定
「还付得起吗」。两处都不重写对方的逻辑 —— 换算口径只有这一份。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

from services.ai.auto_sizing import DEFAULT_OUTPUT_TOKENS, conservative_tokens_for
from services.ai.budget import estimate_chars
from services.ai.llm_client import non_negative_int
from services.ai.pricing import BudgetWeights, equivalent_tokens

if TYPE_CHECKING:  # 只为标注：engine 在模块级导入本模块，这里不能反向导入。
    from services.ai.engine import EngineLimits

__all__ = ["complete_kwargs", "output_limit_hit", "tokens_for_budget", "usage_of"]




def tokens_for_budget(
    usage: Mapping[str, Any],
    user_message_chars: int,
    history_chars: int,
    text: str,
    limits: EngineLimits,
    weights: "BudgetWeights | None" = None,
) -> tuple[int, bool]:
    """这一次调用往单次上限的账上记多少，以及这个数**是不是估算出来的**。

    上游两项都报了就是精确值。缺哪一项补哪一项的保守估算，并把整笔记成「估算」——
    那两条纪律与 `auto_sizing.conservative_member_tokens` 逐字同源：

    * **未知不许当 0** —— 那等于无限放行，单次上限永远判「还没超」；
    * **也不许当真值** —— `estimated` 这个标记会被 `single_run_guard` 带进那句 `reason`
      里（「其中含保守估算的部分」），报告的措辞跟着它走。

    提示词那半按**这一轮实际发出去的那一份**估（历史 + 本轮 user 消息，与 `prompt_chars`
    是同一处口径）；输出那半优先用配置的输出上限 —— 它是我们能承诺的最大值。

    ## `weights`：这一笔记的是**折算后的等效 token**

    有价格表时，缓存命中的输入与输出按配置的单价折算成「未命中输入等价 token」
    （`pricing.BudgetWeights`；没配就是三档各 1.0 = 旧口径）。**算术在 `pricing` 里**
    （`equivalent_tokens`）—— 那一份与费用面板同源，两处不可能算出两种折扣。
    """
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if prompt is not None and completion is not None:
        return (
            equivalent_tokens(
                input_tokens=prompt,
                output_tokens=completion,
                cache_read=usage.get("cache_read_tokens"),
                weights=weights,
            ),
            False,
        )
    prompt_est = history_chars + user_message_chars if prompt is None else max(0, int(prompt))
    if completion is None:
        # 没配输出上限时退到这一轮实际写回来的正文长度 —— 那是**下界**（推理 token 不算在
        # 里面），但比当 0 强；这一档本来就是「别把它当 0」。
        completion_est = usage.get("effective_max_tokens") or limits.max_output_tokens or estimate_chars(
            [{"role": "assistant", "content": text}]
        )
    else:
        completion_est = max(0, int(completion))
    return conservative_tokens_for(prompt_est, completion_est, weights), True


def complete_kwargs(limits: EngineLimits) -> dict[str, Any]:
    """发给 `client.complete` 的那些可选参数。

    ## `max_tokens`：默认值由**客户端**补，这里只送「与默认不同」的那两档

    `EngineLimits.max_output_tokens` 的默认值是 `DEFAULT_OUTPUT_TOKENS`（8192），而
    `LLMClient.complete` 用**哨兵**区分「没传」与「显式 None」：没传 → 按同一个常量
    在 **wire 层补齐** `max_tokens`；显式 `None` → 不传字段。于是引擎默认轮不必多传一个
    与默认值相同的字段（否则每一个测试假 client 都要跟着改签名），而真实请求体照样带
    `max_tokens=8192`。这一层只负责把**用户的决定**送出去：

    * **显式 `None`** → 送 `max_tokens=None`：客户端据此**不传字段**（旧行为，逃生口）。
      不送的话客户端会按默认值封顶，「显式关掉」就成了做不到的事；
    * **与默认不同的整数** → 原样送出去（项目配了 4096 就按 4096）；
    * **等于默认值** → 不送（客户端补同一个数）。

    集中一处是因为调用点有三个（正常轮、压小重发、收尾），而漏掉任何一处都表现为
    「这个参数只在某些轮生效」——那种不一致查起来最费劲。
    """
    kwargs: dict[str, Any] = {"temperature": limits.temperature}
    cap = limits.max_output_tokens
    if cap is None:
        kwargs["max_tokens"] = None
    elif cap != DEFAULT_OUTPUT_TOKENS:
        kwargs["max_tokens"] = cap
    return kwargs


def output_limit_hit(usage: Mapping[str, Any]) -> bool:
    """上游是否明说「这次输出被长度上限截断了」。

    `finish_reason == "length"` 是上游给的**直接证据**，比我们自己按括号配平猜形状准 ——
    断点恰好落在一个仍然合法的 JSON 上时（`requests` 数组已闭合、后面的字段整段没写），
    `json.loads` 会成功、括号也配平，只有这句话能识破它。
    """
    return str(usage.get("finish_reason") or "").strip().lower() == "length"


def usage_of(result: Any) -> dict[str, Any]:
    """把一次模型调用的用量读出来（字段口径见 `llm_client.ChatResult`）。

    集中一处是为了让「重试之后那一次调用」与正常路径用同一套读法 —— 分散在三处读
    `getattr(result, ...)`，漏掉哪一处都是「这一轮看着没花钱」。
    """
    return {
        "text": str(getattr(result, "text", "") or ""),
        # 上游没报就是 `None`（不是 0）—— 口径与缓存那两个字段一致，见 `_sum_optional`。
        "prompt_tokens": non_negative_int(getattr(result, "prompt_tokens", None)),
        "completion_tokens": non_negative_int(getattr(result, "completion_tokens", None)),
        "cache_read_tokens": getattr(result, "cache_read_tokens", None),
        "cache_write_tokens": getattr(result, "cache_write_tokens", None),
        "cache_source": str(getattr(result, "cache_source", "") or ""),
        "finish_reason": str(getattr(result, "finish_reason", "") or ""),
        # 输出里有多少是隐藏推理，以及这个数是从哪个字段读到的（见
        # `llm_client._extract_reasoning_usage`）。**上游没报就是 `None`**，不是 0。
        "reasoning_tokens": non_negative_int(getattr(result, "reasoning_tokens", None)),
        "reasoning_source": str(getattr(result, "reasoning_source", "") or ""),
        "requested_max_tokens": non_negative_int(getattr(result, "requested_max_tokens", None)),
        "effective_max_tokens": non_negative_int(getattr(result, "effective_max_tokens", None)),
        "gateway_max_tokens": non_negative_int(getattr(result, "gateway_max_tokens", None)),
        "max_tokens_adjustment": str(getattr(result, "max_tokens_adjustment", "") or ""),
    }
