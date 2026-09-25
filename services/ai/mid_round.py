"""中间轮的**字段收窄**：那一轮只认四个字段，其余的一律丢弃并逐条记账。

## 为什么要收窄

中间轮存在的意义只有一个 —— 把「我还需要什么」交回来并开始下一轮。而
`report_markdown` / `anomalies` / `dimensions` / `candidate_dispositions` /
`baseline_updates` 这几个字段**在中间轮没有任何读者**（引擎只在 `is_final` 那一支读
它们）。模型顺手在中间轮写一整份报告时，平台原先**不拦不丢**：白花输出 token，还让
那一轮的输出更容易撞上单次输出上限（撞上就是整轮作废）。丢弃是**有账**的，不是静默消失。

## 为什么单独一个模块

`protocol.py` 顶着仓库 2000 行的 ERROR 闸门（`scripts/check_file_length.py`），而这一段
与「解析 / 校验 / 接地」三件事都无关 —— 它只回答「中间轮里多写了什么」，自成一件事。
搬出来之后 `protocol.py` 只剩一行导入。

## `DroppedItem` 为什么在函数里导入

它定义在 `protocol.py`，而 `protocol.py` 在模块级导入本模块 —— 模块级互相导入会成环。
所以运行时那一份在**函数体内**导入（一次分析最多调一轮，开销可以忽略），标注那一份
走 `TYPE_CHECKING`（求值发生在类型检查期，不会真的执行）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 只在类型标注里用；运行时的那一份在函数体内导入（见模块 docstring）。
    from services.ai.protocol import DroppedItem

# 中间轮**只认这四个字段**。其余的一律解析后丢弃并逐条记账（见 `mid_round_drops`）。
#
# 为什么中间轮要收窄：那一轮存在的意义只有一个 —— 把「我还需要什么」交回来并开始下一轮。
# 而 `report_markdown` / `anomalies` / `dimensions` / `candidate_dispositions` 这四个
# 字段**在中间轮没有任何读者**（引擎只在 `is_final` 那一支读它们）。模型顺手在中间轮
# 写一整份报告时，平台原先**不拦不丢**：白花输出 token，还让那一轮的输出更容易撞上
# 单次输出上限（撞上就是整轮作废）。现在丢弃并记账 —— 丢弃是**有账**的，不是静默消失。
MID_ROUND_FIELDS = ("status", "reason", "reason_code", "requests")

# 中间轮里**不该出现**的那四个字段，以及它们在文档里的别名。别名（`report`）只记一条账：
# 记两条会让人以为模型交了两份报告。
_MID_ROUND_EXTRA_FIELDS = (
    ("report_markdown", ("report_markdown", "report")),
    ("anomalies", ("anomalies",)),
    ("dimensions", ("dimensions",)),
    ("candidate_dispositions", ("candidate_dispositions",)),
    # 收口声明同样是**只有 final 有读者**的字段：引擎只在最后那一支读它。中间轮顺手写
    # 一份既白花输出、又更容易撞上单次输出上限，所以和上面四个一样丢弃并记账。
    ("baseline_updates", ("baseline_updates",)),
)


def mid_round_drops(raw: dict) -> tuple[DroppedItem, ...]:
    """中间轮里那份**不该出现的报告**：逐个字段记账。

    判据是「非空才算」：`"anomalies": []` 是模型的正当写法（这一轮没有发现），把它记成
    「写了报告被丢弃」是假账 —— 而假账比没有账更糟，读的人会去找一份不存在的报告。
    """
    from services.ai.protocol import DroppedItem, _safe_repr

    dropped: list[DroppedItem] = []
    for name, keys in _MID_ROUND_EXTRA_FIELDS:
        for key in keys:
            value = raw.get(key)
            if isinstance(value, str):
                # 只有空白（`"report_markdown": "   "`）与空串是一回事：模型**没有**写报告。
                # 不 strip 就把它记成「写了报告被丢弃」是假账 —— 而假账比没有账更糟，
                # 读的人会去找一份不存在的报告。
                value = value.strip()
            if not value:
                continue
            if isinstance(value, (list, tuple, dict, str)):
                # `detail` 里**带上字段名**：面板对 `dropped` 的前缀是「未执行：」（
                # `static/js/ai_think_log.js`），只写「给了 3 条」那句话读不成句 ——
                # 「未执行：给了 3 条」看不出是什么给了 3 条。
                detail = f"{name} 给了 " + (
                    f"{len(value)} 条"
                    if isinstance(value, (list, tuple))
                    else f"{len(value)} 字符"
                )
            else:
                detail = _safe_repr(value)
            dropped.append(
                DroppedItem(
                    "mid_round_field",
                    len(dropped),
                    f"中间轮（need_more_context）不接受 `{name}`，已丢弃"
                    f"（这一轮只认 {'、'.join(MID_ROUND_FIELDS)}）",
                    detail,
                )
            )
            # 同一个字段的两个写法（`report_markdown` 与 `report`）只记一条 —— 记两条
            # 会让人以为模型交了两份报告。
            break
    return tuple(dropped)
