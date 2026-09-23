# -*- coding: utf-8 -*-
"""四种输入并排 + 逐请求诊断的读取侧（工作包 F 的 1/2/3/4 条的落点）。

## 这一组守的是什么

用量页原先只有「输入 token」「其中命中缓存」「输出 token」三格，而**「未命中输入」**
（真正按未命中价计费的那一档）只藏在副标题里、**推理 token** 在逐轮账上根本没有数。
四格并排之后，「这次到底花了多少钱、花在哪一档」才一眼看得出。

## 口径是这一组的主线（指引专列了一节）

上游的 `prompt_tokens` **含**缓存命中，平台存的 `tokens_input` 就是它（两边同口径）。
所以**未命中 = 总输入 − 缓存输入**，只减这一次 —— 跨系统对照时最容易犯的错就是拿
对面那套（已经把命中减掉的）`inputTokens` 当总输入再减一遍。

`None`（上游没报）与 `0`（报了且确实是 0）在**每一格**上都必须是两个值：
把它们合并，界面上就会出现一个确定的「未命中 0 tokens」，而真相是「不知道」。
"""
from __future__ import annotations

import json

import pytest

from app import app as flask_app
from app import create_tables, db
from models.ai_analysis import AiAnalysisRun, AiAnalysisTrace
from services.ai import round_events, run_progress
from services.ai.request_fingerprint import RoundDiagnostics
from services.ai.round_diagnostics import input_breakdown
from services.ai_usage_service import run_usage
from tests.test_ai_round_event_ledger import _drop, _frame, _make_run


@pytest.fixture(scope="module", autouse=True)
def _tables():
    with flask_app.app_context():
        create_tables()


@pytest.fixture(autouse=True)
def _app_context():
    with flask_app.app_context():
        yield


@pytest.fixture(autouse=True)
def _clean_state():
    run_progress.reset_for_tests()
    yield
    run_progress.reset_for_tests()


# ==========================================================================
#  一、纯函数：四格的口径
# ==========================================================================


def test_uncached_input_is_the_total_minus_the_cache_hit():
    """未命中 = 总输入 − 缓存输入（**只减一次**）。"""
    breakdown = input_breakdown(tokens_input=1_000, cache_read=400, tokens_output=50)

    assert breakdown["total_input"] == 1_000
    assert breakdown["cached_input"] == 400
    assert breakdown["uncached_input"] == 600, (
        "上游的 prompt_tokens 含命中，减第二遍会得出一个偏小的未命中数"
    )
    assert breakdown["output"] == 50


def test_an_unreported_addend_makes_the_difference_unknown():
    """两个加数任一没上报 → 未命中**也是 `None`**（不是 0）。"""
    assert input_breakdown(tokens_input=1_000, cache_read=None, tokens_output=50)[
        "uncached_input"
    ] is None
    assert input_breakdown(tokens_input=None, cache_read=400, tokens_output=50)[
        "uncached_input"
    ] is None


def test_zero_is_a_value_and_survives_as_zero():
    """上游明确报了 0（这次一次都没命中）→ 未命中就是 **0**（不是 `None`）。"""
    breakdown = input_breakdown(tokens_input=1_000, cache_read=0, tokens_output=0)

    assert breakdown["uncached_input"] == 1_000
    assert breakdown["output"] == 0, "报了 0 就是 0 —— 不许折成未上报"


def test_reasoning_is_none_when_the_upstream_never_reported_it():
    """推理那一格：上游一轮都没报 → `None`（界面显示「未上报」），**不是 0**。

    0 与「未上报」在这里的含义正好相反：前者是「确实没有隐藏推理」，后者是
    「输出 token 花在写还是想，这一栏答不出来」。
    """
    breakdown = input_breakdown(tokens_input=10, cache_read=0, tokens_output=5)

    assert breakdown["reasoning"] is None
    assert breakdown["reasoning_reported_rounds"] == 0
    assert input_breakdown(
        tokens_input=10, cache_read=0, tokens_output=5, reasoning=0,
        reasoning_reported_rounds=2, reasoning_rounds=2,
    )["reasoning"] == 0


