"""Bounded, reusable matching indexes for a frozen file snapshot.

Text queries use case-insensitive substring matching. Token postings accelerate
identifier queries without changing the candidate set. Workbooks are matched
across all sheets and complete cell values; only hit previews are shortened.

A file that exceeds a compressed, expanded, cell-count or decoded-text bound
is a whole-file coverage gap. Page-wide byte/text bounds stop BEFORE the next
file is accounted as indexed; indexed_files is the next real scan offset.
The repository layer exposes opaque file/hit continuations rather than asking
the model to guess directory prefixes. UTF-8 and GBK are the supported index
encodings; unrecognized encodings remain explicit gaps.
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence

# 配表能不能按工作簿解析，只认这一份判定（与 `file_content` 的表格分支同一个函数）——
# 扩展名清单写第二份，迟早会出现「正文读得出来、检索说它是二进制」这种自相矛盾。
from services.ai.excel_source import _is_openpyxl_workbook
from services.ai.reference_search import MAX_HITS, MAX_LINE_CHARS, Hit, is_binary
from utils.logger import log_print

# 分词：标识符（含下划线）与**三位以上**的数字。与 `search_text` 的「逐行子串匹配」配合，
# 见模块 docstring 里那段等价性论证。
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d{3,}")

# 索引里的严格解码。**只认前两档**（utf-8 优先、中文项目 GBK 第二），后三档
# （gb2312/latin-1/cp1252）是 `decode_text_bytes`「任何字节都出文本」的兜底，用在这里就
# 永远判不出「解不出文本」。顺序必须与 `utils.text_decoding.TEXT_ENCODINGS` 一致
# （`test_the_index_decodes_like_the_shared_implementation` 钉着这件事）。
INDEX_ENCODINGS = ("utf-8", "gbk")

# 单个文件的索引上限。**这是上限不是目标**：代码文件极少超过，超过它的基本是生成的资源
# 清单、日志、导出的纯文本数据 —— 索引它们只会把首次查询拖死，而它们本来也不是「谁在用
# 这个标识符」这个问题的答案所在。超了就不索引，并计入 `oversized_paths`（不静默跳过）。
# **配表不走这一档**：xlsx 是压缩容器，字节数比同等内容的文本大得多，它由下面
# `XLSX_MAX_INDEX_BYTES` 单独限。
MAX_INDEX_FILE_BYTES = 1_500_000

# Workbook resource bounds; the four legacy sampling constants below are
# retained for import compatibility only and no longer truncate matching.
XLSX_MAX_INDEX_BYTES = 5 * 1024 * 1024
XLSX_MAX_INDEX_SHEETS = 3
XLSX_MAX_INDEX_ROWS = 200
XLSX_MAX_CELL_CHARS = 160
XLSX_MAX_ROW_CHARS = 2_000
# These are resource bounds, not silent row/cell sampling. An over-limit
# workbook is excluded as a whole and remains an explicit coverage gap.
XLSX_MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_INDEX_TOTAL_CHARS = 8 * 1024 * 1024
MAX_INDEX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_INDEX_TOTAL_EXPANDED_BYTES = 128 * 1024 * 1024
XLSX_MAX_CELLS = 2_000_000


class IndexReadLimit(ValueError):
    """A reader rejected a blob before reading it because it exceeded its bound."""

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


def _cell_text(value) -> str:
    """一个单元格 → 一行里的一段文本。空值/空白值返回空串（不占位）。

    换行与连续空白压成单个空格：`Hit.render()` 的契约是「一行=一条命中」，一个带换行的
    单元格会把那一行拆成两条、还会让 `evidence_prefetch` 的 `路径:行号: 正文` 解析错位。
    """
    if value is None:
        return ""
    return " ".join(str(value).split())


def _xlsx_expanded_bytes(content) -> int:
    try:
        with zipfile.ZipFile(io.BytesIO(bytes(content))) as archive:
            return sum(entry.file_size for entry in archive.infolist())
    except zipfile.BadZipFile:
        return 0


def _xlsx_lines(content, *, path: str = "") -> Optional[tuple[str, ...]]:
    """工作簿字节 → 按行索引的文本行；打不开返回 `None`（由调用方落进 `binary` 那本账）。

    每行写成 `[第k张表 表名 第r行] 单元格 ｜ 单元格 …`，行号取表里的**物理行号**：
    命中之后模型要回到那张表核对，而 `file_content` 的 `lines` 对配表表示「第几张表」，
    所以表序号也必须写在同一行里 —— 只给一个索引里的序号，两条路都对不上。

    这里**不复用** `excel_source._read_excel_sheets`：那个函数是给 `file_content` 排版用的，
    输出是「抬头 + 整表统计 + 正文」，它的「第 N 行」是**渲染文本的行号**，回不到表里；
    且它按字符额度收敛、不做「前几张表」的上限。共同的读取口径（openpyxl 的
    `read_only=True, data_only=True`、能读哪些扩展名）两边是一致的。

    缓存值与公式表达式都匹配；公式追加显式标记，避免没有计算缓存的公式静默变成空格。
    """
    if not isinstance(content, (bytes, bytearray, memoryview)):
        return None
    if _xlsx_expanded_bytes(content) > XLSX_MAX_EXPANDED_BYTES:
        raise IndexReadLimit("workbook expanded size exceeds indexing bound")
    try:
        from openpyxl import load_workbook
    except ImportError:  # pragma: no cover —— openpyxl 是平台的既有依赖
        return None
    book = formulas = None
    try:
        book = load_workbook(io.BytesIO(bytes(content)), data_only=True, read_only=True)
        formulas = load_workbook(io.BytesIO(bytes(content)), data_only=False, read_only=True)
    except Exception as exc:  # noqa: BLE001 —— 打不开就退回「不是文本」，不能炸掉整批索引
        for opened in (book, formulas):
            if opened is not None:
                opened.close()
        # 这一行是「为什么没搜」的现场记录：账本里它只会变成一个 `binary` 计数。
        log_print(
            f"⚠️ AI 检索：配表解析失败，按二进制跳过 {path or '<未知路径>'} "
            f"{type(exc).__name__}: {exc}"
        )
        return None

    lines: list[str] = []
    try:
        text_chars = 0
        dimensions = sum((book[name].max_row or 0) * (book[name].max_column or 0)
                         for name in book.sheetnames)
        if dimensions > XLSX_MAX_CELLS:
            raise IndexReadLimit("workbook cell dimensions exceed indexing bound")
        visited_cells = 0
        for sheet_index, name in enumerate(book.sheetnames, start=1):
            sheet = book[name]
            formula_sheet = formulas[name]
            # Streaming readers trust the producer's <dimension> by default.
            # Some exporters underreport it, silently hiding real cells outside
            # that range. Read the XML to its end and bound actual traversal too.
            sheet.reset_dimensions()
            formula_sheet.reset_dimensions()
            from itertools import zip_longest
            rows = zip_longest(sheet.iter_rows(values_only=True),
                               formula_sheet.iter_rows(values_only=True), fillvalue=())
            for physical, (row, formula_row) in enumerate(rows, start=1):
                visited_cells += max(1, len(row or ()), len(formula_row or ()))
                if visited_cells > XLSX_MAX_CELLS:
                    raise IndexReadLimit("workbook cell traversal exceeds indexing bound")
                cells = []
                for value, formula in zip_longest(row or (), formula_row or (), fillvalue=None):
                    text = _cell_text(value)
                    if isinstance(formula, str) and formula.startswith("=") and formula != value:
                        text += f" [公式 {formula}]"
                    if text:
                        cells.append(text)
                if not cells:
                    # 整行空：不占一行索引（它本来也搜不到任何词），但物理行号照旧往上走。
                    continue
                body = " ｜ ".join(cells)
                line = f"[第{sheet_index}张表 {name} 第{physical}行] {body}"
                text_chars += len(line)
                if text_chars > MAX_INDEX_TOTAL_CHARS:
                    raise IndexReadLimit("workbook text exceeds indexing bound")
                lines.append(line)
    except IndexReadLimit:
        raise
    except Exception as exc:  # noqa: BLE001 —— 读到一半坏掉，整份按「没索引」算，不给半份
        log_print(
            f"⚠️ AI 检索：配表读取中断，整份不索引 {path or '<未知路径>'} "
            f"{type(exc).__name__}: {exc}"
        )
        return None
    finally:
        for opened in (book, formulas):
            try:
                opened.close()
            except Exception:  # noqa: BLE001
                pass
    return tuple(lines)


def _index_lines(lines: Sequence[str], position: int, token_lines: dict) -> None:
    """把一组行灌进倒排表 `token → [(entries 下标, 行号)]`。

    代码文本与配表行**共用这一份分词**：`search()` 的「桶并集 == 子串匹配」那段论证
    依赖「一行里的每个 token 都进了桶」，两条路各写一份分词迟早会分叉。
    """
    for number, line in enumerate(lines, start=1):
        for token in {match.group(0).lower() for match in _TOKEN_RE.finditer(line)}:
            token_lines.setdefault(token, []).append((position, number))


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
        max_total_chars: int = MAX_INDEX_TOTAL_CHARS,
        max_total_bytes: int = MAX_INDEX_TOTAL_BYTES,
    ) -> "SnapshotReferenceIndex":
        """读一遍这批 `(路径, 提交)` 并建索引。

        `reader(path, commit)` 由两端各自注入（Agent 端与平台本地用的是同一个
        `get_file_content_from_git`，只是跑在不同进程里）。四态处理：`None` → 读不到；
        不是文本（`is_binary`）→ 跳过；超过 `max_file_bytes` → 跳过（**配表按
        `XLSX_MAX_INDEX_BYTES` 单独限**：它是压缩容器，字节数天然比文本大）；
        `INDEX_ENCODINGS` 都解不出 → 跳过。**四者都记账**（见模块 docstring）。

        配表（`_is_openpyxl_workbook`）多一条前置分支：完整读取全部工作表与单元格值
        再进同一条分词（见 `_xlsx_lines`）；抽不出来（打不开 / 超 `XLSX_MAX_INDEX_BYTES`）
        就退回上面那四态，**不会**变成「静默消失」。

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
        total_chars = 0
        total_bytes = 0
        expanded_bytes = 0
        for position, (path, commit) in enumerate(indexed):
            try:
                content = reader(path, commit)
            except IndexReadLimit:
                oversized.add(path)
                continue
            except Exception:  # a missing blob is an index gap, not an index failure
                content = None
            if content is None:
                missing.add(path)
                continue
            cost_bytes = (len(content) if isinstance(content, (bytes, bytearray, memoryview))
                          else 4 * len(str(content)))
            if max_total_bytes and total_bytes + cost_bytes > max_total_bytes and position:
                limit = position
                break
            total_bytes += cost_bytes
            # 配表**在二进制判定之前**：xlsx 的字节是 zip 容器，`is_binary` 必判真，但它的
            # 内容是可读的单元格文本（见模块 docstring 那一节）。抽不出行时落回下面那条路，
            # 于是「打不开的工作簿」仍然进 `binary`、超 5 MB 的进 `oversized` —— 都记账。
            if isinstance(content, (bytes, bytearray, memoryview)) and _is_openpyxl_workbook(path):
                if len(content) > XLSX_MAX_INDEX_BYTES:
                    oversized.add(path)
                    continue
                # Cached values and formula expressions are both searchable;
                # count both streaming passes in the page expansion budget.
                expanded = 2 * _xlsx_expanded_bytes(content)
                if expanded_bytes + expanded > MAX_INDEX_TOTAL_EXPANDED_BYTES and position:
                    limit = position
                    break
                expanded_bytes += expanded
                try:
                    table_lines = _xlsx_lines(content, path=path)
                except IndexReadLimit:
                    oversized.add(path)
                    continue
                if table_lines is not None:
                    cost = sum(map(len, table_lines))
                    if max_total_chars and total_chars + cost > max_total_chars:
                        if position:
                            limit = position
                            break
                        oversized.add(path)
                        continue
                    total_chars += cost
                    lines_by_path[path] = table_lines
                    _index_lines(table_lines, position, token_lines)
                    continue
            # 二进制**先判**：一份 50 MB 的生成物既超上限也不是文本，而「不是文本（没搜）」
            # 比「太大（没索引）」对模型更有用 —— 它知道该换 `file_content` 那条路。
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
            cost = len(text)
            if max_total_chars and total_chars + cost > max_total_chars:
                if position:
                    limit = position
                    break
                oversized.add(path)
                continue
            total_chars += cost
            lines_by_path[path] = lines
            _index_lines(lines, position, token_lines)
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
        hits = tuple(self._hit_at(position, number, needle) for position, number in page)
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

    def _hit_at(self, position: int, number: int, needle: str = "") -> Hit:
        """`(entries 下标, 行号)` → 给模型看的那一条命中（截断口径与穷举搜索逐字一致）。"""
        path = self.entries[position][0]
        line = self.lines_by_path[path][number - 1]
        text = line.strip()
        if len(text) > MAX_LINE_CHARS:
            # Truncate the preview AFTER matching, keeping both workbook
            # coordinates and the match even when it lies in a far-right cell.
            location = ""
            if _is_openpyxl_workbook(path) and text.startswith("[") and "] " in text:
                location, text = text.split("] ", 1)
                location += "] "
            room = max(20, MAX_LINE_CHARS - len(location) - 2)
            offset = text.lower().find(needle) if needle else 0
            start = max(0, offset - min(40, room // 3))
            text = location + ("…" if start else "") + text[start:start + max(room, len(needle))] + "…"
        return Hit(path=path, line=number, text=text)


__all__ = [
    "INDEX_ENCODINGS",
    "MAX_INDEX_FILES",
    "MAX_INDEX_FILE_BYTES",
    "XLSX_MAX_CELL_CHARS",
    "XLSX_MAX_INDEX_BYTES",
    "XLSX_MAX_INDEX_ROWS",
    "XLSX_MAX_INDEX_SHEETS",
    "XLSX_MAX_ROW_CHARS",
    "IndexedSearchResult",
    "SnapshotReferenceIndex",
    "snapshot_digest",
]
