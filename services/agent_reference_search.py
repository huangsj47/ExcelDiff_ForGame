#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent execution of bounded, resumable reference searches.

The platform supplies the exact batch entries, query, prefix, continuation and
output budget. The same batch_reference/repo_reference engine is used locally
and on Agents; no separate sampling or unbounded index cache lives here.
"""

from __future__ import annotations

from dataclasses import replace

from models import Repository, db
from services.ai.reference_search import SearchResult


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

    from services.ai.batch_reference import bounded_git_reader, search_batch_references
    reader = bounded_git_reader(repository, reader)

    outcome = search_batch_references(
        pairs, query, reader=reader, repository_id=int(repository_id),
        prefix=str(payload.get("prefix") or ""), cursor=str(payload.get("cursor") or ""),
        max_output_chars=int(payload.get("max_output_chars") or 7_000),
        total_files=payload.get("total_files"),
    )
    return {
        "text": outcome.text, "query": outcome.query, "scanned": outcome.indexed,
        "files_total": outcome.files_total, "hits": outcome.returned_hits,
        "matched": outcome.matched, "missing": outcome.missing, "binary": outcome.binary,
        "oversized": outcome.oversized, "undecodable": outcome.undecodable,
        "unindexed": outcome.unindexed, "cursor": str(payload.get("cursor") or ""),
        "next_cursor": outcome.next_cursor, "scan_start": outcome.scan_start,
        "scan_end": outcome.scan_end, "query_id": outcome.query_id,
        "coverage_complete": outcome.coverage_complete,
        "truncated_files": bool(outcome.unindexed), "truncated_hits": outcome.truncated_hits,
        "index_version": outcome.index_version, "snapshot_digest": outcome.snapshot_digest,
    }
