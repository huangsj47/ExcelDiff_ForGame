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
from typing import Any, Iterable, Mapping

from services.ai.report_document import severity_label
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
        # 历史结论的断言清单：上一轮存下来的那一份**原样带回**（它在库里就是 JSON 文本）。
        # 不带的话，这条结论的 `claims` 会在「这一轮没有被模型重新报到」之后消失 ——
        # 而它恰恰是「这条结论当时凭什么算核实过了」的唯一记录。
        # 逐条断言的**裁决**不带：那是上一轮的复核结论，这一轮没核过它（`verify_basis`
        # 保持空 = 未取证），拿上一轮的状态冒充这一轮的核实结果是另一回事。
        "claims": _claims(row.get("claims")),
        "pending_claims": 0,
        "original_title": str(row.get("title") or "（无标题）"),
        "verify_basis": "",
        "verify_basis_label": "",
    }


def _claims(raw: Any) -> list:
    """历史行里的 `claims`（JSON 文本）读成数组。坏数据回空数组，不抛。"""
    if isinstance(raw, list):
        return [dict(item) for item in raw if isinstance(item, dict)]
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    return [dict(item) for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []


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
        # 与 `_historical_anomaly` 同一份（键一个不少，读侧不必分支）。
        "claims": item.get("claims") or [],
        "pending_claims": 0,
        "original_title": item.get("title") or "",
        "verify_basis": "",
        "verify_basis_label": "",
        "baseline_state": item["baseline_state"],
        "baseline_run_id": item["baseline_run_id"],
    }


def _line(item: Mapping) -> str:
    """一条历史结论那一行：**标题 + 文件 + 严重度**（2026-09-24，run 63 补的等级）。

    从前只有标题与文件，14 条读下来分不出先后 —— 而这一节要回答的正是「上一轮那些
    问题里，哪些还压着」。等级是平台算好的中文名（`report_document.severity_label`，
    与异常面板、导出同一份映射），不在这里另写一套。
    """
    where = f"（`{item.get('file_path')}`）" if item.get("file_path") else ""
    level = severity_label(item.get("severity"))
    suffix = f" · 严重度 {level}" if level and level != "-" else ""
    return f"- **{item.get('title') or '（无标题）'}**{where}{suffix}"


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
    """「历史结论延续（平台）」这一节 —— **2026-09-25 起它是「账」，不是清单**。

    ## 角色反转过一次（run 63 → 本次）

    2026-09-24（run 63）这一节被做成**确定性清单**，理由是：模型自己在正文里写一节
    「历史结论状态」、平台再列一遍同一批 15 条标题，读者看不出哪一份算数。那时的分工是
    「本节为准」。

    现在产品要求**正文的「风险评估」承担当前仍成立的全部问题**（本轮新增 + 上次遗留仍然
    成立的），于是这里再当一次清单就成了同一批标题的第二份 —— 正是 run 63 那个毛病换个
    方向再来一遍。所以它退回「账」：**总数与分组计数 + 「需要重新确认」那几条逐条**
    （那几条要人真的去看），其余（仍成立 / 本轮重新确认）只报数。
    全量的结构化清单在报告末尾「异常清单（平台按门槛过滤后）」那张表里
    （`report_document` 渲染 `anomalies`，那个集合按契约就是当前仍成立的全集），
    不必靠这一节传。

    「只有结构化反证才能关闭」这句仍然在：旧结论不能因为模型没重复输出就当已修复。
    """
    labels = (
        (STATE_RECONFIRMED, "本轮重新确认"),
        (STATE_CARRIED, "仍成立（相关文件本轮未变化，直接继承）"),
        (STATE_RECHECK, "需要重新确认（相关文件已变化，尚无充分反证）"),
    )
    total = sum(len(groups[state]) for state, _ in labels)
    counts = "、".join(
        f"{len(groups[state])} 条{label.split('（')[0]}" for state, label in labels if groups[state]
    )
    lines = [
        "## 历史结论延续（平台）",
        "",
        f"上一轮（Run {previous_run_id}）报过的问题，本节由平台按指纹与本轮结果确定性"
        f"合并，共 {total} 条：{counts}。"
        "旧结论不能因为模型没有重复输出就视为已修复；只有结构化反证才能关闭。",
        "",
        "**逐条清单以正文的「风险评估」为准**（那一节是当前仍成立的问题全集）。"
        "本节只记数，并列出**需要重新确认**的那几条；全量的结构化清单在末尾的"
        "「异常清单（平台按门槛过滤后）」里。",
    ]
    recheck = groups[STATE_RECHECK]
    if recheck:
        lines.extend(("", f"### 需要重新确认 {len(recheck)} 条", ""))
        lines.extend(_line(item) for item in recheck)
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
