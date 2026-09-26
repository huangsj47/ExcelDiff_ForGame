# -*- coding: utf-8 -*-
"""兜底：**一条结论都没交回来**时，补发一次「现在出结论」（`services/ai/wrap_up.py`）。

## 这一组盯的是哪一种假绿

「补了一次调用」这件事本身不难写，难的是**只在该补的时候补、补了要认账、认不下就别吹**。
所以每条判据都带一个反向：

* 补了要能救回来 —— 结论、正文、降级原因三样都得对上（不是「成功」就行）；
* 补了**不许**因为模型又索取一次就把空壳当结论收下（那比失败更糟：用户看到一份很干净的
  报告，里面一条结论都没有）；
* 传输失败**不许**把已有的轮次作废（它只是没补上，不是这次分析白跑了）；
* 正常运行**一次都不许多花**（这条没有反向对照的话，「每轮都补一次」也能让上面几条全绿）。

## 为什么判据落在「调用次数」上

这个功能的全部代价与全部产出都是一次模型调用。写成「有没有调用 `wrap_up` 里的某个函数」
就退化成了「接线了没有」—— 那种断言在「接了但从没发出去」的实现上照样绿。
"""
from __future__ import annotations

from services.ai.degradation import (
    DEGRADE_BUDGET,
    DEGRADE_PROTOCOL,
    DEGRADE_REQUESTS,
    DEGRADE_ROUNDS,
)
from services.ai.engine import (
    STATUS_DEGRADED,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    EngineLimits,
)
from services.ai.llm_client import ChatResult
from services.ai.round_hints import build_wrap_up_hint
from services.ai.wrap_up import should_request_final_answer
from tests.test_ai_engine import (
    COMMIT,
    TABLE,
    ScriptedClient,
    _anomaly,
    _final,
    _requests,
    _run,
)
from tests.test_ai_single_run_budget import CountingClient, _budget

#: 一次「第 1 轮就花光」的用量：`500 + 400` 记上账后，第 2 轮付不起
#: （`900 + 本轮预留 200 + 收尾预留 100 > 1000`，见 `tests.test_ai_single_run_budget`）。
EXPENSIVE, CHEAP = (dict(prompt_tokens=500, completion_tokens=400), dict(prompt_tokens=10, completion_tokens=5))


def _ask() -> str:
    return _requests({"type": "file_diff", "commit": COMMIT, "path": TABLE})


def _limited(client, **overrides):
    """跑一次**第 1 轮就花光**的分析：那一轮在索取上下文，第 2 轮被闸门拦下。"""
    overrides.setdefault("single_run_budget", _budget())
    return _run(client, **overrides)


class FailingClient:
    """前 `ok_calls` 次正常回答，之后每一次都抛 —— 用来演「某一次调用没发成」。"""

    def __init__(self, replies: list[str], *, ok_calls: int, usage: dict | None = None):
        self._replies = replies
        self._ok_calls = ok_calls
        self._usage = dict(usage or CHEAP)
        self.calls: list[list[dict]] = []

    def complete(self, messages, **kwargs):
        self.calls.append([dict(item) for item in messages])
        if len(self.calls) > self._ok_calls:
            raise ConnectionError("网关 502")
        index = min(len(self.calls) - 1, len(self._replies) - 1)
        return ChatResult(text=self._replies[index], model="fake", **self._usage)


# ==========================================================================
# 一、该补的时候补，并且真的救回来
# ==========================================================================


def test_a_run_that_would_have_failed_gets_one_last_chance():
    """上限用尽 + 手上已有取证 → 补一次「现在出结论」，这次运行从失败变成降级交付。

    这一条就是 2026-09-26 项目 1 的 run 3：9 轮全在索取上下文，闸门一停，整次运行
    「没有拿到可用的结论」。平台手上还留着 `report_reserve_tokens` 那笔报告预留 ——
    原先只被减掉、从来没被花掉。
    """
    client = CountingClient(_ask(), _final(_anomaly()), **EXPENSIVE)

    outcome = _limited(client)

    assert outcome.status == STATUS_DEGRADED, outcome.error_message
    assert outcome.degradation == DEGRADE_BUDGET
    # **真的拿到了东西**，不是「成功返回了一份空报告」。
    assert outcome.anomalies, "补发那一次拿回来的结论没有进报告"
    assert "改了道具表" in outcome.report_markdown
    # 那一次调用的正文就是收尾指令（唯一产地），且它带的是**完整对话**。
    assert len(client.calls) == 2, [call[-1]["content"][:20] for call in client.calls]
    assert client.calls[1][-1]["content"] == build_wrap_up_hint()
    assert client.calls[1][: len(client.calls[0])] == client.calls[0], (
        "补发那一次把已经取到的证据丢了 —— 它带的是前几轮的对话，不是一份千字符目录"
    )


