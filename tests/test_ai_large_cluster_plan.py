"""Large same-commit directories are workloads, not indivisible dependencies."""
from services.ai.auto_sizing import MODE_FAMILY, MODE_SINGLE, SnapshotFacts, plan_analysis


def facts_for(entries):
    return SnapshotFacts.from_mapping({"entries": entries,
        "diff_payload_chars": sum(row["chars"] for row in entries),
        "max_file_diff_chars": max(row["chars"] for row in entries)})


def tables(count=98, chars=10000):
    return [{"path": f"config/table_{i:03}.xlsx", "commit": "same", "chars": chars}
            for i in range(count)]


def test_98_tables_in_one_business_cluster_are_split_by_evidence_volume():
    facts = facts_for(tables())
    plan = plan_analysis(facts, 580000, ("table_data",), subagent_enabled=True)
    assert plan.mode == MODE_FAMILY
    assert plan.member_count == 4
    assigned = [path for member in plan.members for path in member.paths]
    assert sorted(assigned) == sorted(row["path"] for row in facts.entries)
    assert len(assigned) == len(set(assigned))
    assert max(len(member.paths) for member in plan.members) <= 25
    relationships = plan.thresholds["split_cluster_dependencies"]
    assert len(relationships) == 1
    assert set(relationships[0]["source_paths"]) == set(assigned)
    assert "跨分片" in plan.members[0].objective
    more_dims = plan_analysis(facts, 580000, ("table_data", "code_logic", "cross_ref"))
    assert [m.paths for m in plan.members] == [m.paths for m in more_dims.members]


def test_source_table_and_generated_file_stay_in_same_assignment():
    entries = tables()
    entries += [{"path": "generated/table_000.lua", "commit": "other", "chars": 40000}]
    plan = plan_analysis(facts_for(entries), 580000, ("cross_ref",))
    owner = next(member for member in plan.members if "config/table_000.xlsx" in member.paths)
    assert "generated/table_000.lua" in owner.paths
    assert plan.mode == MODE_FAMILY


def test_frozen_manifest_and_runtime_use_the_workload_plan_including_one_dimension():
    from services.ai.engine import EngineLimits
    from services.ai.manifest import build_manifest
    from services.ai.subagent import apply_manifest, plan_family
    entries = tables() + [{"path": "config/table_000.lua", "commit": "other", "chars": 40000}]
    execution = plan_analysis(facts_for(entries), 580000, ("cross_ref",))
    manifest = build_manifest(entries, shard_count=execution.member_count, analysis_plan=execution)
    family = plan_family(mode="weekly", enabled=True, count=execution.member_count,
                         limits=EngineLimits(), sizing=execution.family, dimensions=("cross_ref",))
    assert family is not None
    family = apply_manifest(family, manifest)
    for member, planned in zip(family.members, execution.members):
        assert {item.path for item in member.assigned_files} == set(planned.paths)
        assert member.dimensions == ("cross_ref",)


def test_a_single_oversized_file_is_not_split_into_redundant_analyzers():
    plan = plan_analysis(facts_for(tables(1, 1500000)), 580000, ("table_data",))
    assert plan.mode == MODE_SINGLE


def test_enabled_small_business_cluster_and_disabled_large_cluster_remain_single():
    assert plan_analysis(facts_for(tables(98, 1000)), 580000).mode == MODE_SINGLE
    assert plan_analysis(facts_for(tables()), 580000, subagent_enabled=False).mode == MODE_SINGLE


def test_single_plan_keeps_the_inventory_owner_instead_of_a_blank_synthesis_label():
    from services.ai.manifest import build_manifest
    entries = tables(3, 1000)
    execution = plan_analysis(facts_for(entries), 580000)
    manifest = build_manifest(entries, shard_count=1, analysis_plan=execution)
    assert all(item.assigned_shards == ("S1",) for item in manifest.entries)


def test_split_is_stable_when_input_entry_order_changes():
    entries = tables()
    original = plan_analysis(facts_for(entries), 580000)
    reversed_plan = plan_analysis(facts_for(list(reversed(entries))), 580000)
    assert [m.paths for m in original.members] == [m.paths for m in reversed_plan.members]


def test_accepted_planner_shared_dependencies_are_not_dropped():
    entries = tables(10, 40000)
    paths = [item["path"] for item in entries]
    proposal = {"groups": [
        {"id": "one", "paths": paths[:5], "objective": "one", "qa_dimensions": ["table_data"]},
        {"id": "two", "paths": paths[5:], "objective": "two", "qa_dimensions": ["table_data"]},
    ], "shared_paths": [paths[0]], "reason": "shared schema"}
    plan = plan_analysis(facts_for(entries), 580000, ("table_data",), proposal=proposal)
    assert plan.planner["accepted"] is True
    assert plan.shared_paths == (paths[0],)
