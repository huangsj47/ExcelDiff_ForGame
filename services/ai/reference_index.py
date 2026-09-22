"""Reusable inverted index for reference queries within a frozen snapshot.

## 它替换掉了什么

以前每次 `find_references` 都**重新逐个文件读 blob**（`reference_search.search_files`），
而一次分析里同一个词常被问两遍、不同词各问一次 —— 一次周版本 767 个文件就是几十秒的
重复劳动。这里把「读一遍」与「问很多次」分开：`build()` 读一次，`search()` 只查内存。

## 命中集合必须与穷举搜索**逐条相等**

这是本模块**唯一的正确性判据**（`tests/test_ai_find_references.py::test_index_hits_equal_exhaustive_search`
把两份实现放在同一份语料上逐条比）。索引漏一条命中不是「不完整」，是**「这里没有引用」这条
错误结论** —— 模型据此删掉一个真正的耦合点。所以这里的加速**只能是**候选集的裁剪，
不能是判据的换法。

具体做法（曾经错过一次，见下）：

* token 桶的 key 是**行里的每个 token**。查 `target_id` 时不能只看 `hits_by_token["target_id"]`
  那一个桶 —— 只要**任何一处**把标识符当独立词用过，桶就存在，于是 `target_id_extra`
  / `x_target_id` / `target_ids` 这些「query 在更长 token 内部」的行**全被漏掉**。
  正确做法是并上**所有「key 里包含 query」的桶**。
* 这个并集**恰好**等于子串匹配的结果集（不多不少）当且仅当 query 本身是一个合法 token：
  * 不多：桶 key 是行里真实出现过的 token，key 含 query ⇒ 那一行含 query；
  * 不少：query 合法（`_TOKEN_RE` 全匹配）时，它在任何一行的**每一次出现都落在某个 token
    内部** —— 分词是「从行首贪心吃下来」的，`[A-Za-z_][A-Za-z0-9_]*` 会把整个词吃成一个
    token，`\\d{3,}` 会把整段数字吃掉。反例只可能出现在「query 自身不是 token」的时候
    （`item.id`、`2abc`、中文），那时走逐行兜底（`_scan_lines`），判据仍是子串匹配。
* 一行里可能有**两个**不同的 token 都含 query（`local target_id = target_id_extra`），
  所以并集要按行去重；顺序按「文件在本批次里的次序 → 行号」，与穷举搜索的产出顺序一致。

## 编码：索引这里**比 `file_content` 严格**

`file_content` 那条路的契约是「任何字节都要出文本、绝不返回 None」
（`utils.text_decoding.decode_text_bytes`，五档兜底到 `latin-1`/`replace`）。但
`latin-1` 能解**任意**字节序列，拿它当判据就永远判不出「这份内容解不出文本」——
索引一份乱码进来，抬头却写「搜过了」，而中文标识符在乱码里一个都匹配不上：**又一处
「没搜到」被写成了「不存在」**。所以索引只认 utf-8 与 gbk（`INDEX_ENCODINGS`，与
`utils.text_decoding.TEXT_ENCODINGS` 的前两档同序），解不出的**跳过并计入
`undecodable_paths`** —— 宽松留给 `file_content`，缺口留给模型看。

## 三处「读了但没索引」都要记账

`missing_paths`（blob 取不到）/ `binary_paths`（不是文本）/ `oversized_paths`（超过
`MAX_INDEX_FILE_BYTES`）/ `undecodable_paths`（解不出文本）与 `indexed_files` 之后的
那些（超过 `MAX_INDEX_FILES` 的**预热门槛**，见下）必须能加起来等于本批次的总数 ——
`search()` 把这几个数原样交给 `render_result`，模型靠它们分清「没搜到」与「没搜完」。

## `MAX_INDEX_FILES`：首次查询的成本上限

`build()` 在一个线程里顺序读每个文件的 blob，而 Agent 侧等检索回来只有
`REFERENCES_WAIT_SECONDS = 40` 秒（`services/agent_file_content_dispatch.py`）。索引
**一次读完全部**会让首次查询从 240 个文件涨到 767 个（Run 22 实测就是 767 次 `git show`），
大概率落成 `pending`（模型这一轮的额度白花，下一轮还要再要一次）。所以索引只预热门槛内的
前 N 个文件，剩下的**如实算成缺口**（`search()` 的 `files_total` 仍是本批次的总数、
`unindexed > 0` 时 `truncated_files` 为真），绝不静默当成本批次已经搜全。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence

from services.ai.reference_search import MAX_HITS, MAX_LINE_CHARS, Hit, is_binary

# 分词：标识符（含下划线）与**三位以上**的数字。与 `search_text` 的「逐行子串匹配」配合，
# 见模块 docstring 里那段等价性论证。
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d{3,}")

# 索引里的严格解码。**只认前两档**（utf-8 优先、中文项目 GBK 第二），后三档
# （gb2312/latin-1/cp1252）是 `decode_text_bytes`「任何字节都出文本」的兜底，用在这里就
# 永远判不出「解不出文本」。顺序必须与 `utils.text_decoding.TEXT_ENCODINGS` 一致
# （`test_the_index_decodes_like_the_shared_implementation` 钉着这件事）。
INDEX_ENCODINGS = ("utf-8", "gbk")

# 单个文件的索引上限。**这是上限不是目标**：代码文件极少超过，超过它的基本是配表导出的
# 数据、生成的资源清单、日志 —— 索引它们只会把首次查询拖死，而它们本来也不是「谁在用这个
# 标识符」这个问题的答案所在。超了就不索引，并计入 `oversized_paths`（不静默跳过）。
MAX_INDEX_FILE_BYTES = 1_500_000

# 一次 `build()` 最多索引多少个文件 —— **这是调用方的策略，不是 `build()` 的默认值**。
#
# 顺序读 blob 是有代价的（767 个文件逐个 `git show` 是几十秒），而 Agent 侧等检索回来只有
# 40 秒（`services/agent_file_content_dispatch.REFERENCES_WAIT_SECONDS`）—— 索引一次读完整批
# 会让第一次查询直接落成 `pending`（Run 22 实测），模型这一轮的额度白花。所以两条生产路径
# （`platform_provider._search_local` 与 `agent_reference_search.search_references_for_agent`）
# 都把预热门槛传进来，而 `build()` 自己**只负责如实记账**：
# 没进索引的那些在 `search()` 里算成 `unindexed`，抬头写「文件数到了上限就停了，剩下的没搜」。
#
# 为什么不做成 `build()` 的默认值：直接调 `build()` 的地方（穷举基准、别的用例）要的是
# 「整批都索引」，把一个成本策略塞进默认值等于替它们做了决定 —— 而那样一来
# 「读了几个 blob」这件事就不再由调用方说了算。
MAX_INDEX_FILES = 240


def snapshot_digest(entries: Iterable[tuple[str, str]]) -> str:
    """`(路径, 提交)` 序列的内容指纹。

    **必须含文件清单本身，不能只看条数**：两个周版本的改动文件都超过预热门槛、清单不同时，
    条数一样而内容不同 —— 拿它当缓存键会让本周的查询命中上一批的索引（命中清单与覆盖率的
    分母全是从上一批带过来的，且界面上看不出来）。
    """
    body = "\n".join(f"{path}\0{commit}" for path, commit in entries)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _strict_text(content) -> Optional[str]:
    """字节 → 文本；**只认 `INDEX_ENCODINGS`**，都不是返回 `None`（= 解不出文本）。

    与 `utils.text_decoding.decode_text_bytes` 的分工写在模块 docstring 里：
    「任何字节都要出文本」是 `file_content` 那条路的契约，索引这条要能分辨乱码。
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, (bytes, bytearray, memoryview)):
        return str(content)
    data = bytes(content)
    if not data:
        return ""
    for encoding in INDEX_ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _too_large(content, limit: int) -> bool:
    """这份内容超没超过单文件索引上限。

    `bytes` 按字节数比；`str` 按**字符数**比（上游有些分支返回的是已经解好的字符串，
    再编码一遍只为了量长度是白花的 CPU）。中文一个字 3 字节，所以 `str` 这一档对非 ASCII
    文件是**偏松**的 —— 这里要的是「别把首次查询拖死」的量级判断，不是精确配额。
    """
    if limit <= 0:
        return False
    if isinstance(content, (bytes, bytearray, memoryview)):
        return len(content) > limit
    return len(str(content)) > limit


