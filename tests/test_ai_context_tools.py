"""白名单工具的执行、缓存、预算与失败降级。

这个模块的价值全在「出错的时候怎么办」，所以用例也集中在那里：

* 取数**失败**与**确实没有内容**必须给出不同的文本。把失败静默成空字符串，模型会把
  「我们没读到」当成「这里没有改动」，然后基于这个前提写出一条看起来很确定的结论。
* 工具炸了不能作废整轮，但也不能悄悄少给一条。
* 预算是**一次分析的总量**。第一版按「本轮请求列表的下标」判断，第二轮又从 0 开始，
  预算永远不会触发 —— `test_budget_is_cumulative_across_rounds` 就是抓它的。
"""

from __future__ import annotations

import pytest

from services.ai.budget import TRUNCATION_SUFFIX, ContextItem, elision_marker, truncate_text_middle
from services.ai.context_tools import (
    ContextTools,
    ToolBatch,
    describe_request,
)
from services.ai.protocol import ContextRequest

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
PATH_A = "config/30_goods/item.xlsx"
PATH_B = "scripts/export.py"


class FakeProvider:
    """可控的 provider。记录调用，方便断言「有没有真的去取数」。"""

    def __init__(self, **responses):
        self.responses = responses
        self.calls: list[tuple[str, tuple]] = []

    def _respond(self, key: str, *args):
        self.calls.append((key, args))
        value = self.responses.get(key)
        # 必须是 BaseException 而不是 Exception：KeyboardInterrupt 不属于 Exception，
        # 用 Exception 判断的话它会被当成一个「正常返回值」交给下游，于是
        # `test_a_keyboard_interrupt_is_not_swallowed` 测的其实是一个空字符串。
        if isinstance(value, BaseException):
            raise value
        return value

    def commit_detail(self, commit):
        return self._respond("commit_detail", commit)

    def file_diff(self, commit, path):
        return self._respond("file_diff", commit, path)

    def file_content(self, commit, path, lines=""):
        return self._respond("file_content", commit, path, lines)

    def read_reference(self, name):
        return self._respond("read_reference", name)

    def find_references(self, query, path=""):
        return self._respond("find_references", query, path)


def _diff_request(path=PATH_A, commit=COMMIT_A) -> ContextRequest:
    return ContextRequest(type="file_diff", commit=commit, path=path)


# ==========================================================================
# 正常取数
# ==========================================================================


def test_a_successful_fetch_becomes_a_labelled_item():
    tools = ContextTools(FakeProvider(file_diff="+ 100012 攻击力 100"))
    batch = tools.execute([_diff_request()])

    assert len(batch.items) == 1
    item = batch.items[0]
    assert item.kind == "file_diff"
    assert "100012" in item.text
    assert PATH_A in item.label
    assert batch.executions == 1
    assert not batch.has_failures


def test_each_request_type_reaches_its_provider_method():
    provider = FakeProvider(
        commit_detail="提交说明",
        file_diff="差异",
        file_content="全文",
        read_reference="参考文档",
    )
    tools = ContextTools(provider)
    tools.execute(
        [
            ContextRequest(type="commit_detail", commit=COMMIT_A),
            _diff_request(),
            ContextRequest(type="file_content", commit=COMMIT_A, path=PATH_B),
            ContextRequest(type="read_reference", name="incident-checklist.md"),
        ]
    )

    assert [name for name, _ in provider.calls] == [
        "commit_detail",
        "file_diff",
        "file_content",
        "read_reference",
    ]


def test_paths_are_normalized_before_reaching_the_provider():
    """模型常把分隔符写成反斜杠。归一化后 provider 才拿得到（它按正斜杠查 git）。"""
    provider = FakeProvider(file_diff="差异")
    ContextTools(provider).execute(
        [ContextRequest(type="file_diff", commit=COMMIT_A, path="config\\30_goods\\item.xlsx")]
    )

    assert provider.calls[0][1] == (COMMIT_A, PATH_A)


