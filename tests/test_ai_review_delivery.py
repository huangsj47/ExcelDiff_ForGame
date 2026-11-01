import json

import pytest

from services.ai.protocol import ProtocolError, ground_payload
from tests.test_ai_engine import _scope
from tests.test_ai_structured_report import parse, structured


def test_top_level_review_verdict_survives_protocol_grounding_and_retracts_the_finding():
    from tests.test_ai_verify_verdict import _critical_round, _run
    value = structured(report_items=[], verdicts=[{
        "finding_id": "F1", "verdict": "retracted", "reason": "同一提交的生成物已同步删除",
        "evidence_refs": ["config/[30]道具表_CfgItem.xlsx:12"],
    }])
    payload = ground_payload(parse(value), _scope())
    assert len(payload.verdicts) == 1
    result = _run(_critical_round(json.dumps(value, ensure_ascii=False))).outcome
    assert result.anomalies == ()
    assert result.verdict["rows"][0]["active"] is False
    assert "同一提交的生成物已同步删除" in result.report_markdown
    assert "```json" not in result.report_markdown


@pytest.mark.parametrize("verdicts", ["bad", [{"finding_id": "F1", "verdict": "invalid"}], [
    {"finding_id": "F1", "verdict": "confirmed"}, {"finding_id": "F1", "verdict": "retracted"}]])
def test_malformed_review_delivery_requires_correction(verdicts):
    with pytest.raises(ProtocolError, match="verdicts"):
        parse(structured(verdicts=verdicts))
