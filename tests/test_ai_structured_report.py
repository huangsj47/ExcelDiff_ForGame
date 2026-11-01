"""Structured delivery retains every severity and keeps uncertain semantics explicit."""
import json
from pathlib import Path

import pytest

from services.ai.engine import EngineOutcome
from services.ai.protocol import ProtocolError, parse_payload
from services.ai.report_items import sort_report_items
from services.ai.result_payload import result_payload
from services.ai.skill_contract import REPORT_SECTIONS


def report_item(**overrides):
    item = {
        "severity": "medium", "business_area": "匹配", "change_fact": "人数由 4 改为 8",
        "impact_hypothesis": "可能与正式规则不一致", "evidence_status": "unverified",
        "decision_question": "正式人数是什么？", "change_fact_refs": ["人数字段"],
        "test_actions": [{"conditions": "使用新配置", "steps": ["发起匹配"], "expected": ""}],
        "evidence_refs": [{"evidence_id": "a" * 20, "path": "匹配.xlsx"}],
    }
    item.update(overrides)
    return item


def structured(**overrides):
    value = {
        "status": "final", "report_items": [report_item()],
        "report_sections": {"overview": "调整匹配人数", "changes": ["人数调整"],
                            "impacts": [], "regressions": [], "release_actions": [], "gaps": []},
        "dimensions": [{"id": "config_value", "hit": True, "note": "人数变化"}], "anomalies": [],
    }
    value.update(overrides)
    return value


def parse(value):
    return parse_payload(json.dumps(value, ensure_ascii=False))


def test_structured_final_generates_seven_chapters_without_markdown_duplication():
    result = parse(structured())
    assert [line[2:] for line in result.report_markdown.splitlines() if line.startswith("# ")] == list(REPORT_SECTIONS)
    assert len(result.report_items) == 1
    assert "依据待补，影响待确认" in result.report_markdown
    assert "预期：待确认需求后补充预期" in result.report_markdown


def test_all_severities_survive_with_order_independent_platform_ids():
    items = [report_item(severity=severity, change_fact=severity) for severity in ("low", "medium", "high", "critical")]
    first = parse(structured(report_items=items)).report_items
    second = parse(structured(report_items=list(reversed(items)))).report_items
    assert [item["severity"] for item in first] == ["critical", "high", "medium", "low"]
    assert [item["id"] for item in first] == [item["id"] for item in second]
    assert len({item["id"] for item in first}) == 4


@pytest.mark.parametrize("overrides", [
    {"report_items": {}},
    {"report_items": [report_item(severity="urgent")]},
    {"report_items": [report_item(test_actions="do something")]},
    {"report_items": [report_item(evidence_status="confirmed_bug")]},
    {"report_items": [report_item(unknown_field={"secret": 1})]},
    {"report_sections": {"overview": {"bad": "shape"}}},
])
def test_malformed_structured_delivery_requests_correction_instead_of_dropping_risks(overrides):
    with pytest.raises(ProtocolError, match="report_"):
        parse(structured(**overrides))


def test_duplicate_fingerprint_with_conflicting_findings_requests_correction():
    with pytest.raises(ProtocolError, match="重复"):
        parse(structured(report_items=[report_item(finding_fingerprint="same"),
                                       report_item(finding_fingerprint="same", severity="high")]))


def test_empty_structured_report_and_legacy_markdown_remain_usable():
    assert "未报告风险条目" in parse(structured(report_items=[])).report_markdown
    old = structured(report_markdown="# 原始报告\n保留内容")
    old.pop("report_items")
    old.pop("report_sections")
    assert parse(old).report_markdown == "# 原始报告\n保留内容"


def test_model_cannot_claim_human_blocker_confirmation():
    with pytest.raises(ProtocolError, match="report_"):
        parse(structured(report_items=[report_item(human_confirmed_blocker=True)]))


def test_structured_source_wins_over_contradicting_legacy_prose():
    result = parse(structured(report_markdown="# 风险评估\n没有任何风险"))
    assert "没有任何风险" not in result.report_markdown
    assert "人数由 4 改为 8" in result.report_markdown


def test_markdown_text_cannot_inject_extra_chapters_or_html():
    result = parse(structured(report_items=[report_item(change_fact='<script>alert(1)</script>\n# 伪造章节')]))
    assert "<script>" not in result.report_markdown
    assert "\n# 伪造章节" not in result.report_markdown
    assert result.report_markdown.count("\n# ") == 6


