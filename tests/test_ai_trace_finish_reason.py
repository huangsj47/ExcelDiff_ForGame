# -*- coding: utf-8 -*-
"""「这一轮上游是怎么停下来的」是一个**事实列**，不是错误文本的一部分。

## 这一组盯的是什么

用户报的原话是「正常分析完后，思考过程里的 `finish_reason` 不要用 stop，否则以为是
异常退出」。根因不是措辞，是**范畴错位**：写入侧把上游那个原始枚举拼进了
`ai_analysis_trace.error` 这一列（`"；".join([note, "finish_reason=…"])`），而界面把
`error` 那一列用**错误红**画出来（`.ai-think-missing`）。

三件事同时坏掉，所以三条都要钉：

1. **正常结束被画成错误**（用户看到的那一行红字）；
2. **两条读路径不一致** —— 跑动中那份来自内存（`trace_evidence`，只有 note），落库后
   那份来自这一列（note + finish_reason），于是同一张卡**刷新之后才冒出那行字**；
3. **解析方式脆弱** —— 读侧按分隔符切一个给人看的字符串。

现在：事实进 `finish_reason` 列，`error` 只剩 note；读側优先读新列、**老行回落到老办法**
（那批行不改写）；措辞由界面翻（`static/js/ai_think_log.js` 的 `FINISH`）。
"""
from __future__ import annotations

import json
import uuid

import pytest

from app import app, create_tables, db
from models import Project
from models.ai_analysis import AiAnalysisRun, AiAnalysisTrace
from services.ai.engine import STATUS_SUCCEEDED, EngineOutcome, RoundRecord
from services.ai.result_payload import result_payload
from services.ai.trace_evidence import live_round_entry
from services.ai_analysis_service import _persist_outcome
from services.ai_usage_service import run_usage


@pytest.fixture(scope="module", autouse=True)
def _tables():
    with app.app_context():
        create_tables()


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _legacy_value(error: str) -> str:
    """老格式（`…；finish_reason=length`）里那个值 —— **测试自己切一遍**。

    不调被改造的那个函数：用它当判据的话，「它坏了」与「数据本来就没有」分不开。
    """
    return next(
        (
            part.removeprefix("finish_reason=")
            for part in str(error or "").split("；")
            if part.startswith("finish_reason=")
        ),
        "",
    )


def _run_with_rounds(rounds) -> int:
    """建一条 run、跑一次真正的落库（`_persist_outcome`），返回 run id。"""
    project = Project(code=_uid("P"), name=_uid("finish"))
    db.session.add(project)
    db.session.flush()
    run = AiAnalysisRun(
        project_id=project.id,
        target_type="weekly",
        target_id=1,
        target_key=_uid("g"),
        status="running",
        scope="full",
        trigger_source="manual",
    )
    db.session.add(run)
    db.session.flush()
    outcome = EngineOutcome(
        status=STATUS_SUCCEEDED,
        report_markdown="# 变更理解\n\n正常。\n\n# 风险评估\n\n低。\n",
        rounds=tuple(rounds),
        tool_stats={},
        requests_used=len(rounds),
    )
    _persist_outcome(run, outcome, result_payload(outcome, {}))
    db.session.commit()
    return run.id


def _trace_rows(run_id: int) -> list:
    return (
        AiAnalysisTrace.query.filter_by(run_id=run_id)
        .order_by(AiAnalysisTrace.round_index)
        .all()
    )


# ==========================================================================
# 一、落库侧：事实进自己的列，`error` 只剩 note
# ==========================================================================


def test_the_finish_reason_lands_in_its_own_column_and_not_in_error():
    """`finish_reason` 写进自己的列；`error` 里**不再**出现 `finish_reason=` 的字样。

    变异：把写库那一句换回拼接（`error = "；".join([note, f"finish_reason=…"])`）→
    `error` 里又出现那几个字 → 红。
    """
    with app.app_context():
        run_id = _run_with_rounds([RoundRecord(index=1, status="final", finish_reason="stop")])

        rows = _trace_rows(run_id)
        assert len(rows) == 1
        assert rows[0].finish_reason == "stop", "结束方式没有进它自己的列"
        assert "finish_reason" not in str(rows[0].error or ""), (
            "`error` 里还拼着 finish_reason —— 界面会把它当错误文本用红字画出来，"
            "而 stop 是**正常结束**"
        )
        assert rows[0].error is None, "这一轮没有 note，error 该是空的"


