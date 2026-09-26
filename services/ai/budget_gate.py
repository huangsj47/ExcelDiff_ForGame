# -*- coding: utf-8 -*-
"""单次 token 硬上限的**运行时账**：在再发起一次模型调用之前判「还付得起吗」。

## 它补的是哪一个缺口

计划里那个「单次上限 1,500,000 token」原先**只在子代理路径的成员开跑前**判一次
（`analysis_plan.make_single_run_guard` → `run_family` 的 `should_skip`）。于是：

* **单分析者路径完全没有闸** —— 页面写着「单次上限」，实际一个字的检查都没有；
* 子代理路径**拦不住一个成员超额** —— 只在它开跑前看一眼，它自己跑起来之后花多少没人管。

实测（2026-09-24）时计划的「单次上限」就是这么一句名不副实的话。

## 判据只有一份

算术在 `auto_sizing.single_run_guard`（`已花 + 本轮保守预留 + 收尾预留 > 上限`），
本模块**不重写那个公式** —— 它把三个数摆成 `single_run_guard` 认的形状再调它。
`AnalysisPlan` 本来就有这三个字段，所以这里直接复用，口径不可能漂。

## 闸门按轮判，扣账按次

引擎在**每一轮开头**问一次「还付得起下一轮吗」。不在每次调用前判，是因为一轮里可能有
2~3 次调用（压小重发、收尾补救），而**每轮的保守预留本来就覆盖了那些**（
`round_reserve_tokens` 的语义就是「这一轮最坏情况要花多少」）。按轮判与按次判在
「会不会超」上等价，但按轮判不需要在三个调用点各塞一次检查、也不用异常做控流。

**扣账仍按次**：每次调用拿到上游 usage 之后调 `note_usage`（引擎里那一个出口）。

## 说清楚边界：这是**调用前硬闸 + 单次调用最大误差边界**

模型单次返回可能超出预留（输出上限只是我们请求时给的上限，上游不一定遵守），所以这里
能保证的是「**再发起调用之前**已经付不起了就不发」，不能承诺绝对零超支。缺 usage 时按
`conservative_tokens_for`（请求字符 + 输出上限）**保守估算并标记为估算** —— 不写 0
（那等于无限放行）、也不当成精确值。
"""

from __future__ import annotations

from dataclasses import dataclass

from services.ai.auto_sizing import single_run_guard, single_run_lookahead

__all__ = ["SingleRunBudget"]


@dataclass
class SingleRunBudget:
    """一次运行的 token 账。字段名与 `AnalysisPlan` 对齐，好让 `single_run_guard` 直接认。

    `total_token_budget <= 0` = **没有配单次上限**，这时 `affordable()` 一律放行
    （与 `single_run_guard` 的 `if cap and ...` 同一条口径：没有上限不是「上限为 0」）。
    """

    total_token_budget: int = 0
    round_reserve_tokens: int = 0
    report_reserve_tokens: int = 0
    # 「本次运行**已经**花了多少」的下界。子代理路径从家族累计值起算，
    # 单分析者从 0 起算。
    spent_tokens: int = 0
    # 这个 spent 里有没有估算成分（上游缺用量时按保守值补的）。报告与界面要说出来。
    spent_estimated: bool = False

    @classmethod
    def from_plan(cls, plan, *, spent_tokens: int = 0, spent_estimated: bool = False):
        """按一份 `AnalysisPlan`（或任何有那三个字段的对象）建账。"""
        return cls(
            total_token_budget=int(getattr(plan, "total_token_budget", 0) or 0),
            round_reserve_tokens=int(getattr(plan, "round_reserve_tokens", 0) or 0),
            report_reserve_tokens=int(getattr(plan, "report_reserve_tokens", 0) or 0),
            spent_tokens=max(0, int(spent_tokens or 0)),
            spent_estimated=bool(spent_estimated),
        )

    def note_usage(self, tokens: int | None, *, estimated: bool = False) -> None:
        """把**这一次调用**的用量记上。`None` = 上游没报（见下面的说明）。

        上游没报时调用方应当传**保守估算值**并把 `estimated` 置真；传 `None` 则按 0 记
        —— 那等于「没报就先不拦」，与 `subagent._tokens_of` 同一条既有口径（宁可漏拦一次，
        也不要因为一个数读不到就把一次分析判死）。
        """
        if tokens is None:
            return
        self.spent_tokens += max(0, int(tokens))
        if estimated:
            self.spent_estimated = True

    def check(self) -> dict:
        """`single_run_guard` 的判定（唯一实现，这里只是把三个数递过去）。"""
        return single_run_guard(
            self,
            spent_tokens=self.spent_tokens,
            spent_estimated=self.spent_estimated,
        )

    def affordable(self) -> bool:
        """还付得起下一轮吗。没有配上限时恒真。"""
        return not self.check()["blocked"]

    def next_round_affordable(self, last_round_tokens: int | None = None) -> bool:
        """**这一轮之后**还付得起下一轮吗（`False` = 本轮就该让模型收尾了）。

        **名字里带得清极性**：返回的是「付得起」，不是「该收尾」—— 调用点写
        `not budget.next_round_affordable(...)`。写成 `lookahead()` 那种中性名字，
        读的人（和写的人）迟早会把极性搞反，而搞反的表现是「每一轮都在喊收尾」。

        判据是**同一条公式**（`auto_sizing.single_run_lookahead`，它内部调
        `single_run_guard`），只是把闸门里那个未知的「本轮结束后已花多少」按
        `max(round_reserve_tokens, 上一轮实花)` 估出来 —— 上一轮真的花了多少通常比平台
        的预留更准（run 3：单轮实测 202,283，预留约 140,000）。

        `None` / 0 = 没有上一轮的实测（首轮），退回本轮预留。**这一条是估算，不是事实**：
        单次调用实际花多少只有事后才知道，所以调用方拿它去说话时不许断言「这是最后一轮」
        （轮次与索取额度那两条是硬事实，见 `round_hints` 里四份收敛指令的分别）。
        """
        return not single_run_lookahead(
            self,
            spent_tokens=self.spent_tokens,
            last_round_tokens=int(last_round_tokens or 0),
            spent_estimated=self.spent_estimated,
        )["blocked"]

    def stop_note(self) -> str:
        """停下来的理由（可以直接写进轮次备注与报告的信息缺口）。"""
        return self.check()["reason"]

    @property
    def headroom(self) -> int:
        """按当前口径还能花多少（负数 = 已经超出，仅用于展示）。"""
        return int(self.check()["headroom"])