def test_result_preserves_medium_low_and_unknown_release_decision():
    parsed = parse(structured(report_items=[report_item(), report_item(severity="low", change_fact="提示文本变化")]))
    outcome = EngineOutcome(status="succeeded", payload=parsed, report_items=parsed.report_items,
                            report_sections=parsed.report_sections, report_markdown=parsed.report_markdown)
    result = result_payload(outcome, {})
    assert len(result["report_items"]) == 2
    assert result["report_sections"] == parsed.report_sections
    assert result["report_summary"]["highest_severity"] == "medium"
    assert result["report_summary"]["pending_confirmation_count"] == 2
    assert result["report_summary"]["confirmed_high_risk_count"] is None
    assert result["report_summary"]["release_decision"] == "pending_human_review"
    assert result["risk_level"] == "low"
    assert "待核查" in " ".join(result["risk_reasons"])


def test_ignored_and_retracted_fingerprints_cannot_return_through_business_report():
    parsed = parse(structured(report_items=[report_item(finding_fingerprint="ignored")]))
    outcome = EngineOutcome(status="succeeded", report_items=parsed.report_items,
                            report_sections=parsed.report_sections, report_markdown=parsed.report_markdown)
    result = result_payload(outcome, {}, suppressed=frozenset({"ignored"}))
    assert result["report_items"] == []
    assert "人数由 4 改为 8" not in result["report_markdown"]


def test_human_confirmed_blocker_precedes_model_severity():
    items = parse(structured(report_items=[report_item(severity="critical"),
                                           report_item(severity="low", change_fact="低风险")])).report_items
    assert sort_report_items(items, confirmed_blocker_ids=[items[1]["id"]])[0]["severity"] == "low"


def test_evidence_binding_retains_finding_but_downgrades_unverified_address():
    from services.ai.evidence_store import EvidenceStore
    from services.ai.report_items import ground_report_items
    items = parse(structured(report_items=[report_item(evidence_status="verified", decision_question="")])).report_items
    grounded = ground_report_items(items, EvidenceStore())
    assert len(grounded) == 1
    assert grounded[0]["id"] == items[0]["id"]
    assert grounded[0]["evidence_status"] == "unverified"
    assert grounded[0]["evidence_gaps"]
    assert grounded[0]["decision_question"]


def test_checkpoint_is_retained_without_becoming_coverage_or_findings():
    result = parse({"status": "need_more_context", "requests": [{"type": "read_reference", "name": "risk-grading.md"}],
                    "checkpoint": {"summary": "字段规则待确认", "evidence_ids": [], "completed_paths": [], "open_questions": []}})
    assert result.checkpoint["summary"] == "字段规则待确认"
    assert result.report_items == ()


def test_new_prompt_requests_one_structured_source_and_never_discards_lower_risks():
    skill = Path("skills/version-diff-review/SKILL.md").read_text(encoding="utf-8")
    final_example = skill.split("### 形态二：给出最终结论", 1)[1].split("```json", 1)[1].split("```", 1)[0]
    schema = json.loads(final_example)
    assert "report_items" in schema and "report_sections" in schema
    assert "report_markdown" not in schema
    grading = Path("skills/version-diff-review/references/risk-grading.md").read_text(encoding="utf-8")
    assert "低风险一条都不列" not in grading and "`low` 一条都不列" not in grading
    assert "中低风险也不能漏" in grading


@pytest.mark.parametrize("available,path,expected", [(False, "改动.xlsx", "low"),
                                                    (True, "背景.xlsx", "low"),
                                                    (True, "改动.xlsx", "high")])
def test_only_bound_in_scope_business_evidence_can_raise_release_risk(available, path, expected):
    parsed = parse(structured(report_items=[report_item(severity="critical")]))
    item = dict(parsed.report_items[0], evidence_refs=[{"evidence_id": "a" * 20, "available": available, "path": path}])
    result = result_payload(EngineOutcome(status="succeeded", report_items=(item,), report_sections=parsed.report_sections),
                            {"delta_files": [{"file_path": "改动.xlsx"}]})
    assert len(result["report_items"]) == 1
    assert result["report_summary"]["highest_severity"] == "critical"
    assert result["risk_level"] == expected