# ==========================================================================
#  二、落库：新诊断落在逐轮事件账本上（不另建表）
# ==========================================================================


def _diagnostics(round_index: int = 1, *, chained: bool = True, **overrides) -> dict:
    """一帧里的**诊断值**（形状 = `RoundDiagnostics.event_fields` 的产物）。

    真跑一遍 `RoundDiagnostics` 拿那一份字典，而不是手抄一份键名：手抄的那一份与生产
    那份一旦分叉，这些用例会**照样绿**（它们验的是列映射，抄错一个键名就验不到真东西）。
    指纹也是真的（`before_call` 真算哈希）—— 这样 `prefix_common_messages` 之类的值
    不是编的。
    """
    diagnostics = RoundDiagnostics(
        prompt_version="prompt-x", snapshot_id="tip-x",
        model="m1", endpoint="https://gateway.internal/v1",
    )
    diagnostics.begin_round()
    if chained:
        # 有「上一个请求」→ 这一轮是 append（公共前缀 1 条，真算出来的）。
        diagnostics.fingerprints.before_call([{"role": "system", "content": "规则"}])
        messages = [{"role": "system", "content": "规则"}, {"role": "user", "content": "清单"}]
    else:
        messages = [{"role": "system", "content": "规则"}]  # 第一次调用 → initial
    diagnostics.fingerprint = diagnostics.fingerprints.before_call(messages)
    diagnostics.model_call_ms = 12_345
    diagnostics.tool_fetch_ms = 678
    diagnostics.index_build_ms = 90
    diagnostics.reasoning_tokens = 31
    diagnostics.usage_source = "prompt_cache_hit_tokens"
    diagnostics.reasoning_source = "completion_tokens_details.reasoning_tokens"
    fields = diagnostics.event_fields(round_index)
    fields.update(overrides)
    return fields


def test_the_ledger_column_whitelist_matches_the_diagnostics_keys():
    """事件账本那份列名白名单与 `RoundDiagnostics.event_fields` 的键**逐字相同**。

    两份键名各写一处、谁也不检查谁，表现是「界面上那几列永远是空的」——**而且不报错**。
    这一条把两份钉在一起：`event_fields` 少一个键（白名单里的列取不到值）或多一个键
    （写不进库）都会在这里红。
    """
    assert set(round_events._DIAGNOSTIC_COLUMNS) == set(
        RoundDiagnostics().event_fields(1)
    ), "列名白名单与 event_fields 的键分叉了"
    # 而且这些键在模型上真的都是列（拼错一个名字就只能写进实例属性、进不了库）。
    from models.ai_analysis import AiAnalysisRoundEvent

    columns = {column.name for column in AiAnalysisRoundEvent.__table__.columns}
    missing = set(round_events._DIAGNOSTIC_COLUMNS) - columns
    assert not missing, f"这些键在事件表上没有对应的列：{sorted(missing)}"


def _row(run_id: int) -> object:
    from models.ai_analysis import AiAnalysisRoundEvent

    return (
        AiAnalysisRoundEvent.query.filter_by(run_id=run_id)
        .order_by(AiAnalysisRoundEvent.round.asc())
        .first()
    )


