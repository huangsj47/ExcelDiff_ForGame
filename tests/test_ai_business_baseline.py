import json
from types import SimpleNamespace

from services.ai.business_baseline import business_digest, business_fingerprint, reconcile_business_result
from services.ai.report_document import render_structured_report
from tests.test_ai_structured_report import parse, report_item, structured


def old_run():
    value = parse(structured())
    return SimpleNamespace(id=123, response_payload=json.dumps({"report_items": value.report_items}))


def current():
    parsed = parse(structured(report_items=[]))
    return {"report_items": (), "report_sections": parsed.report_sections,
            "report_markdown": parsed.report_markdown + "\n## 复核说明\n尚未复核\n"}


def test_medium_findings_are_in_baseline_and_survive_omission_with_historical_evidence():
    previous = old_run()
    assert "人数由 4 改为 8" in business_digest(previous)
    result = reconcile_business_result(current(), previous, changed_paths=["匹配.xlsx"])
    assert len(result["report_items"]) == 1
    item = result["report_items"][0]
    assert item["baseline_state"] == "needs_recheck"
    assert item["evidence_refs"][0]["available"] is False
    assert item["evidence_refs"][0]["historical"] is True
    assert "尚未复核" in result["report_markdown"]
    assert "历史发现未在本轮重新取证" in result["report_markdown"]


def test_explicit_model_closure_is_audited_and_does_not_claim_independent_confirmation():
    previous = old_run()
    item = json.loads(previous.response_payload)["report_items"][0]
    result = current()
    result["baseline_updates"] = [{"fingerprint": business_fingerprint(item), "status": "fixed",
                                   "reason": "新配置已恢复为4"}]
    merged = reconcile_business_result(result, previous)
    assert not merged["report_items"]
    assert merged["business_closures"][0]["independently_verified"] is False
    assert "未独立核实" in merged["report_markdown"]


def test_verified_ruling_with_same_fingerprint_retracts_card_and_its_test_action():
    from services.ai.engine import EngineOutcome
    from services.ai.report_family import apply_business_rulings
    parsed = parse(structured(report_items=[report_item(finding_fingerprint="a"*16)]))
    outcome = EngineOutcome(status="succeeded", payload=parsed, report_items=parsed.report_items,
                            report_sections=parsed.report_sections, report_markdown=parsed.report_markdown)
    reduction = SimpleNamespace(by_fingerprint=lambda: {"a"*16: SimpleNamespace(active=False)})
    result = apply_business_rulings(outcome, reduction)
    assert not result.report_items
    assert "人数由 4 改为 8" not in result.report_markdown
    assert "发起匹配" not in result.report_markdown


def test_inherited_business_cards_respect_human_ignored_and_model_closed_anomalies():
    parsed = parse(structured(report_items=[report_item(finding_fingerprint="a"*16)]))
    previous = SimpleNamespace(id=1, response_payload=json.dumps({"report_items": parsed.report_items}))
    ignored = [{"fingerprint": "a"*16, "disposition": "ignored"}]
    assert not reconcile_business_result(current(), previous, previous_rows=ignored)["report_items"]
    reopened = reconcile_business_result(current(), previous, changed_paths=["匹配.xlsx"], previous_rows=ignored)
    assert len(reopened["report_items"]) == 1
    closed = current()
    closed["retracted_findings"] = [{"fingerprint": "a"*16}]
    assert not reconcile_business_result(closed, previous)["report_items"]
