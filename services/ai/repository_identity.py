# -*- coding: utf-8 -*-
"""仓库归属：这条 `(提交, 路径)` 到底属于哪个仓库 —— 以及**什么时候答不出来**。

## 它挡的是哪一次错

`commits_log` 是**跨仓库共用**的一张表（`services/ai/scope.py` 的
`repository_ids_by_commit` 那段说了同一个坑），而 SVN 修订号只在单个仓库内唯一：两个仓库
同时有 revision 42 是常态。取数原先的做法是「按本批次收窄到一组仓库，然后
`order_by(Commit.id.desc()).first()`」—— 那组仓库多于一个时，它按**自增 id** 挑了一个，
于是模型点名要看 A 仓库的 `config/x.xlsx`，读回来的可能是 B 仓库里同名的那个文件（文件名
一样、内容不同）。回执抬头写着模型问的那一条，**没有任何一处看得出来读错了**。

## 三态：查到 / 查不到 / **答不出来**

「查到」与「查不到」不够用。第三种是**歧义**：这条 `(提交, 路径)` 在两个仓库里都真实
存在，而模型没说它要哪一个。原先的 `first()` 把这个状态静默地当成「查到」；这里把它显式
返回（`row is None` 且 `candidates` 多于一条），由调用方回一句**可操作**的拒绝
（「请点名 `repository_id`，候选是 1（配置仓）、2（代码仓）」）—— 平台不替模型猜，
因为猜错的代价是**一份看起来完全正常的错内容**。

## 返回值为什么是两个值

`(行, 候选仓库)`。调用方几乎总要把候选仓库写进拒绝理由里，让函数只返回行、再让调用方
另查一次候选，等于把「为什么没查到」这条信息多查一遍（而且两次之间可能变）。
`row is None and candidates` = 歧义；`row is None and not candidates` = 真的没有这条。
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Sequence

from services.ai.scope import normalize_path
from utils.logger import log_print


def identity_refusal(
    tool: str,
    commit: str,
    path: str,
    candidates: Sequence[int],
    named: Any,
    *,
    describe: Callable[[Any], str],
) -> str:
    """`(提交, 路径)` 归属不明（或点名的仓库里没有这条）时给模型的一句话。

    ## 为什么必须说出来，而不是让它变成一句「取数失败」

    模型收到「读取失败或内容不可用」只会做两件事：换个文件，或者把这条写进信息缺口。
    而这里的真相是**它可以继续问，只是要把仓库点上** —— 平台知道候选是谁，说出来这一轮
    就能问对。这也是「平台不替模型猜」的落点：猜错的代价是一份看起来完全正常的错内容。

    开头用 `[取数失败]`（`trace_evidence.FAILURE_NOTICE_PREFIXES` 认得它）：这一次确实
    **没有**取到内容，面板上的「失败」计数与明细必须对得上。

    `describe` 由调用方给（`platform_provider._describe_repository`）：仓库的中文名来自
    冻结对象，那一层只有 provider 拿得到。
    """
    where = "、".join(describe(item) for item in candidates)
    head = f"[取数失败] {tool} {commit[:12]} {path} 没有取到内容："
    if named:
        if not candidates:
            return (
                f"{head}仓库 {describe(named)} 里查不到这条改动，"
                "本批次里也没有别的仓库有它。\n"
                "**这不等于「这个文件没有改动」** —— 请核对提交号与路径是否配错，"
                "配错的话换一条再问；确实没有就把它写成信息缺口。"
            )
        return (
            f"{head}仓库 {describe(named)} 里没有这条改动（同一条提交、同一条路径）；"
            f"本批次里带它的是 {where}。\n"
            "带上对应的 `repository_id` 再问一次就能取到。**平台不替你换仓库** ——"
            "不同仓库里的同名文件内容并不相同。"
        )
    return (
        f"{head}这条 `(提交, 路径)` 在本批次的多个仓库里都存在，平台不替你猜是哪一个："
        f"请带上 `repository_id`（候选：{where}）再问一次。\n"
        "**同名文件在两个仓库里可能是两份不同的内容**，猜错会读到另一份而回执看不出区别。"
    )


def batch_repository_ids(scope) -> tuple[int, ...]:
    """本批次涉及的全部仓库 id（`repository_ids_by_commit` 的并集，升序）。"""
    ids: set[int] = set()
    if scope is not None:
        for value in (scope.repository_ids_by_commit or {}).values():
            for item in value or ():
                parsed = as_repository_id(item)
                if parsed is not None:
                    ids.add(parsed)
    return tuple(sorted(ids))


def repositories_by_ids(ids: Iterable[int], *, limit: int = 0) -> tuple:
    """按 id 取仓库行（升序）。查不动返回空元组 —— **不抛**：这只是少一个坐标来源。"""
    wanted = [item for item in (as_repository_id(raw) for raw in ids) if item is not None]
    if not wanted:
        return ()
    try:
        from models import Repository

        query = (
            Repository.query
            .filter(Repository.id.in_(sorted(set(wanted))))
            .order_by(Repository.id.asc())
        )
        if limit > 0:
            query = query.limit(limit)
        return tuple(query.all())
    except Exception as exc:  # noqa: BLE001 —— 查不动就退回调用方那条
        log_print(f"⚠️ AI 取数：查仓库失败：{exc}")
        return ()


def batch_repositories(scope) -> tuple:
    """**要冻结的全部仓库**：本批次各仓库所属项目下的全部已接入仓库。

    ## 为什么不是「本批次涉及的那几个」

    一次周版本分析覆盖的仓库可以不止一个（配置仓库 + 代码仓库同窗是常态），而窗口里被
    改动的往往只是其中一个 —— 核查「改了公共接口，调用方改没改」要读的恰恰是**没被改动**
    的那个仓库里的文件。只冻结本批次涉及的那几个，仍然会把「改的是配置表、要看代码仓库里
    的读取方」这类核查挡在门外。

    所以范围取「本项目全部已接入仓库的只读 Git 跟踪内容」——判据仍然是本项目，没有放宽到
    任意文件（路径形状与凭证排除两条判据在多仓之前照旧各判一次）。

    实测 run 57：本批次涉及 `qz_config`(1) 与 `qz_luaworkspace代码`(2)，而按 id 升序只
    冻结了 1，于是 4 条 `code/qz_*` 的文件全被判成「不在本次冻结版本的 Git 跟踪文件里」。

    查不到项目（老 payload、手工构造的 scope、单提交模式）时返回空元组 —— 调用方退回
    「本批次那几个仓库」，与加这一层之前的行为一致。
    """
    ids = batch_repository_ids(scope)
    if not ids:
        return ()
    try:
        from models import Repository

        rows = repositories_by_ids(ids)
        project_ids = {getattr(row, "project_id", None) for row in rows}
        project_ids.discard(None)
        if project_ids:
            return tuple(
                Repository.query
                .filter(Repository.project_id.in_(sorted(project_ids)))
                .order_by(Repository.id.asc())
                .all()
            )
        return rows
    except Exception as exc:  # noqa: BLE001 —— 查不动就退回调用方那条
        log_print(f"⚠️ AI 取数：查本项目仓库失败：{exc}")
        return ()


#: 单个 `(提交, 路径)` 可能对应的行数上限。正常一行就够了（同一仓库的同一提交不会改同一个
#: 文件两次）；给个上限是为了让异常数据（重复导入）不至于把整个查询结果拖进内存。
_ROW_LIMIT = 50


def as_repository_id(raw: Any) -> int | None:
    """仓库 id 转整数（转不出来返回 `None`，**不猜**）。"""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def lookup_commit(
    commit: str,
    path: str,
    repository_id: Any = "",
    *,
    batch: Iterable[int] = (),
) -> tuple[Any | None, tuple[int, ...]]:
    """这条 `(提交, 路径)` 的那一行，以及它**真实**出现在哪几个仓库里。

    `repository_id` 点名了仓库时：那个仓库里没有这条就返回 `(None, 候选)` —— 调用方据此
    说得出「仓库 3 里没有它，有它的是 1、2」，而不是让模型以为「这个文件不存在」。

    `batch` 是本批次的仓库集合（`scope.repository_ids_by_commit`）。空 = **不知道**，
    不收窄（REV-AI-001 的旧语义，逐字保留）。
    """
    grouped = _rows_grouped(commit, path, batch)
    named = as_repository_id(repository_id)
    candidates = tuple(sorted(grouped))
    if named is not None:
        rows = grouped.get(named) or []
        return (rows[-1] if rows else None), candidates
    if len(candidates) == 1:
        return grouped[candidates[0]][-1], candidates
    return None, candidates


def lookup_commit_files(
    commit: str,
    repository_id: Any = "",
    *,
    batch: Iterable[int] = (),
) -> tuple[tuple[Any, ...], tuple[int, ...]]:
    """这条提交改过的**全部**文件行（`commit_detail` 用），以及候选仓库。

    ## 不带仓库时**合并**本批次各仓库的清单（与 `lookup_commit` 刻意不同）

    `commit_detail` 回答的是「**本批次里**这条提交改了哪些文件」。本批次同时包含两个
    仓库、而两个仓库都有这个号时，两边改过的文件**都是**本批次的事实 —— 只留一边会让
    模型看到一份「看起来完整」的清单，而其中一半的文件未被列出（假阴性）。这条口径有
    回归测试钉着（`test_a_batch_spanning_both_repositories_keeps_both`）。

    合并不会放行任何越权读取：真去读某一行的 diff 时走的是 `lookup_commit`（那里**不合并**，
    歧义就回一句请点名仓库的拒绝），而且授权还各自要过 `AnalysisScope.entry_allowed`。
    点名了 `repository_id` 时这里也收窄到那一个。
    """
    grouped = _rows_grouped(commit, None, batch)
    named = as_repository_id(repository_id)
    candidates = tuple(sorted(grouped))
    if named is not None:
        return tuple(grouped.get(named) or ()), candidates
    if not candidates:
        return (), ()
    return tuple(row for rid in candidates for row in grouped[rid]), candidates


def attribution_map(pairs: Iterable[tuple[str, str]], *, scope=None, batch_of=None):
    """逐条的仓库归属：`{(路径, 提交): 仓库 id}`。

    **只收唯一确定的那一条**：定不出仓库（歧义、查不到）的键不出现在表里 —— 而不是给一个
    猜的值。调用方看到键不在表里，就知道这一条**不知道**（本地检索那条路据此退回单仓旧行为，
    而不是把这一条算成读不到）。
    """
    found: dict[tuple[str, str], int] = {}
    memo: dict[tuple[str, str], int | None] = {}
    for path, commit in pairs:
        key = (normalize_path(path), str(commit or "").strip())
        if key not in memo:
            memo[key] = _repository_of_pair(
                key[0], key[1], scope, tuple(batch_of(key[1]) if batch_of else ())
            )
        value = memo[key]
        if value is not None:
            found[key] = value
    return found


def _repository_of_pair(path: str, commit: str, scope, batch: tuple[int, ...]) -> int | None:
    """单条 `(路径, 提交)` 的仓库归属：先冻结事实，后查库（**唯一**时才算数）。"""
    if scope is not None:
        known = scope.repositories_for_path(path, commit)
        if len(known) == 1:
            return next(iter(known))
    try:
        row, candidates = lookup_commit(commit, path, batch=batch)
    except Exception:  # noqa: BLE001 —— 查不动就当不知道（调用方不收窄）
        return None
    if row is None:
        return candidates[0] if len(candidates) == 1 else None
    return as_repository_id(getattr(row, "repository_id", None))


def _rows_grouped(commit: str, path: str | None, batch: Iterable[int]) -> dict[int, list]:
    """`{仓库 id: [行, …]}` —— 按 id 升序（于是 `[-1]` 就是从前那个 `id.desc().first()`）。

    `batch` 非空时收窄到那组仓库（REV-AI-001）。收窄用的是 `Commit.repository_id`，
    而不是 join 出来的仓库表 —— 后者会在仓库行被删掉时把提交行整条滤没。
    """
    from models import Commit

    query = Commit.query.filter_by(commit_id=str(commit))
    if path is not None:
        query = query.filter_by(path=str(path))
    wanted = tuple(as_repository_id(item) for item in (batch or ()))
    wanted = tuple(item for item in wanted if item is not None)
    if wanted:
        query = query.filter(Commit.repository_id.in_(list(wanted)))
    rows = query.order_by(Commit.id.asc()).limit(_ROW_LIMIT).all()
    grouped: dict[int, list] = {}
    for row in rows:
        repository_id = as_repository_id(getattr(row, "repository_id", None))
        if repository_id is None:
            continue
        grouped.setdefault(repository_id, []).append(row)
    return grouped
