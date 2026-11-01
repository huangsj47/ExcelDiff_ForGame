"""Canonical business findings, independent of the high-risk anomaly workflow.

Malformed findings fail the final protocol so the model can correct them. No risk is
clipped, severity-filtered or recovered by parsing prose. IDs belong to the platform;
model confidence and evidence status never stand in for a person's release decision.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

REPORT_SEVERITIES = ("critical", "high", "medium", "low")
EVIDENCE_STATUSES = ("verified", "partial", "unverified")
SECTION_KEYS = ("overview", "changes", "impacts", "regressions", "release_actions", "gaps")
ITEM_KEYS = frozenset((
    "id", "title", "finding_ref", "finding_fingerprint", "severity", "business_area", "change_fact", "change_fact_refs",
    "impact_hypothesis", "evidence_status", "decision_question", "test_actions", "evidence_refs",
))
REFERENCE_KEYS = frozenset(("evidence_id", "repository_id", "repository", "commit", "path", "lines"))


class ReportValidationError(ValueError):
    """A correctable schema error, rather than an item to silently discard."""


def _text(value: Any, location: str, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ReportValidationError(f"{location} 必须是字符串")
    result = value.strip()
    if required and not result:
        raise ReportValidationError(f"{location} 不得为空")
    return result


def _list(value: Any, location: str) -> list:
    if not isinstance(value, (list, tuple)):
        raise ReportValidationError(f"{location} 必须是数组")
    return list(value)


def _strings(value: Any, location: str) -> list[str]:
    return [_text(item, f"{location}[{index}]", required=True)
            for index, item in enumerate(_list(value, location))]


def _object(value: Any, keys: frozenset | set, location: str) -> dict:
    if not isinstance(value, Mapping):
        raise ReportValidationError(f"{location} 必须是对象")
    unknown = set(value) - keys
    if unknown:
        raise ReportValidationError(f"{location} 存在未知字段：{', '.join(sorted(map(str, unknown)))}")
    return dict(value)


def normalize_report_sections(value: Any) -> dict:
    raw = _object(value, set(SECTION_KEYS), "report_sections")
    return {
        "overview": _text(raw.get("overview"), "report_sections.overview", required=True),
        **{key: _strings(raw.get(key, []), f"report_sections.{key}") for key in SECTION_KEYS[1:]},
    }


def _references(value: Any, location: str) -> list[dict]:
    result = []
    for index, entry in enumerate(_list(value, location)):
        label = f"{location}[{index}]"
        if isinstance(entry, str):
            entry = {"evidence_id": entry}
        raw = _object(entry, REFERENCE_KEYS, label)
        reference = {key: _text(val, f"{label}.{key}") for key, val in raw.items()}
        _text(reference.get("evidence_id"), f"{label}.evidence_id", required=True)
        result.append(reference)
    return result


def _actions(value: Any, location: str) -> list[dict]:
    result = []
    for index, entry in enumerate(_list(value, location)):
        label = f"{location}[{index}]"
        raw = _object(entry, {"conditions", "steps", "expected"}, label)
        steps = _strings(raw.get("steps"), f"{label}.steps")
        if not steps:
            raise ReportValidationError(f"{label}.steps 不得为空")
        result.append({
            "conditions": _text(raw.get("conditions"), f"{label}.conditions", required=True),
            "steps": steps,
            "expected": _text(raw.get("expected", ""), f"{label}.expected") or "待确认需求后补充预期",
        })
    if not result:
        raise ReportValidationError(f"{location} 不得为空，每条风险须给出验证动作")
    return result


def stable_report_id(item: Mapping) -> str:
    """Stable across reorderings and severity changes when a fingerprint is supplied."""
    identity = item.get("finding_fingerprint") or json.dumps(
        {key: item.get(key, "") for key in ("business_area", "change_fact", "impact_hypothesis")},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return "R-" + hashlib.sha256(str(identity).encode("utf-8")).hexdigest()[:12]


def sort_report_items(items: Sequence[Mapping], *, confirmed_blocker_ids: Sequence[str] = ()) -> tuple[dict, ...]:
    """Human blocking decisions come from trusted platform state only."""
    blockers = frozenset(confirmed_blocker_ids)
    return tuple(dict(item) for item in sorted(
        items, key=lambda item: (item["id"] not in blockers,
                                 REPORT_SEVERITIES.index(item["severity"]), item["id"]),
    ))


def normalize_report_items(value: Any) -> tuple[dict, ...]:
    result, seen = [], set()
    for index, entry in enumerate(_list(value, "report_items")):
        label = f"report_items[{index}]"
        raw = _object(entry, ITEM_KEYS, label)
        item = {key: _text(raw.get(key, ""), f"{label}.{key}", required=True)
                for key in ("severity", "business_area", "change_fact", "impact_hypothesis", "evidence_status")}
        if item["severity"] not in REPORT_SEVERITIES:
            raise ReportValidationError(f"{label}.severity 必须是 {REPORT_SEVERITIES} 之一")
        if item["evidence_status"] not in EVIDENCE_STATUSES:
            raise ReportValidationError(f"{label}.evidence_status 必须是 {EVIDENCE_STATUSES} 之一")
        item.update(
            title=_text(raw.get("title", ""), f"{label}.title"),
            finding_ref=_text(raw.get("finding_ref", ""), f"{label}.finding_ref"),
            finding_fingerprint=_text(raw.get("finding_fingerprint", ""), f"{label}.finding_fingerprint"),
            decision_question=_text(raw.get("decision_question", ""), f"{label}.decision_question"),
            change_fact_refs=_strings(raw.get("change_fact_refs", []), f"{label}.change_fact_refs"),
            test_actions=_actions(raw.get("test_actions"), f"{label}.test_actions"),
            evidence_refs=_references(raw.get("evidence_refs", []), f"{label}.evidence_refs"),
        )
        if item["evidence_status"] == "verified" and not item["evidence_refs"]:
            raise ReportValidationError(f"{label}.evidence_refs 不得为空，verified 必须提供证据")
        if item["evidence_status"] != "verified" and not item["decision_question"]:
            raise ReportValidationError(f"{label}.decision_question 不得为空，未知语义须写待确认问题")
        item["id"] = stable_report_id(item)
        if item["id"] in seen:
            raise ReportValidationError(f"report_items 存在重复身份 {item['id']}，请合并重复项或修正 finding_fingerprint")
        seen.add(item["id"])
        result.append(item)
    return sort_report_items(result)


def parse_structured_delivery(raw: Mapping) -> tuple[tuple[dict, ...], dict, str]:
    from services.ai.report_document import render_structured_report
    items = normalize_report_items(raw.get("report_items"))
    sections = normalize_report_sections(raw.get("report_sections"))
    return items, sections, render_structured_report(items, sections)


def ground_report_items(items: Sequence[Mapping], store: Any, *, anomalies=()) -> tuple[dict, ...]:
    """Bind coordinates to retrieved evidence; never promote model claims to verified.

    Retain even a finding whose evidence cannot be resolved. Available evidence proves
    provenance only, not that a business hypothesis is a confirmed defect.
    """
    from services.ai.context_tools import _request_of_key

    result = []
    by_id, verify = getattr(store, "by_id", None), getattr(store, "verify", None)
    from services.ai.rules import anomaly_fingerprint
    linked = {}
    card_refs = [item.get("finding_ref") for item in items]
    for anomaly in anomalies:
        if anomaly.finding_ref:
            linked.setdefault(anomaly.finding_ref, []).append(anomaly)
    for original in items:
        item = dict(original)
        matches = linked.get(item.get("finding_ref"), ())
        bound = len(matches) == 1 and card_refs.count(item.get("finding_ref")) == 1
        if bound:
            # 模型只提供显式关联键，真实指纹由接地后的异常计算，不要求模型计算哈希。
            item["finding_fingerprint"] = anomaly_fingerprint(matches[0])
            item["id"] = stable_report_id(item)
        references, gaps = [], []
        if item.get("finding_ref") and (len(matches) > 1 or card_refs.count(item["finding_ref"]) > 1):
            gaps.append("异常关联键重复，未绑定复核裁决；保留待人工核查")
        for reference in item.get("evidence_refs", ()):
            identifier = reference["evidence_id"]
            evidence = by_id(identifier) if callable(by_id) else None
            valid = bool(evidence is not None and callable(verify) and verify(identifier)
                         and not evidence.meta.get("tool_failed") and not evidence.meta.get("tool_empty"))
            if not valid:
                references.append({"evidence_id": identifier, "available": False})
                gaps.append(f"证据 {identifier} 未取回、为空或校验失败，原引用坐标未采信")
                continue
            request = _request_of_key(evidence.meta["cache_key"])
            references.append({"evidence_id": identifier, "available": True,
                               "repository_id": request.repository_id, "commit": request.commit,
                               "path": request.path, "lines": request.lines})
        item["evidence_refs"] = references
        item["evidence_gaps"] = gaps
        item["evidence_status"] = "partial" if any(ref.get("available") for ref in references) else "unverified"
        # 不把每条已取回来源的事实都补成同一句泛化问题；真正的业务问句由模型给出。
        # 地址缺失仍明确要求补证，不能把可定位性不足隐藏成「无需确认」。
        item["decision_question"] = item.get("decision_question") or (
            "请补取可定位依据，再确认该变化与影响的关联" if gaps or not references else ""
        )
        result.append(item)
    return sort_report_items(result)
