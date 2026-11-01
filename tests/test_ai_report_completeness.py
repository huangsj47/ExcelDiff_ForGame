from services.ai.engine import EngineOutcome
from services.ai.result_payload import result_payload
from services.ai.rules import anomaly_fingerprint
from tests.test_ai_engine import _anomaly
from tests.test_ai_structured_report import parse, report_item, structured


def deliver(items):
    parsed = parse(structured(report_items=items, anomalies=[_anomaly()]))
    outcome = EngineOutcome(status="succeeded", anomalies=parsed.anomalies, report_items=parsed.report_items,
                            report_sections=parsed.report_sections, report_markdown=parsed.report_markdown)
    return result_payload(outcome, {})


def test_a_high_risk_cannot_coexist_with_a_no_risk_business_report():
    result = deliver([])
    assert len(result["anomalies"]) == len(result["report_items"]) == 1
    assert "本次未报告风险条目" not in result["report_markdown"]
    assert _anomaly()["title"] in result["report_markdown"]


def test_unrelated_medium_cards_do_not_hide_missing_critical_anomalies():
    result = deliver([report_item()])
    assert len(result["report_items"]) == 2
    assert result["report_items"][0]["severity"] == "critical"
    assert "平台保全" in result["report_markdown"]


def test_an_explicitly_linked_card_is_not_duplicated():
    parsed = parse(structured(anomalies=[_anomaly()]))
    result = deliver([report_item(finding_fingerprint=anomaly_fingerprint(parsed.anomalies[0]))])
    assert len(result["report_items"]) == 1


def test_preserved_card_keeps_original_evidence_without_claiming_it_was_verified():
    result = deliver([])
    item = result["report_items"][0]
    assert item["business_area"] != "config_id"
    assert item["evidence_status"] == "unverified"
    assert item["evidence_refs"] == []
    assert item["evidence_notes"] == _anomaly()["evidence"]
    assert "原异常依据（未重新核验）" in result["report_markdown"]


def test_ignored_anomaly_is_not_restored_as_a_business_card():
    parsed = parse(structured(report_items=[], anomalies=[_anomaly()]))
    outcome = EngineOutcome(status="succeeded", anomalies=parsed.anomalies, report_items=(),
                            report_sections=parsed.report_sections, report_markdown=parsed.report_markdown)
    result = result_payload(outcome, {}, suppressed=frozenset({anomaly_fingerprint(parsed.anomalies[0])}))
    assert result["report_items"] == []


def test_risk_heading_contains_the_business_fact_not_only_a_machine_identifier():
    result = parse(structured())
    heading = next(line for line in result.report_markdown.splitlines() if line.startswith("## R1"))
    assert "人数由 4 改为 8" in heading


def test_suppressed_or_retracted_anomalies_cannot_keep_an_empty_report_at_high_risk():
    from dataclasses import replace

    parsed = parse(structured(report_items=[], anomalies=[_anomaly()]))
    anomaly = parsed.anomalies[0]
    fp = anomaly_fingerprint(anomaly)
    outcome = EngineOutcome(status="succeeded", anomalies=(anomaly,), report_items=(),
                            report_sections=parsed.report_sections, report_markdown=parsed.report_markdown)
    payload = {"delta_files": [{"file_path": anomaly.file_path}], "summary": {}}
    ignored = result_payload(outcome, payload, suppressed=frozenset({fp}))
    retracted = result_payload(replace(outcome, verdict={"rows": [{"fingerprint": fp, "active": False}]}), payload)
    for result in (ignored, retracted):
        assert not result["anomalies"] and not result["report_items"]
        assert result["risk_level"] == "low"
        assert not any("关联不足" in reason for reason in result["risk_reasons"])


def test_risk_reason_does_not_call_anomaly_only_scoring_a_zero_item_business_report():
    parsed = parse(structured(report_items=[], anomalies=[_anomaly()]))
    outcome = EngineOutcome(status="succeeded", anomalies=parsed.anomalies, report_items=(),
                            report_sections=parsed.report_sections, report_markdown=parsed.report_markdown)
    result = result_payload(outcome, {"delta_files": [{"file_path": parsed.anomalies[0].file_path}]})
    assert "处置异常 1 条" in " ".join(result["risk_reasons"])