@dataclass(frozen=True)
class IndexedSearchResult:
    """一次索引搜索的账。字段与 `reference_search.SearchResult` 一一对应（同一份
    `render_result` 渲染两条路的结果），多出来的 `cursor`/`next_cursor`/`matched` 见那边。"""

    query: str
    hits: tuple[Hit, ...]
    files_total: int
    scanned: int
    missing: int
    binary: int
    truncated_files: bool
    truncated_hits: bool
    prefix: str
    index_version: str
    snapshot_digest: str
    matched: int = 0
    oversized: int = 0
    undecodable: int = 0
    unindexed: int = 0
    cursor: int = 0
    next_cursor: Optional[int] = None

    @property
    def hit_files(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(hit.path for hit in self.hits))


@dataclass(frozen=True)
class SnapshotReferenceIndex:
    entries: tuple[tuple[str, str], ...]
    # 前多少个 `entries` 真的进了索引（其余的是预热门槛之外的，见 `MAX_INDEX_FILES`）。
    indexed_files: int
    lines_by_path: dict[str, tuple[str, ...]]
    # token → 它在哪些行出现过，`(entries 下标, 行号)`。
    #
    # **这里刻意不存 Hit / 不存行文本**：一行有 6 个 token 时，存 Hit 就等于把那一行文本
    # 复制 6 份（千文件批次下是几百 MB 的量级），而行文本已经在 `lines_by_path` 里有一份了。
    # 命中要用的文本在 `search()` 里按 `(路径, 行号)` 现取（`_hit_at`）。
    lines_by_token: dict[str, tuple[tuple[int, int], ...]]
    missing_paths: frozenset[str]
    binary_paths: frozenset[str]
    oversized_paths: frozenset[str]
    undecodable_paths: frozenset[str]
    version: str
    snapshot_digest: str

    @property
    def unindexed_paths(self) -> frozenset[str]:
        """本批次里**没进索引**的那些路径（预热门槛之外的）。"""
        return frozenset(path for path, _commit in self.entries[self.indexed_files:])

    @classmethod
    def build(
        cls,
        entries: Iterable[tuple[str, str]],
        *,
        reader: Callable[[str, str], object],
        version: str = "reference-index/v1",
        max_files: Optional[int] = None,
        max_file_bytes: int = MAX_INDEX_FILE_BYTES,
    ) -> "SnapshotReferenceIndex":
        """读一遍这批 `(路径, 提交)` 并建索引。

        `reader(path, commit)` 由两端各自注入（Agent 端与平台本地用的是同一个
        `get_file_content_from_git`，只是跑在不同进程里）。四态处理：`None` → 读不到；
        不是文本（`is_binary`）→ 跳过；超过 `max_file_bytes` → 跳过；
        `INDEX_ENCODINGS` 都解不出 → 跳过。**四者都记账**（见模块 docstring）。

        `max_files` 是**调用方**的预热门槛（超过的不索引、算 `unindexed`，见 `MAX_INDEX_FILES`）。
        默认 `None` = 不限：成本策略由调用方决定，这里只如实记账。
        """
        pairs = tuple(dict.fromkeys((str(path), str(commit)) for path, commit in entries))
        limit = len(pairs) if max_files is None or int(max_files) <= 0 else min(len(pairs), int(max_files))
        indexed = pairs[:limit]

        lines_by_path: dict[str, tuple[str, ...]] = {}
        token_lines: dict[str, list[tuple[int, int]]] = {}
        missing: set[str] = set()
        binary: set[str] = set()
        oversized: set[str] = set()
        undecodable: set[str] = set()
        for position, (path, commit) in enumerate(indexed):
            try:
                content = reader(path, commit)
            except Exception:  # a missing blob is an index gap, not an index failure
                content = None
            if content is None:
                missing.add(path)
                continue
            # 二进制**先判**：一份 50 MB 的 xlsx 既超上限也不是文本，而「配表等二进制（没搜）」
            # 比「太大（没索引）」对模型更有用 —— 它知道该换 `file_ref` 那条路。
            if is_binary(content):
                binary.add(path)
                continue
            if _too_large(content, max_file_bytes):
                oversized.add(path)
                continue
            text = _strict_text(content)
            if text is None:
                undecodable.add(path)
                continue
            lines = tuple(text.splitlines())
            lines_by_path[path] = lines
            for number, line in enumerate(lines, start=1):
                for token in {match.group(0).lower() for match in _TOKEN_RE.finditer(line)}:
                    token_lines.setdefault(token, []).append((position, number))
        return cls(
            entries=pairs,
            indexed_files=limit,
            lines_by_path=lines_by_path,
            lines_by_token={key: tuple(value) for key, value in token_lines.items()},
            missing_paths=frozenset(missing),
            binary_paths=frozenset(binary),
            oversized_paths=frozenset(oversized),
            undecodable_paths=frozenset(undecodable),
            version=version,
            # 指纹算的是**整批**（含没索引的那些）：它标识的是「本批次这一份快照」，
            # 不是「索引里那 240 个文件」——否则两个只在门槛之外不同的批次会算成同一份快照。
            snapshot_digest=snapshot_digest(pairs),
        )

    # -- 查询 ---------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        prefix: str = "",
        max_hits: int = MAX_HITS,
        cursor: int = 0,
    ) -> IndexedSearchResult:
        """查一次。`prefix` 只在这里生效（**索引本身与前缀无关**，见 `platform_provider` 的
        前缀污染那条）；`cursor` 是上一页返回的 `next_cursor`（0 = 第一页）。"""
        needle = str(query or "").strip().lower()
        # 范围内 = 本批次 ∩ 前缀。**分母用的是这个数与没索引的数**，不是索引里那 240 个。
        scoped = tuple(
            (position, path)
            for position, (path, _commit) in enumerate(self.entries)
            if not prefix or path.startswith(prefix)
        )
        paths = tuple(path for _position, path in scoped)
        allowed = set(paths)

        if needle and _TOKEN_RE.fullmatch(needle):
            candidates = self._bucket_candidates(needle, allowed)
        else:
            # query 自身不是 token（含点号、中文、`2abc` 这类写法），或空词：逐行子串匹配。
            candidates = self._scan_lines(needle, scoped)

        matched = len(candidates)
        start = max(0, int(cursor or 0))
        page = candidates[start:start + max_hits]
        hits = tuple(self._hit_at(position, number) for position, number in page)
        end = start + len(page)

        missing = len(self.missing_paths & allowed)
        binary = len(self.binary_paths & allowed)
        oversized = len(self.oversized_paths & allowed)
        undecodable = len(self.undecodable_paths & allowed)
        scanned = sum(1 for path in paths if path in self.lines_by_path)
        unindexed = len(allowed) - scanned - missing - binary - oversized - undecodable
        return IndexedSearchResult(
            query=query,
            hits=hits,
            files_total=len(paths),
            scanned=scanned,
            missing=missing,
            binary=binary,
            oversized=oversized,
            undecodable=undecodable,
            unindexed=unindexed,
            # 「文件数到了上限就停了」在这一层就是「有没索引的」，两条部署路径同一个口径。
            truncated_files=unindexed > 0,
            truncated_hits=end < matched,
            prefix=prefix,
            index_version=self.version,
            snapshot_digest=self.snapshot_digest,
            matched=matched,
            cursor=start,
            next_cursor=end if end < matched else None,
        )

    def _bucket_candidates(self, needle: str, allowed: set[str]) -> list[tuple[int, int]]:
        """所有「key 里包含 `needle`」的桶的并集，按 (文件次序, 行号) 排序并去重。

        只看 `lines_by_token[needle]` 那一个桶会漏掉「query 在更长 token 内部」的每一行 ——
        模块 docstring 里那一段论证是这里唯一的理由，`test_index_hits_equal_exhaustive_search`
        是它的判据。
        """
        found: dict[tuple[int, int], None] = {}
        for token, occurrences in self.lines_by_token.items():
            if needle not in token:
                continue
            for position, number in occurrences:
                if self.entries[position][0] in allowed:
                    # 同一行被多个 token 命中时只留一条（穷举搜索每行也只出一条）。
                    found[(position, number)] = None
        return sorted(found)

    def _scan_lines(self, needle: str, scoped: Sequence[tuple[int, str]]) -> list[tuple[int, int]]:
        """逐行兜底：与 `reference_search.search_text` 相同的子串匹配（大小写不敏感）。"""
        found: list[tuple[int, int]] = []
        for position, path in scoped:
            for number, line in enumerate(self.lines_by_path.get(path, ()), start=1):
                if needle in line.lower():
                    found.append((position, number))
        return found

    def _hit_at(self, position: int, number: int) -> Hit:
        """`(entries 下标, 行号)` → 给模型看的那一条命中（截断口径与穷举搜索逐字一致）。"""
        path = self.entries[position][0]
        line = self.lines_by_path[path][number - 1]
        return Hit(path=path, line=number, text=line.strip()[:MAX_LINE_CHARS])


__all__ = [
    "INDEX_ENCODINGS",
    "MAX_INDEX_FILES",
    "MAX_INDEX_FILE_BYTES",
    "IndexedSearchResult",
    "SnapshotReferenceIndex",
    "snapshot_digest",
]
