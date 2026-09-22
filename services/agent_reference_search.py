#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent 端：在**本机的工作副本**里搜一个关键词出现在哪些文件的哪几行。

与 `services/agent_file_content_reader.py` 是一对：那个读一个文件的正文，这个一次读很多个
文件、只回位置。分开成两个文件的理由与它相同 —— `services/task_worker_service.py` 贴着
2000 行的硬上限，而搜索这件事本身（读哪些文件、怎么收敛、怎么如实回报覆盖率）已经够写一个
模块了。

## 为什么是「读内容再搜」而不是 `git grep`

`git grep <commit>` 更快，但它在工作副本上的行为受 clone 形态影响（浅克隆、部分检出、
GIT_DIR 的解析各不一样），而 `get_file_content_from_git` 是**平台与 Agent 两端已经共用**的
那一条路 —— 同一份实现、两端结果一致这条纪律（见 `utils/content_window`）在这里同样成立。
代价是慢一些，所以这里**不逐个文件重复读**：一次读整批建快照索引
（`ai/reference_index.SnapshotReferenceIndex`，有 `MAX_INDEX_FILES` 的预热门槛），
之后的查询只查内存。**扫了多少、跳过了哪几类都要如实回报** —— 模型必须分得清
「没搜到」与「没搜完」。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import replace

from models import Repository, db
from services.ai.reference_index import (
    MAX_INDEX_FILES,
    SnapshotReferenceIndex,
    snapshot_digest,
)
from services.ai.reference_search import (
    SearchResult,
    render_result,
)

_INDEX_CACHE: "OrderedDict[str, SnapshotReferenceIndex]" = OrderedDict()
_INDEX_CACHE_LOCK = threading.Lock()
_INDEX_CACHE_MAX = 8


def _snapshot_key(repository_id: int, pairs: list[tuple[str, str]]) -> str:
    """索引缓存键 = 仓库 + **文件清单本身**的指纹（不是条数，见 `reference_index.snapshot_digest`）。"""
    return f"{repository_id}:" + snapshot_digest(pairs)


def _cached_index(key: str):
    with _INDEX_CACHE_LOCK:
        value = _INDEX_CACHE.get(key)
        if value is not None:
            _INDEX_CACHE.move_to_end(key)
        return value


def _store_index(key: str, value: SnapshotReferenceIndex) -> None:
    with _INDEX_CACHE_LOCK:
        _INDEX_CACHE[key] = value
        _INDEX_CACHE.move_to_end(key)
        while len(_INDEX_CACHE) > _INDEX_CACHE_MAX:
            _INDEX_CACHE.popitem(last=False)


def apply_batch_total(result: SearchResult, total_files) -> SearchResult:
    """把「这次结论的分母」这一个数换成平台给的那个。

    它比这里数出来的大时，说明**平台看到的本批次比 Agent 手里的这份清单大** ——
    平台发的是整批（索引按快照建，前缀只在查询时筛），所以两者本该相等；不等只可能来自
    更旧的任务 payload 或某一端被截过。这时把分母换成平台的数，并把 `truncated_files` 标上。
    模型靠 `scanned` 与 `files_total` 是否相等区分「没搜到」与「没搜完」，写错了它会把一次
    只扫了一部分的搜索当成结论（`render_result` 的 docstring 明写这两种「没有」必须分得开）。

    单独提出来是因为它是这一段里**唯一**会算错的判断，而它只依赖两个输入 —— 拆开之后
    可以直接喂数进来验，不必起数据库、不必造 Agent 任务。

    `total_files` 是从 JSON payload 里读出来的，所以它可能是任何东西（老任务没有这个键、
    手写的任务带了字符串）。读不出来就按「平台没说」处理，**不许抛**：这是给模型看的
    覆盖率，算不出来只该退回到保守的那一边（分母小一点、但仍然是这里真实数出来的）。
    """
    try:
        total = int(total_files or 0)
    except (TypeError, ValueError):
        total = 0
    if total > result.files_total:
        return replace(result, files_total=total, truncated_files=True)
    return result


def search_references_for_agent(payload: dict) -> dict:
    """返回体（会被原样 JSON 落到 `AgentTask.result_summary`）：

    `{text, query, scanned, files_total, hits, matched, missing, binary, oversized,
    undecodable, unindexed, truncated_files, truncated_hits, index_version, snapshot_digest}`
    —— `text` 就是给模型看的那一整段（抬头 + 命中清单），其余几个数是**账**：
    模型能不能对「没搜到」下结论，取决于 `scanned` 与 `files_total` 是否相等、
    以及那几种缺口各有多少（`missing` / `binary` / `oversized` / `undecodable` / `unindexed`）。

    ## `files_total` 的分母：本批次 ∩ 前缀

    它由**索引自己**从平台发来的整批文件清单里数出来（`search(prefix=...)` 的 `files_total`），
    所以「一共改了多少个」不经过任何一次截断。`total_files` 那份平台声明的兜底（见
    `apply_batch_total`）只在两边不一致时把分母抬上去 —— 平台发的是**整批**，正常情况下
    两个数相等。
    """
    repository_id = payload.get('repository_id')
    query = str(payload.get('query') or '').strip()
    entries = payload.get('entries') or []
    if not repository_id or not query:
        raise ValueError("find_references 任务缺少 repository_id/query")

    repository = db.session.get(Repository, int(repository_id))
    if repository is None:
        raise ValueError(f"find_references 任务的目标仓库不存在: {repository_id}")

    from services.vcs_content_service import get_file_content_from_git

    pairs: list[tuple[str, str]] = []
    for entry in entries:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2 and entry[0]:
            pairs.append((str(entry[0]), str(entry[1] or '')))
    if not pairs:
        raise ValueError("find_references 任务没有可搜的文件（entries 为空）")

    def reader(path: str, commit: str):
        return get_file_content_from_git(repository, commit, path)

    cache_key = _snapshot_key(int(repository_id), pairs)
    index = _cached_index(cache_key)
    if index is None:
        # 一次读整批，但**有预热门槛**（`MAX_INDEX_FILES`）：Agent 这条路的等待上限是
        # 40 秒（`REFERENCES_WAIT_SECONDS`），整批顺序读 blob 会让第一次查询落成 `pending`。
        # 门槛之外的那些如实算缺口（`search()` 报 `unindexed`）。
        index = SnapshotReferenceIndex.build(pairs, reader=reader, max_files=MAX_INDEX_FILES)
        _store_index(cache_key, index)
    result = index.search(query, prefix=str(payload.get('prefix') or ''))
    result = apply_batch_total(result, payload.get('total_files'))
    text = render_result(
        result,
        scope_note=(
            f"索引版本 `{result.index_version}`，快照 `{result.snapshot_digest[:12]}`；"
            f"索引覆盖本批次的前 {index.indexed_files} 个文件，"
            "相同快照后续查询复用索引，不重复读取 blob。"
        ),
    )
    return {
        'text': text,
        'query': result.query,
        'scanned': result.scanned,
        'files_total': result.files_total,
        'hits': len(result.hits),
        # 截断/分页**之前**的真实命中数。少了它，「命中 80 处」在读的人眼里就是全部。
        'matched': result.matched,
        'missing': result.missing,
        'binary': result.binary,
        'oversized': result.oversized,
        'undecodable': result.undecodable,
        'unindexed': result.unindexed,
        'cursor': result.cursor,
        'next_cursor': result.next_cursor,
        'truncated_files': result.truncated_files,
        'truncated_hits': result.truncated_hits,
        'index_version': result.index_version,
        'snapshot_digest': result.snapshot_digest,
    }