def test_the_diagnostic_columns_land_on_the_round_event_ledger():
    """一次 `publish` 就能把指纹 / 推理 token / 三类耗时写进**逐轮事件账本**。

    「不另建表」是硬要求：新数据必须落在工作包 E 那一张上，写入口仍然是同一个
    （`round_events.record`）。
    """
    runner = _make_run()
    try:
        frame = _frame(1, member="S1", member_index=1, member_total=1, tokens_in=100, tokens_out=20)
        frame.round_diagnostics = _diagnostics()

        run_progress.publish(runner.run_id, runner.project_id, frame)

        row = _row(runner.run_id)
        assert row is not None, "事件行没写进去"
        fields = frame.round_diagnostics
        assert row.reasoning_tokens == 31
        assert row.usage_source == "prompt_cache_hit_tokens"
        assert row.reasoning_source == "completion_tokens_details.reasoning_tokens"
        assert row.model_call_ms == 12_345
        assert row.tool_fetch_ms == 678
        assert row.index_build_ms == 90
        assert row.request_fingerprint == fields["request_fingerprint"]
        assert row.stable_prefix_fingerprint == fields["stable_prefix_fingerprint"]
        assert row.prefix_common_messages == 1, "上面那条 system 是公共前缀（真算出来的）"
        assert row.prefix_common_chars == len("规则")
        assert row.prefix_divergence_reason == "append"
        payload = json.loads(row.fingerprint_json)
        assert payload["prompt_version"] == "prompt-x"
        assert payload["snapshot_id"] == "tip-x"
        assert payload["model"] == "m1"
        assert payload["endpoint"] == "https://gateway.internal/v1"
        assert "你是" not in row.fingerprint_json, "诊断数据里不许有提示词正文"
    finally:
        _drop(runner)


def test_a_frame_without_diagnostics_leaves_the_columns_null():
    """老帧（没有诊断的那一份）→ 新列全是 NULL，**不是 0**。"""
    runner = _make_run()
    try:
        run_progress.publish(
            runner.run_id, runner.project_id,
            _frame(1, tokens_in=100, tokens_out=20),
        )

        row = _row(runner.run_id)
        assert row.reasoning_tokens is None
        assert row.model_call_ms is None
        assert row.tool_fetch_ms is None
        assert row.index_build_ms is None
        assert row.request_fingerprint is None
        assert row.prefix_common_messages is None
        assert row.prefix_divergence_reason is None
        assert row.fingerprint_json is None
    finally:
        _drop(runner)


def test_the_member_account_sums_reasoning_but_keeps_none_when_nobody_reported():
    """逐成员的账：推理 token 求和，**一轮都没报过就是 `None`**（不是 0）。"""
    runner = _make_run()
    try:
        for index, reasoning in ((1, 10), (2, 5)):
            frame = _frame(index, member="S1", member_index=1, member_total=1,
                           tokens_in=100, tokens_out=20)
            frame.round_diagnostics = _diagnostics(reasoning_tokens=reasoning)
            run_progress.publish(runner.run_id, runner.project_id, frame)
        totals = round_events.member_totals(runner.run_id)
        assert totals[0]["reasoning_tokens"] == 15
        assert totals[0]["reasoning_reported"] is True

        # 再换一个成员（一轮都没报）→ 它是 `None`，不是 0。
        frame = _frame(1, member="S2", member_index=2, member_total=2, tokens_in=1, tokens_out=1)
        run_progress.publish(runner.run_id, runner.project_id, frame)
        by_member = {item["member"]: item for item in round_events.member_totals(runner.run_id)}
        assert by_member["S2"]["reasoning_tokens"] is None, "没报过就是 None（未上报）"
        assert by_member["S2"]["reasoning_reported"] is False
    finally:
        _drop(runner)


# ==========================================================================
#  三、读取侧：`/runs/<id>/usage` 的四格与逐轮诊断
# ==========================================================================


def _trace_row(run_id: int, round_index: int, **overrides) -> AiAnalysisTrace:
    values = {
        "tokens_input": 1_000,
        "tokens_output": 200,
        "cache_read_tokens": 400,
        "request_chars": 900,
        "context_chars": 500,
        "duration_ms": 1_000,
    }
    values.update(overrides)
    row = AiAnalysisTrace(run_id=run_id, round_index=round_index, outcome="requests", **values)
    db.session.add(row)
    db.session.flush()
    return row


