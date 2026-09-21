# -*- coding: utf-8 -*-
"""截断账：**汇总计数与逐条明细必须是同一份口径**。

## 这条是被什么逼出来的（2026-09-21 实测）

线上同一次分析的三个数对不上：

| 读法 | run 13 | run 12 | run 11 |
|---|---|---|---|
| `tool_stats_json` 里的 `file_diff.truncated` | 18 | 18 | 17 |
| 逐 trace 求和 `dropped_json.truncated` | 18 | 18 | 17 |
| 逐 trace 的 `executed_json.details[].truncated` 求和 | **25** | **26** | **23** |

缺口在**跨成员共享缓存命中**那一条分支上。A 把一份超长 diff 取回来、截断之后交给模型，
那份 `ContextItem`（frozen，`meta` 里带着 `truncated=True`）留在共享 `body_cache` 里；
B 要同一个坐标时拿到的是**这份原件**（`context_tools` 模块 docstring 第 5 条：跨成员必须
给全文，不能给指针）。可是那条分支只记了 `calls` / `cache_hits` / `source_chars` /
`produced_chars` —— **没有记截断**。于是同一条被截断的正文在明细里出现 N 次（它是 B 真实
拿到的东西），在计数里只记 1 次。多出来的 7 条就是这么来的。

## 本文锁住的口径：**交付口径**

`truncated` 记的是「**交给模型时是截断的正文**」的条数 —— 谁拿到的是截断正文就算谁一条，
含跨成员复用同一份。等式（三本账各自求和之后必须同时成立）：

    Σ_轮 dropped_json.truncated
      == Σ_kind tool_stats[kind].truncated
      == Σ_轮 Σ_details details[].truncated    （一轮条数 ≤ TRACE_LIST_MAX_ITEMS 时）
      == Σ_轮 batch.truncated
      == Σ_轮 len([i for i in batch.items if i.meta.get("truncated")])

选「交付」而不是「本次执行」的理由：一条被截断的正文交给 B 时，B 看到的**确实是截断的**，
它据此写下的结论同样有覆盖缺口 —— 这与同一条分支里 `produced_chars` 记全文长度是同一个
道理（那一条早就承认「这次真的把正文交给了模型」）。所以补的是**计数侧漏记的那一笔**，
不是把明细侧的 `truncated` 抹平 —— 抹平会掩盖真实的覆盖缺口（模块 docstring 第 6 条）。

## 这几条测试盯的是

1. 共享命中复用一条**已截断**的正文 → 两本账都对得上，且都不为 0；
2. 共享命中一份**没截断**的正文 → 两本账都不动（否则是修出一个反向的假账）；
3. 共享的**失败条目**（`tool_failed`：给出去的是「取不到」的说明，不是截断正文）→ 不算截断；
4. 共享命中之后同一成员再要一次 → 拿到的是**指针**，不算截断、也不重复计数；
5. 落到 trace 那两列上（`dropped_json.truncated` 与 `executed_json.details`）仍然相等 ——
   这一条正是线上 18 / 25 那个症状本身。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from services.ai.context_tools import ContextTools
from services.ai.protocol import ContextRequest
from services.ai.trace_evidence import encode_evidence

COMMIT = "a" * 40
PATH = "config/30_goods/item.xlsx"
# 单条上限。取小值只是为了让「一份正常长度的 diff」也能测试截断，与真实配置无关。
LIMIT = 400


class FakeProvider:
    """只实现本文件用到的那一个取数入口；记录调用次数，用来断言「有没有真的再取一次」。"""

    def __init__(self, file_diff=None):
        self.file_diff_response = file_diff
        self.calls: list[tuple] = []

    def file_diff(self, commit, path):
        self.calls.append((commit, path))
        return self.file_diff_response


def _long_diff() -> str:
    """超过 `LIMIT` 的纯文本差异（切不开的 diff 走「保留首尾」那一支）。"""
    return "表头\n" + "行内容" * 500


def _diff_request() -> ContextRequest:
    return ContextRequest(type="file_diff", commit=COMMIT, path=PATH)


def _member(shared: dict, provider: FakeProvider) -> ContextTools:
    """一个成员的工具实例。**每个成员各一个**，共享的只有 `body_cache`。"""
    return ContextTools(provider, limits={"file_diff": LIMIT}, body_cache=shared)


def _ledger(*runs) -> tuple[int, int, int]:
    """若干次批量执行的三本账：`(逐条明细, 批次计数, 按类型计数)`。

    `runs` 是 `(tools, batch)` 对。三本账的**来源互不相同**（明细来自条目的 meta、
    批次计数来自 `ToolBatch`、按类型计数来自 `stats`），所以它们相等才有意义。

    **每个实例的轮次要一次给全**：`stats` 是**跨轮累计**的（模块 docstring 第 2 条），
    只给其中一轮的话，按类型计数会把后面几轮的一起带上 —— 那种对不上是调用方给错了
    输入，不是账错了。
    """
    details = sum(
        1 for _, batch in runs for item in batch.items if item.meta.get("truncated")
    )
    batches = sum(batch.truncated for _, batch in runs)
    # 按类型计数是**每个实例一份**（累计值），所以同一个实例出现多轮时只能算一次。
    instances = {id(tools): tools for tools, _ in runs}
    counters = sum(
        int(counters.get("truncated", 0))
        for tools in instances.values()
        for counters in tools.stats.values()
    )
    return details, batches, counters


def _assert_one_ledger(runs, *, expected_hits: int) -> tuple[int, int, int]:
    """三本账相等 + 前提校验（这一组里真的发生过 `expected_hits` 次截断交付）。"""
    details, batches, counters = _ledger(*runs)

    assert details == batches == counters, (
        f"三本截断账对不上：明细 {details} 条、批次计数 {batches}、按类型计数 {counters}"
    )
    assert details == expected_hits, (
        f"这一组里该有 {expected_hits} 条截断交付，实际 {details} "
        "（等式成立但两边同时为 0，就是这条断言在拦：那说明这条用例根本没测到东西）"
    )
    return details, batches, counters


def _round_record(batch, requests=()):
    """引擎交给 `trace_evidence` 的那一份 `RoundRecord` 的等价物。"""
    return SimpleNamespace(
        request_count=len(requests),
        item_count=len(batch.items),
        refused_by_budget=batch.refused_by_budget,
        truncated=batch.truncated,
        requests=tuple(requests),
        executed=batch.items,
        dropped=batch.dropped,
        budget_notes=(),
        response_text="",
        correction_hint="",
    )


# ==========================================================================
# 跨成员复用一条**已被截断**的正文
# ==========================================================================

def test_a_reused_truncated_body_is_counted_for_the_second_member_too():
    """**这一条就是线上那个缺口。**

    A 取回一份超长 diff、截断后交给模型；B 要同一个坐标时从共享缓存拿到**同一份截断正文**
    （第 5 条：跨成员给全文，不能给指针）。B 拿到的内容同样是残缺的，所以 B 这一笔也要记。
    """
    shared: dict = {}
    provider = FakeProvider(_long_diff())

    mine = _member(shared, provider)
    mine_batch = mine.execute([_diff_request()])
    theirs = _member(shared, provider)
    theirs_batch = theirs.execute([_diff_request()])

    assert len(provider.calls) == 1, "第二个成员不该再取一次数"
    assert theirs_batch.items[0].meta.get("truncated") is True, (
        "前提：B 拿到的确实是那份被截断过的正文"
    )
    assert "[已在上文给出]" not in theirs_batch.items[0].text, "且给的是全文而不是指针"

    _assert_one_ledger([(mine, mine_batch)], expected_hits=1)
    _assert_one_ledger([(theirs, theirs_batch)], expected_hits=1)
    # 两家合起来：两次截断交付（同一条正文，但两个成员各拿到一次）
    _assert_one_ledger([(mine, mine_batch), (theirs, theirs_batch)], expected_hits=2)


def test_the_two_ledgers_reconcile_across_rounds_of_the_same_member():
    """同一个成员跨轮次也要对得上：第一轮取数截断，第二轮命中本地缓存给的是指针。

    指针里没有正文（`_repeat_item` 不记 `truncated`），所以它**只算一次** ——
    这一条防的是「修完共享那条之后顺手把指针也算上」。
    """
    shared: dict = {}
    provider = FakeProvider(_long_diff())
    tools = _member(shared, provider)

    first = tools.execute([_diff_request()])
    second = tools.execute([_diff_request()])

    assert second.items[0].meta.get("repeat_pointer") is True, "前提：第二次给的是指针"
    assert second.truncated == 0, "指针里没有正文，不该把第一轮那一笔记到这一轮头上"
    # 把两轮一起看：这一组总共只该有 1 条截断交付（第二轮没有新的正文进提示词）
    _assert_one_ledger([(tools, first), (tools, second)], expected_hits=1)


def test_the_trace_columns_carry_the_same_number_as_the_details():
    """落到库里的两列必须相等 —— 这一条复现的正是线上 18 / 25 那个症状。

    `dropped_json.truncated` 读的是 `batch.truncated`，`executed_json.details[].truncated`
    读的是条目的 meta，两者来源不同，所以「相等」是一件要**测**的事。
    """
    shared: dict = {}
    provider = FakeProvider(_long_diff())
    mine = _member(shared, provider)
    mine_batch = mine.execute([_diff_request()])
    theirs = _member(shared, provider)
    theirs_batch = theirs.execute([_diff_request()])

    runs = [(mine, mine_batch), (theirs, theirs_batch)]
    columns = [_round_record(batch, [_diff_request()]) for _, batch in runs]
    encoded = [encode_evidence(record) for record in columns]

    dropped = sum(int(json.loads(row["dropped_json"])["truncated"]) for row in encoded)
    details = sum(
        bool(item["truncated"])
        for row in encoded
        for item in json.loads(row["executed_json"])["details"]
    )
    counters = sum(
        int(counters.get("truncated", 0))
        for tools in {id(tools): tools for tools, _ in runs}.values()
        for counters in tools.stats.values()
    )

    assert dropped == details == counters, (
        f"落库口径对不上：dropped_json {dropped}、明细求和 {details}、"
        f"tool_stats {counters}"
    )
    assert details > 0, "前提：这一组里真的发生过截断"


# ==========================================================================
# 反向：不该记的一笔都不能多
# ==========================================================================

def test_a_shared_hit_of_an_untruncated_body_stays_out_of_the_ledger():
    """没被截断的共享命中不受影响（否则这一个修复就变成了「凡共享必截断」）。"""
    shared: dict = {}
    provider = FakeProvider("差异" * 20)

    mine = _member(shared, provider)
    mine_batch = mine.execute([_diff_request()])
    theirs = _member(shared, provider)
    theirs_batch = theirs.execute([_diff_request()])

    assert len(provider.calls) == 1, "前提：第二次确实命中了共享缓存"
    assert "truncated" not in theirs_batch.items[0].meta
    _assert_one_ledger(
        [(mine, mine_batch), (theirs, theirs_batch)], expected_hits=0
    )


def test_a_shared_hit_whose_meta_says_truncated_is_false_stays_out_of_the_ledger():
    """分段那一条路**总是**写 `truncated` 这个键（装得下时值是 `False`）。

    所以判据必须是**它的值**，不是「这个键在不在」：写成 `"truncated" in shared.meta`
    的话，一份完整给全的分段内容也会被记成截断 —— 那是往账上灌水，而且灌的是「平台砍了
    内容」这种最容易被当真的一类。
    """
    shared: dict = {}
    provider = FakeProvider("@@ -1,2 +1,2 @@\n-旧\n+新\n\n@@ -10,2 +10,2 @@\n-旧2\n+新2\n")

    mine = _member(shared, provider)
    mine_batch = mine.execute([_diff_request()])
    theirs = _member(shared, provider)
    theirs_batch = theirs.execute([_diff_request()])

    assert len(provider.calls) == 1, "前提：第二次确实命中了共享缓存"
    meta = theirs_batch.items[0].meta
    assert "truncated" in meta and meta["truncated"] is False, (
        "前提：分段那条路把 `truncated` 写成了显式的 False"
    )
    _assert_one_ledger([(mine, mine_batch), (theirs, theirs_batch)], expected_hits=0)


def test_a_shared_failure_is_not_counted_as_truncated():
    """共享的**失败条目**不算截断。

    它同样走共享命中那条分支（`_shared_get` 把失败条目也当命中，见那里的 docstring），
    但它给出去的是「取不到」的说明，不是一段被砍过的正文 —— 判据必须落在
    `meta["truncated"]` 上，不能落在「命中了共享缓存」上。
    """
    shared: dict = {}
    provider = FakeProvider(None)

    mine = _member(shared, provider)
    mine_batch = mine.execute([_diff_request()])
    theirs = _member(shared, provider)
    theirs_batch = theirs.execute([_diff_request()])

    assert len(provider.calls) == 1, "前提：第二次确实命中了共享缓存"
    assert theirs_batch.items[0].meta.get("tool_failed") is True
    _assert_one_ledger(
        [(mine, mine_batch), (theirs, theirs_batch)], expected_hits=0
    )
