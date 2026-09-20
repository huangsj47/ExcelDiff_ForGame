# -*- coding: utf-8 -*-
"""`result_payload()`：落库与下发给界面的**那一份形状**。

## 为什么值得单独一个文件

这一份字典是**唯一的交付物**：SSE 的 `result` 事件、`/latest`、结论回放、导出、
以及用量页读的都是它。上游（`EngineOutcome`）算了什么，只要这里不产出那个键，
就等于没算过 —— 而且不会有任何报错，界面上只是那一块永远空着。

`dimensions` 就是这么丢的：解析（`protocol._coerce_dimensions`，final 必须非空）与
引擎持有（`EngineOutcome.payload`）两段都在，只有 `result_payload()` 不产出这个键。
于是「九个维度都过了一遍」这句保证**只活在提示词里**：报告读完之后谁也核不了，
而未命中维度的理由（「这一块为什么不需要看」的唯一出处）全部消失，只剩异常的
`category` 可以反推。
"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from services.ai.engine import EngineOutcome
from services.ai.protocol import AnalysisPayload, Anomaly, DimensionReview
from services.ai.result_payload import failed_result, result_payload


def _anomaly() -> Anomaly:
    return Anomaly(
        title="【道具】ID 被删除但生成文件仍在",
        category="config_id",
        severity="high",
        confidence="high",
        evidence=("config/道具表.xlsx 第 12 行",),
        commit="a" * 40,
        file_path="config/道具表.xlsx",
        impact="读表时会取到空配置",
        suggestion="确认生成物是否同一批提交",
    )


def _outcome(*, dimensions=(), payload: bool = True) -> EngineOutcome:
    return EngineOutcome(
        status="succeeded",
        payload=AnalysisPayload(status="final", dimensions=tuple(dimensions))
        if payload
        else None,
        anomalies=(_anomaly(),),
        report_markdown="# 风险评估\n\n没问题。\n",
    )


def test_the_dimension_reviews_reach_the_payload():
    """**这条就是那个缺口**：九个维度逐一 `hit` 与理由必须进得了这一份字典。"""
    reviews = (
        DimensionReview(id="config_id", hit=True, note="见上面的异常"),
        DimensionReview(id="config_value", hit=False, note="本批次没有改数值型的表"),
        DimensionReview(id="value_sanity", hit=False, note="没有可比较的整表统计"),
    )

    payload = result_payload(_outcome(dimensions=reviews), {}, suppressed=frozenset())

    assert [item["id"] for item in payload["dimensions"]] == [
        "config_id",
        "config_value",
        "value_sanity",
    ], payload.get("dimensions")
    assert payload["dimensions"][1] == {
        "id": "config_value",
        "hit": False,
        "note": "本批次没有改数值型的表",
    }, "未命中维度的理由没了 —— 那是「这一块为什么不需要看」的唯一出处"


def test_the_dimension_entries_are_json_safe_and_plain():
    """这一份要落库（JSON 列）、要进 SSE、要进模板上下文，所以不能带自定义对象。"""
    payload = result_payload(
        _outcome(dimensions=(DimensionReview(id="config_id", hit=True),)),
        {},
        suppressed=frozenset(),
    )

    json.dumps(payload, ensure_ascii=False)  # 不抛就算过
    entry = payload["dimensions"][0]
    assert isinstance(entry["hit"], bool), "`hit` 必须是真布尔，不能是字符串"
    assert entry["note"] == "", "没写理由时是空串（不是 None）—— 模板里不用再判空"


def test_a_run_without_a_payload_still_has_the_key():
    """形状一致：读取侧只写一处 `payload.get("dimensions")`，不必为兜底那次加分支。"""
    payload = result_payload(_outcome(payload=False), {}, suppressed=frozenset())

    assert payload["dimensions"] == []


def test_the_failed_result_has_the_same_key():
    """没跑起来的那一份也要有 —— 少一个键与空列表在界面上是两件事
    （「九个维度一个都没交代」与「这次根本没跑」，后者已经由 status 说了）。"""
    payload = failed_result({"commits": 1}, "本次分析未发起")

    assert payload["dimensions"] == []
    assert payload["status"] == "failed"


def test_the_keys_the_ui_reads_are_all_present():
    """形状钉住：这几个键各自有界面在读，少一个就是「那一块永远空着」而没有任何报错。"""
    payload = result_payload(
        _outcome(dimensions=(DimensionReview(id="config_id", hit=True),)),
        {},
        suppressed=frozenset(),
    )

    for key in (
        "risk_level", "risk_reasons", "report_markdown", "status", "anomalies",
        "dimensions", "suppressed_count", "rounds_used", "requests_used",
        "usage", "dropped", "subagents", "subagent_skipped", "context",
    ):
        assert key in payload, f"`{key}` 不在结果里了"


# ==========================================================================
# 「上下文索取额度用尽」那本账
# ==========================================================================


def test_the_refused_requests_reach_the_payload():
    """**这条就是那个缺口**：额度用尽时，光有一句「还有文件没看」是不够的。

    用户读完那句仍然不知道三件事：缺的是哪几块（于是判不了这份结论能不能用）、
    占多少、该动哪个设置。前两件的原料是 `EngineOutcome.refused_requests` ——
    引擎侧算了、`result_payload` 不产出，就等于没算过（本文件开头那段话）。
    """
    outcome = _outcome()
    outcome = replace(
        outcome,
        requests_used=180,
        refused_requests=("config/道具表.xlsx", "引用扫描 CfgRewardMode"),
    )

    budget = result_payload(outcome, {}, suppressed=frozenset())["context"]["request_budget"]

    assert budget["used"] == 180
    assert budget["refused"] == 2
    assert budget["refused_items"] == ["config/道具表.xlsx", "引用扫描 CfgRewardMode"]


def test_the_count_survives_the_list_cap():
    """列表封顶，**计数不封顶**。

    拿截断后的列表长度当计数，会把「缺了 40 块」说成「缺了 20 块」—— 而「缺多少」
    正是用户判断这份结论能不能用的依据。
    """
    outcome = replace(
        _outcome(),
        requests_used=10,
        refused_requests=tuple(f"config/file_{index}.xlsx" for index in range(40)),
    )

    budget = result_payload(outcome, {}, suppressed=frozenset())["context"]["request_budget"]

    assert budget["refused"] == 40, "计数被列表上限截掉了"
    assert len(budget["refused_items"]) < 40, "列表没有被封顶（落库的那份会白白撑大）"


def test_a_run_that_never_hit_the_budget_reports_zeros():
    """没超预算时是干净的空账，不是缺键 —— 界面据此区分「没超」与「这份结果太老」。"""
    budget = result_payload(_outcome(), {}, suppressed=frozenset())["context"]["request_budget"]

    assert budget == {"used": 0, "refused": 0, "refused_items": []}
