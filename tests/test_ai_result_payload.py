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

from services.ai.baseline_closures import DECLARED_FIXED, BaselineClosure
from services.ai.degradation import DEGRADE_MARKDOWN
from services.ai.engine import EngineOutcome
from services.ai.protocol import AnalysisPayload, Anomaly, DimensionReview
from services.ai.result_payload import (
    determine_risk_level,
    failed_result,
    note_review_skipped,
    result_payload,
)
from services.ai.verdict import RULING_SUMMARY_TITLE


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


def test_a_finding_is_in_scope_only_with_a_verifiable_relation():
    """`out_of_scope` 的判据是「与本次改动有没有可核实关联」，不是「有没有白名单」。

    WP27 起，`allowed_paths=None` 的运行里：结论指认的文件在 `delta_files` 里才算
    in-scope；缺 `delta_files`（老 payload / 单提交形状）时关联锚点不存在，按保守方向
    单列（`out_of_scope=True`，不进等级）。两种形态都**照常保留条目**。
    """
    outcome = _outcome()
    changed = {"summary": {}, "delta_files": [{"file_path": "config/道具表.xlsx"}]}

    in_change = result_payload(outcome, changed, suppressed=frozenset())
    unknown = result_payload(outcome, {"summary": {}}, suppressed=frozenset())

    assert in_change["anomalies"][0]["out_of_scope"] is False
    assert unknown["anomalies"][0]["out_of_scope"] is True, (
        "缺 `delta_files` 时仍按「默认 in-scope」放行 —— 背景发现会进等级"
    )
    assert len(unknown["anomalies"]) == 1, "单列不等于丢掉，条目必须还在"


# ==========================================================================
# 模型对上一轮结论的收口声明
# ==========================================================================


def test_the_baseline_close_outs_reach_the_payload():
    """**这条键的读者在下一轮**：`reconcile_result` 读的是 `result["baseline_updates"]`。

    链路：`AnalysisPayload.baseline_closures` →（这一份）→ 落库的 `response_payload`
    → 下一轮 `incremental_baseline.reconcile_result`。这里少一个键，这一轮声明过什么
    下一轮就当没说过 —— 而报告正文里「已修复」那句话照样在（实测 run 73 → run 74 的
    翻转就是这么来的：同一批条目在两轮报告里各说了一遍反话）。
    """
    outcome = _outcome()
    outcome = replace(
        outcome,
        payload=replace(
            outcome.payload,
            baseline_closures=(
                BaselineClosure(
                    fingerprint="0123456789abcdef",
                    status=DECLARED_FIXED,
                    reason="兜底常量加回来了，服务端又校验了一次",
                ),
            ),
        ),
    )

    payload = result_payload(outcome, {}, suppressed=frozenset())

    assert payload["baseline_updates"] == [
        {
            "fingerprint": "0123456789abcdef",
            "status": "fixed",
            "reason": "兜底常量加回来了，服务端又校验了一次",
        }
    ], "收口声明没进这一份 —— 下一轮收不到它，那几条又会当在挂条目喂回去"


def test_a_run_without_close_outs_says_so_with_an_empty_list():
    """没声明收口是**常态**（绝大多数轮次没有旧结论要收）—— 两种形态都要有这个键。"""
    assert result_payload(_outcome(), {}, suppressed=frozenset())["baseline_updates"] == []
    assert failed_result({"commits": 1}, "本次分析未发起")["baseline_updates"] == []


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


# ===========================================================================
# 裁决与候选血缘走**结构化的字段**（AI-P0-05 / AI-P0-06）
# ===========================================================================