def test_commit_is_passed_through_unchanged():
    provider = FakeProvider(commit_detail="说明")
    ContextTools(provider).execute([ContextRequest(type="commit_detail", commit=COMMIT_B)])
    assert provider.calls[0][1] == (COMMIT_B,)


# ==========================================================================
# 缓存
# ==========================================================================


def test_the_same_request_is_fetched_only_once():
    """模型会忘，跨轮次反复要同一个文件是常态。不缓存就要一遍遍重跑 Excel diff。"""
    provider = FakeProvider(file_diff="差异")
    tools = ContextTools(provider)

    tools.execute([_diff_request()])
    tools.execute([_diff_request()])

    assert len(provider.calls) == 1
    assert tools.executions == 1
    assert tools.cache_hits == 1


def test_a_different_path_is_a_different_cache_entry():
    provider = FakeProvider(file_diff="差异")
    tools = ContextTools(provider)
    tools.execute([_diff_request(), _diff_request(path=PATH_B)])

    assert len(provider.calls) == 2
    assert tools.cache_hits == 0


def test_a_different_commit_is_a_different_cache_entry():
    provider = FakeProvider(file_diff="差异")
    tools = ContextTools(provider)
    tools.execute([_diff_request(), _diff_request(commit=COMMIT_B)])

    assert len(provider.calls) == 2


def test_the_cache_does_not_outlive_one_analysis():
    """**缓存是一次分析的，不是进程的。**

    仓库被重新导入或 force-push 之后，同一个 commit 的 diff 会变。按 commit 做的全局
    缓存会一直吐旧结果，于是分析报告与页面上看到的 diff 对不上——最难查的一类不一致。
    """
    provider = FakeProvider(file_diff="差异")
    ContextTools(provider).execute([_diff_request()])
    ContextTools(provider).execute([_diff_request()])

    assert len(provider.calls) == 2


def test_a_repeated_request_gets_a_pointer_instead_of_the_body_again():
    """**正文只进提示词一次。**

    第二次索要同一个文件时，原样再 append 一遍的代价有两笔：那 11,000 字的 diff 会以
    **新增内容**的身份第二次计价（它是本轮提示词尾部的新内容，前缀缓存覆盖不到它），
    而且模型的注意力要落在两份一模一样的东西上。所以第二次给的是一条**指针**。

    指针里的 `kind` / `label` 必须与首次那条**逐字一致** —— 那就是模型回查内容的地址，
    与 `render_context_items` 渲染出的标题同形；指针还必须明说「不用再要一遍」，否则
    模型会重新索取，而重复索取照样消耗额度（额度耗光了它还是没看到内容）。
    """
    tools = ContextTools(FakeProvider(file_diff="x" * 50_000), limits={"file_diff": 500})
    first = tools.execute([_diff_request()]).items[0]
    second = tools.execute([_diff_request()]).items[0]

    assert first.meta["truncated"] is True, "首次该截断还是要截断"
    assert second.meta["repeat_pointer"] is True
    assert second.meta["chunk_id"] == first.meta["chunk_id"]
    assert (second.kind, second.label) == (first.kind, first.label), "指针丢了回查地址"
    assert f"### [{first.kind}] {first.label}" in second.text, "没告诉模型内容在哪一节"
    assert "不要再次索取" in second.text, "没告诉模型不用再要一遍"
    assert "x" * 100 not in second.text, "正文又原样来了一遍"