def test_the_round_limit_also_gets_the_final_chance():
    """三种额度用尽都算 —— 「轮次用尽」那条路原先同样是直接失败。

    （钱那条见上一条；索取额度那条与它同一条分支，不重复造。）
    """
    client = ScriptedClient(_ask(), _final(_anomaly()))

    outcome = _run(client, limits=EngineLimits(max_rounds=1))

    assert outcome.degradation == DEGRADE_ROUNDS
    assert outcome.status == STATUS_DEGRADED
    assert outcome.anomalies


def test_the_wrap_up_round_is_recorded_once_with_a_free_index():
    """补发那一次要**进 trace**，而且轮序号不许撞上已有的那一轮。

    `ai_analysis_trace` 上有 `uq_ai_trace_run_round`（run_id + 轮序号）。循环变量在
    「借一轮做格式转换」那条路上会走到 `max_rounds + 1`，照抄它就会撞上唯一约束 ——
    那是一次**写库失败**，症状是「分析跑完了但轨迹缺一条」。
    """
    client = CountingClient(_ask(), _final(_anomaly()), **EXPENSIVE)

    outcome = _limited(client)

    indexes = [record.index for record in outcome.rounds]
    assert len(indexes) == len(set(indexes)), indexes
    assert indexes == [1, 2], indexes
    last = outcome.rounds[-1]
    assert last.status == "final"
    assert "补发" in last.note


# ==========================================================================
# 二、补了救不回来时：不许吹，也不许把已有的轮次作废
# ==========================================================================


def test_asking_for_more_context_again_is_not_a_conclusion():
    """模型在收尾那一次又回一句「我还想看 X」→ **不算结论**。

    收下它等于交出一份一条结论都没有的空壳报告：用户看到的是「跑完了、很干净」，
    而真相是「什么都没查完」。这比失败更糟 —— 失败至少说得清。
    """
    client = CountingClient(
        _ask(), _requests({"type": "commit_detail", "commit": COMMIT}), **EXPENSIVE
    )

    outcome = _limited(client)

    assert outcome.status == STATUS_FAILED
    assert outcome.degradation == DEGRADE_BUDGET
    assert not outcome.anomalies
    assert "已补发一次收尾调用" in outcome.error_message
    assert "没有按协议交回 JSON" in outcome.error_message


def test_the_wrap_up_round_note_does_not_contradict_its_own_status():
    """补发那一轮的备注要与**同一行的状态**对得上，三种结局分别说。

    原先只有「拿到了结论 / 仍未拿到结论」两句，判据是 `wrap_error` 空不空 —— 于是
    「模型没按协议给 JSON、但正文是一份像样的 markdown 报告」那一档（状态是
    `unparsable`，正文照常交付）会被写成「拿到了结论」。读 trace 的人拿这一句去对
    「为什么状态是 unparsable」，怎么对都对不上。

    **正文那一档是真实存在的一条路**（与循环里那条 `markdown_fallback` 同一口径），
    所以它必须有自己的一句话 —— 只测「final 那句还在」是测不出这个分叉的。
    """
    # 章节名要按 SKILL.md 的约定写：`looks_like_markdown_report` 数的是「`# 章节名`」
    # 的命中数（至少两个），自造的小标题过不了那道闸。
    markdown = (
        "# 变更理解\n\n"
        "改了道具表。\n\n"
        "# 风险评估\n\n"
        "- 表结构没变，风险低。\n"
    )
    client = CountingClient(_ask(), markdown, **EXPENSIVE)

    outcome = _limited(client)

    assert outcome.status == STATUS_DEGRADED, outcome.error_message
    assert outcome.report_markdown.strip() == markdown.strip(), "正文没有被当成报告留下"
    last = outcome.rounds[-1]
    # 状态与备注必须自洽：正文那一档记 `unparsable`，而备注不许说「拿到了结论」。
    assert last.status == "unparsable", last.status
    assert "补发" in last.note
    assert "正文" in last.note, last.note
    assert "拿到了结论" not in last.note, last.note

    # 反向两档：结构化 final 那句照旧；什么都没拿回来时说的是「仍未拿到结论」。
    final_client = CountingClient(_ask(), _final(_anomaly()), **EXPENSIVE)
    assert "拿到了结论" in _limited(final_client).rounds[-1].note
    empty_client = CountingClient(_ask(), "嗯，我知道了。", **EXPENSIVE)
    empty_outcome = _limited(empty_client)
    assert empty_outcome.rounds[-1].status == "unparsable"
    assert "仍未拿到结论" in empty_outcome.rounds[-1].note


