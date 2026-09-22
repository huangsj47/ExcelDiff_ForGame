from benchmarks.ai_analysis.dataset import BenchmarkSample, GoldIssue, GoldLabel
from benchmarks.ai_analysis.metrics import evaluate_sample
from services.ai.budget_plan import build_budget_plan, derive_tool_limits
from services.ai.change_set import from_weekly_payload
from services.ai.coverage_ledger import build_ledger
from services.ai.dependency_resolver import DependencyDocument, resolve_dependencies
from services.ai.engine import EngineLimits
from services.ai.manifest import build_manifest, manifest_page
from services.ai.protocol import ContextRequest, parse_payload, sanitize_requests
from services.ai.reference_index import SnapshotReferenceIndex
from services.ai.subagent import apply_manifest, build_member_task, plan_family


def test_gold_issue_keeps_qa_family_trigger_evidence_and_expected_disposition():
    issue = GoldIssue.from_dict(
        {
            "issue_id": "G-1",
            "title": "协议编号位移",
            "family": "protocol_compatibility",
            "severity": "critical",
            "file_paths": ["proto/a.lua"],
            "evidence": ["proto/a.lua:10", "proto/a.lua:20"],
            "trigger": "旧客户端发送编号 7",
            "expected_disposition": "active",
        }
    )

    payload = issue.to_dict()

    assert payload["family"] == "protocol_compatibility"
    assert payload["evidence"] == ["proto/a.lua:10", "proto/a.lua:20"]
    assert payload["trigger"] == "旧客户端发送编号 7"
    assert payload["expected_disposition"] == "active"


def test_benchmark_reports_family_and_critical_recall_and_token_efficiency():
    sample = BenchmarkSample(
        sample_id="run-x",
        run_row={"tokens_input": 900, "tokens_output": 100, "duration_ms": 2000},
        findings=[
            {
                "id": 1,
                "title": "协议编号位移",
                "severity": "critical",
                "file_path": "proto/a.lua",
                "evidence": "proto/a.lua:10",
            }
        ],
    )
    gold = GoldLabel(
        sample_id="run-x",
        change_files=["proto/a.lua", "config/reward.xlsx"],
        issues=[
            GoldIssue(
                "G-1",
                "协议编号位移",
                "critical",
                family="protocol_compatibility",
                file_paths=["proto/a.lua"],
                evidence=["proto/a.lua:10"],
                trigger="旧客户端发送编号 7",
                expected_disposition="active",
            ),
            GoldIssue(
                "G-2",
                "奖励重复发放",
                "high",
                family="economy",
                file_paths=["config/reward.xlsx"],
                evidence=["config/reward.xlsx:2"],
                trigger="重复领取",
                expected_disposition="active",
            ),
        ],
    )

    result = evaluate_sample(sample, gold)

    assert result["recall"]["critical"] == 1.0
    assert result["recall"]["by_family"]["protocol_compatibility"]["recall"] == 1.0
    assert result["recall"]["by_family"]["economy"]["recall"] == 0.0
    assert result["efficiency"]["tokens_per_matched_finding"] == 1000.0
    assert result["efficiency"]["tokens_per_locatable_evidence"] == 1000.0


def test_manifest_assigns_every_file_deterministically_and_pages_without_truncation():
    rows = [
        {
            "repository_id": 1,
            "repository_name": "config",
            "latest_commit_id": f"{index:040x}",
            "file_path": f"config/items/CfgItem{index}.xlsx",
            "source": "delta",
        }
        for index in range(1005)
    ]

    first = build_manifest(rows, shard_count=3)
    second = build_manifest(list(reversed(rows)), shard_count=3)

    assert len(first.entries) == 1005
    assert first.assignment_digest == second.assignment_digest
    assert all(entry.assigned_shards for entry in first.entries)
    assert first.assigned_rate == 1.0
    page = manifest_page(first, cursor=1000, limit=100)
    assert page["total"] == 1005
    assert len(page["items"]) == 5
    assert page["next_cursor"] is None


def test_dependency_resolver_records_one_hop_edges_and_confidence():
    documents = [
        DependencyDocument(
            path="code/shop.lua",
            text='local item = require("config.CfgItem")\nlocal id = 10001',
        ),
        DependencyDocument(path="config/CfgItem.lua", text="return {[10001] = {price=5}}"),
        DependencyDocument(path="config/CfgItem.xlsx", text="10001,price,5"),
    ]

    result = resolve_dependencies(["code/shop.lua"], documents)

    targets = {edge.target for edge in result.edges}
    assert "config/CfgItem.lua" in targets
    assert "config/CfgItem.xlsx" in targets
    assert all(edge.source == "code/shop.lua" for edge in result.edges)
    assert all(edge.confidence in {"high", "medium", "low"} for edge in result.edges)


def test_scope_dependency_closure_adds_same_stem_generated_pair_only_once():
    from types import SimpleNamespace

    from services.ai.scope_sampling import _same_stem_dependency_entries

    root = SimpleNamespace(config_id=1, file_path="config/CfgItem.xlsx")
    generated = SimpleNamespace(config_id=1, file_path="build/lua/CfgItem.lua")
    unrelated = SimpleNamespace(config_id=1, file_path="build/lua/CfgMonster.lua")

    result = _same_stem_dependency_entries(
        [root, generated, unrelated], [root], already={(1, root.file_path)}, limit=10
    )

    assert result == [generated]


