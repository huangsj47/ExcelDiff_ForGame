"""AI 分析的**结论载荷**：风险等级怎么定、结果字典长什么样。

## 为什么单独一个文件

这几个函数此前住在 `services/ai_analysis_service.py` 里。那个文件已经贴着仓库的长度
闸门（`scripts/check_file_length.py --strict` 在 2000 行报错），而它们**与文件的其余部分
几乎没有耦合**：输入是一个 `EngineOutcome` 加一份 payload 摘要，输出是一个纯字典，
不碰数据库、不碰 Flask。搬家是最便宜的腾地方方式（在原地加功能已经加不动了）。

## 一条口径（与「失败」有关的那一条）

**没有结论时按变更规模定级，并且明说那不是模型结论。** 反过来做（没结论就报「低」）
正是这套机制最该避免的事：用户分不出「模型看完说没问题」与「模型压根没答上来」，
而这两件事的处理方式完全相反。`failed_result` 同样只估算等级并写明原因 ——
不伪装成模型结论。
"""
from __future__ import annotations

from typing import List, Tuple

from services.ai.engine import EngineOutcome
from services.ai.rules import anomaly_fingerprint
from services.ai.usage import usage_from_outcome


def determine_risk_level(summary: dict) -> str:
    total_files = int(summary.get("total_files") or 0)
    delta_files = int(summary.get("delta_files") or 0)
    critical = bool(summary.get("critical_paths"))
    if critical or total_files >= 120 or delta_files >= 60:
        return "high"
    if total_files >= 80 or delta_files >= 40:
        return "mid_high"
    if total_files >= 40 or delta_files >= 20:
        return "medium"
    if total_files >= 15 or delta_files >= 8:
        return "mid_low"
    return "low"


def risk_level_from_outcome(outcome: EngineOutcome, summary: dict) -> Tuple[str, List[str]]:
    """风险等级与依据。

    **有结论时按结论定级；没有结论时按变更规模定级，并明说那不是模型结论。**
    反过来做（没结论就报「低」）正是这套机制最该避免的事：用户分不出「模型看完说没问题」
    和「模型压根没答上来」，而这两件事的处理方式完全相反。
    """
    if outcome.anomalies:
        severities = {item.severity for item in outcome.anomalies}
        level = "high" if "critical" in severities else "mid_high"
        reasons = [f"模型报出 {len(outcome.anomalies)} 条达门槛的问题"]
        if "critical" in severities:
            reasons.append("其中含 critical")
        if outcome.degradation:
            reasons.append(outcome.degradation_label or outcome.degradation)
        return level, reasons

    level = determine_risk_level(summary)
    reasons = [
        f"变更规模：{summary.get('total_files', 0)} 个文件"
        f"（本次变化 {summary.get('delta_files', 0)} 个）"
    ]
    if summary.get("critical_paths"):
        reasons.append("含关键路径")
    if outcome.succeeded:
        reasons.append("模型未报出达门槛的问题")
    else:
        reasons.append(
            f"⚠️ 本次未取得完整结论（{outcome.degradation_label or outcome.degradation or '原因未知'}），"
            "该等级仅按变更规模估算，**不是**模型评估结果"
        )
    return level, reasons


def result_payload(
    outcome: EngineOutcome,
    payload: dict,
    *,
    suppressed: frozenset = frozenset(),
    context_budget_note: str = "",
) -> dict:
    """给前端与后续读取用的结果。

    保留既有的 `risk_level`（界面在读它），其余键是真实产出。被人工忽略的结论不进
    `anomalies` —— 尊重分诊结果，而不是每轮再问一次。

    `context_budget_note` 是「这次分析的提示词预算被窗口压过」的说明（`_apply_model_window`）。
    以前它只写进日志 —— 于是「这次分析浅了」在界面上完全看不出原因，而它正是最需要
    被看见的一类降级。
    """
    summary = payload.get("summary") or {}
    risk_level, risk_reasons = risk_level_from_outcome(outcome, summary)
    kept = [item for item in outcome.anomalies if anomaly_fingerprint(item) not in suppressed]

    return {
        "risk_level": risk_level,
        "risk_reasons": risk_reasons,
        "report_markdown": outcome.report_markdown,
        "status": outcome.status,
        "degradation": outcome.degradation,
        "degradation_label": outcome.degradation_label,
        "error_message": outcome.error_message,
        # 上下文预算与压缩的账。放在这里（而不是只写日志）的理由与 usage 一样：
        # SSE 的 result 事件与 /latest 自动都有，界面不必再拉一次接口。
        "context": {
            "budget_note": str(context_budget_note or ""),
            "compaction": outcome.compaction.to_dict(),
        },
        "anomalies": [
            {
                "fingerprint": anomaly_fingerprint(item),
                "title": item.title,
                "category": item.category,
                "severity": item.severity,
                "confidence": item.confidence,
                "evidence": list(item.evidence),
                "commit_ref": item.commit or "",
                "file_path": item.file_path or "",
                "impact": item.impact or "",
                "suggestion": item.suggestion or "",
            }
            for item in kept
        ],
        "suppressed_count": len(outcome.anomalies) - len(kept),
        "rounds_used": outcome.rounds_used,
        "requests_used": outcome.requests_used,
        # 本次的用量。放在这里有两个原因：SSE 的 `result` 事件与 `/latest`（读的是落库的
        # response_payload）**自动都有**，抽屉那一行「本次消耗 N tokens」不必为「正在
        # 分析中」再拉一次接口；而且它随结论一起被缓存复用 —— 同一份结论回放两次，
        # 显示的消耗也是当初那次的，不会变成 0。
        #
        # 口径见 services/ai/usage.py。费用**不在这里算**（这一层拿不到价格表），
        # 由 `/ai-analysis/runs/<id>/usage` 在读取侧按当前价格表算。
        "usage": usage_from_outcome(outcome),
        "dropped": [
            {"kind": item.kind, "reason": item.reason, "detail": item.detail}
            for item in outcome.dropped
        ],
    }


def failed_result(summary: dict, message: str) -> dict:
    """没发起分析时的结果。**等级按规模估算并写明原因** —— 不伪装成模型结论。"""
    return {
        "risk_level": determine_risk_level(summary),
        "risk_reasons": [message, "该等级仅按变更规模估算，**不是**模型评估结果"],
        "report_markdown": "",
        "status": "failed",
        "degradation": "not_started",
        "degradation_label": message,
        "error_message": message,
        "anomalies": [],
        "suppressed_count": 0,
        "rounds_used": 0,
        "requests_used": 0,
        "dropped": [],
    }
