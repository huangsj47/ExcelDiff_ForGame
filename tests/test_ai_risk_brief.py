"""Risk overviews explain change, conditional consequence and open questions."""
from services.ai.report_document import render_structured_report
from tests.test_ai_structured_report import parse, report_item, structured


def overview(report):
    return report.split("**风险速览**", 1)[1].split("## R1", 1)[0]


def test_brief_explains_fact_consequence_and_causal_preconditions_instead_of_title():
    item = report_item(title="列表格式不一致", change_fact="字段由两个元素变为三个，新增元素为0",
                       impact_hypothesis="容器可能无产出", impact_conditions=["第三元素是数量", "0表示不生成"],
                       decision_question="第三元素实际是什么含义？")
    report = parse(structured(report_items=[item])).report_markdown
    brief = overview(report)
    for text in ("字段由两个元素变为三个", "第三元素是数量", "0表示不生成", "容器可能无产出", "待确认"):
        assert text in brief
    assert "关注标题" not in brief and "列表格式不一致" not in brief


def test_mixed_direction_values_survive_in_brief_even_when_title_is_inaccurate():
    item = report_item(title="价格全部下调", change_fact="5件物品价格下调，另一件从800000升至1000000",
                       impact_hypothesis="实际收益取决于产出概率和数量", impact_conditions=["已核实产出与结算链路"])
    brief = overview(parse(structured(report_items=[item])).report_markdown)
    assert "升至1000000" in brief and "价格全部下调" not in brief
    assert "概率和数量" in brief


def test_unknown_preconditions_are_visible_in_the_brief():
    item = report_item(change_fact="列被删除", impact_hypothesis="旧读取方可能取空", impact_conditions=[])
    brief = overview(parse(structured(report_items=[item])).report_markdown)
    assert "前提未核实" in brief and "旧读取方可能取空" in brief
    assert "正式人数是什么？" in brief


def test_brief_is_bounded_but_complete_original_facts_remain_in_details():
    fact = "非常长的配置变化说明" * 80 + "；另一个值升至1000000"
    impact = "需要核实多个后果" * 70
    item = report_item(change_fact=fact, impact_hypothesis=impact,
                       impact_conditions=["前提说明" * 70], decision_question="待确认问题" * 50)
    report = parse(structured(report_items=[item])).report_markdown
    brief = overview(report)
    assert len(brief) < 500
    assert "升至1000000" in brief and "…" in brief
    assert "变化（待核实描述）：" + fact in report
    assert impact in report


def test_source_markup_remains_inert_in_risk_brief():
    item = report_item(change_fact='<img src=x onerror=bad()> **literal**',
                       impact_hypothesis='[link](javascript:bad)')
    brief = overview(parse(structured(report_items=[item])).report_markdown)
    assert '<img' not in brief and '[link](javascript:bad)' not in brief


def test_same_evidence_coordinate_is_rendered_once_in_technical_index():
    reference = {"evidence_id": "e" * 20, "path": "tables/Unit.xlsx", "lines": "#1!A2:C2"}
    item = dict(parse(structured()).report_items[0], evidence_refs=[reference], impact_refs=[reference])
    report = render_structured_report([item], {"overview": "参数变化"})
    assert report.count("- 证据：") == 1


def test_brief_does_not_turn_a_missing_fact_into_a_true_runtime_trigger():
    item = report_item(change_fact="价格变更", impact_hypothesis="收益可能改变",
                       impact_conditions=["仓库未提供产出概率"], decision_question="概率是多少？")
    brief = overview(parse(structured(report_items=[item])).report_markdown)
    assert "若仓库未提供" not in brief
    assert "待确认" in brief and "仓库未提供产出概率" in brief


def test_brief_joins_sentences_without_conflicting_terminal_punctuation():
    item = report_item(change_fact="字段值由0改1。", impact_hypothesis="可能读取失败。",
                       impact_conditions=["读取方只接受0"])
    brief = overview(parse(structured(report_items=[item])).report_markdown)
    assert "。；" not in brief
