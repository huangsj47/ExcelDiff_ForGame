from dataclasses import replace

import pytest

from services.ai.engine import EngineOutcome
from services.ai.family_ledger import AssignedFile
from services.ai.subagent import _call_engine, aggregate_outcomes
from tests.test_ai_report_family import plan, shard


@pytest.mark.parametrize("reason", ["evidence_needs_unresolved", "evidence_coverage_gap"])
def test_family_keeps_member_gap_degradation_when_synthesis_succeeds(reason):
    family = plan()
    step = shard(family)
    step = replace(step, outcome=replace(step.outcome, status="degraded", degradation=reason))
    result = aggregate_outcomes(synthesis=replace(step.outcome, status="succeeded", degradation=""), steps=(step,))
    assert result.status == "degraded"
    assert result.degradation == reason


def test_family_retains_same_need_id_from_different_members_and_synthesis():
    family = plan()
    step = shard(family)
    need = {"need_id": "N1", "kind": "retrievable", "status": "open", "question": "缺少定义",
            "requests": [{"type": "file_content", "path": "a.xlsx", "lines": "#1!A262:H262"}]}
    first = replace(step, outcome=replace(step.outcome, evidence_followup=(need,)))
    second = replace(step, plan=family.members[1], outcome=replace(step.outcome, evidence_followup=(need,)))
    synthesis = replace(step.outcome, evidence_followup=(dict(need, question="汇总仍需核对"),))
    result = aggregate_outcomes(synthesis=synthesis, steps=(first, second))
    rows = result.evidence_followup
    assert len(rows) == 3
    assert len({row["need_id"] for row in rows}) == 3
    assert {row["original_need_id"] for row in rows} == {"N1"}
    assert {row["member"] for row in rows} == {"S1", "S2", "汇总"}
    assert all(row["requests"][0]["lines"] == "#1!A262:H262" for row in rows)


def test_synthesis_needs_survive_an_unpopulated_synthesis_step():
    from services.ai.report_family import family_evidence_followup
    family = plan()
    step = shard(family)
    placeholder = replace(step, plan=replace(step.plan, role="synthesis", label="汇总"), outcome=None)
    need = {"need_id": "N1", "question": "汇总缺少定义", "status": "open"}
    synthesis = replace(step.outcome, evidence_followup=(need,))
    rows = family_evidence_followup(synthesis, (step, placeholder))
    assert len(rows) == 1
    assert rows[0]["need_id"] == "汇总:N1"


@pytest.mark.parametrize("role,assigned,expected", [
    ("subagent", True, True), ("synthesis", False, False), ("verify", False, False),
    ("synthesis", True, False), ("verify", True, False),
])
def test_only_analysis_member_assigned_files_enable_complete_diff_gate(role, assigned, expected):
    family = plan()
    files = (AssignedFile(repository_id=1, commit="a" * 40, path="a.xlsx"),) if assigned else ()
    member = replace(family.members[0], role=role, assigned_files=files)
    seen = {}

    def engine(**kwargs):
        seen.update(kwargs)
        return EngineOutcome(status="succeeded")

    _call_engine(client=None, provider=None, loaded=None, scope=None, change_summary="",
                 limits=family.limits, thresholds=None, project_knowledge="", project_instructions="",
                 baseline_digest="", plan=family, member=member, task_message="", on_round=None,
                 on_start=None, body_cache={}, run_analysis_fn=engine)
    if expected:
        assert seen["coverage_files"] == files
    else:
        assert "coverage_files" not in seen