def test_the_repeat_pointer_counts_toward_the_prompt_budget():
    """指针也会占提示词的字符额度（虽然很小）。

    记账口径必须与「真正进了提示词多少字」一致，否则预算算的是另一个数。
    """
    tools = ContextTools(FakeProvider(file_diff="差异" * 4000))
    first = tools.execute([_diff_request()]).items[0]
    second = tools.execute([_diff_request()]).items[0]

    counters = tools.stats["file_diff"]
    assert counters["produced_chars"] == len(first.text) + len(second.text)
    # `source_chars` 的口径**没有变**：命中缓存时那些字符是上一轮取过的，仍然算这个
    # 类型的产出（见 execute 里那段注释），所以它是两份原文长度之和。
    assert counters["source_chars"] == 2 * len(first.text)
    assert counters["cache_hits"] == 1 and counters["executions"] == 1
    # 真正交给模型的字符数里，第二份只有指针那么长。
    assert len(second.text) < len(first.text) / 10


def test_a_different_line_window_is_a_different_cache_entry():
    """同一个文件的两段窗口**不是同一份内容**。

    缓存键里漏掉 `lines` 的后果不是多花一次取数，而是「模型要第二段，拿回来的是一句
    『你已经拿到这份内容了，见上文』」—— 而它上文里只有第一段。于是它以为看过了。
    """
    provider = FakeProvider(file_content="正文")
    tools = ContextTools(provider)

    first = tools.execute(
        [ContextRequest(type="file_content", commit=COMMIT_A, path=PATH_B, lines="100-200")]
    ).items[0]
    second = tools.execute(
        [ContextRequest(type="file_content", commit=COMMIT_A, path=PATH_B, lines="300-400")]
    ).items[0]

    assert len(provider.calls) == 2, "换了一段窗口就该真的去取"
    assert second.meta.get("repeat_pointer") is None, "第二段被当成重复索取，给成了指针"
    assert first.label != second.label, "两段的标签相同 → 指针指向一个不唯一的位置"
    assert "100-200" in first.label and "300-400" in second.label


def test_a_different_search_keyword_is_a_different_cache_entry():
    """**换一个关键词就是换一份内容 —— 与 `lines` 那条是同一个错，后果更严重。**

    `find_references` 没有 commit、也没有单个文件（`path` 是**可选的**范围前缀，多数
    请求根本不带），所以 `(type, commit, path, name, lines)` 这五项对它的每一次检索都是
    **同一串值**。缓存键里漏掉 `query` 的后果：整次分析里**第二次起的每一次检索**都会
    命中第一次那条，模型拿到的是一句「你已经拿到这份内容了，见上文」，而那一节标的是
    **另一个关键词**。

    线上真实的一次（2026-09-20 的报告）：模型请求 `find_references(CfgRewardMode)`，
    它拿回来的那一节写的是 `find_references _calcSegmentedBonus`（命中 0）——
    于是「引用扫描未执行」，`config_id` 与 `module_coupling` 两个维度只能写成信息缺口。
    子代理模式下更糟：`body_cache` 是**跨成员**共享的，一个成员的检索会砸掉另一个成员
    的关键词。
    """
    provider = FakeProvider(find_references="a.lua:12: target_id = 1")
    tools = ContextTools(provider)

    first = tools.execute(
        [ContextRequest(type="find_references", query="CfgRewardMode")]
    ).items[0]
    second = tools.execute(
        [ContextRequest(type="find_references", query="_calcSegmentedBonus")]
    ).items[0]

    assert len(provider.calls) == 2, "换了个关键词就该真的去搜"
    assert second.meta.get("repeat_pointer") is None, (
        f"第二个关键词被当成重复索取、给成了指针（指向的是「{first.label}」那一节）"
    )
    assert "CfgRewardMode" in first.label and "_calcSegmentedBonus" in second.label


def test_a_shared_cache_does_not_hand_one_members_search_to_another():
    """共享缓存那一份是**跨成员**的，所以关键词漏进键里的后果会跨成员扩散。"""
    shared: dict = {}
    provider = FakeProvider(find_references="a.lua:12: target_id = 1")

    ContextTools(provider, body_cache=shared).execute(
        [ContextRequest(type="find_references", query="CfgRewardMode")]
    )
    other = ContextTools(provider, body_cache=shared).execute(
        [ContextRequest(type="find_references", query="_calcSegmentedBonus")]
    ).items[0]

    assert len(provider.calls) == 2, "另一个成员的另一个关键词不该命中共享缓存"
    assert "_calcSegmentedBonus" in other.label, other.label


