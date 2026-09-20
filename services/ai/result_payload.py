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

# 「额度用尽没轮到的」最多往结果里放几条。界面只展示前几条、其余归到「等 N 条」，
# 所以多放没有用；而额度用尽那一轮模型可能一口气要几十个请求，整份列表会白白撑大
# 落库的 response_payload。**计数不受它影响**（见 `request_budget.refused`）。
_REFUSED_ITEMS_MAX = 20


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
            # 「上下文索取额度」这本账。原先只有一句 `degradation_label`
            # （「上下文索取额度用尽，基于已有证据出结论」），用户看完只知道**出事了**，
            # 不知道三件该知道的事：缺的是哪几块、占多少、该调什么。
            #
            # * `used` / `refused` 是同一个账本的两侧：模型这次一共索取
            #   `used + refused` 次，其中 `refused` 一次都没轮到；
            # * `refused_items` 是那几块的标签（点名到文件 / 查询），界面据此把
            #   「还有文件没看」展开成具体清单。
            #
            # `used` 在**子代理模式下是全家合计**（见 `subagent._final_outcome`），
            # 所以界面不能拿它跟配置里的上限直接比 —— 那个上限是**每个成员**的
            # （而且是配置值的七成）。文案里因此只说比例，不说「已用 N/M」。
            "request_budget": {
                "used": int(outcome.requests_used or 0),
                "refused": len(outcome.refused_requests),
                # 列表**封顶**（界面只展示前几条，剩下的是「等 N 条」），但 `refused`
                # 那个计数始终是全额 —— 拿截断后的列表长度当计数，会把「缺了 40 块」
                # 说成「缺了 20 块」。
                "refused_items": list(outcome.refused_requests[:_REFUSED_ITEMS_MAX]),
            },
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
        # 九个维度**逐一**的交代（`DimensionReview`：命中与否 + 未命中的理由）。
        #
        # 这一份原先在落库时被丢掉了：解析（`protocol._coerce_dimensions`，final 必须非空）
        # 与引擎持有（`EngineOutcome.payload`）两段都在，只有这里不产出这个键 ——
        # 于是「九个维度都过了一遍」这句保证**只活在提示词里**，报告读完之后谁也核不了。
        # 剩下的唯一线索是异常的 `category`（它落在同一集合里），而**未命中维度的理由
        # 全部消失** —— 那正是「这一块为什么不需要看」的唯一出处。
        #
        # 放在这里与 `subagents` 同一个理由：SSE 的 `result` 事件、`/latest`、
        # 结论回放读的都是这同一份字典。
        "dimensions": [
            {"id": item.id, "hit": bool(item.hit), "note": item.note or ""}
            for item in (outcome.payload.dimensions if outcome.payload else ())
        ],
        # **本次分析当时生效的**检查维度清单（id + 中文名），来自
        # `LoadedSkills.dimensions`（见 `engine.run_analysis`）。
        #
        # 它与上面那个 `dimensions` **不是一回事**：上面是模型逐维度的交代（命中与否 +
        # 理由），这里是平台当时用的那份清单本身。它必须随结果落库，因为导出文档要把
        # 异常的 `category` 翻成中文名（`report_document.dimension_label`），而导出发生在
        # **很久之后**：那时项目可能已经改过声明（`references/project-facts.md`）。
        #
        # 现查项目当前声明是**篡改历史**：一条当时归在 `performance` 下的发现，会在新
        # 清单里被显示成「未归类（performance）」——报告读起来完全正常，只是把结论按
        # 今天的口径重新贴了标签。所以清单在分析当时就存进来，导出只读这一份。
        "dimension_specs": [
            {"id": spec.id, "label": spec.label} for spec in outcome.dimension_specs
        ],
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
        # 子代理模式（services/ai/subagent.py）：这次分了几片、每一片跑了什么、谁没跑成。
        #
        # 放在这里是因为**面板与抽屉读的都是这一份**（落库的 response_payload）：不写进来，
        # 「谁没跑成」就只存在于报告正文的一段文字里，而那段文字是最容易被跳过的部分。
        # 没开子代理时是空列表 —— 读取侧据此区分「没开」与「一个成员都没跑」。
        "subagents": [dict(item) for item in outcome.subagents],
        "subagent_skipped": list(outcome.subagent_skipped),
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
        # 形状与 `result_payload` 保持一致：读取侧只写一处 `payload.get("dimensions")`，
        # 不必为「没跑起来的那次」多加一个分支（少一个键与空列表在界面上的区别是
        # 「九个维度一个都没交代」与「这次根本没跑」，而后者已经由 status 说了）。
        "dimensions": [],
        # 同上面那条：形状一致，读取侧只写一处 `payload.get("dimension_specs")`。
        # 空列表 = 「这次没有清单可查」，导出按平台出厂清单回落（这是缺字段的兜底，
        # 不是为旧数据写的兼容分支）。
        "dimension_specs": [],
        "suppressed_count": 0,
        "rounds_used": 0,
        "requests_used": 0,
        "dropped": [],
    }
