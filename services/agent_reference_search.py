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
代价是慢一些，所以有文件数上限与扫描额度（`ai/reference_search.py`），并且**扫了多少、
跳过了多少都要如实回报**：模型必须分得清「没搜到」与「没搜完」。
"""

from __future__ import annotations

from dataclasses import replace

from models import Repository, db
from services.ai.reference_search import (
    MAX_SCAN_FILES,
    SearchResult,
    render_result,
    search_files,
)


def apply_batch_total(result: SearchResult, total_files) -> SearchResult:
    """把「本批次一共改了多少个文件」这一个数换成平台给的那个。

    它比这里数出来的大时，说明发过来的 `entries` 已经被平台截过（额度上限），也就是
    **这一批压根没搜完** —— 分母换成真实的总数，并把 `truncated_files` 标上。模型靠
    `scanned` 与 `files_total` 是否相等区分「没搜到」与「没搜完」，写错了它会把一次只扫了
    240 个文件的搜索当成结论（`render_result` 的 docstring 明写这两种「没有」必须分得开）。

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

    `{text, query, scanned, files_total, hits, missing, binary, truncated_files, truncated_hits}`
    —— `text` 就是给模型看的那一整段（抬头 + 命中清单），其余几个数是**账**：
    平台侧按 `scanned` 扣检索额度，而模型能不能对「没搜到」下结论，取决于
    `scanned` 与 `files_total` 是否相等。

    ## `files_total` 的分母是平台给的，不是这里数出来的

    `entries` 已经被平台截到额度上限了，所以 `len(entries)` **不是**本批次改了多少个文件
    —— 拿它当分母会得出「240/240，全覆盖了」，而真相是 767 个里只看了 240 个。平台把
    真实总数放在 `total_files` 里带过来（见 `agent_file_content_dispatch.request_references`
    的 docstring），这里只负责如实用它，并在两数不等时把 `truncated_files` 标上。
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
    for entry in entries[:MAX_SCAN_FILES]:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2 and entry[0]:
            pairs.append((str(entry[0]), str(entry[1] or '')))
    if not pairs:
        raise ValueError("find_references 任务没有可搜的文件（entries 为空）")

    def reader(path: str, commit: str):
        return get_file_content_from_git(repository, commit, path)

    result = search_files(pairs, query, reader=reader, prefix=str(payload.get('prefix') or ''))
    result = apply_batch_total(result, payload.get('total_files'))
    text = render_result(result)
    return {
        'text': text,
        'query': result.query,
        'scanned': result.scanned,
        'files_total': result.files_total,
        'hits': len(result.hits),
        'missing': result.missing,
        'binary': result.binary,
        'truncated_files': result.truncated_files,
        'truncated_hits': result.truncated_hits,
    }