# ==========================================================================
# 跨成员共享的正文缓存（子代理模式）
# ==========================================================================

def test_a_shared_hit_gives_the_full_body_not_a_pointer():
    """**这是共享缓存存在的意义，也是最容易做错的一处。**

    「见上文那一节」在**另一个成员**的对话里是假话 —— 它的上文里根本没有那一节。
    给指针的话，模型要么去找一个不存在的东西，要么再要一次（照样占额度）。
    """
    shared: dict = {}
    provider = FakeProvider(file_diff="差异" * 100)
    first = ContextTools(provider, body_cache=shared).execute([_diff_request()]).items[0]
    second = ContextTools(provider, body_cache=shared).execute([_diff_request()]).items[0]

    assert len(provider.calls) == 1, "第二个成员不该再取一次"
    assert second.text == first.text, "共享命中给的是全文"
    assert second.meta.get("repeat_pointer") is None
    assert "[已在上文给出]" not in second.text


def test_a_shared_hit_still_lets_the_same_member_get_a_pointer():
    """共享命中之后，**同一个成员**再要一次时才是指针（它的上文里确实有了）。

    两个成员各自的**第一次**都拿全文（跨成员那次不能给指针），第二次才退化 ——
    这正是「共享缓存」与「同一成员内重复索取」两件事的分界。
    """
    shared: dict = {}
    provider = FakeProvider(file_diff="差异" * 100)
    tools = ContextTools(provider, body_cache=shared)
    mine_first = tools.execute([_diff_request()]).items[0]
    mine_second = tools.execute([_diff_request()]).items[0]
    other = ContextTools(provider, body_cache=shared)
    theirs_first = other.execute([_diff_request()]).items[0]
    theirs_second = other.execute([_diff_request()]).items[0]

    assert len(provider.calls) == 1, "两个成员加起来只该取一次"
    assert mine_first.meta.get("repeat_pointer") is None
    assert theirs_first.meta.get("repeat_pointer") is None, "跨成员那次要给全文"
    assert theirs_first.text == mine_first.text, "拿到的必须是同一份正文"
    assert mine_second.meta.get("repeat_pointer") is True
    assert theirs_second.meta.get("repeat_pointer") is True, "同一成员的第二次才给指针"


def test_the_shared_cache_counts_as_a_hit_not_an_execution():
    """记账口径：省下的是取数，**不是模型的索取额度**（那条在 `calls` 里照记）。

    `produced_chars` 与本地命中不同：跨成员那一次真的把正文交给了模型，所以算全文长度。
    """
    shared: dict = {}
    provider = FakeProvider(file_diff="差异" * 100)
    ContextTools(provider, body_cache=shared).execute([_diff_request()])
    second_tools = ContextTools(provider, body_cache=shared)
    item = second_tools.execute([_diff_request()]).items[0]

    counters = second_tools.stats["file_diff"]
    assert counters["calls"] == 1 and counters["cache_hits"] == 1
    assert counters["executions"] == 0, "没有真的去取数"
    assert counters["produced_chars"] == len(item.text)
    assert counters["source_chars"] == len(item.text)
    assert counters["cross_member_replayed_chars"] == len(item.text)


def test_tool_stats_measure_saved_duplicate_chars_and_unscoped_full_reads():
    tools = ContextTools(FakeProvider(file_content="正文" * 500))
    request = ContextRequest(type="file_content", commit=COMMIT_A, path=PATH_B)
    first = tools.execute([request]).items[0]
    second = tools.execute([request]).items[0]

    counters = tools.stats["file_content"]
    assert counters["avoided_duplicate_chars"] == len(first.text) - len(second.text)
    assert counters["unscoped_content_requests"] == 2
    assert first.meta["chunk_id"] == second.meta["chunk_id"]