def test_a_note_and_a_finish_reason_live_side_by_side():
    """同一轮既有 note 又有结束方式 → `error` 正好是那句 note，一个字不多。

    变异：`error=record.note or record.finish_reason` 之类的「兜底塞进去」→ 红。
    """
    with app.app_context():
        run_id = _run_with_rounds(
            [
                RoundRecord(
                    index=1,
                    status="unparsable",
                    note="模型这一轮没按协议返回 JSON",
                    finish_reason="length",
                )
            ]
        )

        row = _trace_rows(run_id)[0]
        assert row.error == "模型这一轮没按协议返回 JSON", row.error
        assert row.finish_reason == "length"


def test_an_unreported_finish_reason_is_null_not_an_empty_string():
    """上游没报这个字段 → 存 NULL（「没上报」与「报了个空」是两件事）。"""
    with app.app_context():
        run_id = _run_with_rounds([RoundRecord(index=1, status="final", finish_reason="")])

        row = _trace_rows(run_id)[0]
        assert row.finish_reason is None, f"没上报被写成了 {row.finish_reason!r}"


def test_the_live_view_and_the_stored_view_agree_on_error():
    """跑动中那份与落库那份的 `error` **必须是同一句话**。

    它们原先不一样：落库那份多挂一句 `finish_reason=stop`。同一张卡刷新一次就多出一行
    红字，而配置、轮次、用量都没变 —— 用户只能理解为「刚刚出了错」。

    变异：把 `trace_evidence.live_round_entry` 的 `error` 也改成拼接 → 红。
    """
    with app.app_context():
        record = RoundRecord(index=1, status="final", note="补发一次收尾调用", finish_reason="stop")
        run_id = _run_with_rounds([record])

        live = live_round_entry(record)
        stored = _trace_rows(run_id)[0]

        assert live["error"] == stored.error
        assert live["finish_reason"] == stored.finish_reason == "stop"


# ==========================================================================
# 二、读侧：新列优先，老行回落到老办法
# ==========================================================================


def _legacy_trace(run_id: int, *, error: str) -> None:
    """一条**加列之前**的行：`finish_reason` 是 NULL，那个值只存在于 `error` 里。"""
    db.session.add(
        AiAnalysisTrace(
            run_id=run_id,
            round_index=99,
            outcome="final",
            parsed_ok=True,
            error=error,
            finish_reason=None,
        )
    )
    db.session.commit()


def test_an_old_row_is_still_readable_through_the_fallback():
    """老行（`finish_reason` 是 NULL）仍要读出 `length` —— 那批行不改写。

    变异：去掉 `_finish_reason` 里的回落（只读新列）→ 老行读出空串 → 红。
    """
    with app.app_context():
        run_id = _run_with_rounds([RoundRecord(index=1, status="final", finish_reason="stop")])
        _legacy_trace(run_id, error="上游报告输出撞上单次输出上限；finish_reason=length")

        rounds = run_usage(run_id)["rounds"]
        legacy = [row for row in rounds if row["round_index"] == 99]
        assert legacy, "老行没有出现在逐轮里（前提不成立）"
        assert legacy[0]["finish_reason"] == "length", (
            "老行读不出结束方式 —— 那批已经交付过的记录会在面板上突然少一句话"
        )
        # 老行的 `error` 仍然带着那句话（不改写历史），只是读的人不再需要去切它。
        assert "上游报告输出撞上单次输出上限" in legacy[0]["error"]


