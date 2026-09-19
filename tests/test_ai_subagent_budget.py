# -*- coding: utf-8 -*-
"""子代理模式的预算早停：**跑到一半超了就不会继续跑完剩下的分片**。

## 这个文件守的四条性质

1. **判据里必须含「本次运行已经花掉的」**。闸门（`budget_gate_reason`）判的是起跑前 ——
   那时这次运行一行都没落库。子代理一家子要跑 n+1 次模型调用，前面几片的花费同样还没
   落库；只看已落库的量，每一片都会看到「还没超」，于是没有哪一片会被拦下，
   一路把预算超穿。`test_the_live_tokens_are_what_push_it_over` 就是抓它的。
2. **判不出来时不拦**。查账失败（`budget_status` 抛异常）只是记一条日志。让它变成
   「剩下的分片全被跳过」的话，报告会说「那几块维度没人看过」，而真相是我们算不出账 ——
   一句把人引到错方向的结论比多花一片的钱糟得多。
3. **写进报告的那句话要按看的人脱敏**。它进的是**报告正文**（信息缺口里那一行），
   平台档超了而看的人不是平台管理员时不给额度数字 —— 与闸门同一口径。
4. **汇总那一次不查预算**：它是唯一产出最终报告的一步。真的付不起就不该起跑（那是闸门
   的事），跑到一半把汇总也跳掉等于这一家子白跑。

判据本身用的是真的 `with_live_usage`（只把 `budget_status` 换成一份构造好的判定），
所以「加进去之后算不算超」这件事走的是线上那一份算术，不是测试自己重写的一份。
"""
from __future__ import annotations

import pytest

from services.ai import analysis_budget as budget_mod
from services.ai.analysis_budget import early_stop_guard

LIMIT_TOKENS = 10_000
USED_TOKENS = 9_500


def _status(*, blocks: bool = False, reason: str = "") -> dict:
    """一份**未超**的判定（形状照 `budget_status` 的返回体，只留被测代码读的那几个键）。"""
    return {
        "limited": True,
        "over": blocks,
        "blocks_analysis": blocks,
        "reason": reason,
        "over_limits": ["tokens"] if blocks else [],
        "period_label": "本月",
        "limits": {"tokens": LIMIT_TOKENS, "cost": None, "currency": "CNY"},
        "used": {"tokens": USED_TOKENS, "cost": None, "currency": "CNY"},
        "platform": {
            "limited": False,
            "over": False,
            "blocks_analysis": False,
            "reason": "",
            "limits": {},
            "used": {},
            "over_limits": [],
        },
    }


@pytest.fixture(autouse=True)
def _no_request_scope(monkeypatch):
    """默认按「看的人不是平台管理员」跑（后台线程里就是这个情形）。"""
    monkeypatch.setattr(budget_mod, "platform_scope_visible", lambda: False)


def _freeze_status(monkeypatch, status):
    monkeypatch.setattr(budget_mod, "budget_status", lambda project_id, **kw: dict(status))


class _Member:
    def __init__(self, label: str = "S1"):
        self.label = label


# ==========================================================================
# 判据
# ==========================================================================


def test_it_does_not_stop_while_the_budget_holds(monkeypatch):
    _freeze_status(monkeypatch, _status())
    assert early_stop_guard(7)(_Member(), 0) == ""


def test_the_live_tokens_are_what_push_it_over(monkeypatch):
    """已落库 9500 / 上限 10000 —— 只看库里的账，「还没超」；加上本次跑的 1000 就超了。

    这正是第一版会漏掉的那件事：**预算是跨轮次、跨成员的总额**，而本次运行的花费
    要等整次分析结束才落库。
    """
    _freeze_status(monkeypatch, _status())
    guard = early_stop_guard(7)

    assert guard(_Member(), 0) == "", "只看已落库的部分不该拦"
    assert guard(_Member(), 400) == "", "还没到上限"
    reason = guard(_Member(), 1_000)
    assert reason, "加上本次已消耗的 1000 就该拦了"
    # 10.5k = 9500（已落库）+ 1000（本次），而**上限是 10.0k** —— 只看库里那份永远到不了。
    assert "10.5k" in reason, f"理由里要带上算出来的数：{reason}"
    assert "含本次运行" in reason, f"要说明这个数含本次已消耗：{reason}"


