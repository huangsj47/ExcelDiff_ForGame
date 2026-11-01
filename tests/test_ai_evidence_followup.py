import json
import re

from services.ai.engine import EngineLimits
from services.ai.protocol import parse_payload
from tests.test_ai_engine import COMMIT, TABLE, ScriptedClient, _final, _run


def final_with_need(*, status="open", evidence_ids=(), kind="retrievable"):
    result = json.loads(_final())
    result["evidence_needs"] = [{"need_id": "N1", "question": "Check a later physical row",
        "kind": kind, "status": status, "finding_refs": [], "evidence_ids": list(evidence_ids),
        "resolution_reason": "Compared the retrieved source" if status != "open" else "",
        "requests": [{"type": "file_content", "commit": COMMIT, "path": TABLE,
                      "lines": "#1!A262:H262"}] if kind == "retrievable" and status == "open" else []}]
    return json.dumps(result)


def test_final_retrievable_gap_runs_tools_delivers_and_requires_reconsideration():
    class Client(ScriptedClient):
        def complete(self, messages, *, temperature=None):
            if self.calls:
                ids = re.findall(r"evidence_id=([0-9a-f]+)", messages[-1]["content"])
                self._replies = [final_with_need(status="resolved", evidence_ids=ids)] * 2
            return super().complete(messages, temperature=temperature)
    client = Client(final_with_need())
    outcome = _run(client, limits=EngineLimits(max_rounds=4))
    assert len(client.calls) == 2
    assert outcome.status == "succeeded"
    assert outcome.evidence_followup[0]["status"] == "resolved"
    assert "#1!A262:H262" in client.calls[1][-1]["content"]


def test_unresolved_repeated_need_never_loops_and_does_not_claim_complete():
    client = ScriptedClient(final_with_need())
    outcome = _run(client, limits=EngineLimits(max_rounds=8))
    assert len(client.calls) == 2
    assert outcome.status == "degraded" and outcome.payload is not None
    assert outcome.evidence_followup[0]["platform_reason"]


def test_external_semantics_does_not_spend_another_tool_or_model_call():
    client = ScriptedClient(final_with_need(kind="external_semantics"))
    outcome = _run(client)
    assert len(client.calls) == 1
    assert outcome.status == "succeeded"
    assert outcome.evidence_followup[0]["kind"] == "external_semantics"


def test_forged_or_fetched_but_undelivered_id_cannot_close_need():
    client = ScriptedClient(final_with_need(status="resolved", evidence_ids=["a" * 20]))
    outcome = _run(client)
    assert outcome.status == "degraded"


def test_exact_coordinate_survives_final_request_parser():
    result = parse_payload(final_with_need())
    assert result.evidence_needs[0]["requests"][0].lines == "#1!A262:H262"


def test_valid_store_id_without_real_delivery_cannot_close_need():
    from services.ai.context_tools import ContextTools
    from services.ai.evidence_followup import EvidenceFollowup
    from services.ai.evidence_store import EvidenceStore
    from services.ai.protocol import ContextRequest
    from tests.test_ai_engine import FakeProvider
    store = EvidenceStore()
    tools = ContextTools(provider=FakeProvider(), body_cache=store)
    fetched = tools.execute([ContextRequest(type="file_content", commit=COMMIT, path=TABLE)])
    identifier = fetched.items[0].meta["evidence_id"]
    tracker = EvidenceFollowup()
    tracker.plan(parse_payload(final_with_need(status="resolved", evidence_ids=[identifier])),
                 store, remaining=0, can_continue=False)
    assert tracker.unresolved


def test_shrunk_prompt_body_cannot_close_using_original_full_body_id():
    from services.ai.budget import ContextItem
    from services.ai.context_tools import ContextTools
    from services.ai.delivery_receipts import delivered_evidence
    from services.ai.evidence_followup import EvidenceFollowup
    from services.ai.evidence_store import EvidenceStore
    from services.ai.protocol import ContextRequest
    from tests.test_ai_engine import FakeProvider
    store = EvidenceStore()
    item = ContextTools(provider=FakeProvider(), body_cache=store).execute([
        ContextRequest(type="file_content", commit=COMMIT, path=TABLE)]).items[0]
    shrunk = ContextItem(item.kind, item.label, "only a preview", {**item.meta, "shrink_level": 1})
    tracker = EvidenceFollowup()
    tracker.observe(delivered_evidence([shrunk]))
    tracker.plan(parse_payload(final_with_need(status="resolved", evidence_ids=[item.meta["evidence_id"]])),
                 store, remaining=0, can_continue=False)
    assert tracker.unresolved


def test_fetch_failure_is_attributed_only_to_matching_need():
    from services.ai.context_tools import ContextTools
    from services.ai.evidence_followup import EvidenceFollowup
    from services.ai.evidence_store import EvidenceStore
    from tests.test_ai_engine import FakeProvider
    raw = json.loads(final_with_need())
    second = dict(raw["evidence_needs"][0], need_id="N2", requests=[{
        "type": "file_content", "commit": COMMIT, "path": "other.xlsx"}])
    raw["evidence_needs"].append(second)
    tracker, store = EvidenceFollowup(), EvidenceStore()
    more, _ = tracker.plan(parse_payload(json.dumps(raw)), store, remaining=10, can_continue=True)
    batch = ContextTools(provider=FakeProvider(failing=True), body_cache=store).execute(more[:1])
    tracker.fetched(batch, (), requests=more[:1])
    assert "platform_reason" in tracker.needs["N1"]
    assert "platform_reason" not in tracker.needs["N2"]


def test_budget_wrapup_final_keeps_retrievable_needs_in_audit():
    from tests.test_ai_engine import _requests

    ask = _requests({"type": "file_content", "commit": COMMIT, "path": TABLE})
    outcome = _run(ScriptedClient(ask, final_with_need()), limits=EngineLimits(max_rounds=1))
    assert outcome.status == "degraded"
    assert outcome.evidence_followup[0]["need_id"] == "N1"
    assert outcome.evidence_followup[0]["status"] == "open"
    assert "额度不足" in outcome.evidence_followup[0]["platform_reason"]
