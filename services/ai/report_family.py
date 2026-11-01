"""Lossless family handoff for business cards, including medium/low hypotheses."""
from __future__ import annotations

import json
from dataclasses import replace
from typing import Mapping, Sequence

from services.ai.degradation import DEGRADE_SUBAGENT
from services.ai.report_document import render_structured_report
from services.ai.report_items import ITEM_KEYS, REFERENCE_KEYS, REPORT_SEVERITIES, sort_report_items


def report_candidate_id(label: str, item: Mapping) -> str:
    return f"{label}:REPORT:{item['id']}".upper()


def business_candidate_rows(steps: Sequence) -> tuple[dict, ...]:
    return tuple({"candidate_id": report_candidate_id(step.plan.label, item),
                  "source": step.plan.label, "report_item": dict(item)}
                 for step in steps if step.plan.role == "subagent" and step.outcome is not None
                 for item in step.outcome.report_items)


def business_candidates_prompt(steps: Sequence) -> tuple[dict, ...]:
    """模型可复写的卡片只含协议字段；平台派生标记另列，避免复制后协议纠正。"""
    result = []
    for row in business_candidate_rows(steps):
        original = row["report_item"]
        item = {key: value for key, value in original.items() if key in ITEM_KEYS}
        item["evidence_refs"] = [{key: value for key, value in ref.items() if key in REFERENCE_KEYS}
                                 for ref in original.get("evidence_refs", ())]
        result.append({**row, "report_item": item,
                       "platform_evidence_gaps": original.get("evidence_gaps", [])})
    return tuple(result)


def _union(left: Sequence, right: Sequence) -> list:
    result, seen = [], set()
    for value in (*left, *right):
        key = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            result.append(value)
            seen.add(key)
    return result


def reconcile_business_reports(synthesis, steps: Sequence):
    """An absent card is pending merge unless explicitly rejected with a reason.

    Evidence/actions from duplicate identities are unioned; downgrades without an
    explicit reason remain visible at the higher concern level for human resolution.
    Rejected cards are absent from the active projection, but the reason stays in gaps.
    """
    rows = business_candidate_rows(steps)
    if not rows and synthesis.report_sections is None:
        return synthesis
    sections = dict(synthesis.report_sections or {
        "overview": "以下为分片业务发现，最终汇总未提供完整结构化结论，请人工核对。",
        "changes": [], "impacts": [], "regressions": [], "release_actions": [], "gaps": [],
    })
    gaps = list(sections.get("gaps", ()))
    by_id = {item["id"]: dict(item) for item in synthesis.report_items}
    dispositions = {entry.candidate_id.upper(): entry for entry in
                    (synthesis.payload.candidate_dispositions if synthesis.payload else ())}
    pending = False
    for row in rows:
        original, identifier = row["report_item"], row["candidate_id"]
        disposition = dispositions.get(identifier)
        if disposition and disposition.status == "rejected" and disposition.reason.strip():
            same = [candidate for candidate in rows if candidate["report_item"]["id"] == original["id"]]
            if all((dispositions.get(candidate["candidate_id"]) is not None
                    and dispositions[candidate["candidate_id"]].status == "rejected"
                    and dispositions[candidate["candidate_id"]].reason.strip()) for candidate in same):
                by_id.pop(original["id"], None)
            gaps.append(f"{identifier} 未纳入业务清单：{disposition.reason}")
            continue
        current = by_id.get(original["id"])
        if current is None:
            current = dict(original, merge_status="pending_merge")
            current["decision_question"] = current.get("decision_question") or "请核实分片发现并完成汇总归并"
            by_id[current["id"]] = current
            reason = disposition.reason if disposition and disposition.reason else "汇总未保留对应稳定 ID"
            gaps.append(f"{identifier} 待合并：{reason}；发现、验证动作与证据已保全")
            pending = True
        else:
            current["test_actions"] = _union(current.get("test_actions", ()), original.get("test_actions", ()))
            current["evidence_refs"] = _union(current.get("evidence_refs", ()), original.get("evidence_refs", ()))
            if (REPORT_SEVERITIES.index(original["severity"]) < REPORT_SEVERITIES.index(current["severity"])
                    and not (disposition and disposition.reason.strip())):
                current["severity"] = original["severity"]
                current["merge_status"] = "pending_merge"
                gaps.append(f"{identifier} 待合并：分片与汇总关注等级不同，暂保留较高档，需核对依据")
                pending = True
    if synthesis.status == "failed" or synthesis.error_message:
        gaps.append("汇总状态说明：" + (synthesis.error_message or "汇总失败"))
    sections["gaps"] = list(dict.fromkeys(gaps))
    items = sort_report_items(tuple(by_id.values()))
    payload = replace(synthesis.payload, report_items=items, report_sections=sections) if synthesis.payload else None
    return replace(synthesis, report_items=items, report_sections=sections, payload=payload,
                   report_markdown=render_structured_report(items, sections),
                   draft_markdown=synthesis.draft_markdown or (
                       synthesis.report_markdown if synthesis.report_sections is None else ""),
                   status="degraded" if pending else synthesis.status,
                   degradation=DEGRADE_SUBAGENT if pending else synthesis.degradation)


def apply_business_rulings(outcome, reduction):
    """只按明确指纹同步裁决，不能靠标题猜卡片与高风险异常是否同一件事。"""
    if outcome.report_sections is None:
        return outcome
    rows = reduction.by_fingerprint()
    items = []
    for original in outcome.report_items:
        row = rows.get(original.get("finding_fingerprint"))
        if row is not None and not row.active:
            continue
        item = dict(original)
        if row is not None:
            item["severity"] = row.anomaly.severity
            item["review_status"] = row.verdict
        items.append(item)
    items = sort_report_items(items)
    payload = replace(outcome.payload, report_items=items) if outcome.payload else None
    return replace(outcome, report_items=items, payload=payload,
                   report_markdown=render_structured_report(items, outcome.report_sections))