def test_the_run_detail_exposes_the_four_inputs_side_by_side():
    """一次运行的明细里，四格都在：总输入 / 未命中 / 缓存输入 / 输出（含推理）。"""
    runner = _make_run(status="succeeded")
    try:
        run = db.session.get(AiAnalysisRun, runner.run_id)
        run.tokens_input = 1_000
        run.tokens_output = 200
        run.cache_read_tokens = 400
        db.session.commit()
        _trace_row(runner.run_id, 1)

        frame = _frame(1, tokens_in=1_000, tokens_out=200)
        frame.round_diagnostics = _diagnostics(reasoning_tokens=40)
        run_progress.publish(runner.run_id, runner.project_id, frame)

        payload = run_usage(runner.run_id)

        breakdown = payload["input_breakdown"]
        assert breakdown["total_input"] == 1_000, "总输入含命中（与上游 prompt_tokens 同口径）"
        assert breakdown["uncached_input"] == 600, "未命中只减一次"
        assert breakdown["cached_input"] == 400
        assert breakdown["output"] == 200
        assert breakdown["reasoning"] == 40
        assert breakdown["reasoning_reported_rounds"] == 1
        assert breakdown["reasoning_rounds"] == 1
    finally:
        _drop(runner)


def test_an_unreported_breakdown_stays_none_end_to_end():
    """上游什么都没报（这一功能上线前的老运行）→ 四格全是 `None`。

    界面据此显示「未上报」，**不是 0**：0 会把「不知道花了多少」说成「没花钱」。
    """
    runner = _make_run(status="succeeded")
    try:
        _trace_row(
            runner.run_id, 1,
            tokens_input=None, tokens_output=None, cache_read_tokens=None,
        )

        breakdown = run_usage(runner.run_id)["input_breakdown"]

        assert breakdown["total_input"] is None
        assert breakdown["uncached_input"] is None
        assert breakdown["cached_input"] is None
        assert breakdown["output"] is None
        assert breakdown["reasoning"] is None
        assert breakdown["reasoning_reported_rounds"] == 0
    finally:
        _drop(runner)


def test_the_round_rows_keep_their_shape_and_the_diagnostics_live_next_to_them():
    """逐轮那一行**不多一个键**；诊断值摆在 `round_diagnostics` 里（按轮次对上）。

    为什么一个键都不能多：`rounds[i]` 的形状必须与「跑的过程中」那一份
    （`trace_evidence.live_round_entry`）**逐字相同** —— 同一个「思考过程」面板两条
    来路，键不一样就会有两种真相，那条契约有测试钉着
    （`tests/test_ai_live_thinking_snapshot.py`）。所以诊断值走旁边那一块。

    而两个来源的轮次口径不同（trace 是家族全局序号，事件账本是成员内序号），
    读取侧按位置对齐、再按 `(分片, 成员内轮次)` 复核（见
    `ai_usage_service._diagnostics_for`）。
    """
    runner = _make_run(status="succeeded")
    try:
        _trace_row(runner.run_id, 1, agent="S1", agent_round=1)
        _trace_row(runner.run_id, 2, agent="S1", agent_round=2,
                   tokens_input=2_000, cache_read_tokens=1_500)
        for index in (1, 2):
            frame = _frame(index, member="S1", member_index=1, member_total=2,
                           tokens_in=1_000 * index, tokens_out=20)
            frame.round_diagnostics = _diagnostics(
                index,
                chained=index > 1,
                reasoning_tokens=10 * index,
                model_call_ms=111 * index,
            )
            run_progress.publish(runner.run_id, runner.project_id, frame)

        payload = run_usage(runner.run_id)
        rounds = payload["rounds"]
        diag = payload["round_diagnostics"]

        # 那一行**不多键**（多一个键就与实时那一份分叉了，见本用例的 docstring）。
        for extra in ("uncached_input", "reasoning_tokens", "model_call_ms",
                      "tool_fetch_ms", "request_fingerprint", "usage_source"):
            assert extra not in rounds[0], (
                f"`rounds[i]` 上多了 {extra} —— 它必须与实时那一份逐字同形（诊断走旁边那块）"
            )

        first, second = diag["1"], diag["2"]
        assert first["uncached_input"] == 600, "1000 − 400"
        assert second["uncached_input"] == 500, "2000 − 1500"
        assert first["reasoning_tokens"] == 10
        assert second["reasoning_tokens"] == 20
        assert first["model_call_ms"] == 111
        assert second["model_call_ms"] == 222
        # 第 1 轮是 `initial`（没有可比的上一请求，公共前缀是 `None` 而不是 0），
        # 第 2 轮是 `append` 且公共前缀是 1 条 —— 都是真算出来的值（不是编的）。
        assert first["request_fingerprint"]["prefix_divergence_reason"] == "initial"
        assert first["request_fingerprint"]["prefix_common_messages"] is None
        assert second["request_fingerprint"]["prefix_divergence_reason"] == "append"
        assert second["request_fingerprint"]["prefix_common_messages"] == 1
        assert second["request_fingerprint"]["prefix_common_chars"] == len("规则")
        assert (
            second["request_fingerprint"]["request_fingerprint"]
            != first["request_fingerprint"]["request_fingerprint"]
        )
    finally:
        _drop(runner)