def test_without_a_shared_cache_nothing_is_shared():
    """单代理那条路（`body_cache=None`）行为与服务端不传它时**完全一致**。"""
    provider = FakeProvider(file_diff="差异")
    ContextTools(provider).execute([_diff_request()])
    ContextTools(provider).execute([_diff_request()])

    assert len(provider.calls) == 2, "不传共享缓存时，两个实例互不相关"


def test_a_shared_failure_is_not_refetched():
    """取不到的条目也共享。

    同一个进程里几秒之内「Agent 离线」不会变，让 N 个成员各等一遍 15 秒的上限只会拖长
    整次分析。给出去的仍是那句带着原因的失败说明（它本来就要求写成信息缺口）。
    """
    shared: dict = {}
    provider = FakeProvider(file_diff=None)
    ContextTools(provider, body_cache=shared).execute([_diff_request()])
    item = ContextTools(provider, body_cache=shared).execute([_diff_request()]).items[0]

    assert len(provider.calls) == 1
    assert item.meta.get("tool_failed") is True
    assert "信息缺口" in item.text


# ==========================================================================
# 失败与空内容
# ==========================================================================


def test_a_failed_fetch_tells_the_model_not_to_guess():
    """**这条是模块的核心性质。**

    只给空字符串的话，模型会把「没读到」当成「没有改动」，然后基于这个前提给出一条
    看起来很有把握的结论。所以失败必须显式说出来，并且明确禁止猜测。
    """
    batch = ContextTools(FakeProvider(file_diff=None)).execute([_diff_request()])
    item = batch.items[0]

    assert item.meta["tool_failed"] is True
    assert batch.has_failures
    assert "没有" in item.text and "不要" in item.text
    assert "信息缺口" in item.text


def test_a_raising_tool_is_downgraded_not_fatal():
    provider = FakeProvider(file_diff=RuntimeError("git 超时"))
    batch = ContextTools(provider).execute([_diff_request()])

    assert batch.has_failures
    assert "git 超时" in batch.items[0].text
    assert any("RuntimeError" in record.reason for record in batch.dropped)


