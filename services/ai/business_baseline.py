"""四档业务发现的增量保全；模型遗漏不能视为问题已修复。"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from services.ai.report_document import render_structured_report
from services.ai.report_items import sort_report_items
from services.ai.scope import normalize_path


def stored_business(run) -> dict:
    try:
        value = json.loads(getattr(run, "response_payload", None) or "{}")
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def business_fingerprint(item) -> str:
    return hashlib.sha256(("business:" + item["id"]).encode()).hexdigest()[:16]


def business_digest(run) -> str:
    items = stored_business(run).get("report_items") or []
    if not items:
        return ""
    # 继承存档只用于线索，不把旧 evidence_id 当本次证据仓地址。
    lines = ["## 上轮业务发现（四档；旧引用是存档，本轮须重新取证）",
             "每条按 #指纹写 baseline_updates；仅遗漏不会关闭，复用卡片时保留 id。"]
    for item in items:
        lines.append(f"- #{business_fingerprint(item)} {item['id']}："
                     f"{item['business_area']}；{item['change_fact']}；{item['impact_hypothesis']}；"
                     f"待确认：{item.get('decision_question', '')}")
    return "\n".join(lines)


def replace_business_body(report: str, before, after, sections) -> str:
    """保留独立的复核/缺口说明，只替换确定性生成的正文，避免丢平台告警。"""
    old = render_structured_report(before, sections)
    new = render_structured_report(after, sections)
    return report.replace(old.rstrip(), new.rstrip(), 1) if old.rstrip() in report else new


def reconcile_business_result(result, previous_run, *, changed_paths=(), previous_rows=()):
    previous = stored_business(previous_run).get("report_items") or []
    if not previous:
        return result
    merged = deepcopy(result)
    current = {item["id"]: item for item in merged.get("report_items") or []}
    changed = {normalize_path(path) for path in changed_paths}
    updates = {item.get("fingerprint"): item for item in merged.get("baseline_updates") or []}
    ignored = {row.get("fingerprint") for row in previous_rows if row.get("disposition") == "ignored"}
    retracted = {row.get("fingerprint") for row in merged.get("retracted_findings") or []}
    closed = list(merged.get("business_closures") or [])
    for old in previous:
        identifier = old["id"]
        fingerprint = old.get("finding_fingerprint")
        paths = {normalize_path(ref.get("path", "")) for ref in old.get("evidence_refs", [])}
        if fingerprint and (fingerprint in retracted or (fingerprint in ignored and not paths & changed)):
            continue
        if identifier in current:
            continue
        declaration = updates.get(business_fingerprint(old), updates.get(fingerprint, {}))
        if declaration.get("status") in ("fixed", "overturned") and declaration.get("reason"):
            closed.append({"id": identifier, "status": declaration["status"],
                           "reason": declaration["reason"], "independently_verified": False})
            continue
        item = deepcopy(old)
        paths = {normalize_path(ref.get("path", "")) for ref in item.get("evidence_refs", [])}
        item["baseline_state"] = "needs_recheck" if paths & changed else "carried_forward"
        item["baseline_run_id"] = getattr(previous_run, "id", None)
        item["evidence_status"] = "unverified"
        # 存档坐标保留，但明确不是本轮实际读取的证明，不能据此抬本轮等级。
        for ref in item.get("evidence_refs", []):
            ref["available"] = False
            ref["historical"] = True
        item["decision_question"] = item.get("decision_question") or "请确认历史发现是否仍成立"
        item["evidence_gaps"] = [*item.get("evidence_gaps", []), "历史发现未在本轮重新取证，保留待核查"]
        current[identifier] = item
    before = merged.get("report_items") or []
    sections = merged.get("report_sections")
    if sections is None:
        sections = {"overview": "本轮报告与历史业务发现并列，请核查未重取证条目。",
                    "changes": [], "impacts": [], "regressions": [], "release_actions": [], "gaps": []}
        legacy = merged.get("report_markdown", "")
        merged["draft_markdown"] = merged.get("draft_markdown") or legacy
    items = sort_report_items(tuple(current.values()))
    merged["report_markdown"] = replace_business_body(merged.get("report_markdown", ""), before, items, sections)
    merged["report_items"], merged["report_sections"] = items, sections
    merged["business_closures"] = closed
    if closed:
        merged["report_markdown"] += "\n## 业务发现声明收口（未独立核实）\n\n" + "\n".join(
            f"- {entry['id']}：{entry['status']}；{entry['reason']}" for entry in closed) + "\n"
    merged["report_summary"] = {
        **merged.get("report_summary", {}),
        "highest_severity": items[0]["severity"] if items else None,
        "pending_confirmation_count": sum(bool(item.get("decision_question")) or
                                          item["evidence_status"] != "verified" for item in items),
        "confirmed_high_risk_count": None, "release_decision": "pending_human_review",
    }
    return merged
