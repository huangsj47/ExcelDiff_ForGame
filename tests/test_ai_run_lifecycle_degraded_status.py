# -*- coding: utf-8 -*-
"""`run.status` 不承认 degraded：降级落库后长得和「完整跑完」一模一样。

## 缺陷形态（实测）

`_persist_outcome` 里只有一句：

    run.status = "failed" if outcome.status == STATUS_FAILED else "succeeded"

于是引擎的三种终态（succeeded / degraded / failed）在库里的 `status` 列上只剩两种。
实测库里 13 条完成运行里 **12 条** `response_payload.status='degraded'`，而 `status`
列全是 `succeeded` —— 「这次是降级交付」这件事在**列**上读不出来，只能去解 payload
那坨大文本。任何按 `status` 写的 SQL（用量面板的筛选、历史列表、基线挑选）都看不见它。

## 这一组守什么（**只守写侧**）

1. 三种终态在列上**原生**分得开；
2. 降级原因用一个**短**列记下来（`degradation`），不把 payload 里那段文字复制一份 ——
   payload 是整份大文本，为了一个枚举值去解析它不该是常态；
3. **失败的语义一个字都不许变**：失败仍然清空结论字段；
4. 降级的运行**照样是「有结论」**：`conclusion_structured` 仍按「有没有结构化 payload」
   判 —— 它此前被那句 `if run.status == "succeeded"` 顺带算对了，改了 status 之后
   如果不同步改，所有降级运行会在**基线**上静默消失（下一轮把上轮报过的问题全部
   当新发现重报）。

读侧（`/latest`、历史列表、导出、用量面板、`_is_run_fresh`）**不在这里**：它们各有
归属，见本次改动的交接说明。
"""
from __future__ import annotations

import uuid

import pytest

from app import app as flask_app
from app import create_tables, db
from models import Project
from models.ai_analysis import AiAnalysisRun, AiWeeklyAnalysisState
from services.ai.engine import (
    DEGRADE_MARKDOWN,
    DEGRADE_REQUESTS,
    STATUS_DEGRADED,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    EngineOutcome,
)
from services.ai_analysis_service import _persist_outcome


@pytest.fixture
def ctx():
    with flask_app.app_context():
        create_tables()
        yield


def _project() -> Project:
    project = Project(code=f"DG{uuid.uuid4().hex[:8]}", name=f"降级{uuid.uuid4().hex[:6]}")
    db.session.add(project)
    db.session.flush()
    return project


def _run(project_id: int) -> AiAnalysisRun:
    run = AiAnalysisRun(
        project_id=project_id,
        target_type="weekly",
        target_id=1,
        target_key=f"g-{uuid.uuid4().hex[:8]}",
        status="running",
        scope="full",
        trigger_source="manual",
    )
    db.session.add(run)
    db.session.flush()
    return run


def _outcome(status: str, **kwargs) -> EngineOutcome:
    return EngineOutcome(status=status, rounds=(), report_markdown="# 报告\n", **kwargs)


def _cleanup(run_ids) -> None:
    for run_id in run_ids:
        AiAnalysisRun.query.filter_by(id=run_id).delete()
    db.session.commit()


# ==========================================================================
#  一、三种终态在列上分得开
# ==========================================================================


def test_a_degraded_run_is_stored_as_degraded(ctx):
    """**核心断言**：降级不许再落成 succeeded。

    原先那句 `else "succeeded"` 把「跑完了」与「跑完整了」合成一个值，于是
    「这 13 条里 12 条是降级」在列上完全看不出来。
    """
    project = _project()
    run = _run(project.id)
    _persist_outcome(
        run,
        _outcome(STATUS_DEGRADED, degradation=DEGRADE_REQUESTS,
                 error_message="上下文索取额度用尽"),
        {"anomalies": [], "status": STATUS_DEGRADED},
    )
    assert run.status == "degraded", (
        f"降级被落成了 {run.status!r} —— 按 status 写的任何查询都看不见它"
    )
    assert run.degradation == DEGRADE_REQUESTS, "降级原因没记下来"
    _cleanup([run.id])


def test_a_succeeded_run_stays_succeeded(ctx):
    """没降级的**不许**被误标成降级（那会让「降级率」这个指标变成噪音）。"""
    project = _project()
    run = _run(project.id)
    _persist_outcome(run, _outcome(STATUS_SUCCEEDED), {"anomalies": []})

    assert run.status == "succeeded"
    assert run.degradation is None, "没降级却记了一个降级原因"
    _cleanup([run.id])


def test_a_failed_run_stays_failed_and_keeps_its_empty_conclusion(ctx):
    """失败的语义一个字都不许变：没有结论、只有原因。"""
    project = _project()
    run = _run(project.id)
    _persist_outcome(
        run,
        _outcome(STATUS_FAILED, error_message="上游把连接掐了"),
        {"anomalies": [], "risk_level": "high"},
    )

    assert run.status == "failed"
    assert run.response_payload is None, "失败的运行不该有结论 payload"
    assert run.response_text == ""
    assert run.conclusion_structured is None
    assert "上游把连接掐了" in (run.error_message or "")
    _cleanup([run.id])


