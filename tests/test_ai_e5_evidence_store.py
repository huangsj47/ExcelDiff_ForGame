# -*- coding: utf-8 -*-
"""E5：共享证据仓（`EvidenceStore`）从「只写不读」变成真能回查。

## 它原先是个只做了一半的特性

`EvidenceStore` 当 `body_cache` 用（跨成员共享正文），ID 侧（`by_id` / `summary`）
**零生产调用点**：`_by_id` 写进去之后从来没被读过。这一份测试把 ID 那半边接上：

1. **`blob_id` 只按内容算**（`sha256(text)[:20]`，不含请求 key）。`evidence_id` 的 hash
   输入里**含请求 key**，所以「同一份 blob 只存一份」在 id 层面从来不成立 —— 成立的
   只是「同一 **window** 只存一份」。两条 id 指向同一份正文时，能说清这件事的只有
   `blob_id`。
2. **回查要能自证没损坏**（`verify`）：读回来的条目重算一次 hash 并比对。`cache_key`
   因此必须存进 `meta`，否则重算不了 —— 这一条与 `context_tools._with_chunk_id` 是一
   对改动。
3. **命中共享缓存时校验一次**：校验不过就**退回真取数**并记一条 `DroppedItem`，
   不静默拿一份可能坏了的内容去下结论。
4. **按 `evidence_id` 取回原件**：中间协议收窄之后，汇总/对账的任务书只给地址与体量
   （`@evidence_id=abc123（原文 11,000 字，按需索取）`）。这个承诺必须真的能兑现 ——
   否则那是一句平台自己写下的假话。

## 跨成员「给全文」那条分支**不动**

`context_tools` 模块 docstring 第 5 条刻意要求跨成员命中给完整正文（「见上文」在另一个
成员的对话里是假话），那条设计是对的。这一份测试里有一条专门钉住它没被这次改动带歪。
"""
from __future__ import annotations

from services.ai.budget import ContextItem
from services.ai.context_tools import ContextTools, _with_chunk_id, describe_request
from services.ai.engine import STATUS_SUCCEEDED, EngineLimits, EngineOutcome
from services.ai.evidence_store import EvidenceStore, blob_id_of, evidence_id_of
from services.ai.protocol import ContextRequest
from services.ai.scope import AnalysisScope

COMMIT = "a" * 40
PATH = "config/30_goods/item.xlsx"
PATH_B = "scripts/export.py"

KEY = ("file_diff", COMMIT, PATH, "", "", "", "")
KEY_B = ("file_diff", COMMIT, PATH_B, "", "", "", "")


class FakeProvider:
    """记录调用次数，用来断言「有没有真的再取一次」。"""

    def __init__(self, **responses):
        self.responses = dict(responses)
        self.calls: list[tuple] = []

    def _respond(self, key, *args):
        self.calls.append((key,) + args)
        return self.responses.get(key)

    def commit_detail(self, commit):
        return self._respond("commit_detail", commit)

    def file_diff(self, commit, path):
        return self._respond("file_diff", commit, path)

    def file_content(self, commit, path, lines="", repository_id=""):
        return self._respond("file_content", commit, path, lines)

    def read_reference(self, name):
        return self._respond("read_reference", name)

    def find_references(self, query, path=""):
        return self._respond("find_references", query, path)


def _item(text: str, key=KEY, **meta) -> ContextItem:
    return ContextItem(
        kind="file_diff",
        label=describe_request(ContextRequest(type="file_diff", commit=COMMIT, path=key[2])),
        text=text,
        meta={**meta},
    )


def _stored(text: str, key=KEY) -> tuple[ContextItem, str]:
    """走一遍真实的生成路径（`_with_chunk_id`），拿到条目与它的 `evidence_id`。"""
    item = _item(text, key)
    item = _with_chunk_id(item, key)
    return item, str(item.meta["evidence_id"])


# ==========================================================================
# 1. blob_id：只按内容算
# ==========================================================================


def test_blob_id_ignores_the_request_key():
    """同一份正文、两条不同的请求 → 两个 evidence_id，**一个 blob_id**。"""
    store = EvidenceStore()
    first, first_id = _stored("同一份正文")
    second, second_id = _stored("同一份正文", KEY_B)

    store[KEY] = first
    store[KEY_B] = second

    assert first_id != second_id
    blob = blob_id_of("同一份正文")
    assert store.by_blob(blob) is not None
    assert sorted(item.meta["evidence_id"] for item in store.by_blob(blob)) == sorted(
        [first_id, second_id]
    )


