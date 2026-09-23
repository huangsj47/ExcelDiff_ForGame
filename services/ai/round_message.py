# -*- coding: utf-8 -*-
"""本轮 user 消息的组装：条目按剩余额度裁剪，以及**第一轮也要带条目**（P3）。

## 为什么它不在 engine.py 里

`services/ai/engine.py` 贴着 2000 行的 ERROR 闸门（`scripts/check_file_length.py`），
而这一层是**纯组装**：给它条目、预算与消息历史，它返回可以发出去的那条消息。
搬出来之后引擎那边只剩两行调用（`prepare_round`），编排逻辑才装得下。

## 与 `prompt.build_user_message` 的分工

那一层负责**排版**（章节、包封套、指针），这一层负责**裁剪与投递**：条目按「除条目
之外还剩多少」压进额度（`fit_items`），并在第一轮把（预取来的）条目附在开场指令之后
（`prompt.build_user_message` 的第一轮分支只渲染开场指令 —— 它假定「第一轮没有待发
条目」）。把条目放进第一轮这件事需要一层说明（`PREPATCHED_NOTICE`）：开场指令里写着
「现在只给了你 …没有任何 diff 内容」，那是「你还没有索取过任何东西」的意思，而下面是
平台**替你**取回的 —— 不点破这层关系，模型会把两份说法读成互相矛盾。

`brief` 用 `Any` 标注：它的形状由引擎的 `_RoundBrief` 定义，这里只按属性读 ——
写成一个 import 会变成循环依赖（engine → 本模块 → engine）。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from services.ai.budget import ContextItem, enforce_budget, estimate_chars
from services.ai.prompt import build_user_message, change_block, render_context_items

#: 第一轮里那批**预取**条目前面的说明（见 `_prepare_round` 末尾）。
#:
#: 用三引号写成真正的多行（源码里**不写**转义换行）：本仓的行尾与转义都踩过坑，
#: 一段跨行的说明写成带转义换行的字面量，最容易在批量替换里被改成半截字符串。
PREPATCHED_NOTICE = """## 平台预先附上的取证（**不是你索取的结果**）

上面那句「没有任何 diff 内容」说的是「你还没有索取过任何东西」；下面是平台按本次
冻结批次**先替你取回**的一小批（改动文件差异 / 符号命中位置 / 命中处的邻近窗口，
每一项都带覆盖与截断说明）。**它们已经在这里了，不要再要一遍**；缺的部分照常点名
索取即可。"""




# 给上下文条目留的最小额度。低于这个值就没什么可给的了，与其压到 0 不如如实记账。
MIN_ITEM_BUDGET = 4_000
def fit_items(
    items: Sequence[ContextItem],
    *,
    messages: Sequence[Mapping[str, str]],
    change_summary: str,
    limits: EngineLimits,
) -> tuple[tuple[ContextItem, ...], list[str]]:
    """把上下文条目压进「除条目之外还剩多少」的额度里。

    **不能拿总预算当条目额度**：系统提示词、变更摘要、基线摘要、前几轮的问答都要从
    同一份预算里出。按总预算给条目，必然超；超了以后要么被服务端拒绝，要么被截断，
    而模型分不出「文件就这么大」和「预算不够」——正是 `budget.py` 要解决的那个问题。
    """
    notes: list[str] = []
    overhead = estimate_chars(messages) + len(change_summary) + limits.baseline_char_budget
    residual = limits.prompt_char_budget - overhead
    if residual < MIN_ITEM_BUDGET:
        notes.append(
            f"提示词已用掉 {overhead:,} 字（上限 {limits.prompt_char_budget:,}），"
            f"留给上下文的额度被压到 {MIN_ITEM_BUDGET:,} 字，本轮内容会大幅压缩。"
        )
        residual = MIN_ITEM_BUDGET

    result = enforce_budget(items, max_items=limits.max_items, total_chars=residual)
    notes.extend(result.notes)
    return result.items, notes



def prepare_round(
    brief: Any, messages: Sequence[Mapping[str, Any]]
) -> tuple[tuple[ContextItem, ...], str]:
    """组装本轮要发的 user 消息，返回 `(真正进得去的上下文, 消息文本)`。

    **预算与消息必须用同一份变更清单文本**（`prompt.change_block`）：按清单全文算预算、
    消息里只放指针，会让平台白白少用几十万字符的额度；反过来的组合则是超预算。

    子代理模式的第 1 轮走上面那条 `task_message` 分支：变更清单已经在**共享消息**里
    （`seed_messages`）且已经按 `estimate_chars(messages)` 计入预算，这里再拼一遍会把它
    算两次 —— 于是平台会白白少给自己的成员几万字符的上下文额度。
    """
    if brief.round_index <= 1 and brief.task_message.strip():
        # 子代理的共享消息里已经带着变更清单，这里只补任务书；待发条目不该有
        # （那条路上不预取，见 `run_analysis` 里 `prefetch` 那一段）。
        return (), brief.task_message
    block = change_block(brief.change_summary, round_index=brief.round_index)
    items, item_notes = fit_items(
        brief.pending_items,
        messages=messages,
        change_summary=block,
        limits=brief.limits,
    )
    message = build_user_message(
        change_summary=brief.change_summary,
        round_index=brief.round_index,
        max_rounds=brief.max_rounds,
        items=items,
        baseline_digest=brief.baseline_digest,
        budget_notes=[*brief.budget_notes, *item_notes],
        requests_remaining=brief.requests_remaining,
        requests_total=brief.requests_total,
        correction_hint=brief.correction_hint,
        budget_exhausted=brief.budget_exhausted,
        history_recap=brief.recap,
        dimension_ids=brief.dimension_ids,
    )
    if brief.round_index <= 1 and items:
        # **第一轮也要带上条目**（P3：预取来的取证在第 1 轮就该到手）。`prompt
        # .build_user_message` 的第一轮分支只渲染开场指令（它假定「第一轮没有待发
        # 条目」），所以这一段由这里补上 —— 而不是去改那个文件（本工作包的文件范围不
        # 含 `services/ai/prompt.py`；这一段将来搬进 prompt 层时，两处都要改）。
        #
        # 紧跟一句说明是**必须的**：开场指令里写着「现在只给了你 … 没有任何 diff 内容」，
        # 那是「你还没有索取过任何东西」的意思，而下面这些是**平台替你取回的**（`requests`
        # 里没有它们）。不点破这层关系，模型会把两份说法读成互相矛盾，或者误以为自己
        # 已经索取过了（进而在报告里引用一份它没要过的证据）。
        message = chr(10).join(
            (message, "", PREPATCHED_NOTICE, "", render_context_items(items))
        )
    return items, message


