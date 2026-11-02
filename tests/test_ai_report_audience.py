"""Planner/QA prose is separate from immutable technical evidence."""
from dataclasses import replace

import pytest

from services.ai.report_document import render_structured_report
from services.ai.report_items import ReportValidationError, normalize_report_items
from tests.test_ai_report_family import plan, shard
from tests.test_ai_structured_report import parse, report_item, structured


def audience_item(**overrides):
    raw = report_item(
        title="奖励由货币改为物品",
        reader_summary="榜单奖励由货币改为指定物品，可能发出与设计不符的奖励。",
        reader_change="四档榜单奖励从星币改成装备券和补给包，玩家结算时收到的内容随之改变。",
        reader_impact="如果物品或数量配错，玩家可能领错奖励；尚无价值基准，不能判断奖励缩水。",
        change_fact="第19行4,700002,10000→14,140007,1",
        impact_hypothesis="若无价值基准，无法推算收益下降倍数",
        decision_question="这四档奖励用于哪些榜单，新的物品和数量是否符合设计？")
    raw.update(overrides)
    return raw


def test_overview_is_one_complete_sentence_not_joined_fact_conditions_and_question():
    item = audience_item()
    parsed = parse(structured(report_items=[item]))
    brief = parsed.report_markdown.split("**风险速览**",1)[1].split("## R1",1)[0]
    assert item["reader_summary"] in brief
    assert all(label not in brief for label in ("变化：", "风险假设：", "触发前提", "待确认：", "…"))
    assert "4,700002,10000" not in brief


def test_business_details_explain_player_effect_and_keep_raw_values_in_technical_index():
    item = audience_item()
    report = parse(structured(report_items=[item])).report_markdown
    main, audit = report.split("## 取证索引（技术详情）",1)
    assert item["reader_change"] in main and item["reader_impact"] in main
    assert "4,700002,10000" not in main
    assert "依据状态：" not in main and "下一步：先确认：" not in main
    assert "建议验证：" in main and "需确认：" in main
    assert item["change_fact"] in audit
    assert "影响假设原文" in audit


@pytest.mark.parametrize("overrides", [
    {"reader_summary":"x"*101}, {"reader_summary":"奖励改动…可能有问题"},
    {"reader_summary":"第一句\n第二句"}, {"reader_change":""}, {"reader_impact":""},
])
def test_incomplete_or_cut_reader_text_requires_correction_not_silent_truncation(overrides):
    with pytest.raises(ReportValidationError,match="reader_"):
        normalize_report_items([audience_item(**overrides)])


def test_markup_in_reader_fields_is_escaped_not_interpreted_as_html():
    item = audience_item(reader_summary='<img src=x onerror=bad()>可能显示异常。')
    report = parse(structured(report_items=[item])).report_markdown
    assert '<img' not in report and 'onerror=bad()' in report


def test_legacy_report_without_reader_fields_still_renders_without_cutting_words():
    item = parse(structured(report_items=[report_item(title="配置调整",impact_hypothesis="玩家可能无法进入副本。")])).report_items[0]
    brief = render_structured_report([item],{"overview":"调整副本"}).split("**风险速览**",1)[1].split("## R1",1)[0]
    assert "可能" in brief and "…" not in brief
    assert "触发前提" not in brief


def test_reader_fields_survive_family_prompt_and_missing_synthesis_presentation():
    from services.ai.report_family import business_candidates_prompt, reconcile_business_reports
    family = plan()
    step = shard(family)
    item = normalize_report_items([audience_item()])[0]
    step = replace(step,outcome=replace(step.outcome,report_items=(item,)))
    handed = business_candidates_prompt((step,))[0]["report_item"]
    assert handed["reader_summary"] == item["reader_summary"]
    current = {k:v for k,v in item.items() if not k.startswith("reader_")}
    synthesis = replace(step.outcome,report_items=(current,))
    merged = reconcile_business_reports(synthesis,(step,)).report_items[0]
    assert merged["reader_change"] == item["reader_change"]


def test_reader_wording_does_not_change_identity_or_technical_judgment():
    raw = audience_item(impact_conditions=["旧规则仍用于发奖"], impact_basis="conditional")
    base = {k:v for k,v in raw.items() if not k.startswith("reader_")}
    original = normalize_report_items([base])[0]
    readable = normalize_report_items([raw])[0]
    assert original["id"] == readable["id"]
    for key in ("severity", "change_fact", "impact_hypothesis", "impact_basis", "impact_conditions"):
        assert readable[key] == original[key]