def _lineage_outcome() -> EngineOutcome:
    """一次**带复核裁决**的子代理运行的结果：裁决在 `verdict` 里，血缘在异常里。"""
    from services.ai.verdict import (
        VERDICT_RETRACTED,
        VerifyVerdict,
        reduce_findings,
    )

    landed = Anomaly(
        title="【协议】A 被就地替换",
        category="code_logic",
        severity="critical",
        confidence="very_high",
        evidence=("code/qz_pub/protocols/ProtoCGas.lua 第 9 行",),
        commit="b" * 40,
        file_path="code/qz_pub/protocols/ProtoCGas.lua",
        # 它来源于分片 S3 的第 3 条候选 —— 血缘要一路落进载荷。
        source_candidate_ids=("S3-3", "S1-2"),
    )
    reduction = reduce_findings(
        [landed],
        verdicts=(
            VerifyVerdict(
                finding_id="F1",
                verdict=VERDICT_RETRACTED,
                reason="客户端已同步",
                evidence_refs=("ProtoCGas.lua:9",),
            ),
        ),
    )
    return EngineOutcome(
        status="succeeded",
        anomalies=reduction.active_anomalies(),
        report_markdown="## 复核裁决（平台）\n\n已撤销 1 条。\n",
        verdict=reduction.as_dict(),
        draft_markdown="# 变更理解\n\n模型自己写的那份草稿。\n",
        verify_report_markdown="## 对账结果（找反证）\n\n未找到反证。\n",
    )


def test_the_candidate_lineage_reaches_the_payload():
    """**血缘必须落库**：读侧要在**运行之后**还能回答「这条结论是哪个分片报的」。

    只存在内存里的话，任何一次回看（抽屉、历史、下一轮基线）都只能再去猜 —— 而「猜」
    正是 AI-P0-06 整段删掉的那套启发式。
    """
    payload = result_payload(_lineage_outcome(), {"summary": {}}, suppressed=frozenset())

    assert payload["retracted_findings"][0]["source_candidate_ids"] == ["S3-3", "S1-2"], (
        "载荷里的裁决行没带上候选血缘 —— 读侧再也核不了「这条候选的去向」"
    )
    # 被撤销的那条不在活动清单里（`anomalies` 是它的活动投影），血缘只在审计轨迹上
    # —— 而那条轨迹恰恰是「这条候选去哪儿了」唯一的出处。
    assert payload["anomalies"] == []
    assert payload["final_findings"][0]["source_candidate_ids"] == ["S3-3", "S1-2"]


def test_the_batch_lineage_survives_on_an_active_finding():
    """活动的那一条同样带血缘（撤销的那条走 `retracted_findings`，两条路都要有）。"""
    from services.ai.verdict import reduce_findings

    landed = Anomaly(
        title="【协议】A 被就地替换",
        category="code_logic",
        severity="high",
        confidence="high",
        evidence=("code/qz_pub/protocols/ProtoCGas.lua 第 9 行",),
        commit="b" * 40,
        file_path="code/qz_pub/protocols/ProtoCGas.lua",
        source_candidate_ids=("S3-3",),
    )
    reduction = reduce_findings([landed])
    payload = result_payload(
        EngineOutcome(
            status="succeeded",
            anomalies=reduction.active_anomalies(),
            report_markdown="# 报告\n",
            verdict=reduction.as_dict(),
        ),
        {"summary": {}},
        suppressed=frozenset(),
    )

    assert payload["anomalies"][0]["source_candidate_ids"] == ["S3-3"]
    assert payload["final_findings"][0]["source_candidate_ids"] == ["S3-3"]


def test_the_draft_and_the_verify_original_land_in_their_own_keys():
    """草稿与对账轮原文各有一个独立键（AI-P1-01），而且**都不在** `report_markdown` 里。"""
    payload = result_payload(_lineage_outcome(), {"summary": {}}, suppressed=frozenset())

    assert payload["draft_markdown"].startswith("# 变更理解"), "草稿没有存档"
    assert payload["verify_report_markdown"].startswith("## 对账结果（找反证）")
    assert "# 变更理解" not in payload["report_markdown"], (
        "同一段正文又出现在 `report_markdown` 里 —— 同一件事在报告里出现两遍"
    )
    assert payload["report_markdown"].startswith("## 复核裁决（平台）")


