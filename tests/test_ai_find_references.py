# -*- coding: utf-8 -*-
"""`find_references`：在本批次改动的文件里找一处标识符的其它出现位置。

## 这个文件守的五条性质

1. **只在本批次的改动文件里搜。** 这是白名单纪律的延续 —— 这个工具能触达的内容不超过
   模型本来就能用 `file_content` 逐个索取的那些文件，所以它**不扩大读取面**。
2. **「没搜到」与「没搜完」必须分得开。** 抬头里要写出扫了几个、跳过了几个、有没有到
   上限就停：只有扫全了才能说「本批次里没有别处引用」，否则那句话是编的。
   线上一个周版本有 767 个文件，而一次搜索最多扫 240 个。
3. **越权的搜索词会被丢掉并说明原因**：太短的词（"id"）会把整批都搜出来，匹配不到任何
   改动文件的范围前缀则只会得到一句「没命中」——那会被读成「那里没有引用」。
4. **两端同一条实现**：平台本地与 Agent 端用的是同一个 `SnapshotReferenceIndex`
   （`ai/reference_index`）+ 同一个 `get_file_content_from_git`，只是跑在不同进程里；
   而它与**穷举基准**（`reference_search.search_files`）必须逐条给出相同的命中（第十节）。
5. **平台本地读不到时如实回报，并去问 Agent**（platform/agent 模式下平台被禁止 clone，
   本地永远读不到 —— 那正是「AI 看不到代码 diff」那个老问题的同一个根）。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from services.agent_reference_search import apply_batch_total
from services.ai import reference_search as rs
from services.ai.protocol import ContextRequest, parse_payload, sanitize_requests
from services.ai.reference_search import (
    is_binary,
    render_result,
    search_files,
    search_text,
)
from services.ai.scope import AnalysisScope, normalize_path
from services.ai import provider_search as agent_mode_module

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40


# ==========================================================================
# 一、纯函数：逐行找
# ==========================================================================


def test_hits_carry_the_line_number_and_the_line_itself():
    text = "local x = 1\nlocal target_id = 5\nprint(target_id)\n"

    hits = search_text(text, "target_id", path="a.lua", limit=10)

    assert [(hit.line, hit.text) for hit in hits] == [
        (2, "local target_id = 5"),
        (3, "print(target_id)"),
    ]
    assert hits[0].render() == "a.lua:2: local target_id = 5"


def test_the_search_is_case_insensitive():
    """同一个标识符在不同语言里的写法不一样（`TargetId` / `target_id`），
    大小写敏感会让「还有谁在用」这个问题漏掉一半答案。"""
    text = "TargetID = 1\nTARGETID = 2\n"

    assert len(search_text(text, "targetid", path="a.lua", limit=10)) == 2


def test_hits_are_capped_by_the_caller():
    text = "\n".join(f"target {index}" for index in range(50))

    assert len(search_text(text, "target", path="a.lua", limit=3)) == 3


# ==========================================================================
# 二、纯函数：读一堆文件
# ==========================================================================


def _reader(files: dict):
    def read(path, commit):
        return files.get(path)

    return read


def test_binary_and_unreadable_files_are_counted_not_ignored():
    """跳过要有理由、要计数：`0/0` 与「扫了 3 个、跳过 5 个」对模型是两件事。"""
    files = {
        "a.lua": "target = 1\n",
        "b.xlsx": b"PK\x03\x04binary",
        "c.lua": None,
        "d.lua": "nothing here\n",
    }
    result = search_files(
        [(path, COMMIT_A) for path in files], "target", reader=_reader(files)
    )

    assert [hit.path for hit in result.hits] == ["a.lua"]
    assert result.scanned == 2, "只算真的搜过的（a.lua 与 d.lua）"
    assert result.binary == 1 and result.missing == 1
    assert result.files_total == 4


def test_a_reader_that_raises_does_not_kill_the_search():
    files = {"a.lua": "target = 1\n"}

    def read(path, commit):
        if path == "boom.lua":
            raise RuntimeError("工作副本里没有这个文件")
        return files.get(path)

    result = search_files(
        [("boom.lua", COMMIT_A), ("a.lua", COMMIT_A)], "target", reader=read
    )

    assert result.missing == 1 and [hit.path for hit in result.hits] == ["a.lua"]


def test_the_file_cap_stops_the_scan_and_says_so():
    files = {f"f{index}.lua": "target = 1\n" for index in range(20)}
    pairs = [(path, COMMIT_A) for path in files]

    result = search_files(pairs, "target", reader=_reader(files), max_files=5)

    assert result.scanned == 5
    assert result.truncated_files is True, "扫到上限就停了，这件事必须能被说出来"
    assert len(result.hits) == 5


def test_the_hit_cap_stops_the_collection():
    files = {f"f{index}.lua": "target\n" * 10 for index in range(20)}
    pairs = [(path, COMMIT_A) for path in files]

    result = search_files(pairs, "target", reader=_reader(files), max_hits=12)

    assert len(result.hits) == 12
    assert result.truncated_hits is True


def test_the_scan_order_is_the_one_it_was_given():
    """顺序确定 → 「扫了前 N 个」这句话可复现，「为什么没搜到那个文件」才有答案。"""
    files = {"b.lua": "target\n", "a.lua": "target\n", "c.lua": "target\n"}

    result = search_files(
        [(path, COMMIT_A) for path in files], "target", reader=_reader(files)
    )

    assert [hit.path for hit in result.hits] == ["b.lua", "a.lua", "c.lua"]


def test_a_zip_is_binary_even_when_it_decodes():
    assert is_binary(b"PK\x03\x04") is True
    assert is_binary(b"plain text") is False
    assert is_binary(None) is True


# ==========================================================================
# 三、渲染：三个数都要在
# ==========================================================================


def _result(**overrides) -> rs.SearchResult:
    data = dict(query="target_id", hits=(rs.Hit("a.lua", 12, "target_id = 1"),),
                files_total=10, scanned=4, missing=2, binary=4, prefix="")
    data.update(overrides)
    return rs.SearchResult(**data)


def test_the_header_carries_the_coverage_numbers():
    text = render_result(_result())

    assert "命中 1 处" in text and "1 个文件" in text
    assert "4/10" in text, "扫了几个 / 共几个"
    assert "4 个不是文本" in text and "2 个读不到" in text


def test_no_hits_after_a_full_scan_may_be_read_as_an_answer():
    text = render_result(_result(hits=(), scanned=10, missing=0, binary=0))

    assert "没有出现这个关键词" in text
    assert "不代表整批里没有" not in text, "扫全了就不该再说这句"


def test_no_hits_after_a_partial_scan_must_not_be_read_as_an_answer():
    """**这是这个工具最容易骗人的地方**：只扫了一部分时的「没命中」不是结论。"""
    text = render_result(_result(hits=(), truncated_files=True))

    assert "没搜到" in text and "不代表整批里没有" in text


def test_the_scope_is_stated():
    text = render_result(_result(prefix="scripts/optional/"))
    assert "范围：scripts/optional/" in text
    assert "仅限" in text and "本批次改动过的文件" in text


def test_the_coverage_note_survives_a_cut_that_eats_the_whole_hit_list():
    """**覆盖率那句必须活过截断。**

    它原先排在最后一行，而这个结果的单条上限是 8,000 字、`MAX_HITS = 80` 条命中各带最多
    200 字正文 —— 实测一份打满命中的结果是 10,850 字，覆盖率那一整句会被**整段砍掉**
    （砍点还落在一行路径中间）。而被砍掉的恰恰是区分「没搜到」与「没搜完」的那一句，
    模型正是拿它决定能不能写下「没有其它引用」：等于**命中一多，它就会把「没搜完」读成
    「不存在」**。

    所以这里断言的是「在**被砍之后**的文本里，那两句话仍然在」。只断言 `render_result`
    的原始输出里有它是不够的 —— 那正是修改前的行为（原文里有、交给模型的那份没有）。
    """
    from services.ai.budget import truncate_text
    from services.ai.context_tools import DEFAULT_TOOL_LIMITS

    limit = DEFAULT_TOOL_LIMITS["find_references"]
    full = render_result(_result(
        hits=tuple(
            rs.Hit(path=f"scripts/optional/module_{i}.lua", line=100 + i,
                   text="local 配置项 = " + "很长的中文内容" * 12)
            for i in range(80)
        ),
        files_total=300, scanned=240, missing=12, binary=8,
        truncated_files=True, truncated_hits=True,
    ))

    assert len(full) > limit, f"fixture 没有触发截断（{len(full)} ≤ {limit}），这条就白测了"
    cut, was_cut = truncate_text(full, limit)

    assert was_cut is True
    assert "文件数到了上限就停了" in cut, f"「没搜完」这句被砍掉了：{cut[-200:]!r}"
    assert "不代表整批里没有" in cut, f"「没搜到不等于不存在」这句被砍掉了：{cut[-200:]!r}"
    # 抬头那行也要在（它是「这次搜了多少」的另一半）。
    assert "240/300" in cut, cut[:200]


# ==========================================================================
# 四、白名单：搜索词与范围
# ==========================================================================


def _scope() -> AnalysisScope:
    return AnalysisScope.from_iterables(
        commits=(COMMIT_A, COMMIT_B),
        paths_by_commit={
            COMMIT_A: ["scripts/optional/activity.lua", "config/goods.xlsx"],
            COMMIT_B: ["scripts/optional/activity.lua", "scripts/net/proto.lua"],
        },
        readable_references=["incident-checklist.md"],
    )


def _sanitize(*requests):
    return sanitize_requests(list(requests), _scope())


def test_a_search_needs_no_commit_and_is_allowed():
    allowed, dropped = _sanitize(
        ContextRequest(type="find_references", query="target_id")
    )

    assert dropped == ()
    assert allowed == (ContextRequest(type="find_references", path="", query="target_id"),)


def test_the_query_survives_the_real_parse_path():
    """**这个工具曾经端到端从未执行过**：模型按 SKILL.md 发
    `{"type": "find_references", "query": "target_id"}`，而 `_coerce_requests` 构造
    `ContextRequest` 时漏了 `query=` —— 解析出来永远是空串，于是 `sanitize_requests`
    把每一条都按「搜索词太短」丢掉，丢掉的理由还把责任推给模型。

    上面那几条用例都直接构造 `ContextRequest`，正好绕过这一层，所以谁也没发现。
    这条从**真解析入口**进去，钉住「模型发什么，拿到的就是什么」。
    """
    payload = parse_payload(json.dumps({
        "status": "need_more_context",
        "reason": "先看看这个字段还有谁在用",
        "requests": [{"type": "find_references", "query": "target_id", "path": "scripts/"}],
    }, ensure_ascii=False))

    request = payload.requests[0]
    assert request.query == "target_id", "解析层把 query 丢了 —— 这个工具就永远跑不起来"
    allowed, dropped = _sanitize(request)
    assert dropped == (), f"一条格式正确的搜索请求被丢掉了：{dropped}"
    assert [r.query for r in allowed] == ["target_id"]


def test_a_too_short_query_is_dropped_with_a_reason():
    """"id" 这种词会把整批都搜出来，纯属浪费额度与上下文。"""
    allowed, dropped = _sanitize(ContextRequest(type="find_references", query="id"))

    assert allowed == ()
    assert "太短" in dropped[0].reason


@pytest.mark.parametrize(
    "query, kept",
    [
        # 拉丁这一侧：与老口径（`len(query) < 3`）**逐条一致**，一个字都不许变。
        ("a", False),
        ("id", False),
        ("hp", False),
        ("get", True),
        ("limit", True),
        ("mana_cost", True),
        ("1002", True),
        # 中文这一侧：**两个字就是一个完整的词**，老口径把这一整侧全部误拒。
        ("队伍", True),
        ("匹配", True),
        ("冷却", True),
        ("药水", True),
        ("队", False),
    ],
)
def test_a_query_is_judged_by_its_script_not_by_its_character_count(query, kept):
    """「够不够具体」按**信息量**判，不按字符数 —— 判据的单位要跟着文字系统走。

    ## 这一条钉的是哪一次误伤

    老口径是 `len(query) < 3`，等于假定「一个标识符至少三个拉丁字母」。而这个工具搜的是
    lua 与配表，里面大量是中文，而中文**一个字就是一个词素**：`队伍`、`匹配`、`冷却`
    都是完整的词，却全部被挡在门外。更糟的是拒掉的理由（「换个具体一点的标识符」）对
    中文词无从下手 —— 没有比「队伍」更具体的两个字了。实测 run 71 的第 2 轮里，模型
    就为这个丢掉了 `队伍` 与 `匹配` 两条调查线，第 3 轮也没有再试。

    ## 为什么判据是这张表而不是一句话

    两个方向的错都要钉住：中文那侧**不许再误拒**（`队伍` 放行），拉丁那侧**不许被顺手
    改严**（`get`/`limit`/`hp` 的取舍与老口径逐条相同）。只断言「中文词能过」的话，
    把阈值整体调小同样能过 —— 而那会让 `id`/`a` 也混进来。
    """
    allowed, dropped = _sanitize(ContextRequest(type="find_references", query=query))

    assert bool(allowed) == kept, (query, kept, dropped)
    if not kept:
        assert "太短" in dropped[0].reason


def test_the_query_weight_is_not_the_character_count():
    """折算规则本身：汉字按 2 计、其余按 1 计（`队伍` 是 2 个字符、权重 4）。"""
    assert rs.query_weight("队伍") == 4
    assert rs.query_weight("get") == 3
    assert rs.query_weight("mana_cost") == 9
    assert len("队伍") < rs.query_weight("队伍")


def test_a_prefix_that_matches_nothing_is_dropped():
    """允许一个匹配不到东西的前缀只会得到「没命中」，而模型会把它读成「那里没有引用」。"""
    allowed, dropped = _sanitize(
        ContextRequest(type="find_references", query="target_id", path="scripts/legacy/")
    )

    assert allowed == ()
    assert "匹配不到" in dropped[0].reason


def test_a_prefix_that_matches_is_kept():
    allowed, _ = _sanitize(
        ContextRequest(type="find_references", query="target_id", path="scripts\\optional\\")
    )

    assert allowed[0].path == "scripts/optional/", "前缀要归一化（反斜杠常见）"
    assert allowed[0].query == "target_id"


def test_the_same_search_twice_in_one_round_is_deduped():
    allowed, _ = _sanitize(
        ContextRequest(type="find_references", query="target_id"),
        ContextRequest(type="find_references", query="target_id"),
    )

    assert len(allowed) == 1


def test_two_different_queries_are_two_requests():
    allowed, _ = _sanitize(
        ContextRequest(type="find_references", query="target_id"),
        ContextRequest(type="find_references", query="target_name"),
    )

    assert [item.query for item in allowed] == ["target_id", "target_name"]


def test_other_types_never_carry_a_query():
    """`query` 只有这一个类型认它 —— 别的类型带上它必须被清空，否则它会进缓存键。"""
    allowed, _ = _sanitize(
        ContextRequest(
            type="file_diff", commit=COMMIT_A, path="config/goods.xlsx", query="target_id"
        )
    )

    assert allowed[0].query == ""


# ==========================================================================
# 五、范围：本批次的文件与各自最后那次提交
# ==========================================================================


def test_batch_paths_are_deduped_and_ordered():
    scope = _scope()

    assert scope.batch_paths() == (
        "config/goods.xlsx",
        "scripts/optional/activity.lua",
        "scripts/net/proto.lua",
    ), "去重、按提交顺序、同一提交内按路径排序（确定性）"


def test_a_path_is_searched_at_the_last_commit_that_touched_it():
    """周版本里同一个文件被多条提交改过是常态，而搜索要看的是它最近的样子。"""
    scope = _scope()

    assert scope.commit_of_path("scripts/optional/activity.lua") == COMMIT_B
    assert scope.commit_of_path("config/goods.xlsx") == COMMIT_A
    assert scope.commit_of_path(normalize_path("scripts/optional/activity.lua")) == COMMIT_B


# ==========================================================================
# 六、工具层：走到 provider 的第五个门
# ==========================================================================


def test_the_tool_calls_the_provider_with_query_and_prefix():
    from services.ai.context_tools import ContextTools
    from tests.test_ai_context_tools import FakeProvider

    provider = FakeProvider(find_references="a.lua:12: target_id = 1")
    ContextTools(provider).execute(
        [ContextRequest(type="find_references", query="target_id", path="scripts/")]
    )

    assert provider.calls == [("find_references", ("target_id", "scripts/"))]


def test_the_label_carries_the_query_so_the_repeat_pointer_points_somewhere_real():
    from services.ai.context_tools import describe_request

    assert describe_request(
        ContextRequest(type="find_references", query="target_id")
    ) == "find_references target_id"
    assert "范围 scripts/" in describe_request(
        ContextRequest(type="find_references", query="target_id", path="scripts/")
    )


# ==========================================================================
# 七、两端取数
# ==========================================================================


def _provider(scope=None):
    from services.ai.platform_provider import PlatformContextProvider

    return PlatformContextProvider(loaded=SimpleNamespace(readable={}), scope=scope)


def test_without_a_scope_it_says_so_instead_of_pretending():
    text = _provider().find_references("target_id")

    assert "检索不可用" in text and "信息缺口" in text


def test_the_local_path_searches_the_batch(monkeypatch):
    """单机模式：平台自己有工作副本，本地搜。"""
    import services.vcs_content_service as vcs
    from services.ai import platform_provider as pp

    files = {
        ("scripts/optional/activity.lua", COMMIT_B): "print(target_id)\n",
        ("config/goods.xlsx", COMMIT_A): b"PK\x03\x04",
        ("scripts/net/proto.lua", COMMIT_B): "local x = 1\n",
    }
    monkeypatch.setattr(
        vcs, "get_file_content_from_git", lambda repo, commit, path: files.get((path, commit))
    )
    monkeypatch.setattr(pp, "is_agent_dispatch_mode", lambda: False)
    # 部署模式在**两个**模块里各被问一次：`_diff_from_agent` / `_content_from_agent`
    # 住在 `platform_provider`，而 `find_references` 那一段（2026-09-24 起）住在
    # `provider_search` —— 只打一处的话，另一条路会照旧按真实部署模式走。
    monkeypatch.setattr(agent_mode_module, "is_agent_dispatch_mode", lambda: False)
    provider = _provider(_scope())
    monkeypatch.setattr(provider, "_repository_of", lambda pairs: SimpleNamespace(id=1))

    text = provider.find_references("target_id")

    assert "scripts/optional/activity.lua:1: print(target_id)" in text
    assert "2/3" in text, "扫了 2 个文本文件、共 3 个改动文件"
    assert "1 个不是文本" in text, "配表是二进制（跳过），这件事要说出来"

    second = provider.find_references("target_id")
    assert second == text, "同一个词问两遍不该让节点重扫一遍"


def test_the_platform_asks_the_agent_when_it_cannot_read_locally(monkeypatch):
    """platform/agent 模式下平台被禁止 clone —— 本地取数是**确定**读不到的，
    这时必须去问业务节点上的 Agent，而不是回一句「读不到」。

    **发给 Agent 的是整批文件清单**（不只是前缀范围内的那些）：Agent 按这份清单建一次
    快照索引，前缀交给它自己的 `search()`。只发前缀命中的那一批会让 Agent 端的索引缓存
    按前缀分叉 —— 同一个「先问 `scripts/`、再问全局」的 bug 换个进程再犯一次。
    分母（`total_files`）仍然是**前缀范围内**的文件数：那是「这次结论覆盖了多少」的分母。
    """
    import services.agent_file_content_dispatch as dispatch
    from services.ai import platform_provider as pp

    calls: list[dict] = []

    def fake(repository, *, query, entries, prefix="", total_files=0):
        calls.append(
            {"query": query, "entries": entries, "prefix": prefix, "total_files": total_files}
        )
        return {"status": "ready", "text": "a.lua:1: hit", "scanned": 3}

    monkeypatch.setattr(dispatch, "request_references", fake)
    monkeypatch.setattr(pp, "is_agent_dispatch_mode", lambda: True)
    # 部署模式在**两个**模块里各被问一次：`_diff_from_agent` / `_content_from_agent`
    # 住在 `platform_provider`，而 `find_references` 那一段（2026-09-24 起）住在
    # `provider_search` —— 只打一处的话，另一条路会照旧按真实部署模式走。
    monkeypatch.setattr(agent_mode_module, "is_agent_dispatch_mode", lambda: True)
    provider = _provider(_scope())
    monkeypatch.setattr(provider, "_repository_of", lambda pairs: SimpleNamespace(id=7))

    text = provider.find_references("target_id", "scripts/")

    # 2026-09-24（工作包 D 的 P1）：没有冻结仓库范围时，结果前面**必须**有一句范围声明。
    # 这条分支（问 Agent）覆盖的仍然只有**本批次改动的文件**，而「没有命中」与「没搜那么宽」
    # 在模型那里必须分得开 —— 少了这句话，它会写「仓库里没有其它引用」。
    assert text.endswith("a.lua:1: hit")
    assert "[范围说明]" in text and "只覆盖本批次改动过的文件" in text
    assert len(calls) == 1
    assert calls[0]["query"] == "target_id"
    assert calls[0]["prefix"] == "scripts/"
    assert [(path, commit) for path, commit in calls[0]["entries"]] == [
        ("config/goods.xlsx", COMMIT_A),
        ("scripts/optional/activity.lua", COMMIT_B),
        ("scripts/net/proto.lua", COMMIT_B),
    ], "整批（含前缀之外的）都要发过去 —— 索引按快照建，前缀只在查询那一刻生效"
    assert calls[0]["entries"][1][1] == COMMIT_B, "每条路径配它自己最后那次提交"
    # 范围前缀 `scripts/` 下本批次有 2 个文件（`config/goods.xlsx` 不在范围内）。
    # 带过去的分母是**这个数**，不是 `len(entries)`（后者现在是整批的 3）。
    assert calls[0]["total_files"] == 2


def test_no_search_budget_gate_remains(monkeypatch):
    """`SearchBudget` 不是被调松了，是**被删了**。

    它曾经挂在 provider 上（子代理模式下 N 个成员共用一份），但 `remaining` 在生产代码里
    **零读者** —— 唯一的效果是让「第 2、3 次检索能搜多少文件」取决于前几次动过几个文件，
    而且两条部署路径扣的数还不一样。真正管住成本的是索引（读一次、查询不再读）与预热门槛。

    这条用例钉两件事：那个名字在模块里不存在了；连续问很多次也不会被任何计数器挡住。
    """
    import services.vcs_content_service as vcs
    from services.ai import platform_provider as pp

    assert "SearchBudget" not in dir(rs), "「额度」这个名字不该再出现在检索模块里"

    files = {("scripts/optional/activity.lua", COMMIT_B): "target_id = 1\n"}
    monkeypatch.setattr(
        vcs, "get_file_content_from_git", lambda repo, commit, path: files.get((path, commit))
    )
    monkeypatch.setattr(pp, "is_agent_dispatch_mode", lambda: False)
    # 部署模式在**两个**模块里各被问一次：`_diff_from_agent` / `_content_from_agent`
    # 住在 `platform_provider`，而 `find_references` 那一段（2026-09-24 起）住在
    # `provider_search` —— 只打一处的话，另一条路会照旧按真实部署模式走。
    monkeypatch.setattr(agent_mode_module, "is_agent_dispatch_mode", lambda: False)
    provider = _provider(_scope())
    monkeypatch.setattr(provider, "_repository_of", lambda pairs: SimpleNamespace(id=1))
    assert not hasattr(provider, "_search_budget"), "provider 上不该再有额度计数器"

    for index in range(6):
        text = provider.find_references(f"target_id{index}")

        assert "检索额度用尽" not in text, f"第 {index + 1} 次检索被额度挡住了"
        assert "索引版本" in text, f"第 {index + 1} 次检索没有真的走检索"


def test_the_agent_receives_the_full_snapshot_for_indexing(monkeypatch):
    """Agent 拿到的是**整批**快照，而覆盖率的分母是整批总数 —— 两者都不能是「被截过的那一批」。

    平台曾经把 `entries` 截到额度上限（`MAX_SCAN_FILES`）才发出去，于是 Agent 手里
    `len(entries)` 恒等于上限。让它拿这个数当分母，抬头就会写成「本批次的 240/240 个文件」
    —— 读起来是**全覆盖**，而真相是那个周版本的 767 个文件里只看了 240 个。

    现在即使批次比索引的预热门槛（`MAX_INDEX_FILES`）大，分母仍然是**整批的总数**，
    没索引的那部分走 `unindexed` 这条缺口如实报出来 —— 上面那段话里「240/240 全覆盖」
    与「一批 280 个文件」在这个 fixture 里同时成立，正是要钉的形状。
    """
    import services.agent_file_content_dispatch as dispatch
    from services.ai import platform_provider as pp
    from services.ai.reference_index import MAX_INDEX_FILES, SnapshotReferenceIndex

    # 比预热门槛大 40：索引只读到 `MAX_INDEX_FILES`，剩下的必须算成缺口而不是消失。
    batch = [f"scripts/f{index:04d}.lua" for index in range(MAX_INDEX_FILES + 40)]
    scope = AnalysisScope.from_iterables(
        commits=(COMMIT_A,), paths_by_commit={COMMIT_A: batch}, readable_references=[]
    )
    sent = {}

    def fake(repository, *, query, entries, prefix="", total_files=0):
        sent["entries"] = entries
        sent["total_files"] = total_files
        # 与 `agent_reference_search.search_references_for_agent` 同一套调用（含预热门槛）：
        # 「分母是整批」与「门槛之外如实报缺口」两件事必须同时成立。
        result = SnapshotReferenceIndex.build(
            entries, reader=lambda path, commit: None, max_files=MAX_INDEX_FILES
        ).search(query)
        result = apply_batch_total(result, total_files)
        return {"status": "ready", "scanned": result.scanned, "text": render_result(result)}

    monkeypatch.setattr(dispatch, "request_references", fake)
    monkeypatch.setattr(pp, "is_agent_dispatch_mode", lambda: True)
    # 部署模式在**两个**模块里各被问一次：`_diff_from_agent` / `_content_from_agent`
    # 住在 `platform_provider`，而 `find_references` 那一段（2026-09-24 起）住在
    # `provider_search` —— 只打一处的话，另一条路会照旧按真实部署模式走。
    monkeypatch.setattr(agent_mode_module, "is_agent_dispatch_mode", lambda: True)
    provider = _provider(scope)
    monkeypatch.setattr(provider, "_repository_of", lambda pairs: SimpleNamespace(id=7))

    text = provider.find_references("target_id")

    assert sent["total_files"] == len(batch), "分母被截成了 Agent 收到的那一批"
    assert len(sent["entries"]) == len(batch), "完整快照必须交给 Agent 构建索引"
    assert f"0/{len(batch)}" in text, f"分母必须是 {len(batch)}（本批次总数）：{text}"
    assert f"{40} 个还没索引" in text, "预热门槛之外的那些必须如实报出来，不能静默"
    assert "不代表整批里没有" in text, "「没搜到」与「没搜完」要分开"


def test_the_batch_total_only_moves_the_denominator_up():
    """纯函数那一半：平台给的总数**小于**这里数出来的时不生效。

    真的会小吗：平台算的是「本批次里落在这个前缀范围内的文件数」，而 `entries` 是整批 ——
    前缀范围的数**必然小于等于** `entries` 的长度（现在这两者可以不等了）。这时必须保持
    原样：把一个更小的数写进分母，会得出「扫了 300 个文件里的 240 个」这种不可能的数。
    历史任务的 payload 也可能没有这个键（`total_files` 缺失 → 0），同样要按「平台没说」处理。
    """
    from services.agent_reference_search import apply_batch_total
    from services.ai.reference_search import SearchResult

    result = SearchResult(query="q", files_total=5, scanned=5)

    assert apply_batch_total(result, 40).files_total == 40
    assert apply_batch_total(result, 40).truncated_files is True
    assert apply_batch_total(result, 5) is result, "一样大就不动它"
    assert apply_batch_total(result, 3).files_total == 5, "更小的数不许写进分母"
    assert apply_batch_total(result, None).files_total == 5, "老 payload 没有这个键"
    assert apply_batch_total(result, "x").files_total == 5, "读不出来就当没给"


def test_a_pending_agent_answer_is_not_a_conclusion(monkeypatch):
    import services.agent_file_content_dispatch as dispatch
    from services.ai import platform_provider as pp

    monkeypatch.setattr(
        dispatch,
        "request_references",
        lambda repository, *, query, entries, prefix="", total_files=0: {
            "status": "pending",
            "message": "Agent 当前离线，取数任务已排队",
        },
    )
    monkeypatch.setattr(pp, "is_agent_dispatch_mode", lambda: True)
    # 部署模式在**两个**模块里各被问一次：`_diff_from_agent` / `_content_from_agent`
    # 住在 `platform_provider`，而 `find_references` 那一段（2026-09-24 起）住在
    # `provider_search` —— 只打一处的话，另一条路会照旧按真实部署模式走。
    monkeypatch.setattr(agent_mode_module, "is_agent_dispatch_mode", lambda: True)
    provider = _provider(_scope())
    monkeypatch.setattr(provider, "_repository_of", lambda pairs: SimpleNamespace(id=7))

    text = provider.find_references("target_id")

    assert "检索还没回来" in text
    assert "不等于「没有其它引用」" in text, "这句话缺了就会被读成「这里没有引用」"


def test_the_agent_side_uses_the_same_index_and_the_same_reader(monkeypatch):
    """Agent 端不是另一套实现：同一个 `SnapshotReferenceIndex` + 同一个读文件函数。

    （它以前也走 `search_files`，那条路现在是**穷举基准**，见文件末尾第十节。）
    """
    import services.vcs_content_service as vcs
    from app import app as flask_app
    from app import create_tables, db
    from models import Project, Repository
    from services.agent_reference_search import search_references_for_agent

    with flask_app.app_context():
        create_tables()
        project = Project(code="p_rs", name="检索测试")
        db.session.add(project)
        db.session.flush()
        repository = Repository(
            project_id=project.id, name="r_rs", type="git",
            url="https://example.invalid/rs.git", branch="main",
            resource_type="code", clone_status="completed",
        )
        db.session.add(repository)
        db.session.flush()
        repository_id = repository.id

        monkeypatch.setattr(
            vcs,
            "get_file_content_from_git",
            lambda repo, commit, path: {"a.lua": "target_id = 1\n", "b.lua": "x = 2\n"}.get(path),
        )
        outcome = search_references_for_agent(
            {
                "repository_id": repository_id,
                "query": "target_id",
                "prefix": "",
                "entries": [["a.lua", COMMIT_A], ["b.lua", COMMIT_A]],
            }
        )

    assert outcome["hits"] == 1 and outcome["scanned"] == 2
    assert "a.lua:1: target_id = 1" in outcome["text"]
    assert outcome["matched"] == 1, "真实命中数要跟着回传（截断之前的那一个）"


def test_the_agent_side_bounds_its_first_read_and_reports_the_rest(monkeypatch):
    """Agent 那条路也有预热门槛，而且**门槛之外要如实报**。

    这条是「首次查询成本」在 Agent 端的判据：`search_references_for_agent` 一次读整批会让
    第一次查询超过它自己的等待上限（40 秒）落成 `pending`，所以它和平台本地那条路一样把
    `MAX_INDEX_FILES` 传给 `build()`。**顺带钉住它不静默**：没索引的那些走 `unindexed`。
    """
    import services.vcs_content_service as vcs
    from app import app as flask_app
    from app import create_tables, db
    from models import Project, Repository
    from services.agent_reference_search import search_references_for_agent
    from services.ai.reference_index import MAX_INDEX_FILES

    batch = [[f"code/f{index:04d}.lua", COMMIT_A] for index in range(MAX_INDEX_FILES + 25)]
    reads: list[str] = []

    with flask_app.app_context():
        create_tables()
        project = Project(code="p_rs_cap", name="检索门槛测试")
        db.session.add(project)
        db.session.flush()
        repository = Repository(
            project_id=project.id, name="r_rs_cap", type="git",
            url="https://example.invalid/rs_cap.git", branch="main",
            resource_type="code", clone_status="completed",
        )
        db.session.add(repository)
        db.session.flush()
        repository_id = repository.id

        def reader(repo, commit, path):
            reads.append(path)
            return "local target_id = 1\n"

        monkeypatch.setattr(vcs, "get_file_content_from_git", reader)
        outcome = search_references_for_agent(
            {
                "repository_id": repository_id,
                "query": "target_id",
                "prefix": "",
                "total_files": len(batch),
                "entries": batch,
            }
        )

    assert len(reads) == MAX_INDEX_FILES, f"Agent 首次查询读了 {len(reads)} 个 blob"
    assert outcome["files_total"] == len(batch), "分母仍是整批（不是被索引的那 240 个）"
    assert outcome["unindexed"] == 25 and outcome["truncated_files"] is True
    assert "25 个还没索引" in outcome["text"] and "文件数到了上限就停了" in outcome["text"]


# ==========================================================================
# 八、Agent 派发：两次不同的检索不能互相复用
# ==========================================================================


def test_two_searches_are_not_the_same_request(monkeypatch):
    """`_matches` 靠 payload 里的三个键认「这是不是同一份请求」，而检索既没有提交也没有
    单个文件 —— 签名必须含关键词与范围，否则查 A 的那次会被当成查 B 的复用，
    模型会拿到**另一个范围**的结果，而且完全看不出来。"""
    from services.agent_file_content_dispatch import (
        _matches,  # noqa: PLC0415
        request_references,
    )

    captured: dict = {}

    def fake_request_from_agent(**kwargs):
        captured.update(kwargs)
        return {"status": "unavailable", "message": "（这条用例只关心请求长什么样）"}

    import services.agent_file_content_dispatch as dispatch  # noqa: PLC0415

    monkeypatch.setattr(dispatch, "_request_from_agent", fake_request_from_agent)
    request_references(
        SimpleNamespace(id=3, project_id=9),
        query="target_id",
        entries=[["a.lua", COMMIT_A]],
        prefix="scripts/",
    )

    task = SimpleNamespace(
        payload=json.dumps(
            {
                "commit_id": captured["commit_id"],
                "file_path": captured["file_path"],
                "lines": captured["lines"],
            }
        )
    )
    same = _matches(
        task, commit_id="", file_path="", lines=captured["lines"]
    )
    other_query = _matches(
        task, commit_id="", file_path="", lines="target_name|scripts/|1"
    )
    other_scope = _matches(task, commit_id="", file_path="", lines="target_id||1")

    assert same is True
    assert other_query is False, "换了关键词必须是另一份请求"
    assert other_scope is False, "换了范围必须是另一份请求"
    # 本批次的文件总数也在签名里：它不影响命中清单，只影响抬头那句覆盖率 ——
    # 少了它，「上周 767 个文件里扫了 240 个」的那份结果会被本周的检索复用。
    other_total = _matches(
        task, commit_id="", file_path="", lines=captured["lines"].replace("|0|", "|767|", 1)
    )
    assert other_total is False, "换了本批次的总数必须是另一份请求"
    assert captured["extra_payload"]["query"] == "target_id"
    assert captured["extra_payload"]["entries"] == [["a.lua", COMMIT_A]]
    assert captured["extra_payload"]["total_files"] == 0, "没传就是 0（这条用例没传）"


def test_two_batches_with_the_same_query_are_not_the_same_request(monkeypatch):
    """**同一个词、同一个范围、同样多的文件，只要不是同一批文件，就不是同一份请求。**

    这条以前是漏的：签名只有 `关键词|范围|条数`，而 `_find_task` 是在**这个项目+仓库**的
    最近 80 条任务里找（`_recent_tasks`），**完全不看批次也不看时间**。而 `entries` 会被
    `pairs[:allowance]` 截到上限 —— 所以「两个不同周版本 + 改动文件都超过上限 + 问同一个
    词」这三个条件凑齐时，两边的条数都是那个上限，签名一模一样：

        本周的检索 → 直接命中上周那条已完成的任务 → 返回**上周的命中清单**
        （路径、行号、那一行的原文），而本周新出现的引用一个都搜不到。
        额度也不扣（根本没派发），从界面上完全看不出来。

    这就是「静默给出过期证据」：模型会把上周的行号和原文写进本周的报告，而报告里那句
    「本次搜索覆盖了…」照样成立。

    判据两条：不同批次必须算出不同的签名；**同一批**文件的同一份检索必须还能复用
    （否则每次分析都要为同一个词多等一轮 40 秒）。
    """
    import services.agent_file_content_dispatch as dispatch  # noqa: PLC0415
    from services.agent_file_content_dispatch import request_references  # noqa: PLC0415

    signatures: list = []

    def fake_request_from_agent(**kwargs):
        signatures.append(kwargs["lines"])
        return {"status": "unavailable", "message": "（这条用例只关心请求长什么样）"}

    monkeypatch.setattr(dispatch, "_request_from_agent", fake_request_from_agent)
    repository = SimpleNamespace(id=3, project_id=9)

    # 两周各自都是 240 条（都被 `pairs[:allowance]` 截到上限），文件名不同。
    week1 = [[f"scripts/w1_{index}.lua", COMMIT_A] for index in range(240)]
    week2 = [[f"scripts/w2_{index}.lua", COMMIT_B] for index in range(240)]

    request_references(repository, query="target_id", entries=week1, prefix="scripts/")
    request_references(repository, query="target_id", entries=week2, prefix="scripts/")

    assert len(signatures) == 2
    assert signatures[0] != signatures[1], (
        "两周的检索签名一模一样（都只有「关键词|范围|条数」）—— "
        "本周会命中上周那条已完成的任务，把上周的命中清单当成本周的结果交出去"
    )

    request_references(repository, query="target_id", entries=week1, prefix="scripts/")
    assert signatures[2] == signatures[0], (
        "同一批文件的同一份检索算出了不同的签名 —— 复用失效，"
        "模型对同一个词问两次就要多等一轮 40 秒"
    )

    # 同一批**截完的**文件、同一个词，只有本批次总数不一样：这仍然不是同一份请求。
    # 命中清单确实会一样（搜的是同样那 240 个文件），但抬头那句覆盖率不一样 ——
    # 「上周一共有 767 个文件」的那份结果被本周（一共 300 个）复用，本周的报告里就会写着
    # 240/767，而模型正是拿这个数决定能不能说「本批次的文件里没有别处引用」。
    request_references(
        repository, query="target_id", entries=week1, prefix="scripts/", total_files=767
    )
    request_references(
        repository, query="target_id", entries=week1, prefix="scripts/", total_files=300
    )
    assert signatures[3] != signatures[4], (
        "只换了本批次总数，签名却一样 —— 覆盖率会被从上一批带过来"
    )


# ==========================================================================
# 九、测试桩的守卫（与 `file_content` 那条同一条纪律）
# ==========================================================================


class TestEveryProviderStubKnowsTheNewDoor:
    """协议加了第五个方法时，**测试里的每一个桩都要跟着加** —— 这是踩过的坑。

    桩少了这个方法不会报错，只在真的索取 `find_references` 时抛 `TypeError`，而取数失败
    是被接住的：表现不是红，是**用例静默退化**成「这一轮什么都没拿到」。
    """

    def _stubs(self):
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        offenders = []
        seen = 0
        for path in sorted((root / "tests").glob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            names = {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            if "read_reference" not in names:
                continue
            seen += 1
            if "find_references" not in names:
                offenders.append(path.name)
        return offenders, seen

    def test_no_stub_is_missing_the_search_method(self):
        offenders, seen = self._stubs()

        assert seen >= 3, f"一个 provider 桩都没扫到（seen={seen}），这条守卫是空的"
        assert not offenders, f"这些文件的 provider 桩少了 find_references：{offenders}"


# ==========================================================================
# 十、索引 == 穷举搜索（E2 的**唯一**正确性判据）
# ==========================================================================
#
# 生产路径已经切到 `SnapshotReferenceIndex`，而 `search_files` 是它替换掉的那份**逐行
# 子串匹配**。两份实现只要有一处不等价，模型拿到的「还有谁在用这个标识符」就是**错的**
# ——不是「不完整」，是「说没有、其实有」，它会据此写下一条错误结论。
#
# 所以这一节把两份实现放在同一份语料上跑，对一组**边界查询**逐条比对命中
# （路径, 行号, 那一行的内容）。`search_files` 从此只在这条用例里活着（穷举基准/oracle）。

# 语料刻意凑齐「同一个关键词的不同写法」：整 token、更长 token 里的前缀/后缀/复数、
# 大小写、点号（非 token）、中文、数字 token、数字开头后接字母。旧索引口径只查
# 「整 token 相等」的桶，于是 query `target_id` 会漏掉 `target_id_extra` 那一行 ——
# **同一个仓库里只要有任何一处把标识符当独立词用过，桶就存在，其它形式就全被漏掉**。
_EQUIV_ACTIVITY = "\n".join([
    "local target_id = 1",           # 1  整 token
    "local target_id_extra = 2",     # 2  query 是**更长** token 的前缀（旧口径漏的就是这一行）
    "local x_target_id = 3",         # 3  后缀变体
    "local target_ids = 4",          # 4  复数变体
    "print(item.target_id)",         # 5  非 token（含点）→ 走逐行兜底
    "local Target_ID = 5",           # 6  大小写
    "-- 攻击力 就是 target_id",      # 7  中文与标识符混排
    "local ver = 12345",             # 8  数字 token（`\\d{3,}`）
    "local y = 12abc",               # 9  数字开头后接字母（`2abc` 只可能是子串，不可能是 token）
    "local unrelated = 9",           # 10 任何 query 都不该命中
]) + "\n"

_EQUIV_PROTO = "\n".join([
    "TARGET_ID = 100",               # 1  全大写
    "local target = 1",              # 2  query `target` 的整 token 形态
    "local targetable = 2",          # 3  query `target` 的更长 token 形态
    "self.target_id = nil",          # 4
]) + "\n"

# 非 ASCII 编码：GBK 的 lua。判成文本的判据是 `looks_binary`（魔数/NUL），不是「按 utf-8
# 解不解得出来」—— 后者会把这份文件判成二进制、一个词都搜不到。
_EQUIV_GBK = "local 攻击力 = 1\n属性 = 攻击力\nlocal target_id = 7\n".encode("gbk")

# 边界查询：整 token / token 子串 / `_` 分段 / 非 token（点号）/ 中文 / 数字 token /
# 数字子串 / 大小写。**每一个都要两种实现给出逐条相同的命中**。
_EQUIV_QUERIES = (
    "target_id",
    "TARGET_ID",
    "target",
    "_id",
    "item.id",
    "abc",
    "2abc",
    "123",
    "12",
    "攻击力",
)


def _equiv_corpus():
    files = {
        ("scripts/optional/activity.lua", COMMIT_A): _EQUIV_ACTIVITY,
        ("scripts/net/proto.lua", COMMIT_A): _EQUIV_PROTO,
        ("config/goods.lua", COMMIT_A): _EQUIV_GBK,
    }
    return files, [(path, commit) for path, commit in files]


def _tuples(hits):
    return [(hit.path, hit.line, hit.text) for hit in hits]


def test_index_hits_equal_exhaustive_search():
    """索引的命中集合必须与穷举搜索**逐条相等**（E2「与穷举一致」的唯一凭据）。

    这条用例之前不存在 —— 生产路径换成了索引，而索引与它替换掉的 `search_text`/`search_files`
    只有一份实现被单独测过，没有任何一条用例把两者放在同一份语料上比。于是那个召回缺口
    （只查整 token 的桶 → `target_id_extra` 这类行全漏）在测试里是隐形的。
    """
    from services.ai.reference_index import SnapshotReferenceIndex

    files, pairs = _equiv_corpus()

    def reader(path, commit):
        return files.get((path, commit))

    index = SnapshotReferenceIndex.build(pairs, reader=reader)
    mismatches = []
    for query in _EQUIV_QUERIES:
        oracle = _tuples(search_files(pairs, query, reader=reader).hits)
        indexed = _tuples(index.search(query).hits)
        if indexed != oracle:
            mismatches.append(
                f"  query={query!r}\n"
                f"    索引  ：{indexed}\n"
                f"    穷举  ：{oracle}"
            )
    assert not mismatches, "索引与穷举搜索的命中不一致：\n" + "\n".join(mismatches)


def test_the_recall_gap_is_named():
    """把缺口本身钉住（上一条是判据，这一条是机理）。

    `target_id_extra` / `x_target_id` / `target_ids` 这三行里，query `target_id` 出现在
    **更长 token 的内部**。旧索引 `hits_by_token.get("target_id")` 只拿到整 token 相等的那
    几行，这三行一个都不在里面，而抬头照样写「本次搜索覆盖了 3/3 个文件」——
    「没搜到」被当成了「不存在」。
    """
    from services.ai.reference_index import SnapshotReferenceIndex

    files, pairs = _equiv_corpus()
    index = SnapshotReferenceIndex.build(pairs, reader=lambda path, commit: files.get((path, commit)))

    lines = {hit.line for hit in index.search("target_id").hits if hit.path.endswith("activity.lua")}

    assert {2, 3, 4} <= lines, (
        "「更长 token 里包含 query」的三行一个都不能少：漏掉它等于告诉模型「这里没有引用」"
    )
    assert lines == {1, 2, 3, 4, 5, 6, 7}, "含 `target_id` 的每一行都要在命中里"


def test_the_oracle_and_the_index_agree_on_a_non_ascii_corpus():
    """非 ASCII 编码那一档单独再钉一次：GBK 的 lua 里中文标识符要能被两种实现同样搜到。"""
    files, pairs = _equiv_corpus()

    def reader(path, commit):
        return files.get((path, commit))

    from services.ai.reference_index import SnapshotReferenceIndex

    oracle = _tuples(search_files(pairs, "攻击力", reader=reader).hits)
    indexed = _tuples(
        SnapshotReferenceIndex.build(pairs, reader=reader).search("攻击力").hits
    )

    assert oracle and indexed == oracle


def test_the_index_decodes_like_the_shared_implementation():
    """索引的严格解码必须是共用那份实现的**前两档**，而且顺序一致。

    顺序本身就是口径的一部分（utf-8 优先、中文项目 GBK 第二）：把 gbk 提到前面，一份两边
    都能解的字节序列就会解出不同的文本 —— 同一个文件在「索引」与「正文」两条路上变成两种
    内容，而两边都不会报错。
    """
    from services.ai.reference_index import INDEX_ENCODINGS
    from utils.text_decoding import TEXT_ENCODINGS

    assert INDEX_ENCODINGS == tuple(TEXT_ENCODINGS[:2])


# ==========================================================================
# 十一、前缀只筛「这次查询」，不筛「索引」
# ==========================================================================


def _scope_across_prefixes() -> AnalysisScope:
    return AnalysisScope.from_iterables(
        commits=(COMMIT_A, COMMIT_B),
        paths_by_commit={
            COMMIT_A: ["config/goods.lua"],
            COMMIT_B: ["scripts/optional/activity.lua", "scripts/net/proto.lua"],
        },
        readable_references=[],
    )


def test_the_second_query_with_a_different_prefix_still_sees_the_whole_batch(monkeypatch):
    """**索引按快照建，前缀只在 `search()` 那一刻筛。**

    这里曾经有过一次污染：`find_references` 先按 `prefix` 过滤 `batch_paths()`，再把那份
    **已过滤**的列表交给 `_search_local` 建索引，而索引被永久缓存在 `self._reference_index`
    上。于是「先问 `scripts/`、再问全局」的第二次查询**只能看见 `scripts/` 下的文件** ——
    `files_total` / `scanned` / 命中全是那个子集的，而模型完全看不出这是上一次查询的残留：
    它只会读到「本次搜索覆盖了 2/2 个文件」，然后写下「本批次里没有别处引用」。
    """
    import services.vcs_content_service as vcs
    from services.ai import platform_provider as pp

    files = {
        ("config/goods.lua", COMMIT_A): "local target_id = 1\n",
        ("scripts/optional/activity.lua", COMMIT_B): "print(target_id)\n",
        ("scripts/net/proto.lua", COMMIT_B): "local target_id = 2\n",
    }
    reads: list[str] = []

    def reader(repo, commit, path):
        reads.append(path)
        return files.get((path, commit))

    monkeypatch.setattr(vcs, "get_file_content_from_git", reader)
    monkeypatch.setattr(pp, "is_agent_dispatch_mode", lambda: False)
    # 部署模式在**两个**模块里各被问一次：`_diff_from_agent` / `_content_from_agent`
    # 住在 `platform_provider`，而 `find_references` 那一段（2026-09-24 起）住在
    # `provider_search` —— 只打一处的话，另一条路会照旧按真实部署模式走。
    monkeypatch.setattr(agent_mode_module, "is_agent_dispatch_mode", lambda: False)
    provider = _provider(_scope_across_prefixes())
    monkeypatch.setattr(provider, "_repository_of", lambda pairs: SimpleNamespace(id=1))

    scoped = provider.find_references("target_id", "scripts/")

    assert "2/2" in scoped, f"第一个范围下就是 2 个文件：{scoped}"
    assert "config/goods.lua" not in scoped

    whole = provider.find_references("target_id")

    assert "3/3" in whole, f"第二次查询（无前缀）必须看到整批 3 个文件：{whole}"
    assert "config/goods.lua:1: local target_id = 1" in whole, (
        "第一个前缀的范围把配置文件从索引里挤掉了 —— 这一条命中再也拿不回来"
    )
    assert reads == ["config/goods.lua", "scripts/net/proto.lua", "scripts/optional/activity.lua"], (
        f"整批只该读一次，且读的是**整批**（前缀不参与建索引）：读了 {reads}"
    )


def test_the_index_belongs_to_exactly_one_snapshot(monkeypatch):
    """索引与它服务的那份批次是**绑定**的：换了批次就重建，绝不复用。

    `PlatformContextProvider` 通常一次分析一个实例（批次在一次分析里是固定的），所以这道
    校验平时不会触发。留着它是因为「索引属于哪一份快照」必须是代码里看得见的事实：
    `self._reference_index` 一旦被同一实例的**另一份**批次复用，命中清单、`files_total`
    （覆盖率的分母）与 `scanned` 就全是从上一份批次带过来的 —— 而界面上完全看不出来，
    模型只会读到一句「本次搜索覆盖了 N/N 个文件」。
    """
    import services.vcs_content_service as vcs
    from services.ai import platform_provider as pp

    files = {
        ("a.lua", COMMIT_A): "target_id = 1\n",
        ("b.lua", COMMIT_A): "other_id = 2\n",
    }
    reads: list[str] = []

    def reader(repo, commit, path):
        reads.append(path)
        return files.get((path, commit))

    monkeypatch.setattr(vcs, "get_file_content_from_git", reader)
    monkeypatch.setattr(pp, "is_agent_dispatch_mode", lambda: False)
    # 部署模式在**两个**模块里各被问一次：`_diff_from_agent` / `_content_from_agent`
    # 住在 `platform_provider`，而 `find_references` 那一段（2026-09-24 起）住在
    # `provider_search` —— 只打一处的话，另一条路会照旧按真实部署模式走。
    monkeypatch.setattr(agent_mode_module, "is_agent_dispatch_mode", lambda: False)
    provider = _provider(_scope())
    monkeypatch.setattr(provider, "_repository_of", lambda pairs: SimpleNamespace(id=1))

    first = provider._search_local([("a.lua", COMMIT_A)], "target_id", prefix="")
    second = provider._search_local([("b.lua", COMMIT_A)], "other_id", prefix="")

    assert "1/1" in first and "a.lua:1: target_id = 1" in first
    assert "1/1" in second and "b.lua:1: other_id = 2" in second, (
        "第二次是另一份批次，索引没重建 —— 它拿第一份的快照搜了，覆盖率的分母也是错的"
    )
    assert reads == ["a.lua", "b.lua"], (
        f"第二份批次必须重新读（读的只是它自己那几个）：读了 {reads}"
    )


# ==========================================================================
# 十二、分页游标
# ==========================================================================


def test_a_cursor_returns_the_next_page():
    """命中被上限截断之后，取下一页的途径必须存在。

    `search()` 原来硬截 `candidates[:max_hits]`，而 `MAX_HITS = 80` —— 一次周版本里一个
    公共字段的命中常常不止 80 处。截掉之后**没有任何途径**取回剩下的：抬头只有一句
    「命中数到了上限」，模型只能把它当成「就这些」。这里同时钉住 `matched`（截断**之前**
    的真实命中数）：少了它，「命中 10 处」在读的人眼里就是全部。
    """
    from services.ai.reference_index import SnapshotReferenceIndex

    body = "\n".join(f"local target_id = {index}" for index in range(25)) + "\n"
    files = {("a.lua", COMMIT_A): body}
    pairs = [("a.lua", COMMIT_A)]

    def reader(path, commit):
        return files.get((path, commit))

    index = SnapshotReferenceIndex.build(pairs, reader=reader)

    first = index.search("target_id", max_hits=10)

    assert len(first.hits) == 10
    assert first.matched == 25, "截断之前的真实命中数必须报出来"
    assert first.truncated_hits is True and first.next_cursor == 10

    second = index.search("target_id", max_hits=10, cursor=first.next_cursor)
    third = index.search("target_id", max_hits=10, cursor=second.next_cursor)

    assert [hit.line for hit in second.hits] == list(range(11, 21))
    assert [hit.line for hit in third.hits] == list(range(21, 26))
    assert third.next_cursor is None and third.truncated_hits is False, "最后一页要说明列完了"

    paged = _tuples(first.hits + second.hits + third.hits)
    oracle = _tuples(search_files(pairs, "target_id", reader=reader, max_hits=100).hits)

    assert paged == oracle, "一页一页拼起来必须与穷举搜索逐条相等（不多一条、不少一条）"

    beyond = index.search("target_id", max_hits=10, cursor=999)

    assert beyond.hits == () and beyond.next_cursor is None, "越界游标给空页，不许回卷"
    assert beyond.matched == 25

    header = render_result(first)

    assert "共 25 处" in header, f"抬头要说明这只是第一页：{header}"
    assert "命中数到了上限" in header


def test_the_two_result_types_carry_the_same_account():
    """同一个 `render_result` 渲染两条路（本地索引 / Agent 索引）的结果 —— 少一个**账**字段，
    那条路的抬头就少一句话，而两条部署路径的文字分叉正是「同一个周版本给出两种覆盖率」的成因。

    唯一允许只有索引那条路有的，是**索引的身份**（`index_version` / `snapshot_digest`）：
    它不进 `render_result` 的判断，只写进抬头末尾那句「索引版本 …」。
    """
    import dataclasses

    from services.ai.reference_index import IndexedSearchResult
    from services.ai.reference_search import SearchResult

    account = (
        "query", "hits", "files_total", "scanned", "missing", "binary",
        "truncated_files", "truncated_hits", "prefix", "matched",
        "oversized", "undecodable", "unindexed", "cursor", "next_cursor",
    )
    indexed = {field.name for field in dataclasses.fields(IndexedSearchResult)}
    plain = {field.name for field in dataclasses.fields(SearchResult)}

    for name in account:
        assert name in indexed, f"索引那条路少了 `{name}`"
        assert name in plain, f"基准那条路少了 `{name}` —— `render_result` 会 AttributeError"
    assert indexed - plain == {"index_version", "snapshot_digest"}
    assert plain - indexed == set()


# ==========================================================================
# 十三、缺口：太大 / 解不出文本 / 预热门槛之外
# ==========================================================================


def test_oversized_and_undecodable_files_are_counted_as_gaps():
    """三种「读了但没索引」的文件都要**有名字、有计数**，不能静默消失。

    * 太大（`MAX_INDEX_FILE_BYTES` 之上）：索引它只会把首次查询拖死；
    * 解不出文本：既不是 utf-8 也不是 gbk。这里**刻意不共用** `decode_text_bytes` 的宽松
      路径 —— `latin-1` 能解任意字节，用它就永远判不出这一档，一份乱码会被算成「搜过了」，
      而中文标识符在乱码里一个都匹配不上：又一处「没搜到」被写成「不存在」；
    * 预热门槛之外：见下一条用例。
    """
    from services.ai.reference_index import MAX_INDEX_FILE_BYTES, SnapshotReferenceIndex

    big = "target_id = 1\n" * (MAX_INDEX_FILE_BYTES // 14 + 10)
    files = {
        ("a.lua", COMMIT_A): "local target_id = 1\n",
        ("latin.lua", COMMIT_A): b"\xff\xfe target_id \x81\x8d",
        ("big.lua", COMMIT_A): big,
        ("bin.xlsx", COMMIT_A): b"PK\x03\x04target_id",
        ("gone.lua", COMMIT_A): None,
    }
    pairs = list(files)

    def reader(path, commit):
        return files.get((path, commit))

    result = SnapshotReferenceIndex.build(pairs, reader=reader).search("target_id")

    assert result.files_total == 5
    assert result.scanned == 1, "只有 a.lua 真的进了索引"
    assert (result.oversized, result.undecodable, result.binary, result.missing) == (1, 1, 1, 1)
    assert [hit.path for hit in result.hits] == ["a.lua"], "没索引的文件不许出现在命中里"

    note = render_result(result)

    assert "1 个太大" in note and "1 个解不出文本" in note
    assert "1 个不是文本" in note and "1 个读不到" in note
    assert "1/5" in note and "不代表整批里没有" in note

    # 与穷举基准的**有意不同**：基准用宽松解码（任何字节都出文本），所以它在「解不出文本」
    # 的那份内容里照样搜得到 ASCII 的 target_id。差别被记成了缺口（上面那几行），不是被抹平。
    oracle = search_files(pairs, "target_id", reader=reader, max_hits=60)

    assert oracle.scanned == 3, "基准把太大与解不出文本的那两份都当文本搜了"
    assert ("latin.lua", 1) in [(hit.path, hit.line) for hit in oracle.hits]


def test_the_first_query_indexes_only_a_bounded_prefix_and_says_so(monkeypatch):
    """首次查询的成本有上限，而**没索引的那部分必须被说出来**。

    767 个文件逐个 `git show` 是几十秒，而 Agent 侧等检索回来只有 40 秒
    （`REFERENCES_WAIT_SECONDS`）—— 索引一次读完整批会让第一次查询直接落成 `pending`
    （Run 22 实测）。所以 `build()` 只预热门槛（`MAX_INDEX_FILES`）内的前 N 个。
    剩下的走 `unindexed`：抬头写「文件数到了上限就停了，剩下的没搜」，
    否则模型会把「只看了前 N 个」当成「本批次里没有别处引用」。
    """
    import services.vcs_content_service as vcs
    from services.ai import platform_provider as pp
    from services.ai.reference_index import MAX_INDEX_FILES

    batch = [f"scripts/f{index:04d}.lua" for index in range(MAX_INDEX_FILES + 30)]
    scope = AnalysisScope.from_iterables(
        commits=(COMMIT_A,), paths_by_commit={COMMIT_A: batch}, readable_references=[]
    )
    reads: list[str] = []

    def reader(repo, commit, path):
        reads.append(path)
        return "local target_id = 1\n"

    monkeypatch.setattr(vcs, "get_file_content_from_git", reader)
    monkeypatch.setattr(pp, "is_agent_dispatch_mode", lambda: False)
    # 部署模式在**两个**模块里各被问一次：`_diff_from_agent` / `_content_from_agent`
    # 住在 `platform_provider`，而 `find_references` 那一段（2026-09-24 起）住在
    # `provider_search` —— 只打一处的话，另一条路会照旧按真实部署模式走。
    monkeypatch.setattr(agent_mode_module, "is_agent_dispatch_mode", lambda: False)
    provider = _provider(scope)
    monkeypatch.setattr(provider, "_repository_of", lambda pairs: SimpleNamespace(id=1))

    text = provider.find_references("target_id")

    assert len(reads) == MAX_INDEX_FILES, f"首次查询只该读预热门槛那么多：读了 {len(reads)}"
    assert f"{MAX_INDEX_FILES}/{len(batch)}" in text
    assert "30 个还没索引" in text, f"门槛之外的那些必须如实报出来：{text}"
    assert "文件数到了上限就停了" in text and "不代表整批里没有" in text

    before = len(reads)
    other_range = provider.find_references("target_id", "scripts/")

    assert len(reads) == before, "同一个快照的第二次查询又读了 blob —— 索引没被复用"
    assert f"{MAX_INDEX_FILES}/{len(batch)}" in other_range

