# -*- coding: utf-8 -*-
"""一条结论都没交回来时的兜底：**补发一次**「现在出结论」。

## 它补的是哪一个缺口

三种额度（钱 / 轮次 / 索取次数）用尽时，引擎在轮首 `break`，然后**直接**走到失败出口 ——
中间没有任何模型调用。实测（2026-09-26 项目 1 的 run 3）：9 轮全在索取上下文，闸门一停，
整次运行以「没有拿到可用的结论」收场 —— 而模型手上其实已经有 9 轮取证。

`report_reserve_tokens` 这笔预留本来就是留给「最后写报告那一次」的，但它原先只被
**减掉**、从来没被花掉：`budget_gate` 的 docstring 当时写着「下面的兜底会带着已有的轮次
出结论」，而那句话只在模型**已经**写过报告时才成立。

## 什么时候发、什么时候不发

* **只在「本来一定会失败」时发**（`payload` 与 `markdown_fallback` 都没拿到）。正常运行
  一次都不多发，一分钱不多花。
* **必须有证据**：`seen_items`（真取到过的上下文）或 `rounds`（跑过的轮次）非空。
  上限连第一轮都付不起时（`cap` 小于两个预留）手上什么都没有，补发只会在一次**根本没
  起跑**的运行上再花一笔。
* **只在三种额度用尽时发**（`WRAP_UP_DEGRADATIONS`）：协议错、上下文超长、传输失败都不算
  「额度用尽」——那些情形补发一次既救不回来，也不该花。
* **只发一次**，且这次失败不引入新的失败模式：上游若以窗口或传输拒了它，运行照旧按
  原来那句话收场（只是多一句「已补发一次」）。

## 为什么带**完整对话**，而不是 `_salvage_user_message` 那一份

超长那条路（引擎里压到 `messages[:1]` + 千字符收尾提示）的理由是**窗口装不下**；
这里的理由是**钱**，窗口一个字都没少。把已取到的证据从提示词里摘掉，模型就只能对着
变更清单写一份空报告 —— 那比「没查完」更糟（`_CONVERGE_TAIL` 要的恰恰是「写清楚哪些
没查完」）。顺带解决了另一个坑：`messages[:1] + 收尾消息` 会把子代理的**任务书**切掉
（`task_message` 是分片第 1 轮的 user 消息），结论会因此变成「未归类」。

## 记账

那一次调用**照样按次扣账**（`model_call.tokens_for_budget` → `SingleRunBudget.note_usage`）：
不记的话，平台自己报的数就是错的。代价是 `headroom` 可能微负 —— 分界写在
`budget_gate` 的 docstring 里：**闸门管的是探索调用，交付那一次在它的定义域之外**。
"""

from __future__ import annotations

from typing import Any, Sequence

from services.ai.degradation import DEGRADE_BUDGET, DEGRADE_REQUESTS, DEGRADE_ROUNDS
from services.ai.round_hints import build_wrap_up_hint

#: 补发收尾调用的**触发条件**：这三种都是「额度用尽」，不是「这次跑坏了」。
#:
#: 顺序与 `degradation` 的取值域一致，方便对照。**不含** `DEGRADE_PROTOCOL`
#: （模型连协议都不肯写，再问一次也只是再问一次）、`DEGRADE_CONTEXT`（窗口确实不够，
#: 补发那一次同样装不下）、`DEGRADE_MARKDOWN`（正文已经拿到了，那不是「没有结论」）。
WRAP_UP_DEGRADATIONS = (DEGRADE_BUDGET, DEGRADE_REQUESTS, DEGRADE_ROUNDS)

__all__ = ["WRAP_UP_DEGRADATIONS", "build_wrap_up_entry", "should_request_final_answer"]


def should_request_final_answer(
    *, degradation: str, rounds: Sequence[Any], seen_items: Sequence[Any]
) -> bool:
    """这一次「没有拿到可用的结论」该不该补一次收尾调用。

    两个条件都要成立（理由见模块 docstring）：**额度用尽**，且**手上确实有东西**。
    """
    if degradation not in WRAP_UP_DEGRADATIONS:
        return False
    return bool(rounds) or bool(seen_items)


def build_wrap_up_entry() -> dict[str, str]:
    """要追加到**完整对话**后面的那一条 user 消息。

    单独一个函数而不是就地拼一个字典：那一份正文（`round_hints.build_wrap_up_hint`）是
    **唯一**能认出「这一次调用是补发的收尾」的凭据 —— 测试与排查都按它认
    （`tests/test_ai_wrap_up.py` 里那个 `_exploration_calls`）。正文的产地只有一处，
    这里不再抄一遍它的开头。
    """
    return {"role": "user", "content": build_wrap_up_hint()}