def test_blob_id_follows_the_content():
    assert blob_id_of("甲") != blob_id_of("乙")
    assert blob_id_of("甲") == blob_id_of("甲")


def test_blob_index_maps_every_blob_to_its_evidence_ids():
    store = EvidenceStore()
    store[KEY], first_id = _stored("同一份正文")
    store[KEY_B], second_id = _stored("同一份正文", KEY_B)
    third_key = ("file_diff", COMMIT, PATH, "3-9", "", "", "")
    store[third_key], third_id = _stored("另一份正文", third_key)

    index = store.blob_index()

    assert set(index) == {blob_id_of("同一份正文"), blob_id_of("另一份正文")}
    assert sorted(index[blob_id_of("同一份正文")]) == sorted([first_id, second_id])
    assert index[blob_id_of("另一份正文")] == [third_id]


def test_blob_index_is_copied_and_entries_are_dropped_with_the_item():
    """索引跟着条目一起增删（`__delitem__` 那一处的反向对照）。"""
    store = EvidenceStore()
    item, evidence_id = _stored("正文")
    store[KEY] = item
    assert store.blob_index() == {blob_id_of("正文"): [evidence_id]}

    del store[KEY]

    assert store.blob_index() == {}
    assert store.by_blob(blob_id_of("正文")) == ()
    assert store.verify(evidence_id) is False


def test_by_blob_of_an_unknown_id_is_empty():
    assert EvidenceStore().by_blob("nope") == ()


# ==========================================================================
# 2. verify：读回来的条目自证没损坏
# ==========================================================================


def test_with_chunk_id_records_what_verify_needs():
    """`verify` 要重算，就必须能拿到**算这个 id 用过的那些输入**。"""
    item, evidence_id = _stored("正文")

    assert item.meta["blob_id"] == blob_id_of("正文")
    assert tuple(item.meta["cache_key"]) == KEY
    assert evidence_id_of(KEY, "正文") == evidence_id


def test_verify_accepts_an_intact_entry():
    store = EvidenceStore()
    item, evidence_id = _stored("正文")
    store[KEY] = item

    assert store.verify(evidence_id) is True


def test_verify_rejects_a_tampered_entry():
    """正文被换掉（缓存坏了、有人在中间改过）→ **不许**说它还是那一份。"""
    store = EvidenceStore()
    item, evidence_id = _stored("正文")
    store[KEY] = item
    store[KEY] = ContextItem(kind=item.kind, label=item.label, text="被换掉的正文", meta=dict(item.meta))

    assert store.verify(evidence_id) is False


def test_verify_rejects_an_unknown_id():
    assert EvidenceStore().verify("0" * 20) is False


# ==========================================================================
# 3. 共享缓存命中时校验一次
# ==========================================================================


def test_a_corrupted_shared_entry_is_refetched_and_recorded():
    """命中共享缓存 → 校验不过 → **退回真取数**，并记一条账。"""
    cache = EvidenceStore()
    good, _evidence_id = _stored("好正文")
    cache[KEY] = ContextItem(
        kind=good.kind, label=good.label, text="坏掉的正文", meta=dict(good.meta)
    )
    provider = FakeProvider(file_diff="刚从仓库取回来的正文")

    batch = ContextTools(provider, body_cache=cache).execute(
        [ContextRequest(type="file_diff", commit=COMMIT, path=PATH)]
    )

    assert provider.calls, "校验不过时必须真的再去取一次"
    assert "刚从仓库取回来的正文" in batch.items[0].text
    kinds = [item.kind for item in batch.dropped]
    assert "shared_cache" in kinds, batch.dropped


def test_an_intact_shared_entry_is_used_without_refetching():
    """反向对照：没坏就不许再取一次（否则这条分支等于没有）。"""
    cache = EvidenceStore()
    good, _evidence_id = _stored("别的成员取过的正文")
    cache[KEY] = good
    provider = FakeProvider(file_diff="不该被用到")

    batch = ContextTools(provider, body_cache=cache).execute(
        [ContextRequest(type="file_diff", commit=COMMIT, path=PATH)]
    )

    assert provider.calls == []
    assert "别的成员取过的正文" in batch.items[0].text
    assert batch.dropped == ()