def test_a_single_agent_payload_has_no_verdict_key_value():
    """单代理路径没有裁决、没有草稿存档：形状不变，值是空/None。"""
    payload = result_payload(_outcome(), {"summary": {}}, suppressed=frozenset())

    assert payload["draft_markdown"] == ""
    assert payload["verify_report_markdown"] == ""
    assert payload["report_markdown"] == "# 风险评估\n\n没问题。\n"
    assert payload["final_findings"][0]["source_candidate_ids"] == []


def test_the_whole_payload_is_json_safe_and_has_no_machine_json():
    """下发给界面那一份必须能序列化，而且**一个字节的机器 json 都没有**。

    这一条同时守着 SSE：`result` 事件的 data 就是这份字典的 `json.dumps`。
    """
    payload = result_payload(_lineage_outcome(), {"summary": {}}, suppressed=frozenset())

    dumped = json.dumps(payload, ensure_ascii=False)
    assert "ai-verify-ruling" not in dumped
    assert "<!--" not in dumped, "载荷里出现了 HTML 注释（渲染器会把它显示出来）"


# ==========================================================================
# 复核「要了却没跑成」那行说明（单代理路径）
# ==========================================================================


def test_a_review_that_was_asked_for_but_could_not_run_is_said_at_the_top():
    """配置要了复核、而这次只分得出一个分析者 ⇒ 报告开篇补一行。

    真机 run 65：`subagent_verify=1`，而本次只有 1 个变更文件 ⇒ `plan_family` 返回 `None`
    走单代理路径 ⇒ 报告里**一个字都没提**复核没跑，而 `help.html` 写的是「对账轮没跑成时
    会如实标成降级」。界面那条横幅只管面板，报告才是被存档/导出/转发的那一份。
    """
    payload = result_payload(_outcome(), {"summary": {}}, suppressed=frozenset())

    noted = note_review_skipped(
        payload,
        project_config={"subagent_enabled": True, "subagent_verify": True},
        payload={"mode": "weekly"},
    )

    assert noted["report_markdown"].startswith(RULING_SUMMARY_TITLE)
    assert "没有跑「找反证」复核" in noted["report_markdown"]
    assert "只分得出一个分析者" in noted["report_markdown"]
    assert "未经复核" in noted["report_markdown"], "要给出读法，不只是报一个状态"
    assert noted["report_markdown"].endswith(payload["report_markdown"]), (
        "说明只能加在最前面 —— 加在后面等于读者读完正文才看到"
    )
    assert payload["report_markdown"].startswith("# 风险评估"), (
        "改了调用方手上那一份 —— 落库与下发的就不是同一个东西了"
    )


@pytest.mark.parametrize(
    "config, run_payload, why",
    [
        ({"subagent_enabled": True, "subagent_verify": True}, {"mode": "commit"}, "不是周版本"),
        (
            {"subagent_enabled": False, "subagent_verify": True},
            {"mode": "weekly"},
            "子代理关着：那个开关本来就不生效",
        ),
        (
            {"subagent_enabled": True, "subagent_verify": False},
            {"mode": "weekly"},
            "用户自己没开复核",
        ),
    ],
)
def test_nothing_is_added_when_the_review_was_not_asked_for(config, run_payload, why):
    """**没要复核就别说话**（三种情形各自都要判）：那是用户自己的配置，报告不该多一句。"""
    payload = result_payload(_outcome(), {"summary": {}}, suppressed=frozenset())

    assert note_review_skipped(payload, project_config=config, payload=run_payload) == payload, why


# ==========================================================================
# 风险等级按结论定级，规模只在没有结论时兜底（WP13）
# ==========================================================================

