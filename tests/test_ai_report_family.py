"""Business findings survive family synthesis independently of anomaly thresholds."""
from dataclasses import replace

from services.ai.engine import EngineLimits, EngineOutcome
from services.ai.family_ledger import MemberOutcome
from services.ai.protocol import AnalysisPayload, CandidateDisposition
from services.ai.rules import RuleThresholds
from services.ai.subagent import (
    _salvage_failed_synthesis,
    aggregate_outcomes,
    build_member_task,
    build_synthesis_task,
    plan_family,
)
from tests.test_ai_structured_report import parse, report_item, structured


def plan():
    value = plan_family(mode="weekly", enabled=True, count=2, limits=EngineLimits())
    assert value is not None
    return value


def shard(family, *, severity="medium"):
    parsed = parse(structured(report_items=[report_item(severity=severity)]))
    return MemberOutcome(plan=family.members[0], outcome=EngineOutcome(
        status="succeeded", payload=parsed, report_items=parsed.report_items,
        report_sections=parsed.report_sections, report_markdown=parsed.report_markdown))


def test_synthesis_receives_complete_structured_cards_even_without_anomalies():
    family = plan()
    step = shard(family, severity="low")
    task = build_synthesis_task(family, (step,))
    assert '"severity": "low"' in task
    assert step.outcome.report_items[0]["id"] in task
    assert "test_actions" in task and "evidence_refs" in task
    assert "report_items" in build_member_task(family.members[0], family)
    assert "低风险与小问题不用写" not in build_member_task(family.members[0], family)


def test_synthesis_can_copy_grounded_candidate_without_copying_internal_schema_fields():
    from services.ai.report_family import business_candidates_prompt
    from services.ai.report_items import normalize_report_items
    family = plan()
    step = shard(family)
    item = {**step.outcome.report_items[0], "merge_status": "pending_merge", "evidence_gaps": ["待补证"],
            "evidence_refs": [{"evidence_id": "a"*20, "path": "匹配.xlsx", "available": True}]}
    step = replace(step, outcome=replace(step.outcome, report_items=(item,)))
    rows = business_candidates_prompt((step,))
    assert len(normalize_report_items([rows[0]["report_item"]])) == 1
    assert rows[0]["platform_evidence_gaps"] == ["待补证"]


def test_successful_synthesis_cannot_silently_drop_medium_findings():
    family = plan()
    step = shard(family)
    parsed = parse(structured(report_items=[]))
    synthesis = EngineOutcome(status="succeeded", payload=parsed, report_sections=parsed.report_sections,
                              report_markdown=parsed.report_markdown)
    result = aggregate_outcomes(synthesis=synthesis, steps=(step,))
    assert len(result.report_items) == 1
    assert result.report_items[0]["merge_status"] == "pending_merge"
    assert result.status == "degraded"
    assert "待合并" in result.report_markdown
    assert result.report_sections["gaps"]


def test_explicit_rejection_removes_card_but_retains_reason():
    from services.ai.report_family import report_candidate_id
    family = plan()
    step = shard(family)
    identifier = report_candidate_id(step.plan.label, step.outcome.report_items[0])
    synthesis = EngineOutcome(status="succeeded", payload=AnalysisPayload(status="final", candidate_dispositions=(
        CandidateDisposition(identifier, "rejected", "已取得需求确认，这是预期行为"),)),
        report_sections=structured()["report_sections"])
    result = aggregate_outcomes(synthesis=synthesis, steps=(step,))
    assert not result.report_items
    assert "已取得需求确认" in result.report_markdown


def test_rejected_disposition_wins_over_accidentally_repeated_synthesis_card():
    from services.ai.report_family import report_candidate_id
    family = plan()
    step = shard(family)
    identifier = report_candidate_id(step.plan.label, step.outcome.report_items[0])
    payload = replace(step.outcome.payload, candidate_dispositions=(
        CandidateDisposition(identifier, "rejected", "反证表明该假设不成立"),))
    result = aggregate_outcomes(synthesis=replace(step.outcome, payload=payload), steps=(step,))
    assert result.report_items == ()
    assert "反证表明该假设不成立" in result.report_markdown


def test_rejecting_a_duplicate_source_does_not_remove_an_adopted_card():
    from services.ai.report_family import report_candidate_id
    family = plan()
    first = shard(family)
    second = replace(first, plan=family.members[1])
    payload = replace(first.outcome.payload, candidate_dispositions=(
        CandidateDisposition(report_candidate_id(first.plan.label, first.outcome.report_items[0]),
                             "adopted", "保留"),
        CandidateDisposition(report_candidate_id(second.plan.label, second.outcome.report_items[0]),
                             "rejected", "与第一分片重复，合并"),
    ))
    for steps in ((first, second), (second, first)):
        result = aggregate_outcomes(synthesis=replace(first.outcome, payload=payload), steps=steps)
        assert len(result.report_items) == 1


def test_failed_synthesis_salvages_business_cards_without_anomalies():
    family = plan()
    step = shard(family, severity="low")
    result = _salvage_failed_synthesis(synthesis=EngineOutcome(status="failed", error_message="upstream down"),
                                       steps=(step,), thresholds=RuleThresholds())
    assert len(result.report_items) == 1
    assert result.report_items[0]["severity"] == "low"
    assert result.status == "degraded"
    assert "upstream down" in result.report_markdown


def test_family_preserves_synthesis_fields_and_duplicate_source_actions():
    family = plan()
    step = shard(family)
    synthesized = dict(step.outcome.report_items[0], test_actions=[{
        "conditions": "老数据", "steps": ["重登"], "expected": "状态一致"}])
    synthesis = replace(step.outcome, report_items=(synthesized,))
    result = aggregate_outcomes(synthesis=synthesis, steps=(step,))
    assert len(result.report_items) == 1
    assert len(result.report_items[0]["test_actions"]) == 2
    assert result.report_sections["overview"] == step.outcome.report_sections["overview"]


def test_member_task_shows_trusted_shared_dependencies():
    family = replace(plan(), shared_paths=("共同表.xlsx",), dependency_notes=("S1与S2需核对同一冻结状态",))
    task = build_member_task(family.members[0], family)
    assert "共同表.xlsx" in task and "同一冻结状态" in task


def test_execution_plan_shared_paths_are_attached_from_the_real_analysis_plan():
    from services.ai.auto_sizing import SnapshotFacts, plan_analysis
    from services.ai.subagent_tasks import attach_execution_context
    execution = plan_analysis(SnapshotFacts(), 100_000)
    execution = replace(execution, shared_paths=("共享表.xlsx",), thresholds={
        **execution.thresholds, "split_cluster_dependencies": ["共享链路需核实"]})
    family = attach_execution_context(plan(), execution)
    assert family.shared_paths == ("共享表.xlsx",)
    assert family.dependency_notes == ("共享链路需核实",)