def test_weekly_change_set_carries_full_manifest_into_member_assignments():
    rows = [
        {
            "repository_id": 1,
            "repository_name": "repo",
            "latest_commit_id": f"{index + 1:040x}",
            "file_path": f"code/module_{index}.lua",
        }
        for index in range(9)
    ]
    change = from_weekly_payload({"delta_files": rows, "list_files": rows})
    plan = plan_family(
        mode="weekly",
        enabled=True,
        count=3,
        limits=EngineLimits(),
    )
    assert plan is not None

    assigned = apply_manifest(plan, change.manifest)

    assert change.manifest.summary()["assigned"] == 9
    assert set().union(*(set(member.assigned_paths) for member in assigned.members)) == {
        row["file_path"] for row in rows
    }
    assert "确定性文件分工" in build_member_task(assigned.members[0], assigned)
    allowed, dropped = sanitize_requests(
        [ContextRequest(type="read_reference", name="change-manifest", lines="2")],
        change.scope,
    )
    assert not dropped
    assert allowed[0].lines == "2"


def test_coverage_ledger_separates_assigned_inspected_and_evidence_layers():
    manifest = build_manifest(
        [
            {"latest_commit_id": "a" * 40, "file_path": "a.lua"},
            {"latest_commit_id": "b" * 40, "file_path": "b.lua"},
        ],
        shard_count=2,
    )
    ledger = build_ledger(
        request_payload={
            "mode": "weekly",
            "delta_files": [
                {"latest_commit_id": "a" * 40, "file_path": "a.lua"},
                {"latest_commit_id": "b" * 40, "file_path": "b.lua"},
            ],
            "manifest": manifest.to_dict(),
        },
        executed=[
            {"kind": "file_diff", "label": f"file_diff {'a' * 12} a.lua"},
            {
                "kind": "file_content",
                "label": f"file_content {'b' * 12} b.lua",
                "failed": True,
            },
        ],
    )

    assert ledger["assignment_coverage"] == {"covered": 2, "total": 2, "ratio": 1.0}
    assert ledger["inspection_coverage"] == {"covered": 2, "total": 2, "ratio": 1.0}
    assert ledger["evidence_coverage"]["by_pair"]["covered"] == 1


def test_snapshot_reference_index_reads_each_blob_once_for_many_queries():
    calls = []
    entries = [(f"code/f{index}.lua", f"{index:040x}") for index in range(300)]

    def reader(path, commit):
        calls.append((path, commit))
        index = int(path.removeprefix("code/f").removesuffix(".lua"))
        return f"local Protocol{index} = true\nshared_symbol = {index}\n"

    index = SnapshotReferenceIndex.build(entries, reader=reader, version="test-v1")
    first = index.search("Protocol299")
    for _ in range(14):
        repeated = index.search("shared_symbol")

    assert len(calls) == 300
    assert first.files_total == 300
    assert first.hits[0].path == "code/f299.lua"
    assert repeated.scanned == 300
    assert repeated.truncated_files is False
    assert repeated.index_version == "test-v1"


def test_protocol_keeps_candidate_disposition_table():
    payload = parse_payload(
        """{
          "status": "final",
          "report_markdown": "# 报告",
          "anomalies": [],
          "dimensions": [{"id": "config_id", "hit": false, "note": "未发现"}],
          "candidate_dispositions": [
            {"candidate_id": "S1-1", "status": "rejected", "reason": "与 S2-1 重复"},
            {"candidate_id": "S2-1", "status": "adopted", "reason": "证据更完整"}
          ]
        }""",
        dimension_ids=("config_id",),
    )

    assert [(item.candidate_id, item.status) for item in payload.candidate_dispositions] == [
        ("S1-1", "rejected"),
        ("S2-1", "adopted"),
    ]


def test_context_render_exposes_stable_evidence_id():
    from services.ai.context_tools import ContextTools
    from services.ai.prompt import render_context_items

    class Provider:
        def file_diff(self, commit, path):
            return "@@ -1 +1 @@\n-old\n+new"

    request = ContextRequest(type="file_diff", commit="a" * 40, path="a.lua")
    item = ContextTools(Provider()).execute([request]).items[0]

    rendered = render_context_items([item])
    assert item.meta["evidence_id"] == item.meta["chunk_id"]
    assert f"evidence_id={item.meta['evidence_id']}" in rendered


def test_budget_plan_exposes_configured_effective_and_job_wide_limits():
    tool_limits = derive_tool_limits(prompt_char_budget=560_000, max_tool_requests=40)
    plan = build_budget_plan(
        configured_prompt_chars=2_000_000,
        effective_prompt_chars=560_000,
        platform_chars=20_000,
        max_rounds=8,
        max_tool_requests=40,
        shard_count=3,
        verify=True,
        tool_limits=tool_limits,
        window_note="端点未声明窗口，按默认值压到 600,000 字",
    )

    assert plan["prompt_chars"]["configured"] == 2_000_000
    assert plan["prompt_chars"]["effective"] == 560_000
    assert plan["roles"]["count"] == 5
    assert plan["job_theoretical_max"]["rounds"] == 40
    assert plan["job_theoretical_max"]["tool_requests"] == 200
    assert 11_000 <= plan["tool_limits"]["file_diff"] <= 32_000


def test_engine_usage_keeps_finish_reason_for_output_truncation_audit():
    from services.ai.engine import _usage_of
    from services.ai.llm_client import ChatResult

    usage = _usage_of(ChatResult(text="{}", finish_reason="length"))

    assert usage["finish_reason"] == "length"


def test_evidence_store_keeps_one_body_and_supports_id_lookup():
    from services.ai.budget import ContextItem
    from services.ai.evidence_store import EvidenceStore

    item = ContextItem(
        kind="file_diff", label="a.lua", text="+ changed", meta={"evidence_id": "ev-1"}
    )
    store = EvidenceStore()
    store[("file_diff", "a")] = item
    store[("file_diff", "alias")] = item

    assert store.by_id("ev-1") is item
    assert store.summary() == {
        "request_keys": 2,
        "unique_evidence": 1,
        "stored_chars": len(item.text),
    }
