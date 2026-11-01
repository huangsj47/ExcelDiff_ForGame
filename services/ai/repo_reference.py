#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reference search over immutable, authorized Git trees.

Each page reads at most 240 files, also respecting decoded-text and raw-byte
budgets. Opaque continuations advance through real file offsets and all hits
within each slice, including flat/root directories and a single high-hit file.
Cursors bind repository identity, tip, tracked tree, authorization scope, query,
prefix and parser version. A tail page alone cannot prove a whole-tree negative.

Matching is independent of delivery: snippets are query-centered, output budgets
create resumable hit pages, and all repository receipts precede the hit bodies.
Index caches are synchronized and bounded by both entry count and memory weight.
"""

from __future__ import annotations

import hashlib
import sys
import threading
from concurrent.futures import Future
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, Optional, Sequence

from services.ai.excel_source import _is_openpyxl_workbook
from services.ai.frozen_repo import repository_label
from services.ai.reference_cursor import decode_cursor, encode_cursor
from services.ai.reference_index import (
    MAX_INDEX_FILE_BYTES,
    XLSX_MAX_INDEX_BYTES,
    IndexedSearchResult,
    IndexReadLimit,
    SnapshotReferenceIndex,
    snapshot_digest,
)
from services.ai.reference_search import MAX_HITS

#: 一次索引最多读多少个文件。**这是初值**（与 `reference_index.MAX_INDEX_FILES` 同量级）：
#: 顺序读 blob 是几秒级的操作，而索引换来的是一整页的搜索能力。超过的部分**如实算成
#: 覆盖缺口**（`unindexed`）并给出 `next_cursor`，绝不静默当成「搜过了」。
MAX_REPO_INDEX_FILES = 240

#: 进程内索引缓存的条数。索引里装着每个文件每一行的 token（几百个文件 = 几 MB），
#: 所以只留最近的两页；键里含 tip 与读取范围，强推 / 换范围之后都不会复用。
INDEX_CACHE_MAX_ENTRIES = 2
INDEX_CACHE_MAX_BYTES = 32 * 1024 * 1024
INDEX_BUILD_MAX_ACTIVE = 32
_INDEX_LOCK = threading.RLock()
_INDEX_READY = threading.Condition(_INDEX_LOCK)
# Only active builds live here. The cache identity keeps injected caches isolated;
# each builder retains its store until removal, so its id cannot be reused.
_INDEX_BUILDING: Dict[tuple, Future] = {}

#: 渲染用的「这是哪一版」一行。
_SCOPE_PREFIX = "仓库冻结版本"


@dataclass(frozen=True)
class RepoSearch:
    """一次仓库范围搜索的结果（文本 + 给人/给测试看的几个数）。

    保留结构化字段而不是只回文本：`tests` 要能断言「覆盖不足」是算出来的，
    而不是靠去正则里捞中文（那种断言在改一个字之后会静默失效）。
    """

    text: str
    query: str
    prefix: str
    tip: str = ""
    files_total: int = 0
    indexed: int = 0
    unindexed: int = 0
    skipped: int = 0
    truncated_hits: bool = False
    next_cursor: str = ""
    scan_start: int = 0
    scan_end: int = 0
    matched: int = 0
    query_id: str = ""
    missing: int = 0
    binary: int = 0
    oversized: int = 0
    undecodable: int = 0
    returned_hits: int = 0
    index_version: str = ""
    snapshot_digest: str = ""

    @property
    def coverage_complete(self) -> bool:
        """扫全了吗（**只有扫全了才能下「没有引用」这个否定结论**）。"""
        return self.unindexed == 0 and self.skipped == 0 and self.scan_start == 0


def _allowed_paths_key(allowed_paths: Any) -> Optional[tuple]:
    """把读取范围折成索引缓存键的一格（`None` = 没有声明范围）。

    ## 为什么范围必须进键

    `FrozenTreeReader.tracked_paths()` 已经按 `allowed_paths` 过滤过，所以索引条目的
    集合**就是范围的函数**：同一个仓库 / tip / 前缀 / 页大小下，全量 reader 与受限
    reader 要建的是**两份不同的索引**。而 `_INDEX_CACHE` 是**进程级**缓存（跨请求、
    跨调用点复用）；键里不含范围时，它会把一份范围的索引交给另一份范围：

    * 先全量、后受限 → 受限那次命中全量索引，搜索列出**范围外**的代码路径（越界）；
    * 先受限、后全量 → 全量那次复用受限索引，代码路径**根本没搜**（漏证据），
      而回执还会说「覆盖了该范围内的全部跟踪文件」。

    两种顺序都静默出错，所以范围是键的一部分，而不是靠「调用方记得别共用缓存」
    （`test_ai_repo_index_cache_scope.py` 的两种顺序钉着它）。

    ## 形态

    `None` 折成一个固定哨兵（`None`）；非 `None` 折成**排序后的路径元组**
    （`frozenset` / tuple / list 都归一成同一种形态，与容器类型、迭代顺序无关）。
    空集合折成空元组，与 `None` **不同键** —— 空集合是「一条都不许」。
    """
    if allowed_paths is None:
        return None
    return tuple(sorted(str(path) for path in allowed_paths))


def _index_key(
    frozen: Any,
    prefix: str,
    max_files: int,
    version: str,
    allowed_paths: Any = None,
) -> tuple:
    """索引缓存键：**仓库 ID + tip + 前缀 + 页大小 + 索引版本 + 读取范围**。

    每一项都是「换了它结果就会不同」的输入：tip 换了（强推/导入）内容整体换一份、
    前缀换了分页不同、页大小换了覆盖不同、索引实现换代（`version`）会让分词/解码口径
    变化、范围换了索引条目整体不同（见 `_allowed_paths_key`）。
    少一项就多一个静默的错命中。
    """
    return (
        getattr(frozen, "repository_id", None),
        str(getattr(frozen, "tip", "") or ""),
        str(prefix or ""),
        int(max_files or 0),
        str(version or ""),
        _allowed_paths_key(allowed_paths),
    )


def _query_identity(reader, entries, query, prefix, max_files):
    frozen = getattr(reader, "frozen", None)
    identity = repr((getattr(frozen, "repository_id", None), str(getattr(frozen, "tip", "") or ""),
                     _allowed_paths_key(getattr(reader, "allowed_paths", None)),
                     snapshot_digest(entries), prefix, query.lower(), "repo-reference-index/v2", max_files))
    return hashlib.sha256(identity.encode()).hexdigest()[:24]


def _initial_cursor(reader, query, prefix, max_files, *, after, group):
    frozen = getattr(reader, "frozen", None)
    tip = str(getattr(frozen, "tip", "") or "")
    paths = sorted(path for path in reader.tracked_paths() or () if not prefix or path.startswith(prefix))
    entries = tuple((path, tip) for path in paths)
    scope = _query_identity(reader, entries, query, prefix, max_files)
    return encode_cursor(scope, getattr(frozen, "repository_id", None), 0, 0, after=after, group=group)


def search_frozen_repository(
    reader: Any,
    query: str,
    *,
    prefix: str = "",
    max_files: int = MAX_REPO_INDEX_FILES,
    max_hits: int = MAX_HITS,
    cursor: str = "",
    max_output_chars: int = 7_000,
    cache: Optional[Dict[tuple, SnapshotReferenceIndex]] = None,
) -> Optional[RepoSearch]:
    """在冻结跟踪树里搜一次。**拿不到跟踪树时返回 `None`**（调用方按「无法检索」回报）。

    `reader` 是 `frozen_repo.FrozenTreeReader`（本模块只要求它有 `tracked_paths()` 与
    `read_bytes(path)`）。`reader.allowed_paths` 是可选的范围（没有该属性按 `None`，
    与旧行为一致）；它**进缓存键**，否则进程级缓存会把一份范围的索引交给另一份。
    """
    paths = reader.tracked_paths()
    if paths is None:
        return None
    frozen = getattr(reader, "frozen", None)
    tip = str(getattr(frozen, "tip", "") or "")
    paths = tuple(sorted(path for path in paths if not prefix or path.startswith(prefix)))
    entries = tuple((path, tip) for path in paths)
    index_version = "repo-reference-index/v2"
    query_id = _query_identity(reader, entries, query, prefix, max_files)
    address = decode_cursor(cursor, scope=query_id) if cursor else {"start": 0, "hit": 0}
    start, hit_offset = address["start"], address["hit"]
    if start > len(entries) or (start == len(entries) and entries):
        raise ValueError("检索游标 cursor 超出当前快照范围")
    # 范围必须进键：`tracked_paths()` 已按它过滤，索引条目就是范围的函数。漏掉它，
    # 先全量后受限会搜出范围外的位置，先受限后全量会漏搜（见 `_allowed_paths_key`）。
    key = _index_key(
        frozen,
        prefix,
        max_files,
        index_version,
        getattr(reader, "allowed_paths", None),
    ) + (start, snapshot_digest(entries))
    store = _INDEX_CACHE if cache is None else cache
    def read(path, _commit):
        bound = XLSX_MAX_INDEX_BYTES if _is_openpyxl_workbook(path) else MAX_INDEX_FILE_BYTES
        size_of = getattr(reader, "blob_size", None)
        size = size_of(path) if callable(size_of) else None
        if size is not None and size > bound:
            raise IndexReadLimit("blob size exceeds indexing bound")
        return reader.read_bytes(path)

    index = _shared_index(
        store, key, lambda: SnapshotReferenceIndex.build(
            entries[start:], reader=read, version=index_version, max_files=max_files))
    result = index.search(query, prefix=prefix, max_hits=max(1, int(max_hits)), cursor=hit_offset)
    end = start + index.indexed_files
    if hit_offset > result.matched:
        raise ValueError("检索游标 cursor 超出当前命中范围")
    # A continuation page describes only its own read set. Previously delivered
    # pages must be retained by the caller; a final tail page alone proves no
    # whole-scope negative.
    result = replace(result, files_total=len(entries),
                     unindexed=len(entries) - index.indexed_files,
                     truncated_files=index.indexed_files < len(entries))
    output_limit = max(1_200, int(max_output_chars))
    # Preview bounds are a delivery page, never a loss of matched locations.
    # The continuation offset is computed AFTER reducing the final hit body.
    while len(result.hits) > 1 and len(render_repo_result(result, frozen=frozen, index=index)) > output_limit - 850:
        hits = result.hits[:-1]
        offset = hit_offset + len(hits)
        result = replace(result, hits=hits, truncated_hits=offset < result.matched,
                         next_cursor=offset if offset < result.matched else None)
    next_address = ""
    if result.next_cursor is not None:
        next_address = encode_cursor(query_id, getattr(frozen, "repository_id", None), start, result.next_cursor)
    elif end < len(entries):
        next_address = encode_cursor(query_id, getattr(frozen, "repository_id", None), end, 0)
    receipt = (f"[reference_page] query_id={query_id} scan_start={start} scan_end={end} "
               f"files_total={len(entries)} indexed={result.scanned} skipped={_skips(result)} "
               f"matched_page={result.matched} hit_offset={hit_offset} "
               f"returned_hits={len(result.hits)} "
               f"coverage_complete={'true' if not start and not result.unindexed and not _skips(result) else 'false'} "
               f'next_cursor="{next_address}"\n')
    continuation = (f'\n继续同一关键词和 path，写 `"cursor": "{next_address}"`。'
                    if next_address else "\n本页已到末尾；全范围结论需要合并同一 query_id 的全部分页和缺口账。")
    return RepoSearch(
        text=receipt + render_repo_result(result, frozen=frozen, index=index) + continuation,
        query=query,
        prefix=prefix,
        tip=str(getattr(frozen, "tip", "") or ""),
        files_total=result.files_total,
        indexed=result.scanned,
        unindexed=result.unindexed,
        skipped=result.binary + result.missing + result.oversized + result.undecodable,
        truncated_hits=result.truncated_hits,
        next_cursor=next_address,
        scan_start=start,
        scan_end=end,
        matched=result.matched,
        query_id=query_id,
        missing=result.missing,
        binary=result.binary,
        oversized=result.oversized,
        undecodable=result.undecodable,
        returned_hits=len(result.hits),
        index_version=index.version,
        snapshot_digest=index.snapshot_digest,
    )


def _index_bytes(index: SnapshotReferenceIndex) -> int:
    """Conservative cache weight, including token posting containers."""
    return (sum(sys.getsizeof(line) for lines in index.lines_by_path.values() for line in lines)
            + sum(sys.getsizeof(token) + sys.getsizeof(rows) + len(rows) * 128
                  for token, rows in index.lines_by_token.items())
            + len(index.entries) * 200)


def _publish_index(store, key, index):
    """Compute weights without holding the cache lock; publish a complete index."""
    weight = _index_bytes(index)
    while True:
        with _INDEX_LOCK:
            previous = tuple(store.items())
        weights = {item_key: _index_bytes(item) for item_key, item in previous}
        with _INDEX_LOCK:
            if len(store) != len(previous) or any(store.get(item_key) is not item for item_key, item in previous):
                continue
            if weight <= INDEX_CACHE_MAX_BYTES:
                store[key] = index
                weights[key] = weight
            total = sum(weights.values())
            while len(store) > INDEX_CACHE_MAX_ENTRIES or total > INDEX_CACHE_MAX_BYTES:
                removed_key = next(iter(store))
                store.pop(removed_key)
                total -= weights.pop(removed_key)
            return


def _shared_index(store, key, build: Callable[[], SnapshotReferenceIndex]) -> SnapshotReferenceIndex:
    """Singleflight per cache/page, with bounded active-build bookkeeping.

    Blob reads, workbook parsing, weight calculation and waiter blocking all run
    outside the global cache lock. At capacity, callers wait for a build slot;
    same-key waiters share even a result too large to retain in the cache.
    """
    address = (id(store), key)
    with _INDEX_READY:
        while True:
            index = store.get(key)
            if index is not None:
                return index
            pending = _INDEX_BUILDING.get(address)
            if pending is not None:
                owner = False
                break
            if len(_INDEX_BUILDING) < max(1, INDEX_BUILD_MAX_ACTIVE):
                pending = Future()
                _INDEX_BUILDING[address] = pending
                owner = True
                break
            _INDEX_READY.wait()
    if not owner:
        return pending.result()
    try:
        index = build()
        _publish_index(store, key, index)
        pending.set_result(index)
        return index
    except BaseException as exc:
        # Include cancellation/interrupt failures: no waiter may be stranded.
        pending.set_exception(exc)
        raise
    finally:
        with _INDEX_READY:
            _INDEX_BUILDING.pop(address, None)
            _INDEX_READY.notify_all()


def _coverage_lines(result: IndexedSearchResult, *, frozen: Any, index: Any) -> list:
    """覆盖账（**每一种「没搜到的东西」各一行**）。

    这是本模块存在的理由那一半：`scanned < files_total` 或有跳过时，抬头必须说清
    「这次只覆盖了这些」，否则「没搜到」会被读成「没有」。每一行都点明**那一类文件
    是怎么被排除的**，因为它们对模型的下一步含义不同（二进制要换 `file_diff`、
    读不到要查工作副本、没索引要用 `path` 前缀再搜一次）。
    """
    lines = [
        f"- 扫描范围：{_SCOPE_PREFIX} `{str(getattr(frozen, 'tip', ''))[:12]}` 的 "
        f"**Git 跟踪文件**；本次请求的范围是"
        + (f"前缀 `{result.prefix}` 内的 {result.files_total} 个" if result.prefix
           else f"全部 {result.files_total} 个")
        + "。",
        f"- 其中**真正读了并建进索引**的有 {result.scanned} 个。",
    ]
    if result.binary:
        lines.append(
            f"- {result.binary} 个不是文本（图片/音频/压缩包，或**打不开**的配表，**没搜**）——"
            "这类文件要看内容请用 `file_diff` / `file_content`"
        )
    if result.missing:
        lines.append(f"- {result.missing} 个读不到（对象库里缺这些 blob，**没搜**）")
    if result.oversized:
        lines.append(f"- {result.oversized} 个超过单文件索引上限（**没索引**）")
    if result.undecodable:
        lines.append(
            f"- {result.undecodable} 个既不是 utf-8 也不是 gbk（**没索引**）"
        )
    if result.unindexed:
        lines.append(
            f"- **{result.unindexed} 个本页还没索引**（含前序页范围，一次只索引一页；继续使用回执里的 cursor，"
            "不要把本页无命中当成全范围无引用）"
        )
    if result.truncated_hits:
        lines.append(
            f"- **命中数到了上限**（共 {result.matched} 处，这一页列了 {len(result.hits)} 处）："
            "继续使用回执里的 cursor 获取未显示命中"
        )
    return lines


def render_repo_result(
    result: IndexedSearchResult, *, frozen: Any, index: SnapshotReferenceIndex
) -> str:
    """把一次仓库范围搜索渲染成给模型看的文本。

    ## 与 `reference_search.render_result` 的关系：**故意的两份**

    那一份的抬头写死了「本批次改动的文件」——在本模块的场景里那是**假话**（范围是整个
    冻结版本）。同一份渲染函数要么在这里说谎，要么在那边丢掉「本批次」这个限定，
    而后者会把「谁在用这个字段」的答案范围悄悄放大到全仓。所以这一层自己渲染。

    ## 覆盖率那一段排在命中清单**之前**（同一个理由）

    命中多的时候（80 条 × 200 字）单条上限会把尾巴砍掉；尾截断砍不到抬头，
    于是「覆盖不足」这句必须排在抬头里。它是模型决定「能不能写『没有其它引用』」的
    唯一依据。
    """
    lines = [
        f"[find_references] 关键词 `{result.query}`"
        + (f"（范围前缀 `{result.prefix}`）" if result.prefix else "")
        + f"：命中 {len(result.hits)} 处"
        + (f"（共 {result.matched} 处）" if result.matched > len(result.hits) else "")
        + f"，分布在 {len(result.hit_files)} 个文件里。",
        "",
        "覆盖账：",
    ]
    lines.extend(_coverage_lines(result, frozen=frozen, index=index))
    if not (result.unindexed == 0 and _skips(result) == 0):
        lines.append(
            "**覆盖不足**：上面有没读到/没索引的文件，所以**现在还不能说「仓库里没有"
            "别处引用」** —— 只能说「已覆盖的这些里没有」。"
        )
    else:
        lines.append(
            "本次覆盖了该范围内的**全部**跟踪文件，所以「没有命中」是可信的结论。"
        )
    lines.append("")
    if result.hits:
        lines.extend(hit.render() for hit in result.hits)
        lines.append("")
        lines.append(
            "以上只是**位置**。要看某一处的正文，用 `file_content` 把同一个路径与行号"
            "点名索取（它读的是**同一个冻结版本**，支持 `lines` 行窗，例如 `lines=\"120-260\"`）；"
            "要看它这次改了什么，用 `file_diff`。"
        )
        if any(_is_openpyxl_workbook(hit.path) for hit in result.hits):
            # 配表的行号与 `file_content` 的 `lines` 不是同一个坐标（那边对配表表示「第几张
            # 表」）。混着用会让模型拿 `文件:417` 去开第 417 张表，得到的是一句「没有这张表」。
            lines.append(
                "配表的命中行号是**表内物理行**（看抬头里的 `[第k张表 表名 第r行]`）："
                "要读正文请用 `file_content`，并把 `lines` 写成**第几张表**（例如 `\"2\"`）——"
                "它对配表不是行号。"
            )
    else:
        lines.append("搜索范围内没有出现这个关键词。")
    return "\n".join(lines)


def search_frozen_repositories(
    readers: Sequence[Any],
    query: str,
    *,
    prefix: str = "",
    cursor: str = "",
    max_output_chars: int = 7_800,
    **kwargs: Any,
) -> Optional[str]:
    """在**每一个**冻结仓库里各搜一次，结果**分开写**。一个都没有时返回 `None`。

    ## 为什么要逐个搜、逐段给

    本项目可能有多个仓库（配置仓库 + 代码仓库），而「这个函数被谁调用」的答案往往就在
    **另一个**仓库里 —— 只搜第一个仓库，正是 run 57 那条「只读到本批次的文件」的病根。
    两份结果的 `path` 可能同名，所以每段**抬头写明是哪个仓库**：混成一段会让模型把 A 仓库
    的命中当成 B 仓库的。

    「不搜」只有一种：某个仓库的跟踪树列不出来，那一段换成「无法检索」（调用方还会补一句
    范围声明）—— **绝不写成「没有命中」**。
    """
    blocks: list[str] = []
    receipts: list[str] = []
    failures: list[str] = []
    readers = tuple(readers or ())
    address = decode_cursor(cursor) if cursor else {}
    owner = address.get("repo")
    group = hashlib.sha256(repr(tuple(
        (getattr(getattr(reader, "frozen", None), "repository_id", None),
         getattr(getattr(reader, "frozen", None), "tip", ""),
         _allowed_paths_key(getattr(reader, "allowed_paths", None))) for reader in readers
    )).encode()).hexdigest()[:24]
    if "after" in address and address.get("group") != group:
        raise ValueError("检索游标 cursor 不属于当前多仓库冻结范围")
    active = [reader for reader in readers or () if owner is None
              or str(getattr(getattr(reader, "frozen", None), "repository_id", None)) == owner]
    if owner is not None and not active:
        raise ValueError("检索游标 cursor 所属仓库不在当前冻结范围内")
    capacity = max(1, max_output_chars // 1_800)
    queue_after = address.get("after")
    if owner is None and len(active) > capacity:
        active = active[:capacity]
        queue_after = capacity
    per_repository = max(1_200, (max_output_chars - 600) // max(1, len(active)) - 150)
    queued_cursor = ""
    last_outcome = None
    for reader in active:
        frozen = getattr(reader, "frozen", None)
        if owner is not None and str(getattr(frozen, "repository_id", None)) != owner:
            continue
        label = repository_label(getattr(frozen, "repository_id", None), getattr(frozen, "name", ""))
        outcome = search_frozen_repository(reader, query, prefix=prefix, cursor=cursor,
                                          max_output_chars=per_repository, **kwargs)
        last_outcome = outcome
        if outcome is None:
            failures.append(
                f"{label}：对象库里读不到 "
                f"{str(getattr(frozen, 'tip', ''))[:12]} 的跟踪文件"
            )
            continue
        receipt, _, body = outcome.text.partition("\n")
        receipts.append(f"【仓库 {label}】{receipt}")
        blocks.append(f"### 仓库 {label}\n{body}" if len(readers) > 1 else body)
    if queue_after is not None:
        if owner is not None and last_outcome is not None and last_outcome.next_cursor:
            next_address = decode_cursor(last_outcome.next_cursor)
            queued_cursor = encode_cursor(next_address["scope"], next_address["repo"],
                                          next_address["start"], next_address["hit"],
                                          after=queue_after, group=group)
        elif queue_after < len(readers):
            queued_cursor = _initial_cursor(readers[queue_after], query, prefix,
                                            kwargs.get("max_files", MAX_REPO_INDEX_FILES),
                                            after=queue_after + 1, group=group)
        receipts.insert(0, f'[reference_repository_page] next_cursor="{queued_cursor}" '
                           f'pending_repositories={max(0, len(readers) - queue_after)}')
    if not blocks and not failures:
        return None
    if not blocks:
        return (
            f"[无法检索] 冻结版本的范围列不出来（{'；'.join(failures)}），"
            f"所以 `{query}` **这一次没有在仓库范围内搜过**。"
            "**这不等于「没有其它引用」** —— 请把这条写成信息缺口。"
        )
    if failures:
        blocks.append(
            f"[范围说明] 还有 {len(failures)} 个仓库这次没能检索（{'；'.join(failures)}）——"
            "上面那份结果**不覆盖**它们，别据此说「仓库里没有引用」。"
        )
    return "\n".join(receipts) + "\n\n" + "\n\n".join(blocks)


def _skips(result: IndexedSearchResult) -> int:
    return (
        int(result.binary or 0)
        + int(result.missing or 0)
        + int(result.oversized or 0)
        + int(result.undecodable or 0)
    )


#: 进程内的索引缓存（键见 `_index_key`）。测试可以传自己的 dict。
_INDEX_CACHE: Dict[tuple, SnapshotReferenceIndex] = {}


def reset_index_cache() -> None:
    """清空索引缓存（测试与「导入/强推之后强制重读」用；生产路径靠键里的 tip）。"""
    with _INDEX_LOCK:
        _INDEX_CACHE.clear()


__all__ = [
    "INDEX_CACHE_MAX_ENTRIES",
    "MAX_REPO_INDEX_FILES",
    "RepoSearch",
    "render_repo_result",
    "reset_index_cache",
    "search_frozen_repository",
    "search_frozen_repositories",
]