def test_a_round_whose_event_is_missing_gets_no_diagnostics_at_all():
    """事件账本缺了一行 → **后面的轮次不许串位**（宁可这一格空着）。

    位置对齐是「两个来源的遍历顺序相同」这条契约换来的。缺行会让位置号整体错位，
    那时把 S1 第 3 轮的指纹安到 S4 第 1 轮头上，比这一格空着难查得多。
    """
    runner = _make_run(status="succeeded")
    try:
        _trace_row(runner.run_id, 1, agent="S1", agent_round=1)
        _trace_row(runner.run_id, 2, agent="S4", agent_round=1)
        # 事件账本里第 1 条是 S1 的第 1 轮，第 2 条却是 S4 的第 2 轮（缺了 S4 的第 1 轮）
        first = _frame(1, member="S1", member_index=1, member_total=2, tokens_in=1, tokens_out=1)
        first.round_diagnostics = _diagnostics(reasoning_tokens=7)
        run_progress.publish(runner.run_id, runner.project_id, first)
        second = _frame(2, member="S4", member_index=4, member_total=4, tokens_in=1, tokens_out=1)
        second.round_diagnostics = _diagnostics(reasoning_tokens=9)
        run_progress.publish(runner.run_id, runner.project_id, second)

        diag = run_usage(runner.run_id)["round_diagnostics"]

        assert diag["1"]["reasoning_tokens"] == 7, "第 1 轮对得上，照给"
        assert diag["2"]["reasoning_tokens"] is None, (
            "分片标签与成员内轮次对不上 —— 不许把别轮的诊断安到这一轮上"
        )
        assert diag["2"]["request_fingerprint"] is None
    finally:
        _drop(runner)


def test_a_run_without_any_event_row_still_gets_one_entry_per_round():
    """一条事件行都没有（老运行）→ 每一轮仍有一条诊断格，里面全是 `None`。

    「这一轮有没有诊断」不去猜键在不在：界面按 `round_index` 查一下就知道 ——
    查不到与查到一份全 `None`，在界面上说同一句话（未上报），而**都不是 0**。
    """
    runner = _make_run(status="succeeded")
    try:
        _trace_row(runner.run_id, 1, tokens_input=1_000, cache_read_tokens=400)

        diag = run_usage(runner.run_id)["round_diagnostics"]["1"]

        assert diag["reasoning_tokens"] is None
        assert diag["model_call_ms"] is None
        assert diag["request_fingerprint"] is None
        assert diag["usage_source"] == "", "来源标记缺失是空串（没读到），不是编一个名字"
        assert diag["uncached_input"] == 600, "未命中是 trace 上算出来的，与有没有事件行无关"
    finally:
        _drop(runner)


# ==========================================================================
#  四、界面：四格并排 + 逐轮诊断（真跑模板里那段脚本）
# ==========================================================================