def test_cross_member_hits_still_send_the_full_body():
    """**那条「跨成员给全文」的分支一个字都不许少**（模块 docstring 第 5 条）。"""
    cache = EvidenceStore()
    body = "很长的正文" * 100
    good, _evidence_id = _stored(body)
    cache[KEY] = good

    batch = ContextTools(FakeProvider(), body_cache=cache).execute(
        [ContextRequest(type="file_diff", commit=COMMIT, path=PATH)]
    )

    assert batch.items[0].text == body
    assert batch.items[0].meta.get("repeat_pointer") is not True


# ==========================================================================
# 4. 按 evidence_id 取回原件
# ==========================================================================


def _scope() -> AnalysisScope:
    return AnalysisScope(
        commits=(COMMIT,),
        paths_by_commit={COMMIT: frozenset({PATH, PATH_B})},
    )


def test_an_evidence_request_delivers_the_stored_body_verbatim():
    cache = EvidenceStore()
    body = "".join(f"+ 第 {index} 行改动\n" for index in range(200))
    good, evidence_id = _stored(body)
    cache[KEY] = good
    provider = FakeProvider(file_diff="不该被用到")

    batch = ContextTools(provider, body_cache=cache).execute(
        [ContextRequest(type="evidence", name=evidence_id)]
    )

    assert provider.calls == [], "按地址取回不该再去取一次数"
    assert batch.items[0].text == body, "给回的必须是原件，一个字不动"
    assert batch.items[0].meta["evidence_id"] == evidence_id
    assert evidence_id in batch.items[0].label


def test_an_unknown_evidence_id_fails_loudly():
    """地址查不到时给的是**明确的失败说明**，不是空正文（空正文会被读成「这里没问题」）。"""
    cache = EvidenceStore()
    batch = ContextTools(FakeProvider(), body_cache=cache).execute(
        [ContextRequest(type="evidence", name="f" * 20)]
    )

    assert "取数失败" in batch.items[0].text
    assert batch.items[0].meta.get("tool_failed")


def test_an_evidence_request_without_a_store_fails_loudly():
    """单代理路径没有共享仓 —— 那时也必须**说清楚**，而不是回一个空正文。"""
    batch = ContextTools(FakeProvider()).execute(
        [ContextRequest(type="evidence", name="f" * 20)]
    )

    assert "取数失败" in batch.items[0].text
    assert batch.items[0].meta.get("tool_failed")


def test_an_evidence_request_that_verifies_false_fails_loudly():
    """地址在、但内容对不上 → 按失败处理（**不把可能坏了的内容交出去**）。"""
    cache = EvidenceStore()
    good, evidence_id = _stored("正文")
    cache[KEY] = ContextItem(kind=good.kind, label=good.label, text="被换掉的正文", meta=dict(good.meta))

    batch = ContextTools(FakeProvider(), body_cache=cache).execute(
        [ContextRequest(type="evidence", name=evidence_id)]
    )

    assert "取数失败" in batch.items[0].text
    assert [item.kind for item in batch.dropped] == ["evidence"]


def test_the_delivered_evidence_counts_against_the_request_budget():
    """按地址取回也是一次索取 —— 不计数就等于开了一条不花额度的旁路。"""
    cache = EvidenceStore()
    good, evidence_id = _stored("正文")
    cache[KEY] = good
    tools = ContextTools(FakeProvider(), body_cache=cache)

    tools.execute([ContextRequest(type="evidence", name=evidence_id)])

    assert tools.requests_seen == 1


# ==========================================================================
# 5. 任务书里的地址：给地址而不是贴正文，且**那个地址真的取得回来**
# ==========================================================================


def _store_with(diff_text: str, path: str = PATH, lines: str = "") -> tuple[EvidenceStore, str]:
    """建一个只装着「某个文件的 diff」的证据仓，返回 (仓, evidence_id)。"""
    store = EvidenceStore()
    key = ("file_diff", COMMIT, path, lines, "", "", "")
    item = _item(diff_text, key)
    item = _with_chunk_id(item, key)
    store[key] = item
    return store, str(item.meta["evidence_id"])


