from dataclasses import replace

from services.ai.engine import EngineOutcome
from services.ai.protocol import ground_payload
from services.ai.report_family import apply_business_rulings
from services.ai.report_items import ground_report_items
from services.ai.rules import anomaly_fingerprint
from tests.test_ai_engine import COMMIT, TABLE, _anomaly, _scope
from tests.test_ai_structured_report import parse, report_item, structured


def test_explicit_model_reference_binds_to_real_fingerprint_and_review_can_retract_the_card():
    from types import SimpleNamespace
    parsed = ground_payload(parse(structured(
        anomalies=[_anomaly(finding_ref="risk_1", commit=COMMIT[:12])],
        report_items=[report_item(finding_ref="risk_1")],
    )), _scope())
    bound = ground_report_items(parsed.report_items, {}, anomalies=parsed.anomalies)
    fingerprint = anomaly_fingerprint(parsed.anomalies[0])
    assert bound[0]["finding_fingerprint"] == fingerprint
    assert parsed.anomalies[0].commit == COMMIT
    result = apply_business_rulings(EngineOutcome(status="succeeded", report_items=bound,
        report_sections=parsed.report_sections), SimpleNamespace(by_fingerprint=lambda: {
            fingerprint: SimpleNamespace(active=False)}))
    assert not result.report_items
    assert "发起匹配" not in result.report_markdown


def test_ambiguous_reference_never_merges_or_silently_drops_distinct_cards():
    parsed = parse(structured(anomalies=[_anomaly(finding_ref="same")], report_items=[
        report_item(finding_ref="same"), report_item(finding_ref="same", change_fact="另一处字段变化")]))
    bound = ground_report_items(parsed.report_items, {}, anomalies=parsed.anomalies)
    assert len(bound) == len({item["id"] for item in bound}) == 2
    assert all("关联键" in item["evidence_gaps"][0] for item in bound)
