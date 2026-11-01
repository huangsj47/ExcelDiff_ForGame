"""Observed window recovery must retain assignment and usable evidence addresses."""
from dataclasses import dataclass
from types import SimpleNamespace

from services.ai.budget import ContextItem, estimate_chars
from services.ai.context_working_set import (
    fit_observed_window,
    protected_history,
    restore_pending_pointers,
)
from services.ai.delivery_receipts import delivered_evidence
from services.ai.evidence_store import EvidenceStore, evidence_id_of
from tests.test_ai_context_compaction import (
    _ROTATION,
    RejectAt,
    _anomaly,
    _final,
    _requests,
    _run_big,
)


@dataclass
class Brief:
    limits: object
    pending_items: tuple = ()
    recap: str = ""
    task_message: str = "分析指定分片，保留所有未决问题"


def evidence(store, path="table.xlsx", body="原始字段=0，语义待确认"):
    key = ("file_content", "a" * 40, path, "", "", "", "1")
    eid = evidence_id_of(key, body)
    item = ContextItem("file_content", path, body, {"evidence_id": eid, "cache_key": key})
    store[key] = item
    return item


def prepare(brief, _messages):
    return brief.pending_items, brief.recap + "\n当前证据\n" + "\n".join(item.text for item in brief.pending_items)


def test_observed_window_preserves_family_seed_and_task_and_removes_whole_exchanges():
    store = EvidenceStore()
    original = evidence(store)
    pointer = ContextItem(original.kind, original.label, "已在上文给出，不要再次索取",
                          {"evidence_id": original.meta["evidence_id"], "repeat_pointer": True})
    brief = Brief(SimpleNamespace(prompt_char_budget=500), pending_items=(pointer,))
    protected = [{"role": "system", "content": "输出协议"},
                 {"role": "user", "content": "冻结批次"},
                 {"role": "user", "content": brief.task_message}]
    messages = protected + [{"role": "assistant", "content": "旧答复" * 200},
                            {"role": "user", "content": "旧证据" * 200},
                            {"role": "assistant", "content": "继续"}]
    base, items, text, note, removed = fit_observed_window(
        brief, messages, prepare, protect_head=3, store=store)
    assert base == protected
    assert removed == 2 and note
    assert original.text in text and "已在上文给出" not in text
    assert original.meta["evidence_id"] in text
    assert estimate_chars(base) + len(text) <= brief.limits.prompt_char_budget
    assert items[0] is original
    receipt = delivered_evidence(items)[0]
    assert receipt["evidence_id"] == original.meta["evidence_id"]
    assert receipt["path"] == "table.xlsx"


def test_all_verified_archived_addresses_remain_visible_even_if_directory_cannot_fit():
    store = EvidenceStore()
    originals = [evidence(store, path=f"table-{i}.xlsx") for i in range(12)]
    brief = Brief(SimpleNamespace(prompt_char_budget=100))
    messages = [{"role": "system", "content": "规则"},
                {"role": "user", "content": "x" * 300},
                {"role": "assistant", "content": "旧答复"}]
    base, _items, text, _note, removed = fit_observed_window(brief, messages, prepare, store=store)
    assert removed == 1 and len(base) == 1
    assert all(item.meta["evidence_id"] in text for item in originals)
    # The engine's hard-cap check must reject this result, never truncate the directory.
    assert estimate_chars(base) + len(text) > brief.limits.prompt_char_budget


def test_unrecoverable_pending_pointer_is_explicitly_failed_not_delivered_as_original():
    pointer = ContextItem("file_content", "gone.xlsx", "已在上文给出",
                          {"evidence_id": "e" * 20, "repeat_pointer": True})
    restored = restore_pending_pointers((pointer,), EvidenceStore())[0]
    assert restored.meta["tool_failed"] is True
    assert "恢复失败" in restored.text and "已在上文给出" not in restored.text
    assert delivered_evidence((restored,))[0]["failed"] is True


def test_first_round_seeded_retry_keeps_current_task_once_and_salvage_restores_it():
    brief = Brief(SimpleNamespace(prompt_char_budget=1000))
    seed = [{"role": "system", "content": "规则"}, {"role": "user", "content": "冻结批次"}]
    assert protected_history(seed, brief, 3, include_task=False) == seed
    salvage = protected_history(seed, brief, 3)
    assert salvage[-1]["content"] == brief.task_message
    assert sum(message["content"] == brief.task_message for message in salvage) == 1


def test_seeded_overflow_retry_and_later_rounds_keep_assignment_and_recovery_directory():
    task = "S1专责字段契约；保留分片所有未决问题"
    seed = [{"role": "system", "content": "规则"}, {"role": "user", "content": "冻结批次"}]
    client = RejectAt(_requests(_ROTATION[0]), _requests(_ROTATION[1]), _final(_anomaly()),
                      reject_at=(2, 3))
    result = _run_big(client, seed_messages=seed, task_message=task)
    assert result.status == "degraded"
    assert len(client.calls) >= 5
    for call in client.calls:
        assert call[:2] == seed
        assert sum(message["content"] == task for message in call) == 1
    assert "可恢复证据目录" in client.calls[3][-1]["content"]
    assert "@evidence_id=" in client.calls[3][-1]["content"]


def test_first_round_seeded_overflow_salvage_keeps_task_once():
    task = "S1仅审查分配的字段契约"
    seed = [{"role": "system", "content": "规则"}, {"role": "user", "content": "冻结批次"}]
    client = RejectAt(_final(_anomaly()), reject_at=(1,))
    result = _run_big(client, seed_messages=seed, task_message=task)
    assert result.status == "degraded"
    assert len(client.calls) == 2
    for call in client.calls:
        assert call[:2] == seed
        assert sum(message["content"] == task for message in call) == 1


def test_first_round_fixed_family_task_above_observed_cap_fails_without_discarding_task():
    task = "必须保留的任务边界" * 1000
    seed = [{"role": "system", "content": "规则"}, {"role": "user", "content": "冻结批次"}]
    client = RejectAt(_final(_anomaly()), reject_at=(1,))
    result = _run_big(client, seed_messages=seed, task_message=task)
    assert result.status == "failed"
    assert len(client.calls) == 1
    assert client.calls[0][-1]["content"] == task
    assert "固定指令" in result.error_message and "水位" in result.error_message
    assert "收尾请求未发出" in result.error_message
