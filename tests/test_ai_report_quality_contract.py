"""Unknown business meaning must survive as a condition, never a fixed oracle."""
import pytest

from services.ai.report_document import render_structured_report
from services.ai.report_items import ReportValidationError, ground_report_items, normalize_report_items
from tests.test_ai_structured_report import report_item


def test_legacy_expected_is_auditable_but_not_a_requirement():
    item = normalize_report_items([report_item(test_actions=[{
        "conditions": "领取新奖励", "steps": ["领取并记录"], "expected": "必须等于旧奖励价值",
    }])])[0]
    assert item["test_actions"][0]["expected_status"] == "needs_confirmation"
    report = render_structured_report([item], {"overview": "奖励类型改变"})
    business, audit = report.split("取证索引（技术详情）", 1)
    assert "预期：必须等于旧奖励价值" not in business
    assert "待确认需求后补充预期" in business
    assert "必须等于旧奖励价值" in audit


def test_known_expected_requires_explicit_requirement_evidence():
    with pytest.raises(ReportValidationError, match="expected_refs"):
        normalize_report_items([report_item(test_actions=[{
            "conditions": "已知规则", "steps": ["验证"], "expected": "返回目标值",
            "expected_status": "known", "expected_refs": [],
        }])])


def test_business_question_prevents_fixed_expected_even_with_model_known_status():
    item = normalize_report_items([report_item(test_actions=[{
        "conditions": "领取", "steps": ["记录实际到账"], "expected": "一定等值",
        "expected_status": "known", "expected_refs": ["e" * 20],
    }])])[0]
    assert item["test_actions"][0]["expected_status"] == "needs_confirmation"


def test_unavailable_requirement_evidence_cannot_keep_known_expected():
    item = normalize_report_items([report_item(decision_question="", evidence_status="verified",
        evidence_refs=["e" * 20], test_actions=[{
            "conditions": "读取配置", "steps": ["校验"], "expected": "必须相等",
            "expected_status": "known", "expected_refs": ["e" * 20],
        }])])[0]
    grounded = ground_report_items([item], {})[0]
    assert grounded["test_actions"][0]["expected_status"] == "needs_confirmation"


def test_retrieved_expected_rule_is_still_not_human_requirement_confirmation():
    from services.ai.budget import ContextItem
    from services.ai.evidence_store import EvidenceStore, evidence_id_of
    store = EvidenceStore()
    key = ("read_reference", "", "", "rules.md", "", "", "")
    text = "The new reward grants the declared item."
    eid = evidence_id_of(key, text)
    store[key] = ContextItem("read_reference", "rules", text, {"cache_key": key, "evidence_id": eid})
    item = normalize_report_items([report_item(decision_question="", evidence_status="verified",
        evidence_refs=[eid], test_actions=[{
            "conditions": "触发奖励", "steps": ["领取"], "expected": "到账符合规则",
            "expected_status": "known", "expected_refs": [eid],
        }])])[0]
    grounded = ground_report_items([item], store)[0]
    assert grounded["test_actions"][0]["expected_status"] == "known"
    report = render_structured_report([grounded], {"overview": "奖励类型改变"})
    assert "预期：到账符合规则" in report
    assert "不等于业务需求已获人工确认" in report


def test_unqualified_impact_is_rendered_as_a_conditional_claim():
    item = normalize_report_items([report_item(impact_hypothesis="实际吞吐提高十倍")])[0]
    assert item["impact_basis"] == "conditional"
    report = render_structured_report([item], {"overview": "间隔缩短"})
    assert "仅在待确认前提成立时，可能出现：实际吞吐提高十倍" in report


def test_supported_impact_requires_conditions_and_evidence():
    with pytest.raises(ReportValidationError, match="impact_"):
        normalize_report_items([report_item(impact_basis="supported", impact_conditions=[], impact_refs=[])])


def test_impact_evidence_failure_downgrades_basis_without_discarding_claim():
    item = normalize_report_items([report_item(impact_basis="supported",
        impact_conditions=["调度仅由间隔决定"], impact_refs=["e" * 20])])[0]
    grounded = ground_report_items([item], {})[0]
    assert grounded["impact_basis"] == "conditional"
    assert grounded["impact_hypothesis"] == item["impact_hypothesis"]


