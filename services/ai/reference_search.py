"""在本批次改动的文件里找一处标识符的**其它出现位置**（只给位置，不给正文）。

## 它解决什么

模块耦合这一类风险里最值钱的一句话是「**这个字段/协议/函数还有谁在用**」。
今天模型只能靠**猜文件名**：清单里有 `scripts/optional/activity.lua`，它猜另一个调用方
可能叫 `activity_mgr.lua`，然后一个个点名索取 diff —— 而额度是按次数计的（默认 20 次），
767 个文件里这么猜等于放弃。

`find_references` 把这一步变成一次调用：给一个标识符，返回 `文件:行: 那一行的内容`
（**不返回正文**，命中之后模型再点名索取真正要看的那一份）。

## 只搜**本批次改动过的文件**

这是白名单纪律的延续：这个工具能触达的内容，不超过模型本来就能用 `file_content` 单独
索取的那些文件。因此搜索**不会**扩大读取面 —— 它只是把「在一堆文件里翻同一个名字」这件事
从 N 次索取变成 1 次调用。搜到的是这些文件在**该提交上的完整内容**（不只是本次改动的行）：
「谁在读这个字段」这个问题，答案通常就在没改的那几行里。

## 必须如实回报的每一个数

`scanned` / `files_total`（覆盖率的分母是**本批次的总数**，不是索引里那多少个）/
`missing`（读不到）/ `binary`（不是文本）/ `oversized`（太大没索引）/ `undecodable`
（解不出文本）/ `unindexed`（首次查询的预热门槛之外）/ `truncated_*`（文件数或命中数被
上限截断）/ `matched`（截断之前的真实命中数）。

「没搜到」与「没搜完」在模型那里必须分得开 —— 否则它会写出一句「没有其它引用」，
而那只是我们扫了一半。**少写一种缺口，那一类文件就被当成了「搜过、里面没有」**，
所以抬头（`_coverage_note`）把每一种各写一行。

## 两份实现、一份口径

生产路径是 `ai/reference_index.SnapshotReferenceIndex`（读一次建索引、查询只查内存），
本模块的 `search_files` 是**穷举基准**（子串匹配那份语义的可执行定义），
两者必须逐条给出相同的命中（`tests/test_ai_find_references.py::test_index_hits_equal_exhaustive_search`）。
渲染（`render_result`）与那两个结果类型是**共用**的：两条部署路径（平台本地 / 问 Agent）
的文字必须逐字一致，否则同一次分析在单机与多节点下会给出不同的覆盖率。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from utils.text_decoding import decode_text_bytes, looks_binary

# 一次搜索最多扫多少个文件、最多给多少条命中、每条命中留多长。
#
# **前两个数是穷举基准自己的上限**（见 `search_files`）：生产路径已经切到
# `ai/reference_index.SnapshotReferenceIndex`，它有自己的 `MAX_INDEX_FILES`（预热门槛）
# 与 `MAX_HITS`（每页命中数）。`MAX_SCAN_FILES` 留着是因为基准要有一份「最多读多少个文件」
# 的口径，两条实现在同一份语料上比命中时必须站在同一条起跑线上。
#
# 「上限而不是目标」这条没变：命中数上限是因为「这个字段有 300 处引用」这句话本身没有价值
# ——模型要的是「还有哪几个文件在用」。
MAX_SCAN_FILES = 240
MAX_HITS = 80
MAX_LINE_CHARS = 200
# 太短的词（"id"、"a"）会把整个批次都搜出来，纯属浪费额度。让它写清楚一点再搜。
#
# **判据是「能不能构成一个词」，所以单位要跟着文字系统走，不能数字符。**
# 原先这里是 `len(query) < 3`，那等于假定「一个词至少三个拉丁字母」。可这个工具搜的是
# lua 与配表 —— 里面大量是中文，而**中文一个字就是一个词素**：`队伍`、`匹配`、`冷却`
# 都是完整的词，却全部被这条挡在门外。更糟的是拒掉的理由（「换个具体一点的标识符」）
# 对中文词**无从下手**（没有比「队伍」更具体的两个字了），实测 run 71 里模型就因此
# 放弃了那两条调查线。
#
# 反过来它也漏：`get`/`set`/`max`/`new` 这类三个字母的词在 lua 里满地都是，一个都没拦住。
#
# 所以按文字系统折算成一个「信息量」权重：**一个汉字按 2 计**（一个汉字承载的信息量
# 约等于两个拉丁字母），其余字符按 1 计。阈值仍是 3 —— 也就是「三个拉丁字符，或两个汉字」。
# 这样纯拉丁那侧的行为与原来逐字一致（`id`/`a` 照样被拒，`limit`/`mana_cost` 照样放行），
# 改动只发生在被误伤的那一侧。
MIN_QUERY_WEIGHT = 3
# 汉字所在的几个区段（基本区、扩展 A、兼容表意文字）。只认汉字，不认标点与全角符号 ——
# 它们是分隔符，不承载「这是个什么标识符」的信息。
_CJK_RANGES = ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF))
# 单条命中给模型看的形态：`路径:行号: 内容`
_HIT_SEPARATOR = ": "


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    text: str

    def render(self) -> str:
        return f"{self.path}:{self.line}{_HIT_SEPARATOR}{self.text}"


@dataclass(frozen=True)
class SearchResult:
    """一次搜索的账。**每一个上限都要有对应的一行**，模型才知道自己拿到的是不是全部。

    字段与 `ai/reference_index.IndexedSearchResult` 一一对应：两条部署路径（平台本地读
    工作副本 / 问业务节点上的 Agent）各自建索引，但**渲染与口径是同一份**（`render_result`），
    所以两边的字段必须一样多、意思必须一样。

    * `matched` 是**真实命中数**（截断/分页之前的那一个）。少了它，「命中 80 处」这句话
      在读的人（模型或人）眼里就是全部，而真相可能是 300 处 —— 「没列出来」被读成「只有这些」。
    * `cursor` / `next_cursor` 是分页：`hits` 只是**这一页**，`next_cursor` 为 None 才是
      真的列完了（`truncated_hits` 就是这个判断）。
    * `oversized` / `undecodable` / `unindexed` 是三种**没被索引**的文件（太大 / 解不出文本 /
      预热门槛之外），与 `missing`（读不到）/ `binary`（不是文本）同级 —— 全都是缺口。
    """

    query: str
    hits: tuple[Hit, ...] = ()
    files_total: int = 0
    scanned: int = 0
    missing: int = 0
    binary: int = 0
    truncated_files: bool = False
    truncated_hits: bool = False
    prefix: str = ""
    matched: int = 0
    oversized: int = 0
    undecodable: int = 0
    unindexed: int = 0
    cursor: int = 0
    next_cursor: "int | None" = None

    @property
    def hit_files(self) -> tuple[str, ...]:
        seen: list[str] = []
        for hit in self.hits:
            if hit.path not in seen:
                seen.append(hit.path)
        return tuple(seen)


def normalize_query(raw: str) -> str:
    """搜索词：去掉首尾空白。太短的会被调用方拒掉（见 `MIN_QUERY_WEIGHT`）。"""
    return str(raw or "").strip()


def _is_cjk(char: str) -> bool:
    point = ord(char)
    return any(low <= point <= high for low, high in _CJK_RANGES)


def query_weight(raw: str) -> int:
    """搜索词的**信息量权重**：汉字按 2 计，其余字符按 1 计（理由见 `MIN_QUERY_WEIGHT`）。

    它只有一个用途：给「这个词够不够具体」一个不偏袒任何一种文字系统的尺子。**不要**
    拿它去和字符数比 —— 它们不是一回事：`队伍` 是 2 个字符、权重 4，`get` 是 3 个字符、
    权重 3。
    """
    return sum(2 if _is_cjk(char) else 1 for char in str(raw or ""))


def is_binary(content) -> bool:
    """读回来的东西能不能当文本搜。

    判据是**真正的二进制**（魔数 / NUL，`utils.text_decoding.looks_binary`），不是
    「能不能按 utf-8 解码」—— 后者会把 GBK 的 lua、gb2312 的配置判成二进制并跳过，
    于是模型搜不到任何词，而抬头把原因写成「N 个不是文本（配表等二进制，没搜）」：
    **「编码不兼容」被记成了「它是二进制」**。命中的行文本也由同一个模块的
    `decode_text_bytes` 解码，所以「判成文本的内容」与「解码用的编码清单」永远一致。
    """
    return looks_binary(content)


def search_text(text: str, query: str, *, path: str, limit: int) -> list[Hit]:
    """在一份文本里逐行找。**大小写不敏感**（同一个标识符在不同语言里的写法不一样）。"""
    needle = query.lower()
    hits: list[Hit] = []
    for index, line in enumerate(text.splitlines(), start=1):
        if needle in line.lower():
            hits.append(
                Hit(path=path, line=index, text=line.strip()[:MAX_LINE_CHARS])
            )
            if len(hits) >= limit:
                break
    return hits


def search_files(
    entries: Iterable[tuple[str, str]],
    query: str,
    *,
    reader: Callable[[str, str], object],
    max_files: int = MAX_SCAN_FILES,
    max_hits: int = MAX_HITS,
    prefix: str = "",
) -> SearchResult:
    """**穷举基准（oracle）：生产路径已经不用它了。**

    它现在是「子串匹配、逐个文件读」这份语义的**唯一可执行定义**：
    `ai/reference_index.SnapshotReferenceIndex` 是生产走的那条路（读一次建索引、查询只查内存），
    而 `tests/test_ai_find_references.py::test_index_hits_equal_exhaustive_search` 把两者放在
    同一份语料上逐条比命中 —— 索引漏一条命中就是一句错误结论（「这里没有引用」），
    所以这份基准必须留着、且必须是对的。它的正确性由上面那组用例保证，不是由「谁在调它」保证。

    按 `(路径, 提交)` 逐个读回内容并搜索。

    `reader(path, commit)` 由两端各自注入（Agent 端与平台本地用的是同一个
    `get_file_content_from_git`，只是跑在不同的进程里）。它的返回值按三态处理：
    文本 → 搜；bytes → 不是文本就跳过并计数；`None` → 读不到，计数。

    **顺序扫、扫到上限就停**，并把「停在哪」如实记进结果（`truncated_files`）——
    这个函数不并发：它与整次分析共用一条线程，也因为顺序让「扫了多少个」是确定的。
    """
    pairs = list(entries)
    total = len(pairs)
    hits: list[Hit] = []
    scanned = missing = binary = 0
    truncated_files = False

    for path, commit in pairs:
        if scanned >= max_files:
            truncated_files = True
            break
        try:
            content = reader(path, commit)
        except Exception:  # noqa: BLE001 —— 读一个文件失败只该少一条命中，不该作废整次搜索
            missing += 1
            continue
        if content is None:
            missing += 1
            continue
        if is_binary(content):
            binary += 1
            continue
        if isinstance(content, bytes):
            # 解码走 `utils.text_decoding`（**两端唯一一份实现**）：GBK 的 lua 与搜索词
            # 都要能被解出来，否则「这个词在这份文件里出现过吗」这个问题根本没法回答
            # （按 utf-8 + replace 解出来的乱码里，中文标识符一个都匹配不上）。
            #
            # 注意这里是**宽松**的那一档（五级兜底、任何字节都出文本），而索引那条路是
            # 严格的（只认 utf-8/gbk，解不出就算缺口）—— 这是**有意的不同**，写在
            # `ai/reference_index` 的模块 docstring 里：基准要的是「子串匹配这份语义」，
            # 而索引要的是「别把一份乱码索引进去还声称搜过了」。
            content = decode_text_bytes(content)
        scanned += 1
        remaining = max_hits - len(hits)
        if remaining <= 0:
            # 命中数满了：**这一页已经列不下了**。`matched` 取已知条数（扫到一半就停下来了，
            # 真实总数这里本来就算不出来），`truncated_hits` 是「还有没列出来的」。
            return SearchResult(
                query=query,
                hits=tuple(hits),
                files_total=total,
                scanned=scanned,
                missing=missing,
                binary=binary,
                truncated_files=truncated_files,
                truncated_hits=True,
                prefix=prefix,
                matched=len(hits),
            )
        hits.extend(
            search_text(str(content), query, path=path, limit=remaining)
        )

    return SearchResult(
        query=query,
        hits=tuple(hits),
        files_total=total,
        scanned=scanned,
        missing=missing,
        binary=binary,
        truncated_files=truncated_files,
        truncated_hits=len(hits) >= max_hits,
        prefix=prefix,
        matched=len(hits),
    )


def render_result(result: SearchResult, *, scope_note: str = "") -> str:
    """把一次搜索渲染成给模型看的文本。

    **两种「没有」必须分得开**：

    * 「扫完了，一处都没有」→ 可以据此下结论（但仍然只覆盖了本批次改动过的文件，
      这句话要写在抬头里）；
    * 「扫了一部分」→ 只是「没扫到」，不是「不存在」。所以只要 `scanned < files_total`
      或有跳过，抬头就把这三个数摆出来。

    ## 覆盖率那句必须排在**最前面**

    它原先排在最后一行，而这条结果的单条上限是 8,000 字、`MAX_HITS = 80` 条命中各带
    最多 200 字的正文 —— 实测一份打满命中的结果渲染出来是 10,850 字，**覆盖率那一整句
    会被整段砍掉**（砍点还落在一行路径中间）。而被砍掉的恰恰是区分「没搜到」与「没搜完」
    的那一句：「文件数到了上限就停了，剩下的没搜」「所以『没搜到』只代表搜过的这些里没有」
    —— 模型正是拿它决定能不能写下「没有其它引用」。等于**每次命中多的时候，它都会把
    「没搜完」读成「不存在」**，而这正是本模块存在的理由。

    尾截断砍不到第一行，所以它排在抬头之后、命中清单之前。命中的位置清单因此落在后面，
    被砍时少几条**位置**（模型可以再点名索取），而不是少掉「这次搜索覆盖了多少」这个
    结论所需的数。
    """
    where = f"（范围：{result.prefix}）" if result.prefix else ""
    total_note = (
        f"（共 {result.matched} 处）" if result.matched > len(result.hits) else ""
    )
    lines = [
        f"[find_references] 关键词 `{result.query}`{where}："
        f"命中 {len(result.hits)} 处{total_note}，分布在 {len(result.hit_files)} 个文件里。",
        _coverage_note(result, scope_note),
    ]
    if result.hits:
        lines.extend(hit.render() for hit in result.hits)
        lines.append(
            "以上只是**位置**。要看某一处的正文或它所在文件的改动，"
            "用 `file_content` / `file_diff` 点名索取（路径与行号直接抄上面那几行）。"
        )
    else:
        lines.append("本批次改动的文件里没有出现这个关键词。")
    return "\n".join(lines)


def _coverage_note(result: SearchResult, scope_note: str) -> str:
    """覆盖率那一句。**每一种「没搜到的东西」都要在这里出现一次**。

    五类缺口各有一行：不是文本（`binary`）、读不到（`missing`）、太大没索引（`oversized`）、
    解不出文本（`undecodable`）、首次查询的预热门槛之外（`unindexed`）。少写一行，模型就会
    把那一类文件当成「搜过、里面没有」—— 而它正是拿这个决定能不能写「没有其它引用」。
    """
    parts = [
        f"本次搜索覆盖了本批次改动的 {result.scanned}/{result.files_total} 个文件"
    ]
    if result.binary:
        parts.append(f"{result.binary} 个不是文本（配表等二进制，没搜）")
    if result.missing:
        parts.append(f"{result.missing} 个读不到（工作副本里取不到，没搜）")
    if result.oversized:
        parts.append(f"{result.oversized} 个太大（超过单文件索引上限，没索引）")
    if result.undecodable:
        parts.append(f"{result.undecodable} 个解不出文本（既不是 utf-8 也不是 gbk，没索引）")
    if result.unindexed:
        parts.append(f"{result.unindexed} 个还没索引（首次查询只索引本批次的前一部分，没读）")
    if result.truncated_files:
        parts.append("**文件数到了上限就停了，剩下的没搜**")
    if result.truncated_hits:
        parts.append(
            "**命中数到了上限，还有没列出来的**"
            + (f"（共 {result.matched} 处，这一页列了 {len(result.hits)} 处）" if result.matched else "")
        )
        parts.append("要看得更全就给一个更具体的前缀（`path`）再搜一次")
    text = "；".join(parts) + "。"
    if result.scanned < result.files_total or result.missing:
        text += "所以「没搜到」只代表**搜过的这些**里没有，不代表整批里没有。"
    text += "搜索范围仅限**本批次改动过的文件**，没改动过的文件不在里面。"
    if scope_note:
        text += scope_note
    return text


def entries_for(paths: Sequence[str], commit_of: Callable[[str], str | None]):
    """把路径列表配成 `(路径, 提交)` 序列，跳过查不到提交的那些。"""
    pairs: list[tuple[str, str]] = []
    for path in paths:
        commit = commit_of(path)
        if commit:
            pairs.append((path, commit))
    return pairs