def _candidate(path: str = PATH, evidence_index=None):
    """走**真实那条路**抽候选：`_candidates_of` 才会填 `evidence_ids`。

    直接 `Candidate(...)` 会得到一条 `evidence_ids=()` 的候选，测的就不是「平台把候选和
    正文连起来了没有」，而是「渲染函数在拿不到地址时会不会贴文本」—— 两件事。
    """
    from services.ai.protocol import AnalysisPayload, Anomaly
    from services.ai.subagent import _candidates_of

    anomaly = Anomaly(
        title="道具表改动与生成物不一致",
        category="config_linkage",
        severity="high",
        confidence="high",
        evidence=("道具表第 12 行改了名字，生成物里没跟着改",),
        file_path=path,
        impact="客户端读到的名字仍然是旧的",
    )
    outcome = EngineOutcome(
        status=STATUS_SUCCEEDED,
        payload=AnalysisPayload(status="final", anomalies=(anomaly,)),
    )
    members = _synthesis().members
    return _candidates_of(members[0], outcome, evidence_index)[0]


def _synthesis():
    """一个只用来取「第一个成员」的计划（`_candidates_of` 只读它的 label）。

    `count=2` 是下限：`plan_family` 在 `count < 2` 时返回 `None`（一个成员就是原来的
    单代理，拆了没意义）。
    """
    from services.ai.subagent import plan_family

    plan = plan_family(mode="weekly", enabled=True, count=2, limits=EngineLimits())
    assert plan is not None
    return plan


def test_the_evidence_index_only_keeps_bodies_that_can_be_fetched():
    """指针条目、取数失败、空内容**都不进**索引。

    把一句「取不到」宣传成「原文 11,000 字，按需索取」，模型会去取、取回一句失败说明，
    白花一次索取额度 —— 而它还会以为那一份内容本身有问题。
    """
    from services.ai.family_ledger import evidence_index_of

    store = EvidenceStore()
    good, good_id = _stored("真的正文")
    store[KEY] = good
    failed, failed_id = _stored("取数失败")
    store[KEY_B] = ContextItem(
        kind=failed.kind,
        label=failed.label,
        text=failed.text,
        meta={**failed.meta, "tool_failed": True},
    )
    pointer_key = ("file_diff", COMMIT, PATH, "9-9", "", "", "")
    pointer, pointer_id = _stored("见上文那一节")
    store[pointer_key] = ContextItem(
        kind=pointer.kind,
        label=pointer.label,
        text=pointer.text,
        meta={**pointer.meta, "repeat_pointer": True},
    )

    index = evidence_index_of(store)

    assert set(index) == {good_id}
    assert index[good_id].chars == len("真的正文")
    assert failed_id not in index and pointer_id not in index


def test_a_candidate_is_rendered_as_an_address_not_as_pasted_text():
    from services.ai.family_ledger import evidence_index_of

    store, evidence_id = _store_with("+ 100012 攻击力 100")
    index = evidence_index_of(store)

    candidate = _candidate(evidence_index=index)
    assert candidate.evidence_ids == (evidence_id,), "平台没把候选和正文连起来"
    text = candidate.describe(index)

    assert f"@evidence_id={evidence_id}" in text
    assert "按需索取" in text
    assert f"原文 {len('+ 100012 攻击力 100'):,} 字" in text
    # **正文与对正文的复述都不再贴进来** —— 这一条才是「引用 ID 而不是贴正文」。
    assert "道具表第 12 行改了名字" not in text
    assert "100012 攻击力" not in text


def test_a_candidate_without_an_address_still_shows_its_own_evidence():
    """查不到地址是**常态**（那个文件本次没被取过）—— 那时照旧贴它自己写的证据。

    这一条是反向对照：少了它，「永远不贴证据」的实现也能让上面那条测试变绿。
    """
    from services.ai.family_ledger import evidence_index_of

    store, _evidence_id = _store_with("别的文件的正文", path=PATH_B)
    index = evidence_index_of(store)

    candidate = _candidate(evidence_index=index)
    assert candidate.evidence_ids == ()
    text = candidate.describe(index)

    assert "@evidence_id=" not in text
    assert "道具表第 12 行改了名字" in text


