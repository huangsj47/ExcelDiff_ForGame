"""A final response cannot silently close an unread frozen input manifest."""
from types import SimpleNamespace

from services.ai.engine import EngineLimits, run_analysis
from tests.test_ai_engine import COMMIT, FakeProvider, ScriptedClient, _final, _loaded, _scope


def run(client, *, count=20, requests=40, rounds=5, provider=None, budget=None):
    entries = tuple(SimpleNamespace(repository_id="", commit=COMMIT, path=f"tables/{i}.xlsx")
                    for i in range(count))
    return run_analysis(client=client, provider=provider or FakeProvider(), loaded=_loaded(),
                        scope=_scope(paths={COMMIT: {e.path for e in entries}}),
                        change_summary="frozen inputs", coverage_files=entries, single_run_budget=budget,
                        limits=EngineLimits(max_rounds=rounds, max_tool_requests=requests,
                                            prompt_char_budget=180000))


def test_early_final_gets_missing_diffs_before_completion():
    client = ScriptedClient(_final())
    outcome = run(client)
    assert len(client.calls) > 1
    assert outcome.status == "succeeded"
    delivered = {i["path"] for r in outcome.rounds for i in (r.delivered or ())
                 if i["kind"] == "file_diff" and not i["failed"]}
    assert delivered == {f"tables/{i}.xlsx" for i in range(20)}


def test_not_enough_requests_retains_report_but_degrades_without_exceeding_cap():
    outcome = run(ScriptedClient(_final()), count=98, requests=12)
    assert outcome.status == "degraded"
    assert outcome.payload is not None
    assert outcome.requests_used <= 12
    assert "取证" in outcome.error_message or "额度" in outcome.error_message


def test_failed_fetch_never_loops_or_falsely_finishes():
    client = ScriptedClient(_final())
    outcome = run(client, provider=FakeProvider(failing=True))
    assert outcome.status == "degraded"
    assert len(client.calls) <= 5
    assert outcome.payload is not None


def test_last_round_final_does_not_claim_unread_inputs_complete():
    outcome = run(ScriptedClient(_final()), count=98, rounds=1)
    assert outcome.status == "degraded"
    assert outcome.payload is not None


def test_complete_segment_receipts_union_but_background_content_is_not_current_diff():
    from services.ai.coverage_completion import CoverageCompletion
    entry = SimpleNamespace(repository_id=1, commit=COMMIT, path="tables/a.xlsx")
    coverage = CoverageCompletion((entry,))
    base = dict(kind="file_diff", repository_id=1, commit=COMMIT, path=entry.path,
                segments=3, failed=False, empty=False)
    coverage.observe([dict(base, complete_segments=[1, 2], truncated=True)])
    assert coverage.missing_count == 1
    assert coverage.next_requests(1)[0].lines == "3"
    coverage.observe([dict(base, kind="file_content", complete_segments=[1, 2, 3])])
    assert coverage.missing_count == 1
    coverage.observe([dict(base, complete_segments=[3])])
    assert coverage.missing_count == 0


def test_transport_failure_during_catchup_retains_valid_draft():
    class FailingAfterDraft(ScriptedClient):
        def complete(self, messages, *, temperature=None):
            if self.calls:
                raise RuntimeError("network failed during coverage catchup")
            return super().complete(messages, temperature=temperature)

    outcome = run(FailingAfterDraft(_final()))
    assert outcome.status == "degraded"
    assert outcome.payload is not None
    assert any("network failed" in r.note for r in outcome.rounds)


def test_real_window_receipt_does_not_turn_one_requested_segment_into_full_diff():
    from services.ai.budget import ContextItem
    from services.ai.coverage_completion import CoverageCompletion
    from services.ai.delivery_receipts import delivered_evidence
    from services.ai.windowed_view import render_window
    entry = SimpleNamespace(repository_id=1, commit=COMMIT, path="tables/a.xlsx")
    body = "\n".join(f"@@ -{i},1 +{i},1 @@\n-old{i}\n+new{i}" for i in range(1, 4))
    text, meta = render_window(kind="file_diff", label="diff", text=body, window="2", limit=11000)
    meta["cache_key"] = ("file_diff", COMMIT, entry.path, "", "2", "", "1")
    coverage = CoverageCompletion((entry,))
    coverage.observe(delivered_evidence([ContextItem("file_diff", "diff", text, meta)]))
    assert coverage.missing_count == 1


def test_header_overflow_receipts_only_claim_segments_present_in_final_body():
    from services.ai.windowed_view import render_window, split_segments
    body = "\n".join(f"@@ -{i},1 +{i},1 @@\n-old{i} xxxxxxxxx\n+new{i} xxxxxxxxx" for i in range(1, 81))
    text, meta = render_window(kind="file_diff", label="diff", text=body, window="", limit=2000)
    parts, _ = split_segments(body, kind="file_diff")
    assert meta["complete_segments"]
    for part in meta["complete_segments"]:
        assert parts[part - 1].rstrip("\n") in text


def test_length_retry_final_still_checks_coverage():
    from services.ai.llm_client import ChatResult
    class TruncatedThenFinal(ScriptedClient):
        def complete(self, messages, *, temperature=None):
            result = super().complete(messages, temperature=temperature)
            if len(self.calls) == 1:
                return ChatResult(text='{"status":"final",', model="fake", finish_reason="length",
                                  prompt_tokens=10, completion_tokens=8192)
            return result
    client = TruncatedThenFinal(_final())
    outcome = run(client, rounds=5)
    assert outcome.status == "succeeded"
    assert len(client.calls) >= 3
    assert any("补齐差异取证" in r.note for r in outcome.rounds)


def test_catchup_requests_contiguous_missing_segments_as_a_page_not_one_call_per_hunk():
    from services.ai.coverage_completion import CoverageCompletion
    entry = SimpleNamespace(repository_id=1, commit=COMMIT, path="table.xlsx")
    coverage = CoverageCompletion((entry,))
    coverage.observe([dict(kind="file_diff", repository_id=1, commit=COMMIT, path=entry.path,
                           segments=100, complete_segments=list(range(1, 51)))])
    assert coverage.next_requests(1)[0].lines == "51-100"


def test_token_ceiling_does_not_trigger_unbudgeted_coverage_calls():
    from services.ai.budget_gate import SingleRunBudget
    budget = SingleRunBudget(total_token_budget=16, round_reserve_tokens=1, report_reserve_tokens=1)
    client = ScriptedClient(_final())
    outcome = run(client, budget=budget)
    assert outcome.status == "degraded"
    assert outcome.payload is not None
    assert len(client.calls) == 1


def test_exact_request_ceiling_is_not_degradation_after_complete_final():
    outcome = run(ScriptedClient(_final()), count=20, requests=12)
    assert outcome.requests_used == 12
    assert outcome.status == "succeeded"
