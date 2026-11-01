import pytest

from services.ai.budget import ContextItem
from services.ai.context_working_set import compact_working_set, parse_checkpoint
from services.ai.evidence_store import EvidenceStore, evidence_id_of


def test_checkpoint_compaction_keeps_exact_critical_evidence_and_open_questions():
    store = EvidenceStore()
    text = "旧值=200，新值=0；0的语义未确认"
    key = ("file_diff", "repo", "commit", "table", "", "")
    eid = evidence_id_of(key, text)
    store[key] = ContextItem("file_diff", "table", text,
                             {"evidence_id": eid, "cache_key": key})
    messages = [{"role": "system", "content": "规则"}, {"role": "user", "content": "冻结输入"}]
    for i in range(4):
        messages += [{"role": "assistant", "content": str(i)},
                     {"role": "user", "content": "证据" * 1500}]
    cp = parse_checkpoint({"summary": "发现零值变化，不能确定禁用", "evidence_ids": [eid],
                           "open_questions": ["0是否表示默认？"]})
    result = compact_working_set(messages, hard_chars=20_000, current_chars=100,
                                checkpoint=cp, store=store)
    assert result.compacted
    assert text in result.recap and "0是否表示默认？" in result.recap
    assert result.messages[:2] == tuple(messages[:2])
    assert store.verify(eid)


def test_unknown_checkpoint_evidence_cannot_trigger_early_compaction():
    messages = [{"role": "user", "content": "x"*1000}] * 5
    result = compact_working_set(messages, hard_chars=9000, current_chars=0,
                                checkpoint={"summary": "done", "evidence_ids": ["fake"]},
                                store=EvidenceStore())
    assert not result.compacted


def test_malformed_checkpoint_is_not_silently_truncated():
    with pytest.raises(ValueError):
        parse_checkpoint({"summary": "x", "evidence_ids": "fake"})


def test_archived_evidence_can_be_restored_once_after_discovery_quota_is_used():
    from services.ai.context_tools import ContextTools
    from services.ai.protocol import ContextRequest

    class Provider:
        def file_diff(self, commit, path, repository_id=""):
            return "重要事实：奖励数量从10变为0"

    tools = ContextTools(Provider(), max_tool_requests=1, body_cache=EvidenceStore())
    batch = tools.execute([ContextRequest(type="file_diff", commit="a"*40, path="table")])
    original = batch.items[0]
    tools.archive_history()
    restored = tools.execute([ContextRequest(type="evidence", name=original.meta["evidence_id"])])
    assert restored.items[0].text == original.text
    assert tools.requests_seen == 1
    assert tools.execute([ContextRequest(type="evidence", name=original.meta["evidence_id"])]).refused_by_budget == 1
    tools.archive_history()
    again = tools.execute([ContextRequest(type="evidence", name=original.meta["evidence_id"])])
    assert again.items[0].text == original.text


def test_repeated_compaction_keeps_every_archived_address_even_after_old_turns_are_gone():
    store = EvidenceStore()
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "frozen"}]
    identifiers = []
    for cycle in range(2):
        for index in range(4):
            key = ("file_diff", "repo", "commit", f"file-{cycle}-{index}", "", "")
            text = f"事实{cycle}-{index}" * 400
            eid = evidence_id_of(key, text)
            identifiers.append(eid)
            store[key] = ContextItem("file_diff", str(key), text, {"evidence_id": eid, "cache_key": key})
            messages += [{"role": "user", "content": text}, {"role": "assistant", "content": "continue"}]
        result = compact_working_set(messages, hard_chars=7000, current_chars=100, store=store)
        assert result.compacted
        assert all(eid in result.recap for eid in identifiers)
        messages = list(result.messages)
