# -*- coding: utf-8 -*-
"""逐轮**可观测性**：这一轮的钱花在哪、为什么（工作包 D 的 P5）。

## 这一组字段回答什么

run 45/46 的性能归因里有一句**没被证过**的话：输出 token 涨了 3 倍。而逐轮账里当时
一个数都读不出来：

* `response_text` 是按 `TRACE_RESPONSE_MAX_CHARS` 截断后的原文，从它身上量不出「模型
  **可见正文**有多长」（正文里裹着 `think` 块与转义）；
* `completion_tokens` 里可能混着**推理 token**，而推理 token 不产生可见正文 ——
  两者不加区分，「输出 token 涨了」既可能是写得更长，也可能是想得更久，
  而这两种情况该调的东西完全相反（前者压输出格式，后者根本不是钱的问题）；
* 「这一轮为什么被重试/截断」只有一句自由文本，没法按原因统计。

所以这里给四个观测值：**可见正文长度**、**推理 token**、**工具重放字符**、
**截断与纠正原因**。

## 口径只有一条：**未上报存 `None`，不存 0**

0 是一个确定的观测值（「确实没有」），`None` 是「上游没报 / 读不出来」。把它们合并，
界面上就会出现一个确定的「推理 0 tokens」，而真相是这次调用根本没上报这个字段。
与 `llm_client.ChatResult` 的 `prompt_tokens` / `cache_*` 同一条纪律。

## 为什么它是一个独立模块

`services/ai/engine.py` 贴着 2000 行的 ERROR 闸门（`scripts/check_file_length.py`），
而这一组是**纯函数 + 一层合并**：把它们放在引擎里，引擎就再也装不下真正的编排改动。
引擎那边只留一个薄钩子 `_with_observability`（把「算不出来就返回 None」的兜底做在
`_live_round_entry` 上），实现与理由都在这里。
"""

from __future__ import annotations

from typing import Any

from services.ai.protocol import _THINK_BLOCK_RE

# 纠正原因的**短标签**。值进 `RoundRecord.correction_reason`；纠正的**成本**就是它占掉
# 的那一轮 —— 于是「这次分析为什么多花了两轮」可以按原因统计，而不是读一段自由文本。
CORRECTION_UNPARSABLE = "unparsable_json"
CORRECTION_TRUNCATED_OUTPUT = "truncated_output"
CORRECTION_CHANNEL_MISMATCH = "channel_mismatch"
# 解析**成功**、但历史清单没有逐条交代（`baseline_updates` 缺条目）。它占的那一轮是
# 「平台催模型把清单答完」的成本 —— 与上面三种「这一轮的答案没法用」不是一回事。
CORRECTION_BASELINE_COVERAGE = "baseline_coverage"


def visible_response_chars(text: Any) -> int | None:
    """这一轮模型**可见正文**的字符数（剥掉 `think` 块）。`None` = 没有响应文本。

    ## 为什么要剥 `think`

    `completion_tokens` 里可能混着推理 token，而**推理 token 不产生可见正文**。
    不区分两者的话，「输出 token 涨了 3 倍」既可能是模型写得更长，也可能是它想得更久。

    判据用 `protocol._THINK_BLOCK_RE`（**唯一一份** think 块识别）：在这里另写一个
    正则，两处迟早会在「`<think/>` 这种自闭合写法算不算」上分叉。

    空文本 → `None`（**不是 0**）：一次没跑成的调用与一次写了零个字的调用不同。
    整段只有思考块 → 可见正文**确实是 0**（那是一个观测值）。
    """
    raw = str(text or "")
    if not raw:
        return None
    return len(_THINK_BLOCK_RE.sub("", raw))


def truncation_reason(batch: Any) -> str:
    """这一轮**交付**的截断原因（`truncated_by` 的取值去重后拼接）。空串 = 没截断。

    取的是条目上的 `truncated_by`（`context_tools` 与 `budget.shrink_item` 都写它，
    各自的名字点明了是哪条约束砍的）——**不在这一层再判一次**「砍没砍」：
    判据分散就会漂移，而漂移的表现是「面板说截断了、明细里没有」。
    """
    reasons: list[str] = []
    for item in getattr(batch, "items", ()) or ():
        meta = dict(getattr(item, "meta", None) or {})
        if not meta.get("truncated"):
            continue
        name = str(meta.get("truncated_by") or "")
        if name and name not in reasons:
            reasons.append(name)
    return "、".join(reasons)


def replay_chars(tools: Any) -> int:
    """`ContextTools` 累计的**跨成员重放**字符数（按类型统计求和）。读不出来返回 0。

    0 在这里是「没有可读的统计」而不是「确实没重放」—— 但这一格进的是 `RoundRecord` 上
    一个 `int` 字段（不是可空的用量字段），所以取 0 不会把「未上报」伪装成一个观测值：
    真正的观测值是**差值**，而差值读不出来时它本来就是 0（见 `run_analysis` 里
    「执行前后各量一次」那两行）。
    """
    try:
        stats = tools.stats or {}
    except Exception:  # noqa: BLE001 —— 统计读不到不该作废这一轮
        return 0
    return sum(
        int((counters or {}).get("cross_member_replayed_chars") or 0)
        for counters in stats.values()
    )


def with_observability(record: Any, entry: dict | None) -> dict | None:
    """把 P5 的字段**并进**这一轮的明细；`entry` 是 `trace_evidence.live_round_entry` 的产物。

    ## 为什么在外面合并，而不是去改那一份

    `trace_evidence.live_round_entry` 的键必须与「跑完之后从库里读」那一份
    （`ai_usage_service.run_usage()["rounds"]`）逐字对齐 —— 它不在本工作包的文件范围内。
    而 P5 的四个数是**新的**观测项：并进这一份 dict 之后，`round_events.event_fields` 的
    `entry_json`（dump 的就是它）会原样带上，于是「跑的过程中」与「落库之后」两条来路
    看到的是同一份东西。

    `entry` 为 `None`（明细算不出来）时返回 `None`：**不编一个只有 P5 字段的半份明细** ——
    那会让读的人以为这一轮的其余明细「本来就是空的」。
    """
    if entry is None:
        return None
    return {
        **entry,
        # 未上报存 `None`（不存 0）：见模块 docstring 的口径那一条。
        "visible_response_chars": getattr(record, "visible_response_chars", None),
        "reasoning_tokens": getattr(record, "reasoning_tokens", None),
        "tool_replay_chars": int(getattr(record, "tool_replay_chars", 0) or 0),
        "truncation_reason": str(getattr(record, "truncation_reason", "") or ""),
        "correction_reason": str(getattr(record, "correction_reason", "") or ""),
    }


__all__ = [
    "CORRECTION_CHANNEL_MISMATCH",
    "CORRECTION_TRUNCATED_OUTPUT",
    "CORRECTION_UNPARSABLE",
    "replay_chars",
    "truncation_reason",
    "visible_response_chars",
    "with_observability",
]