def test_a_row_whose_column_is_present_never_falls_back_to_error():
    """这一列**在**（哪怕是空）就不许回落去 `error` 里翻。

    ## 这条钉的是判据，不是一条真实数据

    写入侧把空串归一成 NULL（`or None`），所以「这一列是空串」这一档**平台自己写不出来**
    —— 它只对外部直写的行可达。但判据本身要成立：回落只认「这一列没有」（NULL），
    按真值判断（`if not value`）就会把「列在、值是空」也送进回落，于是 `error` 里任何
    一个**碰巧长得像**的片段都会被当成结束方式。

    所以这里的行是**故意造成那种形状的**（`error` 就是老格式、能切出 `stop`）：不这么造，
    「不回落」与「回落了但没切到」在断言上分不开 —— 那样这条用例就什么都没证明。

    变异：`if value is not None` 改成 `if value` → 读出来是 `stop` → 红。
    """
    with app.app_context():
        run_id = _run_with_rounds([RoundRecord(index=1, status="final", finish_reason="stop")])
        db.session.add(
            AiAnalysisTrace(
                run_id=run_id,
                round_index=98,
                outcome="final",
                parsed_ok=True,
                error="模型这一轮没按协议返回；finish_reason=stop",
                finish_reason="",
            )
        )
        db.session.commit()

        rounds = {row["round_index"]: row for row in run_usage(run_id)["rounds"]}

        assert rounds[98]["finish_reason"] == "", (
            "这一列在（值是空）却被回落成了 error 里那段字样 —— 列是权威的，空就是空"
        )
        # 前提守卫：那一行**确实**切得出来（不然这条用例什么也没证明）。
        assert _legacy_value(rounds[98]["error"]) == "stop"


def test_every_round_carries_the_key_even_when_it_is_empty():
    """形状契约：逐轮那一份**永远有** `finish_reason` 这个键（空的也要在）。

    `tests/test_ai_live_thinking_snapshot.py` 与 `tests/test_ai_usage_input_breakdown.py`
    按形状读它 —— 这次只是把取值的来源从「切 error」换成「读列」，键不许消失。
    """
    with app.app_context():
        run_id = _run_with_rounds([RoundRecord(index=1, status="requests")])
        _legacy_trace(run_id, error="没有那句话")

        for row in run_usage(run_id)["rounds"]:
            assert "finish_reason" in row
            assert isinstance(row["finish_reason"], str)


def test_the_runs_own_payload_is_untouched_by_this_change():
    """这次改动只动 trace 那一张表：`response_payload`（结论）逐字没变。

    它是一份**已经交付过**的结论 —— 顺手动它会让历史报告与当初看到的不一样。
    """
    with app.app_context():
        run_id = _run_with_rounds([RoundRecord(index=1, status="final", finish_reason="stop")])
        run = db.session.get(AiAnalysisRun, run_id)
        payload = json.loads(run.response_payload or "{}")

        assert isinstance(payload, dict) and payload, "结论那一份没有落库（前提不成立）"
        assert "finish_reason" not in payload, (
            "结束方式是**逐轮明细**，不该混进这次运行的结论里"
        )


def test_the_live_view_and_the_column_use_the_same_width():
    """跑动中那一份的截断上限与这一列的宽度**是同一个数**。

    两处都写着 32，但它们在两个模块里（`trace_evidence` 是纯层，刻意不 import `models`）
    —— 所以只能靠这条断言把两份钉在一起。不一致的后果正是本次改动在修的那件事：
    「跑动中看到的」与「落库后看到的」不是同一个串（而在 MySQL 上，超过列宽的那一段
    还会直接写失败）。

    变异：`_clip(..., 32)` 改回 40，或把 `FINISH_REASON_MAX_CHARS` 改成别的数 → 红。
    """
    from models.ai_analysis.trace import FINISH_REASON_MAX_CHARS

    long_value = "x" * 80
    record = RoundRecord(index=1, status="final", finish_reason=long_value)

    with app.app_context():
        run_id = _run_with_rounds([record])

        live = live_round_entry(record)["finish_reason"]
        stored = _trace_rows(run_id)[0].finish_reason

        assert len(live) == FINISH_REASON_MAX_CHARS, (
            f"跑动中那一份的截断上限（{len(live)}）与列宽（{FINISH_REASON_MAX_CHARS}）不一致"
        )
        assert live == stored, "同一个超长值，跑动中看到的与落库后看到的不是同一个串"