def test_a_keyboard_interrupt_is_not_swallowed():
    """工具失败只该让那一条降级，但 Ctrl-C 必须继续往上抛。

    `except Exception` 与 `except BaseException` 的差别就在这里，值得钉一条用例。
    """
    provider = FakeProvider(file_diff=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        ContextTools(provider).execute([_diff_request()])


def test_an_empty_result_is_distinguishable_from_a_failure():
    """「取数成功但没有内容」是确定性结论（例如新增文件没有旧版本），与失败不同。"""
    batch = ContextTools(FakeProvider(file_diff="")).execute([_diff_request()])
    item = batch.items[0]

    assert item.meta.get("tool_empty") is True
    assert not item.meta.get("tool_failed")
    assert not batch.has_failures
    assert "没有风险" in item.text, "空内容不能被当成「没问题」"


def test_a_failure_returned_as_a_sentence_is_counted_as_a_failure():
    """**provider 的「拿不到」有两副面孔：`None`，以及一句说明。**

    后者是刻意的（那一层要把「为什么拿不到」带给模型），但它是一个**非空字符串** ——
    只看 `meta.tool_failed` 就会把它记成「成功取回 N 个字符」。于是用量页按工具类型的
    「失败」列是 0，而同时 trace 面板上「N 条取不到」是真的 —— 两处口径不同，而
    「失败 0 次」会被读成「这次取数都很顺」。

    判据与 `trace_evidence.summarize_executed` 用同一个 `failure_notice`，不另写一份。
    """
    from services.ai.platform_provider import _render_agent_file_content

    # 配表正文解析失败（`file_content` 读到一张读不了的表）—— 这是线上真实会出现的那句
    sentence = _render_agent_file_content(
        {"kind": "excel", "file_path": "config/道具表.xlsx", "content": ""},
        path="config/道具表.xlsx",
    )
    assert sentence, "前提：这句失败说明不为空"

    tools = ContextTools(FakeProvider(file_content=sentence))
    tools.execute([ContextRequest(type="file_content", commit=COMMIT_A, path=PATH_B)])

    counters = tools.stats["file_content"]
    assert counters["failed"] == 1, (
        f"「内容无法解析成文本表格」被记成了成功取回：{counters}"
    )
    assert counters["source_chars"] == 0 and counters["produced_chars"] == 0, (
        "失败条目的字符数不该被算进取回量（那会把「没取到」伪装成「取到了几十个字」）"
    )


def test_a_real_conclusion_that_starts_with_a_bracket_is_not_a_failure():
    """反面：`[配表]` 那两句是**内容**（该表已被删除 / 没有可展示的差异），不是失败。

    按前缀识别区分不了这两类，所以配表正文的解析失败换了专属前缀
    （`[配表解析失败]`），而 `[配表]` 留作真结论 —— 把它算成失败会让「这次有一张表
    被删了」变成「这次取数失败了一次」。
    """
    tools = ContextTools(
        FakeProvider(file_diff="[配表] config/a.xlsx：本次没有可展示的差异。")
    )
    batch = tools.execute([_diff_request()])

    counters = tools.stats["file_diff"]
    assert counters["failed"] == 0, counters
    assert counters["produced_chars"] > 0, "真结论的字符数要照常算进取回量"
    assert not batch.has_failures


def test_one_failure_does_not_discard_the_other_results():
    provider = FakeProvider(file_diff=None, file_content="全文")
    batch = ContextTools(provider).execute(
        [_diff_request(), ContextRequest(type="file_content", commit=COMMIT_A, path=PATH_B)]
    )

    assert len(batch.items) == 2
    assert batch.has_failures
    assert "全文" in batch.items[1].text


def test_an_unknown_type_reports_a_failure_instead_of_empty_text():
    """走到这里说明 `sanitize_requests` 漏了一种类型。

    此时宁可如实报失败，也不要静默返回空 —— 那会让模型以为拿到了内容。
    """
    batch = ContextTools(FakeProvider()).execute(
        [ContextRequest(type="read_secret_file", commit=COMMIT_A, path=PATH_A)]
    )
    assert batch.items[0].meta["tool_failed"] is True


# ==========================================================================
# 预算
# ==========================================================================


def test_requests_beyond_the_budget_are_refused_with_a_notice():
    provider = FakeProvider(file_diff="差异")
    tools = ContextTools(provider, max_tool_requests=2)
    batch = tools.execute(
        [
            _diff_request(path="a/1.xlsx"),
            _diff_request(path="a/2.xlsx"),
            _diff_request(path="a/3.xlsx"),
            _diff_request(path="a/4.xlsx"),
        ]
    )

    assert len(provider.calls) == 2
    assert batch.refused_by_budget == 2
    assert len(batch.dropped) == 2
    assert any(item.kind == "budget_notice" for item in batch.items)
    assert tools.requests_remaining == 0


def test_budget_is_cumulative_across_rounds():
    """**预算是一次分析的总量，不是每轮一份。**

    第一版按「本轮请求列表的下标」判断，于是第二轮又从下标 0 开始、永远不会触发 ——
    这等于没有预算，模型可以无限要下去。
    """
    provider = FakeProvider(file_diff="差异")
    tools = ContextTools(provider, max_tool_requests=2)

    first = tools.execute([_diff_request(path="a/1.xlsx"), _diff_request(path="a/2.xlsx")])
    second = tools.execute([_diff_request(path="a/3.xlsx")])

    assert first.refused_by_budget == 0
    assert second.refused_by_budget == 1
    assert len(provider.calls) == 2
    assert tools.requests_seen == 2


def test_a_cache_hit_still_consumes_the_budget():
    """命中缓存省下的是我们的耗时，不是模型的索取额度。

    重复索要同一个文件正是「它没在收敛」的信号，把这种请求也计入，预算才真正起到
    逼模型收敛的作用。
    """
    tools = ContextTools(FakeProvider(file_diff="差异"), max_tool_requests=2)
    tools.execute([_diff_request()])
    batch = tools.execute([_diff_request()])

    assert batch.cache_hits == 1
    assert batch.refused_by_budget == 0
    assert tools.requests_remaining == 0

    refused = tools.execute([_diff_request()])
    assert refused.refused_by_budget == 1


def test_a_zero_budget_refuses_everything():
    batch = ContextTools(FakeProvider(file_diff="差异"), max_tool_requests=0).execute(
        [_diff_request()]
    )
    assert batch.items[0].kind == "budget_notice"
    assert batch.refused_by_budget == 1


# ==========================================================================
# 截断
# ==========================================================================


def test_a_long_diff_is_truncated_but_keeps_its_tail():
    """结构化 diff **保留首尾**。

    只砍尾巴会让排在后面的整张表完全不可见，而「配表 A 改了、配表 B 也要跟着改」正是
    这个平台最关心的风险。保留尾巴之后，至少每张被改动的表都露过面。
    """
    content = "开头-" + "中" * 20_000 + "-结尾"
    batch = ContextTools(FakeProvider(file_diff=content), limits={"file_diff": 2_000}).execute(
        [_diff_request()]
    )
    item = batch.items[0]

    assert item.meta["truncated"] is True
    assert item.meta["original_chars"] == len(content)
    assert item.text.startswith("开头-")
    assert item.text.endswith("-结尾")
    assert "中间省略" in item.text
    assert batch.truncated == 1


def test_a_long_plain_text_keeps_the_head_only():
    """普通文本没有「必须看到结尾」的性质，用带截断标记的头部截断即可。"""
    content = "y" * 30_000
    batch = ContextTools(FakeProvider(file_content=content), limits={"file_content": 1_000}).execute(
        [ContextRequest(type="file_content", commit=COMMIT_A, path=PATH_B)]
    )
    item = batch.items[0]

    assert item.text.endswith(TRUNCATION_SUFFIX)
    assert len(item.text) <= 1_000


def test_content_within_the_limit_is_not_marked_as_truncated():
    """反向自检：没有截断就不该打上截断标记，否则模型会去重新索取它已经有的东西。"""
    batch = ContextTools(FakeProvider(file_diff="短"), limits={"file_diff": 1_000}).execute(
        [_diff_request()]
    )
    assert "truncated" not in batch.items[0].meta
    assert batch.truncated == 0


def test_each_type_has_its_own_limit():
    batch = ContextTools(
        FakeProvider(commit_detail="c" * 5_000, file_diff="d" * 5_000),
        limits={"commit_detail": 100, "file_diff": 10_000},
    ).execute(
        [
            ContextRequest(type="commit_detail", commit=COMMIT_A),
            _diff_request(),
        ]
    )

    assert batch.items[0].meta.get("truncated") is True
    assert "truncated" not in batch.items[1].meta


# ==========================================================================
# 标签
# ==========================================================================


def test_labels_identify_the_context_without_the_whole_body():
    assert describe_request(ContextRequest(type="commit_detail", commit=COMMIT_A)) == (
        "commit_detail " + COMMIT_A[:12]
    )
    assert describe_request(
        ContextRequest(type="file_diff", commit=COMMIT_A, path=PATH_A)
    ) == f"file_diff {COMMIT_A[:12]} {PATH_A}"
    assert describe_request(
        ContextRequest(type="read_reference", name="incident-checklist.md")
    ) == "read_reference incident-checklist.md"


def test_tool_batch_defaults_are_empty():
    assert ToolBatch().items == ()
    assert not ToolBatch().has_failures


# ==========================================================================
# 保留首尾的截断（budget 侧的性质，用在这里更方便对照）
# ==========================================================================


def test_middle_truncation_reports_how_much_was_elided():
    """**标注的省略量 + 实际保留的字数 = 原文长度**。

    标注一个编出来的数字比不标更糟：模型会据此判断「缺口有多大」。第一版用例自己
    算错了首尾长度（把标记自身的字符也算进了保留量），断言失败——是测试的算术错了，
    代码是对的。所以这里改成用正则把标记整段切出来，剩下的就是真正的保留量。
    """
    import re

    content = "x" * 5_000
    result, truncated = truncate_text_middle(content, 1_000)
    assert truncated
    assert len(result) == 1_000

    match = re.search(r"\n\n\.\.\. \[中间省略 (\d+) 字，内容未完整展示\] \.\.\.\n\n", result)
    assert match, f"标记格式不符合预期：{result[:120]!r}"

    head, tail = result[: match.start()], result[match.end() :]
    assert head and tail, "保留首尾的截断必须两端都留下内容"
    assert int(match.group(1)) == 5_000 - len(head) - len(tail)


def test_middle_truncation_is_a_no_op_within_the_limit():
    result, truncated = truncate_text_middle("短文本", 100)
    assert result == "短文本"
    assert not truncated


def test_middle_truncation_never_exceeds_the_limit_even_when_tiny():
    """标记本身要占位，很小的上限也不能被撑破。"""
    for limit in (60, 100, 137, 500):
        result, truncated = truncate_text_middle("z" * 10_000, limit)
        assert truncated
        assert len(result) <= limit, f"limit={limit} 时结果 {len(result)} 字，超了"


@pytest.mark.parametrize("limit", [0, -5])
def test_middle_truncation_rejects_a_non_positive_limit(limit):
    with pytest.raises(ValueError):
        truncate_text_middle("abc", limit)


def test_elision_marker_states_the_count():
    assert "123" in elision_marker(123)


def test_context_item_char_count_tracks_text():
    assert ContextItem(kind="k", label="l", text="abc").char_count == 3


def test_the_refused_requests_are_named_not_just_counted():
    """被拒的请求要**点名**，不能只给一个计数。

    用户看到「上下文索取额度用尽，还有文件没看」时的第二个问题一定是「哪些」——
    缺一个文件与缺十四个文件，这份结论的可信度完全不同。

    标签用 `_human_request_label` 而不是 `describe_request`：后者是模型回查内容的
    **地址**（`file_content 9e315a3abcde config/x.xlsx`），直接拼进给用户看的那句话里
    读不出「缺的是哪个文件」。
    """
    tools = ContextTools(FakeProvider(file_diff="差异"), max_tool_requests=1)
    batch = tools.execute(
        [
            _diff_request(path="config/道具表.xlsx"),
            _diff_request(path="config/奖励表.xlsx"),
            ContextRequest(type="find_references", query="CfgRewardMode"),
        ]
    )

    assert batch.refused_by_budget == 2
    assert batch.refused_items == ("config/奖励表.xlsx", "引用扫描 CfgRewardMode")


def test_a_refused_window_says_which_lines():
    """带窗口的请求要连「哪几行」一起说 —— 同一个文件的两段是两个请求。"""
    tools = ContextTools(FakeProvider(file_content="正文"), max_tool_requests=0)
    batch = tools.execute(
        [ContextRequest(type="file_content", commit=COMMIT_A, path=PATH_A, lines="100-300")]
    )

    assert batch.refused_items == (f"{PATH_A}（100-300 行）",)


def test_nothing_is_named_when_nothing_was_refused():
    """没超预算时是空的 —— 界面据此决定要不要展开那一段。"""
    batch = ContextTools(FakeProvider(file_diff="差异"), max_tool_requests=5).execute(
        [_diff_request()]
    )

    assert batch.refused_by_budget == 0
    assert batch.refused_items == ()
