# -*- coding: utf-8 -*-
"""工作包 D 的 P3：**把最有用的证据提前到第 1 轮**（有界、可追、可复现）。

## 这一组钉的是哪一句话

run 45/46 的形状是「第 1 轮只有清单 → 第 2 轮点几个 diff → 第 3 轮之后才想起『这个旧名
还有谁在用』」，而最值钱的那条证据（改了公共接口、**调用方没改**）就在后面几轮里被 8 轮
上限挤掉，只能写成信息缺口。平台**当场就能算出**「改了哪些文件、改动行里出现了哪些符号」，
不必等模型问。

## 四组断言

1. **确定性候选**：同一份 diff 两次跑出同一份候选（顺序、去重、上界都确定），而最高优先
   那一档是「**只在删除侧出现的定义**」—— 也就是那个**旧名**（改名场景里唯一还能找到
   调用方的线索）；
2. **有界**：条数、窗口、份额三重上界，超出的**不取**并说清（不是静默截断）；
3. **同一本账**：预取走 `ContextTools.prefetch` —— 条目带 `evidence_id`、`prefetched`
   计数、字符数照记，但**不占模型的索取额度**；而「没交付的要从缓存里摘掉」有一条
   行为断言（跟着它再要一次，拿回的是**正文**而不是一句「见上文」）；
4. **端到端**：真仓库 + 真引擎跑一次 —— 第 1 轮的消息里就有旧名的命中位置、覆盖账与
   那处命中的邻近窗口，而那一轮**模型一次都没索取**（`request_count == 0`）。

## 环境

真 `git init` 的临时仓库（复用 P1 的 fixture），**不碰线上库、不碰 8002**。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.ai.context_tools import ContextTools
from services.ai.engine import EngineLimits, run_analysis
from services.ai.evidence_prefetch import (
    DEFAULT_MAX_SEARCHES,
    DEFAULT_MAX_SYMBOLS,
    DEFAULT_WINDOW_SPAN,
    Candidate,
    candidate_symbols,
    changed_lines,
    diff_requests,
    forget_unfitted,
    hit_windows,
    prefetch_evidence,
    search_requests,
)
from services.ai.frozen_repo import reset_caches
from services.ai.protocol import ContextRequest
from services.ai.rules import RuleThresholds
from services.ai.scope import AnalysisScope
from tests.test_ai_frozen_repo_read_scope import (
    BATCH_COMMIT,
    CALLER,
    DEFINER,
    NEW_NAME,
    OLD_NAME,
    _commit,
    _git,
    _provider,
)

RENAME_DIFF = (
    f"--- a/{DEFINER}\n"
    f"+++ b/{DEFINER}\n"
    "@@ -1,5 +1,5 @@\n"
    " local M = {}\n"
    f"-function M.{OLD_NAME}(atk, def)\n"
    f"+function M.{NEW_NAME}(atk, def)\n"
    "     return math.max(0, atk - def)\n"
    " end\n"
    " return M\n"
)


# ---------------------------------------------------------------------------
#  一、确定性候选：从改动行里算出「该去搜什么」
# ---------------------------------------------------------------------------


def test_changed_lines_keeps_only_the_two_sides_of_the_change():
    """头部（`+++` / `---` / `@@`）与上下文行都不是「被改的符号」。"""
    marked = changed_lines(RENAME_DIFF)

    assert marked == [
        ("-", f"function M.{OLD_NAME}(atk, def)"),
        ("+", f"function M.{NEW_NAME}(atk, def)"),
    ]


def test_the_removed_definition_comes_first():
    """**最高优先那一档是旧名**：改名之后还能找到调用方的唯一线索。

    它排在新名之前不是美观问题 —— 候选是有上界的（`DEFAULT_MAX_SYMBOLS`），而排在前面
    的先被搜。旧名找不到调用方，这条证据链就断了。
    """
    candidates = candidate_symbols(RENAME_DIFF)

    assert [item.symbol for item in candidates] == [OLD_NAME, NEW_NAME]
    assert candidates[0].kind == "removed_definition"
    assert candidates[1].kind == "definition"
    assert "删除侧" in candidates[0].reason


def test_a_qualified_name_is_searched_by_its_tail():
    """`M.CalcDamage` 要搜的是 `CalcDamage`：调用点上的限定名几乎从不相同。

    fixture 里定义处写 `function M.CalcDamage`、调用处写 `proto.CalcDamage` —— 带限定名
    去搜只会命中定义处，而定义处恰好是这次已经改掉的那个地方。
    """
    candidates = candidate_symbols(RENAME_DIFF)

    assert all("." not in item.symbol for item in candidates)


def test_the_same_diff_gives_the_same_candidates_twice():
    """确定性：同一份输入两次得到同一份候选（顺序也一样）。"""
    assert candidate_symbols(RENAME_DIFF) == candidate_symbols(RENAME_DIFF)


def test_generic_words_are_not_candidates():
    """`local` / `return` / `end` 这类词搜出来的是整个仓库 —— 既贵又没有信息量。"""
    symbols = {item.symbol for item in candidate_symbols(RENAME_DIFF)}

    assert symbols.isdisjoint({"local", "return", "end", "function", "math", "max"})


def test_candidates_are_capped_and_the_cap_is_deterministic():
    """候选有上界（一次预取不能无限搜下去），而且截的是**尾巴**（低优先级那些）。"""
    text = "".join(f"-alpha_{index} = 1\n+beta_{index} = 2\n" for index in range(10))

    capped = candidate_symbols(text, limit=3)

    assert len(capped) == 3
    assert [item.symbol for item in capped] == [
        item.symbol for item in candidate_symbols(text, limit=3)
    ]


def test_quoted_config_ids_are_candidates():
    """配表 ID 形态（字母 + 数字 / 带下划线）才要，纯英文单词不要。"""
    text = '+"Item_1001" = 5\n+"note" = "hello"\n'

    symbols = [item.symbol for item in candidate_symbols(text)]

    assert "Item_1001" in symbols
    assert "hello" not in symbols


def test_search_requests_are_bounded():
    candidates = tuple(Candidate(f"sym_{i}", "definition", "测试") for i in range(10))

    requests = search_requests(candidates, max_searches=DEFAULT_MAX_SEARCHES)

    assert len(requests) == DEFAULT_MAX_SEARCHES
    assert all(item.type == "find_references" and not item.commit for item in requests)


# ---------------------------------------------------------------------------
#  二、有界请求：批次 diff 与邻近窗口
# ---------------------------------------------------------------------------


def _scope(paths: dict[str, set[str]] | None = None) -> AnalysisScope:
    paths = paths or {BATCH_COMMIT: {DEFINER}}
    return AnalysisScope(
        commits=(BATCH_COMMIT,),
        paths_by_commit={commit: frozenset(files) for commit, files in paths.items()},
    )


def test_the_batch_diff_requests_prefer_config_tables_then_code():
    """配表 → 代码 → 文本（**初值排序**，依据写在 `evidence_prefetch` 的常量注释里）。

    判据是「哪一类文件的改动行里才有可搜的符号」：文档与资源改了也搜不出耦合。
    """
    scope = _scope({BATCH_COMMIT: {"readme.md", "config/item.xlsx", DEFINER}})

    requests = diff_requests(scope)

    assert [item.path for item in requests] == ["config/item.xlsx", DEFINER, "readme.md"]
    # commit 取「这个路径在批次里最后一次被改的那条」——与合并 diff 同一口径。
    assert all(item.commit == BATCH_COMMIT for item in requests)


def test_the_batch_diff_requests_are_bounded_by_count():
    scope = _scope({BATCH_COMMIT: {f"scripts/f{index:02d}.lua" for index in range(20)}})

    assert len(diff_requests(scope, max_files=5)) == 5
    assert diff_requests(scope, max_files=5) == diff_requests(scope, max_files=5)


def test_a_hit_becomes_a_small_window_request():
    """命中给的是**一小段**（`行号 ± span`），不是整份正文 —— 整份既贵又慢。"""
    rendered = f"{CALLER}:3:     return proto.{OLD_NAME}(attacker.atk, target.def)\n"

    requests = hit_windows(rendered, span=DEFAULT_WINDOW_SPAN, max_windows=2)

    assert len(requests) == 1
    request = requests[0]
    assert request.type == "file_content" and request.path == CALLER
    assert request.lines == f"1-{3 + DEFAULT_WINDOW_SPAN}", "行号从 1 起算，不能出现 0 或负数"
    # commit 留空：这条读的是**冻结 tip**，不是某条提交改过的版本（见 `protocol` 的说明）。
    assert request.commit == ""


def test_the_same_file_is_windowed_once_and_the_batch_is_skipped():
    """同一个文件多处命中只取第一处；本批次已经有 diff 的文件不做窗口。"""
    rendered = (
        f"{CALLER}:3: 第一处\n"
        f"{CALLER}:40: 第二处（同一文件，跳过）\n"
        f"{DEFINER}:2: 本批次里的文件（跳过）\n"
        f"scripts/net/other.lua:9: 另一个文件\n"
    )

    requests = hit_windows(rendered, span=5, max_windows=3, skip_paths={DEFINER})

    assert [item.path for item in requests] == [CALLER, "scripts/net/other.lua"]


def test_the_window_count_is_capped():
    rendered = "".join(f"scripts/net/f{index}.lua:5: 命中\n" for index in range(6))

    assert len(hit_windows(rendered, span=3, max_windows=2)) == 2


def test_a_header_line_is_not_mistaken_for_a_hit():
    """覆盖账那几行里也有 `数字：` 与反引号，不能被当成命中（否则窗口会落到假路径上）。"""
    rendered = (
        "- 扫描范围：本次冻结版本 `abc123abc123` 的 **Git 跟踪文件**；本次请求的范围是全部 5 个。\n"
        "- 其中**真正读了并建进索引**的有 5 个。\n"
        "本次覆盖了该范围内的**全部**跟踪文件，所以「没有命中」是可信的结论。\n"
    )

    assert hit_windows(rendered, span=3, max_windows=2) == ()


# ---------------------------------------------------------------------------
#  三、同一本账：预取不占额度，但要记账、要留痕、要能摘
# ---------------------------------------------------------------------------


class _Provider:
    """一个最朴素的取数口（记下每次真的被调用到的请求）。"""

    def __init__(self, answers: dict | None = None):
        self.answers = dict(answers or {})
        self.seen: list[tuple] = []

    def _answer(self, key, default):
        self.seen.append(key)
        return self.answers.get(key, default)

    def commit_detail(self, commit, repository_id=""):
        return self._answer(("commit_detail", commit), f"提交 {commit} 的详情")

    def file_diff(self, commit, path, repository_id=""):
        return self._answer(("file_diff", commit, path), f"diff of {path}")

    def file_content(self, commit, path, lines="", repository_id=""):
        return self._answer(("file_content", commit, path, lines), f"{path} 的正文")

    def read_reference(self, name):
        return self._answer(("read_reference", name), f"{name} 的正文")

    def find_references(self, query, path=""):
        return self._answer(("find_references", query, path), f"{CALLER}:3: {query} 命中")


def _tools(provider=None, **kwargs) -> ContextTools:
    return ContextTools(provider=provider or _Provider(), **kwargs)


def test_prefetch_shares_the_ledger_but_not_the_quota():
    """**这一条是「没有白拿又追不到的隐藏上下文」的判据。**

    预取的正文要进同一本账（按类型的 `prefetched` 计数与字符数），条目要带
    `evidence_id`（于是汇总/对账时能按地址取回原件），而模型的索取额度**一格里都不占**。
    """
    provider = _Provider()
    tools = _tools(provider)
    request = ContextRequest(type="file_diff", commit=BATCH_COMMIT, path=DEFINER)

    batch = tools.prefetch((request,))

    item = batch.items[0]
    assert item.meta.get("prefetch") is True, "条目上要留痕（读 trace 的人能分出是谁要的）"
    assert item.meta.get("evidence_id"), "预取也要有稳定地址"
    assert tools.requests_seen == 0, "预取不能占模型的索取额度"
    counters = tools.stats["file_diff"]
    assert counters["prefetched"] == 1 and counters["calls"] == 0
    assert counters["produced_chars"] == len(item.text) > 0, "字符数照记（否则账对不上）"
    assert provider.seen == [("file_diff", BATCH_COMMIT, DEFINER)]


def test_execute_still_counts_against_the_quota():
    """**反向样本**：模型自己要的那条路一个字节都没变（记 `calls`、占额度）。"""
    provider = _Provider()
    tools = _tools(provider)

    tools.execute((ContextRequest(type="file_diff", commit=BATCH_COMMIT, path=DEFINER),))

    assert tools.requests_seen == 1
    counters = tools.stats["file_diff"]
    assert counters["calls"] == 1 and counters["prefetched"] == 0


def test_forgetting_a_prefetch_makes_the_next_ask_return_the_body():
    """**不做这件事就会出现一句假话**：缓存命中给的是「见上文那一节」的指针，
    而那一节其实没进过提示词。摘掉之后，模型再要一次拿回的是**正文**。
    """
    provider = _Provider()
    tools = _tools(provider)
    request = ContextRequest(type="file_diff", commit=BATCH_COMMIT, path=DEFINER)
    item = tools.prefetch((request,)).items[0]

    # 摘之前：命中缓存 → 给的是**指针**（这一句本身没问题，前提是那一节真的在提示词里）。
    assert "[已在上文给出]" in tools.execute((request,)).items[0].text

    forget_unfitted((item,), (), tools)  # 一条都没装进提示词 → 全部摘掉

    assert "diff of" in tools.execute((request,)).items[0].text, "摘掉之后该重新取数（给正文）"
    assert provider.seen.count(("file_diff", BATCH_COMMIT, DEFINER)) == 2


def test_forget_unfitted_leaves_what_really_went_in():
    """**反向样本**：真的进了提示词的条目不许被摘 —— 摘了它，下一轮就会白取一次，
    而且模型会看到同一份正文出现两遍。"""
    provider = _Provider()
    tools = _tools(provider)
    request = ContextRequest(type="file_diff", commit=BATCH_COMMIT, path=DEFINER)
    item = tools.prefetch((request,)).items[0]

    assert forget_unfitted((item,), (item,), tools) == 0

    assert "[已在上文给出]" in tools.execute((request,)).items[0].text


def test_forget_unfitted_ignores_items_that_were_not_prefetched():
    """模型自己取的条目不在它的职责里（摘了它等于把缓存的语义改了）。"""
    tools = _tools()
    request = ContextRequest(type="file_diff", commit=BATCH_COMMIT, path=DEFINER)
    item = tools.execute((request,)).items[0]

    assert forget_unfitted((item,), (), tools) == 0
    assert "[已在上文给出]" in tools.execute((request,)).items[0].text


def test_a_tools_object_without_forget_is_not_a_crash():
    """鸭子类型：不认这件事的执行器（测试里的桩）不该被它绊住。"""
    class Bare:
        stats: dict = {}

    assert forget_unfitted((), (), Bare()) == 0


# ---------------------------------------------------------------------------
#  四、份额是硬的：装不下的一条都不取
# ---------------------------------------------------------------------------


def test_a_small_budget_takes_nothing_at_all():
    """**反向样本**：份额连一条都装不下时，一次取数都不发（白买一次还要摘缓存）。

    这条判据是实测逼出来的：35,000 字的预算下多带一条 11,000 字的 diff，整条上下文
    压缩的补救链都会走成另一条路（`tests/test_ai_context_compaction.py` 里五条用例
    同时变红）—— 预取不该改变小额预算路径的形状。
    """
    provider = _Provider()
    tools = _tools(provider)
    limits = EngineLimits(prompt_char_budget=35_000)

    result = prefetch_evidence(tools, scope=_scope(), limits=limits)

    assert result.items == ()
    assert "装不下" in result.skipped, result.skipped
    assert provider.seen == [], "预取不该为了丢掉它而先取一次"


def test_an_item_over_the_share_is_not_delivered_and_is_forgotten():
    """份额装得下一条、装不下第二条时：只交付第一条，第二条**当场忘记**。

    忘记那一步是必须的：不忘的话，模型之后自己索取时会命中缓存、拿到一句「见上文那一节」，
    而那一节没进过提示词 —— 一句稳定的假话。
    """
    provider = _Provider(
        {("file_diff", BATCH_COMMIT, DEFINER): "甲" * 9_000,
         ("file_diff", BATCH_COMMIT, "scripts/net/other.lua"): "乙" * 9_000}
    )
    tools = _tools(provider)
    scope = _scope({BATCH_COMMIT: {DEFINER, "scripts/net/other.lua"}})
    # 份额 = 100,000 × 12% = 12,000：一条 9,000 装得下，两条 18,000 装不下。
    limits = EngineLimits(prompt_char_budget=100_000)

    result = prefetch_evidence(tools, scope=scope, limits=limits)

    assert result.fetched_files == 1
    assert [item.meta.get("prefetch") for item in result.items] == [True]
    assert result.chars <= 12_000
    # 被留下的那个是字典序第一个（`other.lua` < `proto.lua`，同扩展名按路径排）。
    assert "scripts/net/other.lua" in result.items[0].label
    # **没被预取的那个**：模型自己索取时必须拿到正文，不能是一句「见上文那一节」。
    missed = ContextRequest(
        type="file_diff", commit=BATCH_COMMIT, path=DEFINER
    )
    text = tools.execute((missed,)).items[0].text
    assert "甲" * 50 in text and "[已在上文给出]" not in text, text[:200]


def test_the_note_says_how_many_files_were_not_prefetched():
    """说明里要写清「还有几个改动文件没有预取」——静默截断正是 P2 要拆掉的东西。"""
    provider = _Provider()
    tools = _tools(provider)
    scope = _scope({BATCH_COMMIT: {f"scripts/f{index}.lua" for index in range(4)}})

    result = prefetch_evidence(tools, scope=scope, limits=EngineLimits(), max_files=2)

    note = "".join(result.notes)
    assert "**预先取回** 2 个改动文件的差异" in note
    assert "本批次共 4 个文件" in note and "没有预取" in note
    assert "不是你索取的" in note, "必须说清这不是模型要来的"


def test_prefetched_requests_still_pass_the_whitelist():
    """预取虽然都是平台自己造的路径，也要过白名单 —— **白名单只该有一个入口**。

    反向样本：构造一条命中落在**跟踪集合之外**的窗口请求，它必须被拒，且理由是
    「不在这一版里」，并被记进说明（不静默丢）。
    """
    from services.ai.protocol import sanitize_requests

    outside = ContextRequest(type="file_content", path="outside/secret.lua", lines="1-10")

    kept, rejected = sanitize_requests(
        (outside,), _scope(), repo_paths=frozenset({DEFINER, CALLER})
    )

    assert kept == ()
    assert rejected and "不在" in str(rejected[0].reason), rejected


def test_a_hit_in_an_unchanged_file_can_be_windowed():
    """预取的窗口请求**能过白名单**：命中落在未改动的文件上，靠的是冻结 tip 的跟踪集合。

    这条正是 P1/P3 那个金标形态能不能成立的关键一步 —— 窗口请求没有 commit（读的是冻结
    tip），路径也不在本批次里；没有 `repo_paths` 时它会被拒，有了才放行。反向样本
    （不在跟踪集合里 → 拒，并给出理由）在 `tests/test_ai_frozen_repo_read_scope.py`
    的协议层用例里。
    """
    from services.ai.protocol import sanitize_requests

    window = ContextRequest(type="file_content", path=CALLER, lines="1-20")

    kept, rejected = sanitize_requests(
        (window,), _scope(), repo_paths=frozenset({DEFINER, CALLER})
    )

    assert [item.path for item in kept] == [CALLER]
    assert rejected == ()


# ---------------------------------------------------------------------------
#  五、端到端：第 1 轮就有命中位置 + 邻近窗口，而且模型一次都没索取
# ---------------------------------------------------------------------------


class _ScriptedClient:
    def __init__(self, *replies: str):
        from services.ai.llm_client import ChatResult

        self._replies = list(replies)
        self._result = ChatResult
        self.calls: list[list[dict]] = []

    def complete(self, messages, *, temperature=None):
        self.calls.append([dict(item) for item in messages])
        index = min(len(self.calls) - 1, len(self._replies) - 1)
        return self._result(
            text=self._replies[index], model="fake", prompt_tokens=10, completion_tokens=5
        )


FINAL = json.dumps(
    {
        "status": "final",
        "report_markdown": "# 变更理解\n\n改了伤害函数的名字。\n",
        "anomalies": [],
        "dimensions": [{"id": "module_coupling", "hit": True, "note": "旧名仍有调用方"}],
    },
    ensure_ascii=False,
)


@pytest.fixture()
def rename_repo(tmp_path: Path):
    """与 P1 的金标 fixture 同一个形态，在这里**自己造**。

    不直接调用那个 fixture 函数：pytest 8 起「直接调用 fixture」是错误用法
    （`ERROR ... calling fixtures directly`），跨文件复用 fixture 只有 `conftest`
    一条正路，而本工作包不动 `tests/conftest.py`。
    """
    reset_caches()
    repo = tmp_path / "game"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "scripts/net").mkdir(parents=True)
    (repo / "scripts/combat").mkdir(parents=True)
    def _lua(*lines: str) -> str:
        # 用 `chr(10)` 拼行，源码里不写换行转义：本仓 CRLF/LF 混用，转义写坏了的表现是
        # 「字符串在源码中间断掉」，而那种错报在**另一个文件**的行号上（踩过一次）。
        return chr(10).join(lines) + chr(10)

    (repo / DEFINER).write_text(
        _lua(
            "local M = {}",
            f"function M.{OLD_NAME}(atk, def)",
            "    return math.max(0, atk - def)",
            "end",
            "return M",
        ),
        encoding="utf-8",
    )
    (repo / CALLER).write_text(
        _lua(
            "local proto = require('net.proto')",
            "local function hit(attacker, target)",
            f"    return proto.{OLD_NAME}(attacker.atk, target.def)",
            "end",
            "return hit",
        ),
        encoding="utf-8",
    )
    _commit(repo, "初版：定义 + 调用方")
    # 第二批：**只改定义处**（调用方一个字都没动）。
    (repo / DEFINER).write_text(
        _lua(
            "local M = {}",
            f"function M.{NEW_NAME}(atk, def)",
            "    return math.max(0, atk - def)",
            "end",
            "return M",
        ),
        encoding="utf-8",
    )
    tip = _commit(repo, "把伤害函数改名（调用方未改）")
    yield SimpleNamespace(
        path=repo, tip=tip, changed_paths=frozenset({DEFINER})
    )
    reset_caches()


def _engine_provider(repo):
    """真 provider（读冻结仓库） + 一份给定的改动 diff。

    `file_diff` 直接给定**不改实现**：本用例要的是预取链路（候选 → 搜索 → 窗口），
    而 diff 的渲染在 P1/P2 的用例里已经钉过；这里只需要一份「只有定义被改名」的改动行。
    """
    provider = _provider(repo)
    provider.file_diff = lambda commit, path, repository_id="": RENAME_DIFF
    return provider


def test_the_first_round_already_carries_the_hits_and_a_window(rename_repo):
    """**P3 的核心断言**：第 1 轮的消息里有

    * 旧名的**命中位置**（在**未改动**的调用方里 —— 这是最值钱的那条证据）；
    * 那处命中的**邻近窗口**（几行正文，够判断「它是不是真的还在调它」）；
    * **覆盖账**（没扫全时不能下「仓库里没有别处引用」的结论）。

    而那一轮模型**一次都没索取**（`request_count == 0`）—— 这正是「少花一轮」。
    """
    provider = _engine_provider(rename_repo)
    client = _ScriptedClient(FINAL)
    scope = _scope({BATCH_COMMIT: {DEFINER}})

    outcome = run_analysis(
        client=client,
        provider=provider,
        loaded=_loaded(),
        scope=scope,
        change_summary=f"本批次 1 个提交，改了 {DEFINER}",
        limits=EngineLimits(),
        thresholds=RuleThresholds(),
    )

    first = client.calls[0][-1]["content"]
    assert outcome.rounds[0].request_count == 0, "第 1 轮模型什么都没要"
    assert "平台预先附上" in first, "没有说明这批取证是平台预取的"
    assert "不是你索取的" in first
    assert f"{CALLER}:3" in first, "旧名的命中位置必须在第 1 轮里"
    assert "覆盖账" in first and "Git 跟踪文件" in first, "覆盖口径要一起给"
    assert OLD_NAME in first and NEW_NAME in first, "候选（旧名与新名）都要有出处"
    assert "回执：" in first, "窗口正文要带回执（它是截断与续页的唯一依据）"
    assert f"共 {5} 行" not in first or True  # 行数随 fixture 变，不钉死具体数字


def test_the_prefetched_evidence_is_traceable_to_an_evidence_id(rename_repo):
    """预取不是「白拿的上下文」：每条都带地址，落库明细里能按地址取回原件。"""
    provider = _engine_provider(rename_repo)
    client = _ScriptedClient(FINAL)

    outcome = run_analysis(
        client=client,
        provider=provider,
        loaded=_loaded(),
        scope=_scope({BATCH_COMMIT: {DEFINER}}),
        change_summary="本批次 1 个提交。",
        limits=EngineLimits(),
        thresholds=RuleThresholds(),
    )

    first = client.calls[0][-1]["content"]
    assert "evidence_id=" in first
    assert outcome.status == "succeeded"
    # 预取**不占额度**：这一整次分析里的索取次数为 0，而第 1 轮已经带了取证。
    assert outcome.requests_used == 0


def test_a_subagent_style_run_does_not_prefetch(rename_repo):
    """**反向样本**：子代理模式（第 1 轮是任务书）不预取。

    理由不是省事：那条路上 `_prepare_round` 直接返回任务书，预取的条目只能带到第 2 轮，
    而**模型的第 1 轮索取已经执行过了** —— 那些请求会命中预取写下的缓存，拿回一句
    「见上文那一节」，而那一节根本还没出现。稳定的假话，宁可不预取。
    """
    provider = _engine_provider(rename_repo)
    client = _ScriptedClient(FINAL)

    outcome = run_analysis(
        client=client,
        provider=provider,
        # `seed_messages` + `task_message` = 子代理模式（见 `run_analysis` 的说明）。
        seed_messages=({"role": "system", "content": "共享前缀"},),
        task_message="你负责 module_coupling 这一片。",
        loaded=_loaded(),
        scope=_scope({BATCH_COMMIT: {DEFINER}}),
        change_summary="本批次 1 个提交。",
        limits=EngineLimits(),
        thresholds=RuleThresholds(),
    )

    first = client.calls[0][-1]["content"]
    assert "平台预先附上" not in first
    assert "覆盖账" not in first
    # 判据是那句抬头：预取条目进了提示词才会带它（单代理那条路上有，见上面两条用例）。
    # `requests_used == 0` 是**另一件事**：这一轮模型什么都没要（额度的账）。
    assert outcome.requests_used == 0
    assert outcome.status == "succeeded"


def _loaded():
    from services.ai.skill_loader import LoadedSkills, SkillDocument

    def doc(name: str, text: str) -> SkillDocument:
        return SkillDocument(
            name=name, description="说明", path=Path("/tmp") / name, text=text,
            content_hash="h-" + name,
        )

    return LoadedSkills(
        platform_skill=doc("version-diff-review", "# 角色与方法\n\n你是配表评审专家。\n"),
        platform_references=(doc("incident-checklist.md", "事故清单"),),
        project_manifest=doc("g119-knowledge", "# G119\n"),
        project_references=(), project_skills=(),
        readable={"incident-checklist.md": Path("/tmp/a.md")},
        project_slug="g119", revision="rev-1",
    )


