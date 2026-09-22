"""Deterministically merge the previous cumulative findings into an incremental result.

The model receives the baseline as guidance, but omission is not evidence that an old
problem was fixed.  This module keeps that safety rule in platform code: untouched old
findings remain active, and findings whose files changed remain active as needing review
until a structured result explicitly settles them.
"""

from __future__ import annotations

import json
from copy import deepcopy
from difflib import SequenceMatcher
from typing import Iterable, Mapping

from services.ai.scope import normalize_path

STATE_RECONFIRMED = "reconfirmed"
STATE_CARRIED = "carried_forward"
STATE_RECHECK = "needs_recheck"


def _evidence(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return [value] if value.strip() else []
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    return []


def _historical_anomaly(row: Mapping, state: str, previous_run_id: int) -> dict:
    disposition = str(row.get("disposition") or "pending")
    if state == STATE_RECHECK:
        disposition = "pending"
    return {
        "fingerprint": str(row.get("fingerprint") or ""),
        "title": str(row.get("title") or "（无标题）"),
        "category": str(row.get("category") or ""),
        "severity": str(row.get("severity") or "high"),
        "confidence": str(row.get("confidence") or "high"),
        "evidence": _evidence(row.get("evidence")),
        "commit_ref": str(row.get("commit_ref") or ""),
        "file_path": str(row.get("file_path") or ""),
        "impact": str(row.get("impact") or ""),
        "suggestion": str(row.get("suggestion") or ""),
        "disposition": disposition,
        "baseline_state": state,
        "baseline_run_id": previous_run_id,
        "finding_id": "",
        "source_candidate_ids": [],
        "body_label": "",
        "verify_verdict": "",
        "verify_verdict_label": "",
        "verify_reason": "",
        "verify_note": "",
        "verify_evidence": [],
        "verify_evidence_unlocatable": [],
        "evidence_capped": False,
        "original_severity": str(row.get("severity") or "high"),
        "original_confidence": str(row.get("confidence") or "high"),
    }


def _final_row(item: Mapping) -> dict:
    return {
        "finding_id": "",
        "source": "baseline",
        "verdict": "",
        "verdict_label": "",
        "active": True,
        "reason": "",
        "evidence_refs": [],
        "unlocatable_refs": [],
        "evidence_capped": False,
        "note": "",
        "body_label": "",
        "source_candidate_ids": [],
        "fingerprint": item["fingerprint"],
        "title": item["title"],
        "category": item["category"],
        "file_path": item["file_path"],
        "commit_ref": item["commit_ref"],
        "severity": item["severity"],
        "confidence": item["confidence"],
        "original_severity": item["severity"],
        "original_confidence": item["confidence"],
        "baseline_state": item["baseline_state"],
        "baseline_run_id": item["baseline_run_id"],
    }


def _line(item: Mapping) -> str:
    where = f"（`{item.get('file_path')}`）" if item.get("file_path") else ""
    return f"- **{item.get('title') or '（无标题）'}**{where}"


def _semantic_match(old: Mapping, current: Mapping) -> float:
    """Conservative fallback for fingerprints that drift when wording changes."""
    old_path = normalize_path(str(old.get("file_path") or ""))
    current_path = normalize_path(str(current.get("file_path") or ""))
    if not old_path or old_path != current_path:
        return 0.0
    old_category = str(old.get("category") or "")
    current_category = str(current.get("category") or "")
    if old_category and current_category and old_category != current_category:
        return 0.0
    old_title = str(old.get("title") or "").strip()
    current_title = str(current.get("title") or "").strip()
    if not old_title or not current_title:
        return 0.0
    return SequenceMatcher(None, old_title, current_title).ratio()


def _report_section(groups: Mapping[str, list[dict]], previous_run_id: int) -> str:
    lines = [
        "## 历史结论延续（平台）",
        "",
        f"本节由平台将 Run {previous_run_id} 的结论与本轮结果确定性合并。"
        "旧结论不能因为模型没有重复输出就视为已修复；只有结构化反证才能关闭。",
    ]
    labels = (
        (STATE_RECONFIRMED, "本轮重新确认"),
        (STATE_CARRIED, "仍成立（相关文件本轮未变化，直接继承）"),
        (STATE_RECHECK, "需要重新确认（相关文件已变化，尚无充分反证）"),
    )
    for state, label in labels:
        items = groups[state]
        if not items:
            continue
        lines.extend(("", f"### {label} {len(items)} 条", ""))
        lines.extend(_line(item) for item in items)
    return "\n".join(lines).rstrip() + "\n"


def reconcile_result(
    result: dict,
    previous: Iterable[Mapping],
    *,
    changed_paths: Iterable[str],
    previous_run_id: int | None,
) -> dict:
    """Return a cumulative incremental result without treating model silence as resolution."""
    old_rows = [row for row in previous if str(row.get("fingerprint") or "")]
    if not old_rows or previous_run_id is None:
        return result

    merged = deepcopy(result)
    anomalies = list(merged.get("anomalies") or [])
    final_findings = list(merged.get("final_findings") or [])
    current = {str(item.get("fingerprint") or ""): item for item in anomalies}
    final_by_id = {
        str(item.get("fingerprint") or ""): item
        for item in final_findings
        if item.get("active") is not False
    }
    changed = {normalize_path(path) for path in changed_paths if str(path or "").strip()}
    groups = {STATE_RECONFIRMED: [], STATE_CARRIED: [], STATE_RECHECK: []}
    suppressed = 0
    matched_current: set[str] = set()

    for old in old_rows:
        fingerprint = str(old.get("fingerprint") or "")
        path = normalize_path(str(old.get("file_path") or ""))
        file_changed = bool(path and path in changed)
        matched_fingerprint = fingerprint if fingerprint in current else ""
        if not matched_fingerprint:
            scored = sorted(
                (
                    (_semantic_match(old, item), current_id)
                    for current_id, item in current.items()
                    if current_id not in matched_current
                ),
                reverse=True,
            )
            if scored and scored[0][0] >= 0.72:
                matched_fingerprint = scored[0][1]
        if matched_fingerprint:
            item = current[matched_fingerprint]
            item["baseline_state"] = STATE_RECONFIRMED
            item["baseline_run_id"] = previous_run_id
            if matched_fingerprint != fingerprint:
                item["baseline_previous_fingerprint"] = fingerprint
            item.setdefault("disposition", str(old.get("disposition") or "pending"))
            if matched_fingerprint in final_by_id:
                final_by_id[matched_fingerprint]["baseline_state"] = STATE_RECONFIRMED
                final_by_id[matched_fingerprint]["baseline_run_id"] = previous_run_id
                if matched_fingerprint != fingerprint:
                    final_by_id[matched_fingerprint]["baseline_previous_fingerprint"] = fingerprint
            matched_current.add(matched_fingerprint)
            groups[STATE_RECONFIRMED].append(item)
            continue
        if str(old.get("disposition") or "pending") == "ignored" and not file_changed:
            suppressed += 1
            continue
        state = STATE_RECHECK if file_changed else STATE_CARRIED
        item = _historical_anomaly(old, state, previous_run_id)
        anomalies.append(item)
        final_findings.append(_final_row(item))
        groups[state].append(item)

    merged["anomalies"] = anomalies
    merged["final_findings"] = final_findings
    merged["baseline_reconciliation"] = {
        "previous_run_id": previous_run_id,
        "previous_total": len(old_rows),
        "reconfirmed": len(groups[STATE_RECONFIRMED]),
        "carried_forward": len(groups[STATE_CARRIED]),
        "needs_recheck": len(groups[STATE_RECHECK]),
        "suppressed": suppressed,
    }
    section = _report_section(groups, previous_run_id)
    report = str(merged.get("report_markdown") or "").rstrip()
    merged["report_markdown"] = (report + "\n\n" + section).lstrip() if report else section
    if any(str(item.get("severity") or "") == "critical" for item in anomalies):
        merged["risk_level"] = "high"
    reasons = list(merged.get("risk_reasons") or [])
    reasons.append(
        f"累积基线：延续 {len(groups[STATE_CARRIED])} 条，"
        f"待复核 {len(groups[STATE_RECHECK])} 条，本轮重新确认 {len(groups[STATE_RECONFIRMED])} 条"
    )
    merged["risk_reasons"] = reasons
    return merged
