"""Business readers see priorities and actions; exact evidence remains auditable."""
from services.ai.report_document import render_structured_report
from tests.test_ai_structured_report import parse, report_item, structured


def test_short_labels_are_shared_by_risk_summary_details_actions_and_evidence_index():
    parsed = parse(structured(report_items=[report_item(), report_item(severity="high", change_fact="领取时间为空")]))
    report = parsed.report_markdown
    assert "风险速览" in report
    assert "## R1 · 高" in report and "## R2 · 中" in report
    assert "## 验证 R1" in report and "## 验证 R2" in report
    assert "取证索引（技术详情）" in report
    for item in parsed.report_items:
        assert item["id"] in report.split("取证索引（技术详情）", 1)[1]
        assert item["id"] not in report.split("取证索引（技术详情）", 1)[0]


def test_exact_evidence_coordinates_are_kept_at_the_end_not_repeated_in_each_card():
    ref = {"evidence_id": "e" * 20, "repository_id": "7", "commit": "a" * 40,
           "path": "tables/Unit.xlsx", "lines": "3-5", "available": True}
    item = dict(parse(structured()).report_items[0], evidence_refs=[ref], evidence_status="partial")
    report = render_structured_report([item], {"overview": "变更", "gaps": ["读取方仍待确认"]})
    business, technical = report.split("取证索引（技术详情）", 1)
    assert ref["evidence_id"] not in business and ref["commit"] not in business
    for value in (ref["evidence_id"], ref["commit"], "tables/Unit", "3"):
        assert value in technical
    assert "读取方仍待确认" in business
    assert "来源已取回，影响待确认" in business


def test_long_fact_is_shortened_only_in_navigation_and_never_lost_from_detail():
    fact = "参数调整" * 60
    report = parse(structured(report_items=[report_item(title="人数上限由4增至8", change_fact=fact)])).report_markdown
    heading = next(line for line in report.splitlines() if line.startswith("## R1"))
    assert len(heading) < 110
    assert "变化：" + fact in report


def test_legacy_title_fallback_does_not_cut_the_key_value_at_the_end():
    fact = "较长的业务对象说明" * 10 + "；入局消耗由10改为空"
    report = parse(structured(report_items=[report_item(change_fact=fact)])).report_markdown
    summary = report.split("风险速览", 1)[1].split("## R1", 1)[0]
    assert "入局消耗由10改为空" in summary


def test_next_step_does_not_skip_the_unknown_business_precondition():
    report = parse(structured()).report_markdown
    assert "下一步：先确认：正式人数是什么？" in report


def test_unbound_original_evidence_is_not_described_as_missing_coordinates():
    item = dict(parse(structured()).report_items[0], evidence_refs=[], evidence_notes=["Unit.xlsx 第3行 上限4→8"])
    report = render_structured_report([item], {"overview": "上限调整"})
    assert "尚未绑定本轮取证回执" in report
    assert "尚无可定位证据" not in report


def test_unknown_expected_result_is_an_explicit_confirmation_action():
    report = parse(structured()).report_markdown
    assert "先确认：正式人数是什么？" in report
    assert "待确认需求后补充预期" in report


def test_grounding_does_not_invent_a_generic_question_when_all_sources_exist():
    from services.ai.budget import ContextItem
    from services.ai.evidence_store import EvidenceStore, evidence_id_of
    from services.ai.report_items import ground_report_items

    store = EvidenceStore()
    key = ("file_diff", "a" * 40, "tables/Unit.xlsx", "", "", "", "7")
    body = "第3行 上限 4→8"
    eid = evidence_id_of(key, body)
    store[key] = ContextItem("file_diff", "Unit", body, {"cache_key": key, "evidence_id": eid})
    items = parse(structured(report_items=[report_item(
        evidence_status="verified", decision_question="", evidence_refs=[{"evidence_id": eid}],
    )])).report_items
    grounded = ground_report_items(items, store)
    assert grounded[0]["decision_question"] == ""
    # 证据可定位不等于业务结论已核实，仍然只标来源已取回。
    assert grounded[0]["evidence_status"] == "partial"


def test_standalone_lower_risk_reference_is_not_treated_as_a_missing_anomaly_link():
    from services.ai.report_items import ground_report_items

    items = parse(structured(report_items=[report_item(finding_ref="note_1")])).report_items
    grounded = ground_report_items(items, {}, anomalies=())
    assert not any("关联键" in gap for gap in grounded[0]["evidence_gaps"])


def test_missing_optional_title_does_not_duplicate_long_facts_in_synthesis_input():
    item = parse(structured(report_items=[report_item(change_fact="完整事实" * 100)])).report_items[0]
    assert item["title"] == ""
