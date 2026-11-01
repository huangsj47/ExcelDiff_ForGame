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

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping, Sequence

if TYPE_CHECKING:
    from services.ai.engine import EngineLimits

from services.ai.budget import ContextItem, enforce_budget, estimate_chars
from services.ai.prompt import build_user_message, change_block, render_context_items
from services.ai.skill_contract import DIMENSION_IDS

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
    overhead_chars: int | None = None,
) -> tuple[tuple[ContextItem, ...], list[str]]:
    """把上下文条目压进「除条目之外还剩多少」的额度里。

    **不能拿总预算当条目额度**：系统提示词、变更摘要、基线摘要、前几轮的问答都要从
    同一份预算里出。按总预算给条目，必然超；超了以后要么被服务端拒绝，要么被截断，
    而模型分不出「文件就这么大」和「预算不够」——正是 `budget.py` 要解决的那个问题。
    """
    notes: list[str] = []
    overhead = (estimate_chars(messages) + len(change_summary) + limits.baseline_char_budget
                if overhead_chars is None else overhead_chars)
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
    # 与实际模板同源计量，检查点现在包含关键证据原文，不能漏算它。
    base_message = build_user_message(
        change_summary=brief.change_summary, round_index=brief.round_index,
        max_rounds=brief.max_rounds, items=(), baseline_digest=brief.baseline_digest,
        budget_notes=brief.budget_notes, requests_remaining=brief.requests_remaining,
        requests_total=brief.requests_total, correction_hint=brief.correction_hint,
        budget_exhausted=brief.budget_exhausted, token_budget_low=brief.token_budget_low,
        history_recap=brief.recap, dimension_ids=brief.dimension_ids,
    )
    items, item_notes = fit_items(
        brief.pending_items,
        messages=messages,
        change_summary=block,
        limits=brief.limits,
        overhead_chars=estimate_chars(messages) + len(base_message)
            + (len(PREPATCHED_NOTICE) if brief.round_index <= 1 else 0)
            + 500 + 300 * len(brief.pending_items),
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
        token_budget_low=brief.token_budget_low,
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




@dataclass(frozen=True)
class RoundBrief:
    """一轮里「与历史无关」的那些输入。

    攒成一包是因为**同一轮可能要组装两次**：压掉历史之后条目额度会变大，必须重算一遍
    （见 `_prepare_round`）。散着传十几个参数，两次调用之间漏传一个就是一条静默的错
    —— 而它表现为「分析结果说不清哪里不对」。
    """

    round_index: int
    max_rounds: int
    change_summary: str
    baseline_digest: str
    correction_hint: str
    budget_exhausted: bool
    budget_notes: tuple[str, ...]
    pending_items: tuple[ContextItem, ...]
    recap: str
    requests_remaining: int
    # 本次运行的总额度（`limits.max_tool_requests`）。**必须与 `requests_remaining` 一起
    # 给模型**：只报剩余数时，第 2 轮起那句「总共可索取 N 次」会把剩余说成总额 ——
    # 模型会把它原样抄进报告的信息缺口（见 `prompt._budget_line` 的 docstring）。
    requests_total: int
    limits: EngineLimits
    # 子代理模式下的**任务书**（第 1 轮的 user 消息原文，见 `run_analysis`）。空串 = 常规路径。
    task_message: str = ""
    # 本次生效的维度清单（id 那一份）。只给第一轮的开场指令用：清单里没有
    # `config_data` / `value_sanity` 的项目不该读到「按配表数值看」那两步
    # （见 `prompt._first_round_hint`）。默认是平台出厂那一份 —— 与这段改动之前
    # 逐字相同（`run_analysis` 每次都会按 `loaded.dimensions` 传真的那份进来）。
    dimension_ids: tuple[str, ...] = DIMENSION_IDS
    # 「钱只够这一轮了」——`SingleRunBudget.next_round_affordable` 的估算结果（见 `round_hints
    # .build_token_budget_final_hint`）。与 `budget_exhausted`（索取**次数**额度）是两条
    # 不同的额度：那一条是硬事实，这一条是估算，所以 `prompt.build_user_message` 里把它
    # 排在最后 —— 硬事实成立时它不说话。
    #
    # 带默认值所以放在这一组（dataclass 的非默认字段必须排在前面）。
    token_budget_low: bool = False


