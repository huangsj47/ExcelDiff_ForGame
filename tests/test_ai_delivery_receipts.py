"""Coverage follows evidence actually sent, including platform prefetch."""
import json
from types import SimpleNamespace

from services.ai.engine import EngineLimits
from services.ai.trace_evidence import decode_evidence, encode_evidence
from tests.test_ai_engine import ScriptedClient, _final, _run


def test_first_round_prefetch_is_recorded_as_delivered_even_when_model_immediately_finishes():
    outcome = _run(ScriptedClient(_final()), limits=EngineLimits(prompt_char_budget=180000))
    encoded = encode_evidence(outcome.rounds[0])
    row = SimpleNamespace(**encoded)
    delivered = decode_evidence(row).get("delivered")
    assert delivered
    assert any(item["kind"] == "file_diff" and item["path"] for item in delivered)


def test_explicit_empty_delivery_never_falls_back_to_fetched_but_unsent_evidence():
    from services.ai.delivery_receipts import coverage_evidence
    fetched = [{"kind": "file_diff", "label": "not sent"}]
    assert coverage_evidence({"executed": fetched, "delivered": []}) == []
    assert coverage_evidence({"executed": fetched}) == fetched


def test_delivery_receipts_preserve_all_addresses_beyond_old_diagnostic_preview_limit():
    from services.ai.budget import ContextItem
    from services.ai.delivery_receipts import delivered_evidence
    items = [ContextItem("file_diff", "label", "diff", {
        "cache_key": ("file_diff", "a" * 40, f"tables/file{i}.xlsx", "", "", "", "7"),
    }) for i in range(65)]
    receipts = delivered_evidence(items)
    assert len(receipts) == 65
    assert receipts[-1]["path"] == "tables/file64.xlsx"
    assert receipts[-1]["repository_id"] == "7"
    assert receipts[-1]["commit"] == "a" * 40


def test_new_receipts_round_trip_without_changing_existing_execution_diagnostics():
    from services.ai.engine import RoundRecord
    record = RoundRecord(1, "final", delivered=({"kind": "file_diff", "path": "tables/a.xlsx"},))
    encoded = encode_evidence(record)
    assert json.loads(encoded["executed_json"])["delivered"][0]["path"] == "tables/a.xlsx"
    assert decode_evidence(SimpleNamespace(**encoded))["executed"] == []


def test_partial_delivery_is_not_a_complete_incremental_baseline():
    from services.ai.coverage_ledger import build_ledger
    from services.ai.snapshot_store import covers_completely
    payload = {"delta_files": [{"repository_id": 1, "file_path": "table.xlsx",
                                 "latest_commit_id": "a" * 40}],
               "summary": {"batch_files": 1, "window_files": 1}}
    receipt = dict(kind="file_diff", delivery_version=1, repository_id=1, path="table.xlsx",
                   commit="a" * 40, segments=3, complete_segments=[1], failed=False, empty=False)
    ledger = build_ledger(request_payload=payload, executed=[receipt])
    assert ledger["evidence_coverage"]["by_pair"]["covered"] == 1
    assert ledger["evidence_coverage"]["complete_diffs"]["covered"] == 0
    assert not covers_completely(ledger=ledger, snapshot_files=1)


def test_incremental_compensation_keeps_partial_and_other_repository_paths_unread():
    from services.ai.coverage_completion import completed_paths
    entries = [dict(repository_id=i, commit="a" * 40, path="table.xlsx") for i in (1, 2)]
    first = dict(kind="file_diff", repository_id=1, path="table.xlsx", commit="a" * 40,
                 segments=2, complete_segments=[1])
    assert not completed_paths(entries, [first])
    first["complete_segments"] = [1, 2]
    assert not completed_paths(entries, [first])
    assert completed_paths(entries, [first, dict(first, repository_id=2)]) == {"table.xlsx"}