def test_a_blocked_status_stops_even_with_no_live_usage(monkeypatch):
    """已落库那一档自己就超了（多进程/上一次运行留下的）时，同样要拦。"""
    _freeze_status(monkeypatch, _status(blocks=True, reason="本月的 token 已用超上限"))
    assert "已用超上限" in early_stop_guard(7)(_Member(), 0)


def test_it_does_not_stop_when_the_accounts_cannot_be_read(monkeypatch):
    """**查账失败不许变成「剩下的分片全跳过」。**"""

    def _boom(project_id, **kw):
        raise RuntimeError("数据库连接断了")

    monkeypatch.setattr(budget_mod, "budget_status", _boom)
    assert early_stop_guard(7)(_Member(), 999_999) == ""


def test_the_reason_is_redacted_for_a_non_platform_admin(monkeypatch):
    """平台档超了、看的人不是平台管理员时，写进报告的那句话不给额度数字。

    判定用的那一份判定由**真的** `_merge_scopes` 合成（顶层 `reason` 就是两档理由拼出来的
    那一段）—— 自己手写一份带数字的顶层理由，测的就不是线上那条脱敏路径了。
    """
    platform_node = {
        "limited": True,
        "over": True,
        "blocks_analysis": True,
        "reason": "本月已用 9,000,000 tokens，超过上限 10,000,000",
        "period_label": "本月",
        "limits": {"tokens": 10_000_000, "cost": None},
        "used": {"tokens": 9_000_000, "cost": None},
        "over_limits": ["tokens"],
    }
    monkeypatch.setattr(budget_mod, "platform_scope_visible", lambda: False)
    _freeze_status(monkeypatch, budget_mod._merge_scopes(_status(), platform_node))

    reason = early_stop_guard(7)(_Member(), 0)

    assert reason, "平台档超了照样拦"
    assert "9,000,000" not in reason, f"不该带平台额度数字：{reason}"
    assert "平台管理员" in reason, reason


def test_the_reason_is_a_sentence_that_can_go_into_the_report(monkeypatch):
    """它会**原样**进报告的信息缺口那一行（「S3 未运行：<这句话>；它负责的维度…」）。"""
    _freeze_status(monkeypatch, _status())
    reason = early_stop_guard(7, entry="subagent")(_Member("S3"), 1_000)

    assert reason.startswith("预算不足，提前收工"), reason
    assert "。" in reason, "要是一句能读懂的话，不是一串数字"


# ==========================================================================
# 接到一家子上
# ==========================================================================


def test_the_guard_sees_what_the_earlier_members_spent(monkeypatch):
    """`run_family` 把**已跑成员烧掉的 token** 传给判据，而不是每一片都从 0 开始。"""
    from services.ai.subagent import run_family
    from tests.test_ai_subagent_family import (  # noqa: PLC0415 —— 复用那一份假引擎
        FakeProvider,
        FlakyClient,
        _args,
        _final,
        _plan,
    )

    seen: list[int] = []

    def should_skip(member, tokens):
        seen.append(tokens)
        return ""

    run_family(
        client=FlakyClient(_final()),
        provider=FakeProvider(),
        plan=_plan(3),
        should_skip=should_skip,
        **_args(),
    )

    assert len(seen) == 3, "三个分片各问一次（汇总不问）"
    assert seen[0] == 0
    assert seen[1] > 0 and seen[2] >= seen[1], f"后一片要看到前面已经花掉的：{seen}"


def test_the_synthesis_is_never_skipped(monkeypatch):
    """汇总那一次不查预算 —— 跳掉它这一家子就没有报告了。"""
    from services.ai.subagent import run_family
    from tests.test_ai_subagent_family import (  # noqa: PLC0415 —— 复用那一份假引擎
        FakeProvider,
        FlakyClient,
        _args,
        _final,
        _plan,
    )

    asked: list[str] = []

    def should_skip(member, tokens):
        asked.append(member.label)
        return "预算不足"

    result = run_family(
        client=FlakyClient(_final()),
        provider=FakeProvider(),
        plan=_plan(3),
        should_skip=should_skip,
        **_args(),
    )

    assert asked == ["S1", "S2", "S3"], "汇总那一次不该被问"
    synthesis = [step for step in result.steps if step.plan.is_synthesis]
    assert synthesis and synthesis[0].ran, "汇总必须照跑"
    assert result.outcome.report_markdown, "报告仍然要有（并带着三条信息缺口）"
