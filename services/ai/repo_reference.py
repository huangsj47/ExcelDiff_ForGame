#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在**冻结仓库的跟踪文件**里找一处标识符（工作包 D 的核心取数）。

## 它替掉了什么

`find_references` 原先只搜**本批次改动过的文件**（`scope.batch_paths()`）。小 diff 恰好
改了公共接口时，这个范围会让模型既不能证伪「调用方没改」（调用方不在批次里），也不能
证实它 —— run 45/46 的报告因此把「旧函数名是否仍被其它模块调用」写成信息缺口，
而**继续加 `file_diff` 次数解决不了它**：缺的是范围，不是额度。

## 只读**对象库里的**那个版本

读取一律走 `frozen_repo.FrozenTreeReader`（`commit.tree[path]`），tip 由
`platform_provider` 固定，模型给的 commit / 分支一概忽略。绝不经由工作区（见那边的说明）。

## 索引的规模上限：**分页**，而不是「搜不完就算了」

`SnapshotReferenceIndex` 一次最多索引 `max_files` 个文件（顺序读 blob 是有代价的）。为了
让「仓库很大」不等于「后面那些永远搜不到」，本模块做两件事：

1. **按前缀（目录）分页**：模型给的 `path` 前缀命中的文件**排在最前面**，于是「先问
   `scripts/net/`、再问全局」时那一页真的覆盖了它；
2. **把下一页的起点写进回执**（`next_cursor`）：一句可以直接抄进下一轮 `path` 的**目录
   前缀**。没有它，「还有 500 个文件没索引」是句没法行动的话。

（按**文件类型**分页这一档没做：`ContextRequest` 上只有 `path` 一个范围字段，而
「`.lua` 结尾的文件」不是一个路径前缀。要么给它加一个新字段（工作包 D 明说不要另造语义
重复的名字），要么让模型用目录分页走到——先按目录走。真需要时再按金标证据加。）

## 「没搜到」与「没搜完」必须分得开

回执里逐个列出：`files_total`（分母：这个前缀范围里跟踪文件的总数）、`scanned`（真读了
文本的）、`missing`（读不到）、`binary`（不是文本）、`oversized`（太大）、`undecodable`
（解不出文本）、`unindexed`（这一页之外的）、`truncated_hits`（命中数到上限）。
**只要有一个不为零，就不许下「全仓没有调用」的结论** —— 那句话在回执里被显式禁掉。

## 与 Agent 路径的关系

多节点（platform/agent）模式下平台没有本地工作副本，这一层拿不到冻结版本，于是照旧走
「问 Agent 的本批次检索」并**显式声明覆盖范围只有本批次**（`platform_provider` 里那一支）。
本模块只管单机这一条。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence

from services.ai.reference_index import (
    IndexedSearchResult,
    SnapshotReferenceIndex,
)
from services.ai.reference_search import MAX_HITS

#: 一次索引最多读多少个文件。**这是初值**（与 `reference_index.MAX_INDEX_FILES` 同量级）：
#: 顺序读 blob 是几秒级的操作，而索引换来的是一整页的搜索能力。超过的部分**如实算成
#: 覆盖缺口**（`unindexed`）并给出 `next_cursor`，绝不静默当成「搜过了」。
MAX_REPO_INDEX_FILES = 240

#: 进程内索引缓存的条数。索引里装着每个文件每一行的 token（几百个文件 = 几 MB），
#: 所以只留最近的两页；键里含 tip，强推之后自然不复用。
INDEX_CACHE_MAX_ENTRIES = 2

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

    @property
    def coverage_complete(self) -> bool:
        """扫全了吗（**只有扫全了才能下「没有引用」这个否定结论**）。"""
        return self.unindexed == 0 and self.skipped == 0


def _index_entries(paths: Sequence[str], prefix: str, tip: str) -> tuple:
    """索引的条目顺序：**前缀命中的排最前面**（见模块抬头第 1 条）。

    提交位一律填 tip（本模块只搜冻结的那个版本）——`SnapshotReferenceIndex` 的身份是
    `(路径, 提交)`，填 tip 让同一个文件的两次搜索命中同一份条目，
    也让缓存键里的 tip 与条目身份对得上。
    """
    head = [path for path in paths if not prefix or path.startswith(prefix)]
    tail = [path for path in paths if prefix and not path.startswith(prefix)]
    return tuple((path, tip) for path in head + tail)