def test_the_address_never_matches_a_different_file_with_the_same_suffix():
    """`x/config/a.lua` 与 `config/a.lua` 是两份不同的正文，不许互相顶替。

    子串判据（`path in label`）会把它们判成同一个，而那种错让复核的人读到另一个文件的
    diff —— 看起来完全正常，且他不会知道。
    """
    from services.ai.family_ledger import evidence_index_of, evidence_refs_for

    index = evidence_index_of(_store_with("别的文件", path="x/" + PATH)[0])

    assert evidence_refs_for(index, PATH) == ()


def test_the_address_in_the_task_book_actually_fetches_that_body():
    """**这个承诺必须是真的**：任务书里印出来的地址，按它索取要能拿回那份原件。

    只断言「渲染里有 @evidence_id=」是不够的 —— 那对「印一个查不到的地址」的实现
    同样成立，而模型照做时会拿回一句「取不到」，白花一次额度。
    """
    from services.ai.family_ledger import evidence_index_of

    body = "+ 100012 攻击力 " + "x" * 500
    store, evidence_id = _store_with(body)
    index = evidence_index_of(store)
    rendered = _candidate(evidence_index=index).describe(index)
    assert f"@evidence_id={evidence_id}" in rendered

    batch = ContextTools(FakeProvider(), body_cache=store).execute(
        [ContextRequest(type="evidence", name=evidence_id)]
    )

    assert batch.items[0].text == body


def test_the_synthesis_task_book_carries_the_address_and_says_how_to_fetch_it():
    from services.ai.family_ledger import MemberOutcome, evidence_index_of
    from services.ai.subagent import build_synthesis_task

    store, evidence_id = _store_with("+ 100012 攻击力 100")
    index = evidence_index_of(store)
    plan = _synthesis()
    step = MemberOutcome(
        plan=plan.members[0],
        # `ran` 的判据是 `outcome is not None` —— 不传它的成员在任务书里
        # 根本不出现（那是「没跑成」的意思），候选也跟着看不到。
        outcome=EngineOutcome(status=STATUS_SUCCEEDED),
        candidates=(_candidate(evidence_index=index),),
    )

    text = build_synthesis_task(plan, (step,), evidence_index=index)

    assert f"@evidence_id={evidence_id}" in text
    assert "evidence" in text, "任务书要说明「按地址索取」这件事，否则地址只是一个装饰"


def test_the_verify_task_book_carries_the_address_of_the_finding_s_file():
    from services.ai.family_ledger import evidence_index_of
    from services.ai.subagent import build_verify_task

    store, evidence_id = _store_with("+ 100012 攻击力 100")
    index = evidence_index_of(store)
    synthesis = EngineOutcome(
        status=STATUS_SUCCEEDED,
        # 对账轮挑的是 `synthesis.anomalies`（已过门槛、已封顶的那一份）。
        anomalies=(_candidate(evidence_index=index).anomaly,),
        report_markdown="# 变更理解\n\n正文",
    )
    text = build_verify_task(_synthesis(), synthesis, evidence_index=index)

    assert f"@evidence_id={evidence_id}" in text
    assert "道具表第 12 行改了名字" not in text


def test_the_address_shown_by_the_synthesis_task_book_is_fetchable():
    """**把承诺闭合**：任务书里印出来的那个地址，按它索取要能拿回那份原件。

    渲染与取回是两处实现的，只测渲染等于只测了「印了一个字符串」。
    """
    import re

    from services.ai.family_ledger import MemberOutcome, evidence_index_of
    from services.ai.subagent import build_synthesis_task

    body = "+ 100012 攻击力 " + "y" * 300
    store, evidence_id = _store_with(body)
    index = evidence_index_of(store)
    plan = _synthesis()
    step = MemberOutcome(
        plan=plan.members[0],
        # `ran` 的判据是 `outcome is not None` —— 不传它的成员在任务书里
        # 根本不出现（那是「没跑成」的意思），候选也跟着看不到。
        outcome=EngineOutcome(status=STATUS_SUCCEEDED),
        candidates=(_candidate(evidence_index=index),),
    )

    text = build_synthesis_task(plan, (step,), evidence_index=index)
    printed = re.findall(r"@evidence_id=([0-9a-f]{20})", text)
    assert printed, text

    for one in printed:
        batch = ContextTools(FakeProvider(), body_cache=store).execute(
            [ContextRequest(type="evidence", name=one)]
        )
        assert batch.items[0].text == body