# 周版本实测的整窗口径：1012 个文件。旧口径（`total_files >= 120`）在这个数字上
# 恒判 high —— 而那个 high 只是「这周文件多」，不是模型报了什么。
WEEKLY_SCALE = {"total_files": 1012, "delta_files": 33}

# 本节几条用例的前提是「结论引用的正是本次改动的文件」：WP27 起 `delta_files` 是
# 「参与定级」的关联锚点（判据见 `test_ai_related_evidence_scope.py`），缺了它结论会被
# 单列、等级掉到规模兜底 —— 那本节测的就变成「按规模兜底」而不是「按结论定级」了。
CHANGE_SET = {"delta_files": [{"file_path": "config/道具表.xlsx"}]}


def _finding(*, severity: str, file_path: str = "config/道具表.xlsx") -> Anomaly:
    return Anomaly(
        title="【道具】ID 被删除但生成文件仍在",
        category="config_id",
        severity=severity,
        confidence="high",
        evidence=(f"{file_path} 第 12 行",),
        commit="a" * 40,
        file_path=file_path,
    )


def _concluded(*anomalies: Anomaly) -> EngineOutcome:
    """一次**有结论**的运行（模型按协议交回了结构化条目）。"""
    return EngineOutcome(
        status="succeeded",
        anomalies=tuple(anomalies),
        report_markdown="# 风险评估\n\n有一处问题。\n",
    )


def test_a_big_week_with_only_a_medium_finding_is_not_high():
    """1012 个文件 + 1 条 medium：**等级按结论定，不按文件数顶格**。

    旧口径下这个窗口恒 high，于是「模型只报了 1 条 medium」和「模型报了 critical」
    在报告里长得一模一样，等级没有数值含义（方案 §4.4 第 6 条）。
    """
    payload = result_payload(
        _concluded(_finding(severity="medium")),
        {"summary": dict(WEEKLY_SCALE), **CHANGE_SET},
        suppressed=frozenset(),
    )

    assert payload["risk_level"] != "high", "文件数又把等级顶到 high 了"
    assert payload["risk_level"] == "mid_high"
    assert payload["risk_reasons"][0] == "模型报出 1 条达门槛的问题"


def test_a_big_week_with_only_a_low_finding_is_not_high():
    """medium / low 同一支：原规则只有 critical 才 high，文件数不许把它抬上去。"""
    payload = result_payload(
        _concluded(_finding(severity="low")),
        {"summary": dict(WEEKLY_SCALE), **CHANGE_SET},
        suppressed=frozenset(),
    )

    assert payload["risk_level"] == "mid_high"
    assert payload["risk_level"] != "high"


def test_a_big_week_with_a_critical_finding_is_high():
    """有 critical 时仍然是 high —— 结论里的严重度才是唯一能顶格的东西。"""
    payload = result_payload(
        _concluded(_finding(severity="critical")),
        {"summary": dict(WEEKLY_SCALE), **CHANGE_SET},
        suppressed=frozenset(),
    )

    assert payload["risk_level"] == "high"
    assert any("critical" in reason for reason in payload["risk_reasons"])


@pytest.mark.parametrize(
    "outcome, why",
    [
        (
            EngineOutcome(status="failed", error_message="模型调用超时"),
            "失败（完全没有结论）",
        ),
        (
            EngineOutcome(
                status="degraded",
                degradation=DEGRADE_MARKDOWN,
                report_markdown="# 风险评估\n\n只有 markdown。\n",
            ),
            "降级且没有结构化结论",
        ),
    ],
)
def test_no_conclusion_falls_back_to_the_scale_but_never_high(outcome, why):
    """没有结论 ⇒ 规模兜底，且**最高只到 mid_high**。

    反过来（没跑成也报 high）会把「没查」显示成「最严重」；而只报「低」又会把
    「模型没答上来」说成「模型看完说没问题」—— 两件事的处理方式完全相反。
    """
    payload = result_payload(outcome, {"summary": dict(WEEKLY_SCALE)}, suppressed=frozenset())

    assert payload["risk_level"] != "high", why
    assert payload["risk_level"] == "mid_high", why
    assert any("规模兜底" in reason for reason in payload["risk_reasons"]), why
    assert any("本次没有可用结论" in reason for reason in payload["risk_reasons"]), why


