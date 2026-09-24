# -*- coding: utf-8 -*-
"""工作包 D 的 P3：把**最有用的证据**提前到第 1 轮（有界、可追、可复现）。

## 它解决的问题（形态来自 run 45/46）

那两次分析的形状是：第 1 轮只有变更清单 → 第 2 轮点名拿几个 diff → 第 3 轮之后才想起
「这个旧名字还有谁在用」→ 于是最值钱的那条证据（改了公共接口、**调用方没改**）被推到
最后，常常在 8 轮上限里挤不进来，只能写成信息缺口。而「先搜一轮、再读正文一轮」这种
串行消耗，本质上不是模型笨，是**平台没把它已经知道的东西提前给它**：冻结批次之后，
「哪些文件改了」「改动行里出现了哪些符号」这两件事平台**当场就能算出来**，不必等模型问。

## 三条纪律

### 1. 有界：按「装得下多少」算，装不下的**不取**，并且说出来

四个上界（文件数 / 候选符号数 / 搜索数 / 每个窗口的行数）加一个字符份额
（`DEFAULT_CHAR_SHARE`，初值 12% 的提示词预算）。到界限就停，然后**在给模型的说明里
写清「还有几个改动文件没有预取」** —— 静默截断正是 P2 要拆掉的那个东西。

### 2. 可追：全部走 `ContextTools.prefetch`（同一本账、同一套证据地址）

预取**不是**平台偷偷塞给模型的免费上下文。它：
* 走同一本 `ContextTools` 账（`stats[kind]["prefetched"]`、`executions`、字符数照记）；
* 拿同一套 `evidence_id`（进同一份共享证据仓，于是汇总/对账时**按地址就能取回原件**）；
* 条目上带 `meta["prefetch"]`，所以读 trace 的人一眼能分出「这条是模型要的」还是
  「这条是平台预取的」。
**唯一不占的是模型的索取额度**（`max_tool_requests`）：那个额度是用来逼模型收敛的，
平台自己的预取去吃它，等于把「少搜一轮」省下来的轮次又赔回去。

### 3. 可复现：候选与窗口都是**算**出来的，与模型无关

同一份 diff 两次跑出同一份候选（顺序、去重、上界都是确定的）。理由不是洁癖：
不确定的话，「为什么这次没预取到那个文件」就永远说不清，而这句话正是复核的人要问的。

## 交付顺序：命中位置 → 邻近窗口 → 批次 diff

`_fit_items` 按顺序保留，额度不够时从**尾巴**开始丢。所以最有价值、最小的排最前：
搜索命中（几行位置 + 覆盖账）排第一，邻近窗口第二，批次 diff（大）排最后 ——
丢了它模型自己再要即可，而命中位置丢了就只能再搜一轮。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from services.ai.budget import ContextItem
from services.ai.protocol import ContextRequest, sanitize_requests
from services.ai.scope import AnalysisScope, normalize_path
from services.ai.trace_evidence import failure_notice

# ---------------------------------------------------------------------------
#  上界（**都是初值**：写下来是为了可复现与可调，不是因为算过最优）
# ---------------------------------------------------------------------------

#: 预取最多取几个改动文件的 diff。
#:
#: 初值 8，依据是**推断**（不是实测）：周版本批次动辄几百个文件，而一次提示词能装下的
#: diff 只有十几个（单条上限 × 条数），所以「取前 8 个」的收益是「模型第 2 轮不必再点名
#: 一轮」，代价是它开局就背上一段大正文（每一轮都要重发）。批次文件数 ≤ 8 时全取。
DEFAULT_MAX_FILES = 8

#: 预取最多搜几个候选符号。
#: 初值 4：搜索很便宜（只回位置），但每个候选要占一次执行，且命中多时正文也不小。
DEFAULT_MAX_SEARCHES = 4

#: 单个候选符号里最多取几个候选（含旧名与新名）。
DEFAULT_MAX_SYMBOLS = 6

#: 最多为几处命中预取邻近窗口。
#: 初值 2：窗口是**正文**，是这份预取里最贵的东西；两处够回答「调用方长什么样」。
DEFAULT_MAX_WINDOWS = 2

#: 邻近窗口的半径（行）。初值 15：够看清一次调用的上下文与周围几行，又不至于整段搬进来。
DEFAULT_WINDOW_SPAN = 15

#: 预取最多占提示词预算的比例（初值 12%，**推断**）。
#:
#: 取这个数的理由：预取的正文会在**每一轮**重发（`messages` 是 append-only 的），
#: 所以它不只是第 1 轮的成本。12% 是「明显有用、又不动摇后续轮次」的量级 —— 值得用
#: 实测替换（跑几次对比预取开/关的轮数与 token）。
DEFAULT_CHAR_SHARE = 0.12

#: 配表扩展名（预取时排最前：配表是「ID / 字段」类候选的主要来源）。
_CONFIG_EXTS = frozenset({".xlsx", ".xls", ".xlsm", ".csv"})
#: 代码扩展名（排第二：函数/字段改名类候选的来源）。
_CODE_EXTS = frozenset(
    {".lua", ".py", ".cs", ".cpp", ".cc", ".h", ".hpp", ".js", ".ts", ".java", ".go"}
)
#: 文本/配置类（排第三）。
_TEXT_EXTS = frozenset({".json", ".txt", ".md", ".xml", ".yaml", ".yml", ".ini", ".cfg"})


# ---------------------------------------------------------------------------
#  一、确定性候选：从改动行里算出「该去搜什么」
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """一个候选符号：搜它，是为了补上「耦合的另一端」。"""

    symbol: str
    kind: str
    reason: str


# 定义形态的判据。**只认这几种**：宁可漏（少一次预取）也不认错的 —— 一次错候选要花掉
# 一次执行，还会把无关命中灌进上下文。
_DEF_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bfunction\s+([A-Za-z_][\w.:]*)"), "函数定义"),
    (re.compile(r"\bdef\s+([A-Za-z_]\w*)"), "函数定义"),
    (re.compile(r"\bclass\s+([A-Za-z_]\w*)"), "类定义"),
    (re.compile(r"^\s*(?:local\s+|const\s+|let\s+|var\s+|static\s+)*(?:[\w.<>\[\],]+\s+)?([A-Za-z_]\w*)\s*="),
     "赋值"),
    (re.compile(r"\b([A-Za-z_][\w.]*)\s*[:=]\s*(?:function|\()"), "字段赋值"),
)

# 太通用的名字：搜它们等于把整个仓库的赋值行都捞回来（而且毫无信息量）。
_STOPWORDS = frozenset(
    {
        "the", "and", "for", "not", "nil", "true", "false", "self", "null", "none",
        "local", "function", "end", "then", "else", "elseif", "return", "while", "for",
        "if", "do", "break", "repeat", "until", "in", "or", "and", "print", "require",
        "import", "from", "def", "class", "try", "except", "catch", "raise", "pass",
        "new", "this", "int", "str", "bool", "float", "string", "number", "table",
        "new", "value", "values", "data", "name", "type", "item", "items", "list",
        "map", "set", "get", "key", "keys", "result", "index", "count", "size", "len",
        "on", "off", "yes", "no", "none", "undefined", "void", "const", "static",
    }
)

# 引号里的标识（配表 ID、字段名、协议名）：`"Item_1001"`、`'CfgRewardMode'`。
_QUOTED_RE = re.compile(r"""["']([A-Za-z_][\w.\-]{2,63})["']""")
# 配表 ID 形态：至少一个字母一个数字，或带下划线（纯英文单词太泛，不值得预取）。
_ID_LIKE_RE = re.compile(r"^(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9_.\-]+$|^[A-Za-z]+_[A-Za-z0-9_]+$")


def changed_lines(text: str) -> list[tuple[str, str]]:
    """把一份渲染好的 diff 拆成 `[(标记, 内容)]`，**只保留改动行**。

    标记是 `+` / `-`。头部（`+++` / `---` / `@@`）与上下文行都丢掉：前者的内容是文件名
    与坐标（不是被改的符号），后者两侧一样（改的是什么，看不出改名）。
    """
    result: list[tuple[str, str]] = []
    for raw in str(text or "").splitlines():
        if raw.startswith(("+++", "---", "@@")):
            continue
        if raw.startswith("+"):
            result.append(("+", raw[1:]))
        elif raw.startswith("-"):
            result.append(("-", raw[1:]))
    return result


def _tail(name: str) -> str:
    """取限定名的**最后一段**：`M.CalcDamage` / `proto.CalcDamage` → `CalcDamage`。

    调用点上的限定名几乎从不与定义处相同（模块别名、`self.`、表名变量各写各的），
    带限定名去搜等于只搜定义处 —— 而定义处恰好是这次**已经改掉**的那个地方。
    """
    return re.split(r"[.:]", str(name or "").strip())[-1]


def _defs_in(line: str) -> list[str]:
    out: list[str] = []
    for pattern, _kind in _DEF_PATTERNS:
        for match in pattern.finditer(line):
            name = _tail(match.group(1))
            if name:
                out.append(name)
    return out


def _acceptable(symbol: str) -> bool:
    token = str(symbol or "").strip()
    if len(token) < 3 or len(token) > 64:
        return False
    if token.lower() in _STOPWORDS:
        return False
    return any(ch.isalpha() for ch in token)


def _ids_in(line: str) -> list[str]:
    out: list[str] = []
    for match in _QUOTED_RE.finditer(line):
        token = _tail(match.group(1))
        if _acceptable(token) and _ID_LIKE_RE.match(token):
            out.append(token)
    return out


def candidate_symbols(text: str, *, limit: int = DEFAULT_MAX_SYMBOLS) -> tuple[Candidate, ...]:
    """从一份 diff 的改动行里**算**出该去搜哪些符号（确定性：同输入同输出）。

    顺序即优先级，按「补上耦合另一端」的价值排：

    1. **只在删除侧出现的定义**（旧名）—— 这次改名/删掉的那个名字，调用方若没跟着改，
       就只剩那些调用点在提它。**这是本模块最值钱的一档**（run 45/46 缺的正是它）；
    2. 只在新增侧出现的定义（新名）—— 已跟进的调用方；
    3. 两侧都出现的定义（改了实现、名字没变）—— 耦合点仍在，值得看一眼；
    4. 引号里的配表 ID / 字段名（`"Item_1001"`）—— 跨表引用。

    ## 只认**定义**，不认出现在改动行里的每一个标识

    第一版把「两侧都出现的标识」全都算成候选，于是参数名（`atk`、`def`）也进来了 ——
    搜 `atk` 会把半个仓库捞回来（实测：一次搜索的命中清单能顶满单条上限），而它**不是**
    「耦合的另一端」。判据收窄到「定义/赋值形态」之后，参数与局部变量自动出局，
    第 3 档就真的是「同名定义两侧都在」。

    每一档内部按**首次出现顺序**去重（不是按字典序：diff 的顺序就是改动的顺序，读的人
    能对上）。截到 `limit` 条；截掉了多少由调用方在说明里写出去（不静默）。
    """
    removed_defs: list[str] = []
    added_defs: list[str] = []
    ids: list[str] = []

    for marker, line in changed_lines(text):
        defs = _defs_in(line)
        if marker == "-":
            removed_defs.extend(defs)
        else:
            added_defs.extend(defs)
        ids.extend(_ids_in(line))

    def _dedup(seq: Iterable[str]) -> list[str]:
        seen: list[str] = []
        for token in seq:
            if _acceptable(token) and token not in seen:
                seen.append(token)
        return seen

    removed = _dedup(removed_defs)
    added = _dedup(added_defs)
    removed_only = [name for name in removed if name not in set(added)]
    added_only = [name for name in added if name not in set(removed)]
    both = [name for name in removed if name in set(added)]

    ordered: list[Candidate] = []
    for symbol in removed_only:
        ordered.append(
            Candidate(
                symbol,
                "removed_definition",
                "删除侧的定义：调用方若没跟着改，只剩它在提这个名字",
            )
        )
    for symbol in added_only:
        ordered.append(
            Candidate(symbol, "definition", "新增侧的定义：看谁已经跟到了新名字")
        )
    for symbol in _dedup(ids):
        ordered.append(
            Candidate(symbol, "config_id", "改动行里引号出现的标识：跨表引用")
        )
    for symbol in both:
        ordered.append(
            Candidate(symbol, "changed_identifier", "两侧都出现的定义：改了实现、名字没变")
        )
    return tuple(ordered[: max(0, int(limit))])


# ---------------------------------------------------------------------------
#  二、有界请求：批次 diff / 搜索 / 邻近窗口
# ---------------------------------------------------------------------------


def _ext_rank(path: str) -> int:
    """预取顺序：配表 → 代码 → 文本 → 其它（**推断**，不是实测的最优序）。

    依据：配表与代码的改动行里才有「ID / 字段 / 函数名」这类可搜符号；文档与资源改了
    也搜不出耦合。同一档内按路径字典序（确定性的那一条）。
    """
    lowered = str(path or "").lower()
    for rank, group in enumerate((_CONFIG_EXTS, _CODE_EXTS, _TEXT_EXTS)):
        if any(lowered.endswith(ext) for ext in group):
            return rank
    return len((_CONFIG_EXTS, _CODE_EXTS, _TEXT_EXTS))


def diff_requests(scope: AnalysisScope, *, max_files: int = DEFAULT_MAX_FILES) -> tuple[ContextRequest, ...]:
    """只预取**本轮输入**里最该先看的几个文件的 `file_diff` 请求。

    周窗口的旧提交仍在 `batch_paths` 供模型按需核查；把它们也预取并称作
    「本次改动」，会再次把历史差异混进当前 delta。手工 scope 未给 `input_paths`
    时保留原有行为。提交号由冻结的 `commit_of_path` 解析。
    """
    input_paths = scope.input_paths if scope.input_paths is not None else scope.batch_paths()
    paths = sorted(input_paths, key=lambda item: (_ext_rank(item), item))
    requests: list[ContextRequest] = []
    for path in paths[: max(0, int(max_files))]:
        commit = scope.commit_of_path(path)
        if not commit:
            continue
        # 仓库是**身份的一部分**（P1a）：两个仓库都有这条 `(提交, 路径)` 时，不带仓库号的
        # 预取请求要么被拒（取数层不猜），要么读到另一个仓库的同名文件 —— 而这一条是
        # **平台自己发起**的，模型根本没机会纠正它。归属唯一时才写；写不出来就不写，
        # 让取数层照旧判（同样的三态口径，见 `AnalysisScope.entries`）。
        owners = scope.repositories_for_path(path, commit)
        repository_id = str(next(iter(owners))) if len(owners) == 1 else ""
        requests.append(
            ContextRequest(
                type="file_diff",
                commit=commit,
                path=path,
                repository_id=repository_id,
            )
        )
    return tuple(requests)


def search_requests(
    candidates: Sequence[Candidate], *, max_searches: int = DEFAULT_MAX_SEARCHES
) -> tuple[ContextRequest, ...]:
    """给候选符号各建一条 `find_references`（不带范围前缀 = 整个冻结版本）。"""
    requests: list[ContextRequest] = []
    for candidate in tuple(candidates)[: max(0, int(max_searches))]:
        requests.append(ContextRequest(type="find_references", query=candidate.symbol))
    return tuple(requests)


#: 命中行的渲染格式（`reference_search.Hit.render`）：`路径:行号: 那一行的内容`。
#: 路径里带 `:` 的情况（Windows 盘符、`a:b` 文件名）排在行号前不可能出现 —— 判据是
#: **最后一个 `:数字: `**，所以按贪婪匹配到行号那一段。
_HIT_RE = re.compile(r"^(?P<path>[^\s].*?):(?P<line>\d+):\s?(?P<text>.*)$")


def hit_windows(
    rendered: str,
    *,
    span: int = DEFAULT_WINDOW_SPAN,
    max_windows: int = DEFAULT_MAX_WINDOWS,
    skip_paths: Iterable[str] = (),
) -> tuple[ContextRequest, ...]:
    """从渲染好的命中清单里取几处，各建一条**小窗口** `file_content` 请求。

    为什么只要窗口、不要整份：命中在**未改动**的文件里时（本工作包的金标形态），
    那一段的上下文才是判断「这里是不是真的还在调它」的依据，而整份文件既贵又慢。
    窗口是 `行号-span` 到 `行号+span`，夹在文件内（行号从 1 开始）。

    `skip_paths` 里的文件跳过：本批次已经取过 diff 的文件，正文要按需再要
    （它不在「补耦合另一端」的取材范围里）。同一个文件的多处命中只取**第一处**：
    第二处的上下文与第一处大概率重叠，而每条窗口都是要付费的正文。
    """
    skipped = {normalize_path(path) for path in skip_paths}
    seen_paths: list[str] = []
    requests: list[ContextRequest] = []
    for raw in str(rendered or "").splitlines():
        if len(requests) >= max(0, int(max_windows)):
            break
        match = _HIT_RE.match(raw.strip())
        if not match:
            continue
        path = normalize_path(match.group("path"))
        if not path or path in skipped or path in seen_paths:
            continue
        try:
            line = int(match.group("line"))
        except (TypeError, ValueError):
            continue
        if line <= 0:
            continue
        seen_paths.append(path)
        start = max(1, line - max(0, int(span)))
        requests.append(
            ContextRequest(
                type="file_content",
                path=path,
                lines=f"{start}-{line + max(0, int(span))}",
            )
        )
    return tuple(requests)


# ---------------------------------------------------------------------------
#  三、编排：跑一次预取（引擎在进循环之前调它）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrefetchResult:
    """预取的交付物：条目 + **给模型看的一句说明** + 账。"""

    items: tuple[ContextItem, ...] = ()
    notes: tuple[str, ...] = ()
    # 本该预取的改动文件数 / 实际取到几个（说明里要写出差额，不静默）。
    batch_files: int = 0
    fetched_files: int = 0
    candidates: tuple[Candidate, ...] = ()
    searches: int = 0
    windows: int = 0
    chars: int = 0
    # 整次预取被跳过时的原因（空串 = 真的跑过）。给读 trace 的人一个答案，而不是
    # 让「这次一条预取都没有」变成一个只能猜的现象。
    skipped: str = ""


def _prefetch_note(
    *, files_wanted: int, files_taken: int, searches: int, windows: int, chars: int
) -> str:
    """给模型的一段说明。**它必须说清「这是平台预取的」与「还有什么没预取」**。

    不说「这是预取的」→ 模型会以为自己**要过**这些内容（进而在报告里引用一份它没要过的
    证据，复核的人对不上账）；不说「还有什么没预取」→ 它会把「预取里没有」读成「没有」。
    """
    parts = [
        f"平台已按本次冻结批次**预先取回** {files_taken} 个改动文件的差异",
    ]
    if files_wanted > files_taken:
        parts.append(
            f"（本批次共 {files_wanted} 个文件，其余**没有预取** —— 需要时自己点名索取，"
            "没预取不等于没改动）"
        )
    if searches:
        parts.append(f"并预先检索了 {searches} 个改动行里出现的符号（命中位置见上文，带覆盖账）")
    if windows:
        parts.append(f"附了 {windows} 处命中的邻近窗口")
    parts.append(f"；预取合计 {chars:,} 字。这些**不是你索取的**，不需要再要一遍。")
    return "".join(parts)


def prefetch_evidence(
    tools: Any,
    *,
    scope: AnalysisScope,
    limits: Any,
    repo_paths: Any = None,
    max_files: int = DEFAULT_MAX_FILES,
    max_searches: int = DEFAULT_MAX_SEARCHES,
    max_windows: int = DEFAULT_MAX_WINDOWS,
    span: int = DEFAULT_WINDOW_SPAN,
    char_share: float = DEFAULT_CHAR_SHARE,
) -> PrefetchResult:
    """跑一次有界预取，返回**可以进第 1 轮**的条目与说明。

    三个阶段，每一段都可能在额度处停下（停下之后的东西由模型自己索取）：

    1. **批次 diff**：按「配表 → 代码 → 文本」的顺序逐个取，累计到份额就停；
    2. **候选搜索**：从已取回的 diff 改动行里算候选，各搜一次（只回位置，很便宜）；
    3. **邻近窗口**：从命中清单里挑**不在本批次**的文件，各取一小段正文。

    取不到的（返回 `None` / 一句失败说明）**照样进条目**：那份「取不到」本身就是一条
    证据（模型据此写信息缺口），而且在第 1 轮就拿到比等到第 6 轮才拿到好得多。

    预取请求也要过 `sanitize_requests`：它们虽然都是平台自己造的（批次路径来自 scope、
    命中路径来自冻结索引），但**白名单只该有一个入口** —— 多一条绕过校验的路，就多一种
    「平台自己读了一个本不该读的文件」的可能。被拒的请求进说明，不静默丢。

    ## 份额是**硬的**：装不下的一条都不取（这条比「多给一点」重要得多）

    `char_share` 不是「目标」而是**上界**，两条判据都用它：

    * **先预估**：剩余额度连「一条的最小可能体积」都装不下时，**一次取数都不发** ——
      一条 `file_diff` 最多就是 `tool_limits["file_diff"]` 那么大，连它都放不进去，
      取回来只能丢掉（白买一次取数），而丢掉的那条还会写进缓存（指针会指向不存在的那一节）；
    * **逐条卡**：取回的条目只有 `已用 + 这一条 ≤ 份额` 时才交付，超了的那条**当场忘记**
      （`forget`，于是模型之后自己索取时拿到的是正文，不是一句「见上文」）。

    为什么这么较真：预取的正文会在**每一轮**重发（`messages` 是 append-only）。一次
    「顺手多给一条」在 8 轮的运行里就是 8 倍的提示词，而**小额预算的路径会被它彻底改形**
    ——实测：35,000 字的预算下多带一条 11,000 字的 diff，整条上下文压缩的补救链都会走成
    另一条路（`tests/test_ai_context_compaction.py` 里五条用例同时变红）。
    """
    tool_limits = getattr(limits, "tool_limits", None) or {}
    prompt_budget = max(0, int(getattr(limits, "prompt_char_budget", 0) or 0))
    char_budget = int(prompt_budget * max(0.0, float(char_share)))
    # 一条的最小可能体积：配额连它都装不下就不必开始（见 docstring 的「先预估」）。
    per_item = max(
        int(tool_limits.get("file_diff") or 0),
        int(tool_limits.get("file_content") or 0),
        1_000,
    )

    planned_files = diff_requests(scope, max_files=max_files)
    wanted = len(scope.input_paths if scope.input_paths is not None else scope.batch_paths())
    if char_budget < per_item:
        # 静默跳过？**不写说明**：模型从来没要过预取，告诉它「这次没预取」只是噪音；
        # 但账面要留下（`skipped`），读出 trace 的人能回答「这次为什么一条预取都没有」。
        return PrefetchResult(
            batch_files=wanted,
            skipped=f"提示词预算 {prompt_budget:,} 字 × {char_share:.0%} = {char_budget:,} 字，"
            f"装不下一条预取条目（单条上限 {per_item:,} 字）",
        )

    notes: list[str] = []
    dropped_requests: list[str] = []

    def _sanitized(requests: Sequence[ContextRequest]) -> tuple[ContextRequest, ...]:
        kept, rejected = sanitize_requests(requests, scope, repo_paths=repo_paths)
        for entry in rejected:
            dropped_requests.append(
                str(getattr(entry, "detail", "") or getattr(entry, "reason", ""))
            )
        return tuple(kept)

    def _take(request: ContextRequest, used: int, bucket: list[ContextItem]) -> int:
        """取一条并记账；装不下就**当场忘掉**（返回新的已用字符数）。"""
        batch = tools.prefetch(_sanitized((request,)))
        for item in batch.items:
            if used + len(item.text) > char_budget:
                # 超了份额：不交付，并从缓存里摘掉 —— 否则模型之后要同一份内容时，
                # 缓存会给出一条「见上文那一节」的指针，而那一节没有进过提示词。
                forget = getattr(tools, "forget", None)
                if callable(forget):
                    forget((item,))
                continue
            bucket.append(item)
            used += len(item.text)
        return used

    # --- 阶段 1：批次 diff ---------------------------------------------------
    diff_items: list[ContextItem] = []
    used = 0
    for request in planned_files:
        if used + per_item > char_budget:
            break
        before = len(diff_items)
        used = _take(request, used, diff_items)
        if len(diff_items) == before:
            # 这一条没进来（取数为空、或体积超了份额）：不必再试后面的 —— 后面的只会更大。
            break

    # --- 阶段 2：候选搜索（从**真的取回来的**改动行里算） ----------------------
    blob = chr(10).join(item.text for item in diff_items if not failure_notice(item.text))
    candidates = candidate_symbols(blob, limit=max(0, int(max_searches)))
    search_items: list[ContextItem] = []
    for request in search_requests(candidates, max_searches=max_searches):
        if used + per_item > char_budget and search_items:
            break
        used = _take(request, used, search_items)

    # --- 阶段 3：邻近窗口（跳过本批次文件：它们已经有 diff 了） ----------------
    window_items: list[ContextItem] = []
    hits_text = chr(10).join(
        item.text for item in search_items if not failure_notice(item.text)
    )
    for request in hit_windows(
        hits_text,
        span=span,
        max_windows=max_windows,
        skip_paths=set(scope.batch_paths()),
    ):
        used = _take(request, used, window_items)

    if dropped_requests:
        notes.append(
            "预取里有 "
            f"{len(dropped_requests)} 条请求没通过白名单校验（{'；'.join(dropped_requests[:3])}），"
            "已跳过 —— 这不影响你自己按合法路径索取。"
        )

    items = [*search_items, *window_items, *diff_items]
    if items:
        notes.insert(
            0,
            _prefetch_note(
                files_wanted=wanted,
                files_taken=len(diff_items),
                searches=len(search_items),
                windows=len(window_items),
                chars=used,
            ),
        )
    return PrefetchResult(
        items=tuple(items),
        notes=tuple(notes),
        batch_files=wanted,
        fetched_files=len(diff_items),
        candidates=candidates,
        searches=len(search_items),
        windows=len(window_items),
        chars=used,
    )


def forget_unfitted(
    candidates: Sequence[ContextItem], fitted: Sequence[ContextItem], tools: Any
) -> int:
    """把**预取到了、但这一轮没装进提示词**的条目从缓存里摘掉，返回摘掉几条。

    ## 为什么必须做这一件事

    预取是直接写缓存的，而缓存命中给的是指针（「见上文那一节」，见 `context_tools`
    模块 docstring 第 4 条）。若那一节其实因为预算没进提示词，指针就是一句假话 ——
    模型会以为自己看过。判据是**交付**（`fitted` 就是 `_fit_items` 真正留下的那些），
    不是「取到了」：取到 8 份、只装下 3 份时，另外 5 份必须从缓存里消失。

    调用点在引擎里（第 1 轮拼完之后），因为只有那一层同时知道「预取了什么」与
    「装进去了什么」。

    **判据是「同一个对象」（`is`），不是相等**：`enforce_budget` 若为了压额度造出**新的**
    条目（`budget.shrink_item` 那一支），那些条目就不在 `fitted` 里，于是会被当成没交付而
    摘掉缓存。这个方向是安全的 —— 代价是模型再要一次时重新取数（多花一次取数），
    而不是收到一句指向不存在那一节的指针。反过来（把交付过的当成没交付）也一样只是多取
    一次；**错不到「给出一句假话」那一侧**。
    """
    missed = tuple(
        item
        for item in candidates
        if item.meta.get("prefetch") and not any(item is other for other in fitted)
    )
    if not missed:
        return 0
    forget = getattr(tools, "forget", None)
    if not callable(forget):  # 鸭子类型：不认这件事的执行器（测试里的桩）不该被它绊住
        return 0
    return int(forget(missed))


__all__ = [
    "Candidate",
    "DEFAULT_CHAR_SHARE",
    "DEFAULT_MAX_FILES",
    "DEFAULT_MAX_SEARCHES",
    "DEFAULT_MAX_SYMBOLS",
    "DEFAULT_MAX_WINDOWS",
    "DEFAULT_WINDOW_SPAN",
    "PrefetchResult",
    "candidate_symbols",
    "changed_lines",
    "diff_requests",
    "hit_windows",
    "prefetch_evidence",
    "search_requests",
    "forget_unfitted",
]