def test_the_dashboard_renders_the_four_inputs_and_the_diagnostic_lines():
    """**真跑**模板里那段脚本：诊断行要说得出来分叉原因、耗时构成与推理。

    静态断言只看得见字符串，而这里要断言的是「算出来的那几行长什么样」。查表那一步
    （`aiuRoundDiag`）也真跑：诊断值不在 `rounds[i]` 上，而在 `round_diagnostics`
    那一块里（见 `run_usage` 的 docstring 与 `test_the_round_rows_keep_their_shape…`）。
    """
    from tests.test_ai_usage_budget_ui import _dashboard_script, _run_node

    script = _dashboard_script()
    out = """function (A, sandbox) {
        var table = A.round_diagnostics;
        return {lines: A.rounds.map(function (round) {
            return sandbox.aiuRoundDiagnosticLines(sandbox.aiuRoundDiag(round, table));
        })};
    }"""
    cases = {
        "rounds": [{"round_index": 1}, {"round_index": 2}, {"round_index": 3}, {"round_index": 9}],
        "round_diagnostics": {
            "1": {
                "request_fingerprint": {
                    "request_fingerprint": "abc123", "stable_prefix_fingerprint": "def456",
                    "prefix_common_messages": 6, "prefix_common_chars": 12345,
                    "prefix_divergence_reason": "append", "message_count": 8,
                    "request_chars": 20000, "prompt_version": "prompt-x",
                    "snapshot_id": "tip-y", "model": "m1", "endpoint": "https://h/v1"
                },
                "reasoning_tokens": 31, "reasoning_source": "completion_tokens_details.reasoning_tokens",
                "model_call_ms": 61000, "tool_fetch_ms": 250, "index_build_ms": 900,
            },
            # 老运行：没有诊断（指纹那一格是 null）→ 一行都不该编出来。
            "2": {"request_fingerprint": None, "reasoning_tokens": None, "reasoning_source": "",
                  "model_call_ms": None, "tool_fetch_ms": None, "index_build_ms": None},
            # 上游没报推理，但报了别的东西 —— 只说得出耗时，不说分叉。
            "3": {"request_fingerprint": None, "reasoning_tokens": None,
                  "usage_source": "", "model_call_ms": 1200},
            # 第 9 轮**连一条诊断格都没有**（事件行对不上）→ 与「有一份全 None」同款：
            # 一行都不编。
        }
    }
    result = _run_node(
        script, cases,
        probe_source="__probeLines = aiuRoundDiagnosticLines; __probeDiag = aiuRoundDiag;",
        probe_name="",
        out_source=out,
    )
    first, second, third, missing = result["lines"]

    assert any("追加" in line for line in first), first
    assert any("6 条消息 / 12,345 字" in line for line in first), first
    assert any("模型调用 1.0 min" in line for line in first), first
    assert any("本地取数 250 ms" in line for line in first), first
    assert any("其中推理 token 31" in line for line in first), first
    # **哈希不是命中率** 这句话必须跟着这些数字出现在界面上。
    assert any("哈希相同" in line and "不保证缓存命中" in line for line in first), first

    assert second == [], f"没有诊断值的轮次不该编出一行：{second}"
    assert third and all("分叉原因" not in line for line in third), third
    assert any("模型调用 1.2 s" in line for line in third), third
    assert missing == [], f"查不到诊断格的轮次同样一行都不编：{missing}"


def test_the_dashboard_has_the_four_input_tiles():
    """四格并排（总输入 / 未命中输入 / 缓存输入 / 输出）在同一个 KPI 网格里。

    它们是**同一件事的四个面**，所以必须并排：读者要能一眼看出「未命中那一档」
    与「命中那一档」各多少 —— 分开放就又要自己做一次减法，而那个减法是这里最容易
    算错的一步（跨系统对照时对面那套已经减过了）。
    """
    from tests.test_ai_usage_budget_ui import _dashboard_script

    script = _dashboard_script()
    start = script.index("var breakdown = payload.input_breakdown || {};")
    block = script[start:script.index("grid.appendChild(kpi('合计 token'", start)]

    for label in ("'总输入 token'", "'未命中输入'", "'缓存输入（命中）'", "'输出（含推理）'"):
        assert label in block, f"四格里少了 {label}：{block[:400]}"
    # 「未上报」与「0」必须分开：没有值的那几格走 `aiuOrUnknown`（显示「未上报」），
    # 而不是被 `Number(null)` 变成 0。
    assert block.count("aiuOrUnknown(") >= 4, "四格都要走「没有就写未上报」这一支"