def test_the_scale_fallback_is_capped_even_when_key_paths_changed():
    """关键路径也救不回 high：这一支只服务「没有结论」，严重度只能由结论给。"""
    payload = result_payload(
        EngineOutcome(status="failed", error_message="模型调用超时"),
        {"summary": {**WEEKLY_SCALE, "critical_paths": True}},
        suppressed=frozenset(),
    )

    assert payload["risk_level"] == "mid_high"
    assert any("含关键路径" in reason for reason in payload["risk_reasons"])
    assert any("规模兜底" in reason for reason in payload["risk_reasons"])


def test_the_scale_function_itself_never_returns_high():
    """兜底函数的天花板钉在这里：任何规模组合都不许再产出 high。

    这一条守的是**直接调用** `determine_risk_level` 的读侧（`failed_result` 等）——
    只改 `risk_level_from_outcome` 的话，另一条路仍会报 high。
    """
    for summary in (
        {"total_files": 120},
        {"total_files": 1012, "delta_files": 33},
        {"total_files": 5000, "delta_files": 5000},
        {"total_files": 60, "delta_files": 60},
        {"critical_paths": True},
        {},
    ):
        assert determine_risk_level(summary) != "high", summary


def test_the_failed_result_labels_its_level_as_a_scale_fallback():
    """没发起分析的那一份同样是规模兜底，措辞与 `risk_level_from_outcome` 同一句。"""
    payload = failed_result(dict(WEEKLY_SCALE), "本次分析未发起")

    assert payload["risk_level"] != "high"
    assert any("规模兜底" in reason for reason in payload["risk_reasons"])


def test_an_out_of_scope_critical_does_not_raise_the_level():
    """越界的 critical 不抬等级（与 WP7 同一条口径）：它不是本批次该评的文件。

    这条用例把两个变量一起钉住：窗口是 1012 个文件（旧口径必 high），活动清单里
    还有一条 critical —— 但它引用的代码文件不在 `allowed_paths` 里。
    """
    payload = result_payload(
        _concluded(
            _finding(severity="medium"),
            _finding(severity="critical", file_path="code/a.lua"),
        ),
        {
            "summary": dict(WEEKLY_SCALE),
            "allowed_paths": ["config/道具表.xlsx"],
        },
        suppressed=frozenset(),
    )

    assert payload["risk_level"] == "mid_high", "越界的 critical 把等级抬上去了"
    assert payload["risk_level"] != "high"
    assert any(entry["out_of_scope"] for entry in payload["anomalies"])
    assert any("范围外" in reason for reason in payload["risk_reasons"])


def test_a_run_whose_only_finding_is_out_of_scope_uses_the_capped_fallback():
    """只报了越界条目的那次会落到规模兜底 —— 它更不能因为文件多就顶格 high。

    这是 WP7（越界不计入）与 WP13（兜底有天花板）的交界：两个都做了，才有
    「越界的 critical 既不抬等级、也不靠 1012 个文件把等级顶到 high」这个结果。
    """
    payload = result_payload(
        _concluded(_finding(severity="critical", file_path="code/a.lua")),
        {
            "summary": dict(WEEKLY_SCALE),
            "allowed_paths": ["config/道具表.xlsx"],
        },
        suppressed=frozenset(),
    )

    assert payload["risk_level"] != "high"
    assert payload["risk_level"] == "mid_high"
    assert any("规模兜底" in reason for reason in payload["risk_reasons"])
    assert any("范围外" in reason for reason in payload["risk_reasons"]), (
        "越界条数没交代 —— 读侧会把「模型只报了范围外的」读成「什么都没报」"
    )