def test_a_transport_failure_on_the_wrap_up_keeps_the_rounds():
    """补发那一次撞上传输故障 → 按原来的失败收，但**已有的轮次一条都不丢**。

    这是「兜底不引入新的失败模式」的反面：兜底失败只能是**没补上**，不能让一次跑过的
    分析看起来像没跑过。那一条记录没有用量（调用没成），所以它不许挂上半张账。
    """
    client = FailingClient([_ask()], ok_calls=1, usage=EXPENSIVE)

    outcome = _limited(client)

    assert outcome.status == STATUS_FAILED
    assert len(client.calls) == 2, "没有补发"
    assert [record.index for record in outcome.rounds] == [1, 2]
    assert outcome.rounds[0].status == "requests"
    assert outcome.rounds[1].prompt_tokens is None, "没发成的那一次不该挂上半张账"
    assert "ConnectionError" in outcome.error_message
    assert "已补发一次收尾调用" in outcome.error_message


def test_a_transport_failure_is_not_a_quota_and_gets_no_extra_call():
    """反向的一半：**传输失败不算「额度用尽」**，不补收尾。

    它在循环里当场就返回了（`RoundRecord(..., "transport_error")` 那一条），而兜底那张
    名单**只有三种额度用尽** —— 补发一次既救不回来（同样的端点、同样的故障），也不该花。
    """
    client = FailingClient([_ask()], ok_calls=1)

    outcome = _run(client)  # 不设单次上限：第 2 轮是被故障打断的，不是被钱拦下的

    assert outcome.status == STATUS_FAILED
    assert len(client.calls) == 2
    assert client.calls[1][-1]["content"] != build_wrap_up_hint()
    assert "已补发一次收尾调用" not in outcome.error_message



# ==========================================================================
# 三、那一次调用要记在账上；不该补的时候一次都不发
# ==========================================================================


def test_the_extra_call_is_booked_on_both_accounts():
    """补发那一次**照样记账**：单次账本与整次运行的总账都要涨。

    不记单次账 = 平台自己报的预算数字是错的；不记总账 = 用量统计少算一次调用
    （而它恰恰是平台破例多花的那一笔）。
    """
    from services.ai.budget_gate import SingleRunBudget

    client = CountingClient(_ask(), _final(_anomaly()), **EXPENSIVE)
    budget = SingleRunBudget(
        total_token_budget=1_000, round_reserve_tokens=200, report_reserve_tokens=100
    )

    outcome = _limited(client, single_run_budget=budget)

    # 上游每次都报 500 + 400：两次调用都记上了（第 1 轮 + 补发那一次）。
    assert budget.spent_tokens == 1_800, budget.spent_tokens
    assert outcome.prompt_tokens == 1_000
    assert outcome.completion_tokens == 800


def test_a_normal_run_pays_for_nothing_extra():
    """反向的一半：**跑出结论的运行一分钱不多花**。

    没有这条，「每一轮都补一次」也能让上面几条全绿 —— 而那会把每次分析的成本推高。
    """
    client = ScriptedClient(_final(_anomaly()))

    outcome = _run(client)

    assert outcome.status == STATUS_SUCCEEDED
    assert len(client.calls) == 1
    assert build_wrap_up_hint() not in "\n".join(
        item["content"] for item in client.calls[0]
    )


def test_only_quota_exhaustion_with_evidence_is_worth_an_extra_call():
    """`should_request_final_answer` 的取值域（这一条的判据是**枚举**，不是算术）。

    * 协议错 / 上下文超长 / 传输失败 → **不补**：那些情形再问一次也救不回来；
    * 手上什么都没有（上限连第一轮都不够）→ **不补**：那是在一次根本没起跑的运行上再花一笔。
    """
    assert should_request_final_answer(
        degradation=DEGRADE_BUDGET, rounds=[object()], seen_items=[]
    )
    assert should_request_final_answer(
        degradation=DEGRADE_ROUNDS, rounds=[], seen_items=[object()]
    )
    assert should_request_final_answer(
        degradation=DEGRADE_REQUESTS, rounds=[object()], seen_items=[object()]
    )
    for other in (DEGRADE_PROTOCOL, "", "transport_error", "context_overflow"):
        assert not should_request_final_answer(
            degradation=other, rounds=[object()], seen_items=[object()]
        ), other
    for quota in (DEGRADE_BUDGET, DEGRADE_ROUNDS, DEGRADE_REQUESTS):
        assert not should_request_final_answer(
            degradation=quota, rounds=[], seen_items=[]
        ), quota
