# -*- coding: utf-8 -*-
"""退化（degradation）的取值域与它的中文标签。

## 为什么单独一个模块

`services/ai/engine.py` 贴着仓库的 2000 行 ERROR 闸门
（`scripts/check_file_length.py --strict`）。这一组是**常量与文案**：没有任何依赖，
只被引擎与少数读侧按名字取用，搬出来之后引擎那边只留回导。

## 为什么它值得一份正经的取值域

`degraded` 与 `succeeded` 必须分开（见 `STATUS_DEGRADED`），而**分得不细等于没分**：
「轮次用尽」是「这块看过了但看得不够」，「子代理缺口」是「这块**没有人看过**」——
后者最容易被读成「这里没问题」。所以每一档都要有名字，也要有一句能给用户看的话
（抽屉会显示 ⚠️ 与 `DEGRADATION_LABELS` 里那句），不能只写在报告正文里。
"""

from __future__ import annotations

# 退化原因。空字符串表示没有退化。
DEGRADE_NONE = ""
DEGRADE_ROUNDS = "rounds_exhausted"
DEGRADE_REQUESTS = "requests_exhausted"
DEGRADE_MARKDOWN = "markdown_report"
DEGRADE_PROTOCOL = "protocol_corrections_exhausted"
# 上游以「上下文超长」拒绝了请求，平台收缩提示词后把结论收回来了。**它是一次退化**：
# 模型是在一份被压过的提示词上作答的，与正常跑完不是一回事，必须说出来。
DEGRADE_CONTEXT = "context_overflow"
# 子代理模式（`services/ai/subagent.py`）下「有成员没跑成」或「它报出的结论没有进入最终
# 报告」。**它比上面几条都重**：那几条说的是「这块看过了但看得不够」，这一条说的是
# 「这块**没有人看过**」—— 而它最容易被读成「这里没问题」。所以它必须出现在
# `degradation` 上（抽屉会显示 ⚠️ 与这段话），不能只写在报告正文里。
DEGRADE_SUBAGENT = "subagent_gap"
# 对账轮（`subagent_verify`）没跑成。它与上面那条的分别要读清楚：那条是「有一块维度
# 没人看过」，这条是**「结论没经过复核」**—— 报告本身是完整的，只是少了「找反证」这一步。
# 所以它比 `DEGRADE_SUBAGENT` 轻一档（见 subagent.py 的 `_DEGRADE_RANK`），但**仍然要说**：
# 用户打开对账轮，图的正是那一步，静默没了等于他以为自己买到了没买到的东西。
DEGRADE_VERIFY = "subagent_verify"
# **单次分析预算**到了：这一次运行还能花的不够了，平台在再发起一次**探索**调用
# 之前停住，基于已经拿到的证据出结论。
#
# **「交付那一次」不在这个定义域里**：手上已经有取证、却一条结论都没交回来时，平台会
# 再补一次「现在出结论」（那笔钱来自 `report_reserve_tokens`，见 `services/ai/wrap_up.py`）。
#
# 它与 `DEGRADE_REQUESTS` / `DEGRADE_ROUNDS` 是三种不同的「停」：那两条是**额度**用尽
# （问了几次 / 跑了几轮），这一条是**钱**用尽。用户看到「轮次用尽」会去调轮次上限，
# 而这里该调的是预算 —— 混成一句就等于把人引到错的那个旋钮上。
DEGRADE_BUDGET = "token_budget_exhausted"

DEGRADATION_LABELS = {
    DEGRADE_ROUNDS: "轮次用尽，基于已有证据出结论",
    DEGRADE_REQUESTS: "上下文索取额度用尽，基于已有证据出结论",
    DEGRADE_MARKDOWN: "模型没有按协议输出 JSON，已按 markdown 报告降级保存",
    DEGRADE_PROTOCOL: "连续多轮无法解析出协议要求的 JSON",
    # 三种补救都算（先压条目、再丢历史、最后收尾），所以这里**不写具体压了什么** ——
    # 写死「已压掉历史」在「只压了条目、历史还在」那一支上就是一句假话。
    # 具体做了什么在 trace 的轮次备注里（`round_notes`）。
    DEGRADE_CONTEXT: "提示词超出模型上下文窗口，已压缩上下文后出结论",
    DEGRADE_SUBAGENT: (
        "子代理模式：有分片没有跑成、或它报出的结论没有进入最终报告"
        "（见报告末尾的「信息缺口（平台补充）」）"
    ),
    DEGRADE_VERIFY: (
        "子代理模式：对账轮（找反证）没有跑成，报告里的结论**没有经过这道复核**"
    ),
    DEGRADE_BUDGET: (
        "单次分析预算已到，停止继续索取上下文 —— 基于已经拿到的证据出结论"
        "（不是「这里没问题」，是「这里没查完」）"
    ),
}

__all__ = [
    "DEGRADATION_LABELS",
    "DEGRADE_BUDGET",
    "DEGRADE_CONTEXT",
    "DEGRADE_MARKDOWN",
    "DEGRADE_NONE",
    "DEGRADE_PROTOCOL",
    "DEGRADE_REQUESTS",
    "DEGRADE_ROUNDS",
    "DEGRADE_SUBAGENT",
    "DEGRADE_VERIFY",
]