def test_coverage_distinguishes_business_cards_from_disposition_findings():
    from services.ai.coverage_ledger import build_ledger
    ledger = build_ledger(request_payload={}, executed=[], response_payload={
        "report_items": [{"finding_ref": f"r{i}"} for i in range(6)],
        "anomalies": [{"finding_ref": f"r{i}"} for i in range(3)],
        "final_findings": [{"source": "synthesis", "active": True} for _ in range(3)],
    })
    rows = dict(ledger["rows"])
    assert "业务条目 6 条" in rows["业务条目 / 处置异常"]
    assert "处置异常 3 条" in rows["业务条目 / 处置异常"]
    assert "正文没写全" not in str(rows)
    assert "明显少于" not in str(rows)


@pytest.mark.parametrize("fact,title,release", [
    ("买价下调，卖价及交易入口尚未取回", "已形成无成本循环获利", "必须先改定价才能放行"),
    ("全表只有少数行配置了限制，目标行未知", "指定目标会导致整表失败", "必须先补目标行才准发布"),
])
def test_uncertain_claim_cannot_become_release_blocker_via_freeform_sections(fact, title, release):
    item = normalize_report_items([report_item(change_fact=fact, title=title)])[0]
    report = render_structured_report([item], {"overview": "变更", "release_actions": [release]})
    business, audit = report.split("取证索引（技术详情）", 1)
    assert release not in business
    assert release in audit
    assert "关注标题：" + title in business
    assert "变化（待核实描述）：" + fact in business
    assert "由负责人决定修复与放行" in business


def test_unassociated_release_instruction_without_findings_is_audited_not_authorized():
    release = "直接上线即可"
    report = render_structured_report([], {"overview": "无业务条目", "release_actions": [release]})
    business, audit = report.split("取证索引（技术详情）", 1)
    assert release not in business and release in audit


def test_platform_audit_keeps_omitted_open_needs_and_their_exact_request():
    from services.ai.coverage_ledger import build_ledger
    ledger = build_ledger(request_payload={}, executed=[], response_payload={"context": {
        "evidence_followup": [{"need_id": "N1", "question": "读取未确认行", "kind": "retrievable",
            "status": "open", "platform_reason": "本轮额度不足", "finding_refs": ["risk_1"],
            "requests": [{"type": "file_content", "path": "table.xlsx", "lines": "#1!A262:H262"}],
            "evidence_ids": []}],
    }})
    audit = "\n".join(ledger["gaps"])
    for value in ("N1", "读取未确认行", "本轮额度不足", "table.xlsx", "#1!A262:H262", "risk_1"):
        assert value in audit


def test_platform_audit_retains_resolution_and_external_semantics():
    from services.ai.coverage_ledger import build_ledger
    needs = [
        {"need_id": "N1", "question": "已取得定义", "kind": "retrievable", "status": "resolved",
         "resolution_reason": "与规则逐条核对", "evidence_ids": ["e" * 20]},
        {"need_id": "N2", "question": "需求口径是什么", "kind": "external_semantics", "status": "open"},
    ]
    ledger = build_ledger(request_payload={}, executed=[], response_payload={"context": {"evidence_followup": needs}})
    audit = "\n".join(ledger["gaps"])
    for value in ("N1", "已声明解决", "与规则逐条核对", "e" * 20, "N2", "业务语义待确认"):
        assert value in audit


def test_summary_and_impact_prose_cannot_override_conditional_findings_or_release_authority():
    overview = "检查分支会阻断发布，是唯一阻断项"
    impact = "生成物无法产出，所有发布环节全部阻塞"
    fact = "约束字段由 0 改为 1"
    item = normalize_report_items([report_item(change_fact=fact,
        impact_conditions=["实际导出入口调用该分支且异常未被处理"])])[0]
    report = render_structured_report([item], {"overview": overview, "changes": [fact], "impacts": [impact]})
    assert "本节为模型归纳，含待核实推断，不构成发布裁决" in report
    assert report.index("本节为模型归纳") < report.index(overview)
    assert "实际后果须满足对应条目的推断前提并经验证" in report
    assert report.index("实际后果须满足") < report.index(impact)
    assert overview in report and impact in report and fact in report