def test_the_degradation_reason_is_a_code_not_a_copy_of_the_payload(ctx):
    """降级原因用**短标识**记，不复制 payload 里那段给人看的文字。

    这一列存在的理由是「能按原因分组计数」（这一周有多少次是分片没跑成），
    它不该是一段话。
    """
    project = _project()
    run = _run(project.id)
    payload = {
        "anomalies": [],
        "status": STATUS_DEGRADED,
        "degradation": DEGRADE_REQUESTS,
        "degradation_label": "上下文索取额度用尽，基于已有证据出结论",
    }
    _persist_outcome(
        run, _outcome(STATUS_DEGRADED, degradation=DEGRADE_REQUESTS), payload
    )

    assert run.degradation == DEGRADE_REQUESTS
    assert len(run.degradation) <= 40, "这一列是给分组计数用的，不该塞一段话"
    # 给人看的那句话仍在 payload 里（**只有一份**，不在这边再抄一遍）。
    assert "上下文索取额度用尽" not in (run.degradation or "")
    _cleanup([run.id])


# ==========================================================================
#  二、降级照样是「有结论」（不然基线会静默清空）
# ==========================================================================


def test_a_degraded_run_with_a_payload_is_still_a_structured_conclusion(ctx):
    """**回归守卫。** `conclusion_structured` 算的是「有没有结构化结论」，
    不是「status 是不是 succeeded」。

    它此前靠 `if run.status == "succeeded"` 顺带算对了 —— status 一改，这个条件
    对降级就变成假，于是**所有降级运行在基线上消失**：下一轮读到的基线是「共 0 条」，
    上一批已知问题会被全部当成新发现重报一遍，而没有任何地方说得出为什么。
    """
    project = _project()
    run = _run(project.id)
    _persist_outcome(
        run,
        _outcome(STATUS_DEGRADED, degradation=DEGRADE_REQUESTS, payload=object()),
        {"anomalies": [], "status": STATUS_DEGRADED},
    )
    assert run.conclusion_structured is True, (
        "有结构化 payload 的降级运行被判成「没有结论」—— 它会从基线上消失"
    )
    _cleanup([run.id])


def test_a_markdown_only_degraded_run_is_not_a_structured_conclusion(ctx):
    """只有 markdown 的那种降级（`DEGRADE_MARKDOWN`）**仍然不能当基线**。

    它的 payload 是空的 —— 一条结构化结论都没有。这一条与上一条是同一个判据的两面，
    改动时只能一起动。
    """
    project = _project()
    run = _run(project.id)
    _persist_outcome(
        run,
        _outcome(STATUS_DEGRADED, degradation=DEGRADE_MARKDOWN, payload=None),
        {"anomalies": [], "status": STATUS_DEGRADED},
    )
    assert run.conclusion_structured is False, (
        "只有 markdown 的降级运行被判成有结构化结论 —— 下一轮的基线是空的"
    )
    _cleanup([run.id])


# ==========================================================================
#  三、水位线仍然按引擎状态判（status 改了不许把它带偏）
# ==========================================================================


def test_the_weekly_watermark_still_uses_the_engine_status(ctx):
    """水位线的判据是引擎给的 `engine_status`，与 `run.status` 无关。

    这条是**防手滑**的：`_update_weekly_state` 曾经写的是 `if run.status != "succeeded"`，
    而那句话在降级上永远为假。status 现在能原生表示 degraded 了，看起来「终于可以
    直接读 status」—— 但**不能**：降级运行的水位线判据必须仍然来自引擎。
    这里用一条 status="degraded" 的 run 配 `engine_status="degraded"` 走一遍，
    水位线必须原地不动。
    """
    from datetime import datetime

    project = _project()
    run = _run(project.id)
    run.status = "degraded"
    start, end = datetime(2026, 3, 1), datetime(2026, 3, 8)
    state = AiWeeklyAnalysisState(
        project_id=project.id, group_key=run.target_key, base_name="W",
        start_time=start, end_time=end, last_analyzed_at=None,
    )
    db.session.add(state)
    db.session.commit()

    payload = {
        "group": {
            "project_id": project.id, "key": run.target_key, "base_name": "W",
            "start_time": start.isoformat(), "end_time": end.isoformat(),
        },
        "summary": {"total_files": 3},
    }
    from services.ai_analysis_service import _update_weekly_state

    _update_weekly_state(payload, run, state, engine_status=STATUS_DEGRADED)
    assert state.last_analyzed_at is None, (
        "status 能表示 degraded 之后，水位线被改成读 status 了 —— 降级照样推进了水位线"
    )
    AiWeeklyAnalysisState.query.filter_by(id=state.id).delete()
    db.session.commit()
    _cleanup([run.id])