def _index_key(frozen: Any, prefix: str, max_files: int, version: str) -> tuple:
    """索引缓存键：**仓库 ID + tip + 前缀 + 页大小 + 索引版本**。

    每一项都是「换了它结果就会不同」的输入：tip 换了（强推/导入）内容整体换一份、
    前缀换了分页不同、页大小换了覆盖不同、索引实现换代（`version`）会让分词/解码口径
    变化。少一项就多一个静默的错命中（`test_the_index_cache_is_keyed_by_tip` 钉着它）。
    """
    return (
        getattr(frozen, "repository_id", None),
        str(getattr(frozen, "tip", "") or ""),
        str(prefix or ""),
        int(max_files or 0),
        str(version or ""),
    )


def _looks_like_a_directory(path: str) -> str:
    """`next_cursor` 给一段**可以抄进 `path` 的目录前缀**（不是单个文件）。

    模型下一轮要做的是「把范围缩到那一段」，所以给 `scripts/net/` 比给
    `scripts/net/proto.lua` 有用：后者只能搜一个文件（而它已经在这一页里了）。
    """
    text = str(path or "")
    cut = text.rfind("/")
    if cut <= 0:
        return ""
    return text[: cut + 1]


def search_frozen_repository(
    reader: Any,
    query: str,
    *,
    prefix: str = "",
    max_files: int = MAX_REPO_INDEX_FILES,
    max_hits: int = MAX_HITS,
    cache: Optional[Dict[tuple, SnapshotReferenceIndex]] = None,
) -> Optional[RepoSearch]:
    """在冻结跟踪树里搜一次。**拿不到跟踪树时返回 `None`**（调用方按「无法检索」回报）。

    `reader` 是 `frozen_repo.FrozenTreeReader`（本模块只要求它有 `tracked_paths()` 与
    `read_bytes(path)`）。
    """
    paths = reader.tracked_paths()
    if paths is None:
        return None
    frozen = getattr(reader, "frozen", None)
    tip = str(getattr(frozen, "tip", "") or "")
    entries = _index_entries(tuple(paths), prefix, tip)
    index_version = "repo-reference-index/v1"
    key = _index_key(frozen, prefix, max_files, index_version)
    store = _INDEX_CACHE if cache is None else cache
    index = store.get(key)
    if index is None:
        index = SnapshotReferenceIndex.build(
            entries,
            # 读者忽略 commit：本模块只搜冻结的那个版本（条目位填的就是 tip）。
            reader=lambda path, _commit: reader.read_bytes(path),
            version=index_version,
            max_files=max_files,
        )
        store[key] = index
        while len(store) > INDEX_CACHE_MAX_ENTRIES:
            store.pop(next(iter(store)), None)
    result = index.search(query, prefix=prefix, max_hits=max_hits)
    return RepoSearch(
        text=render_repo_result(result, frozen=frozen, index=index),
        query=query,
        prefix=prefix,
        tip=str(getattr(frozen, "tip", "") or ""),
        files_total=result.files_total,
        indexed=result.scanned,
        unindexed=result.unindexed,
        skipped=result.binary + result.missing + result.oversized + result.undecodable,
        truncated_hits=result.truncated_hits,
        next_cursor=_next_cursor(index, prefix),
    )


def _next_cursor(index: SnapshotReferenceIndex, prefix: str) -> str:
    """没索引到的那些文件里，**按目录分页**的下一页起点（空串 = 这一页已到末尾）。

    取的是「没索引 + 落在当前前缀范围内」的第一个路径的目录（见 `_looks_like_a_directory`）。
    刻意**不**给一个「还剩多少个」之外的东西：分页的粒度是目录，模型下一轮只需要一个能抄的
    前缀。
    """
    for path in index.entries[index.indexed_files:]:
        candidate = path[0]
        if prefix and not candidate.startswith(prefix):
            continue
        directory = _looks_like_a_directory(candidate)
        if directory and (not prefix or directory != prefix):
            return directory
    return ""


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
            f"- {result.binary} 个不是文本（配表/生成物等，**没搜**）——"
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
        cursor = _next_cursor(index, result.prefix)
        lines.append(
            f"- **{result.unindexed} 个还没索引**（一次只索引一页，避免首次查询被拖死）"
            + (f"；按目录分页的下一页起点：把 `path` 前缀写成 `{cursor}` 再搜一次" if cursor else "")
        )
    if result.truncated_hits:
        lines.append(
            f"- **命中数到了上限**（共 {result.matched} 处，这一页列了 {len(result.hits)} 处）："
            "要看得更全就给一个更具体的前缀（`path`）再搜一次"
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
    else:
        lines.append("搜索范围内没有出现这个关键词。")
    return "\n".join(lines)


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
    _INDEX_CACHE.clear()


__all__ = [
    "INDEX_CACHE_MAX_ENTRIES",
    "MAX_REPO_INDEX_FILES",
    "RepoSearch",
    "render_repo_result",
    "reset_index_cache",
    "search_frozen_repository",
]
