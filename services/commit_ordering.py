#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提交定序 —— 「同一时刻的多个提交，谁在前」的判据只在这里。

## 为什么单独一个文件

同一份排序键原先有两份逐字相同的实现：合并 diff 引擎
（`services/commit_diff_logic.py`）与周版本逻辑（`services/weekly_version_logic.py`）
各一份 `_commit_sort_key_for_merge`。它们**必须给出同一个答案** —— 排序键决定
`commits[-1]` 是哪个提交，也就是 `latest_commit_id` 写谁；两份实现分叉的那天，
同一份快照的 `latest_commit_id` 与合并 diff 的收尾内容会指向不同的提交，而两边
都各自「自洽」，排查时看不到任何矛盾。所以判据收口到这里，两个模块只保留一行委托。

## 它解决的是什么

按 `commit_time` 排序在本仓库**不足以定序**：机器人提交会把 committer date 打成
同一个固定时刻。实测 `226470e6c290` 与 `91bcc3439cf5` 的提交时间逐字相同
（2026-09-17 17:00:47+08:00），而前者是后者的**父提交**。平局时原先一律落回
数据库行 id，行 id 是**入库顺序**（逐条 `git log` 新建），在这里恰好把父提交排在
子提交后面 —— 于是 `latest_commit_id` 指向前一个提交，模型据它判出一条并不存在的
悬空引用（成因记录见 `skills/version-diff-review/references/anti-false-positive.md`）。

真正的次序只有 git 知道（拓扑序）。但这个判据在**排序键里**，而排序键不能做 I/O
（它被 `sorted()` 反复调用，`services/commit_diff_logic.py` 与
`services/weekly_excel_merge_helpers.py` 都在用）。所以拆成两半：

* `annotate_same_instant_order(...)` —— 有 I/O 的一半：**只在发现平局时**问一次 git，
  把「平局组内谁在前」记进进程内缓存；没有任何平局时**一次 git 都不调**。
* `commit_merge_sort_key(...)` —— 纯函数的一半：排序时先按时间，平局时查那张表。

没被问过 git 的提交保持原有行为（行 id 兜底）。**这条路只用来更接近真相，不用来报错**：
取不到 git 答案时调用方的行为与改动前逐字相同，同步不会因为定序失败而中断。

## 平局不是唯一的坑：**回填日期**

上面那一套只治「同一时刻」。**时间序与图序不一致还有第二种成因**：自动导表那类工具
提交时带上原始日期，于是**子提交可能比父提交「旧」**。按时间排出来就是反的，而后果比
平局严重得多 —— `handle_consecutive_commits_merge_internal` 取 `file_commits[0]` 当
earliest、`[-1]` 当 latest；回填时 earliest 是**图序上最后**的那条，于是它去取
`get_parent_commit(earliest)`（那正是 latest 自己）再算 `latest ↔ parent(earliest)`，
**自己和自己比**：整段区间的改动被算成「无变化」，缓存行的 `latest_commit_id` 也指向旧状态。

所以多了一半：

* `annotate_topology_order(...)` —— 有 I/O 的一半：**整组提交问一次 git 拓扑序**
  （与平局那条路共用 `order_commits_by_topology`，都是一次 `rev-list`）。
* `order_for_merge(...)` —— 用它的那一半：**全有或全无**。组里每一条都问过才按拓扑排，
  否则整体退回 `(时间, 平局序, 行 id)` —— 两套序号不可比，混排出来的次序是任意的，
  而且看起来完全正常。

## 缓存为什么按 (repository_id, commit_id) 存

不挂在 ORM 对象上：`db.session.commit()` 会 expire 实例，而被搬走/重查的提交
（`weekly_excel_merge_helpers` 会按缓存里的 commit_ids 重新查一遍）是**新的对象**，
挂在对象上的属性到了那边就没了。commit_id 在同一个仓库内唯一，跨仓库可能撞
（SVN 的 commit_id 是修订号），所以键里带上 repository_id。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from utils.logger import log_print

# (repository_id, commit_id) -> 平局组内的拓扑序（0 是最早的）
_SAME_INSTANT_RANK: Dict[Tuple[Any, str], int] = {}
# 缓存上限：超过就整体丢弃重建。一次同步涉及的提交是千级，这个上限只在长期不重启的
# 进程里才会碰到，而重建的代价只是下一次同步多问一次 git。
_SAME_INSTANT_RANK_LIMIT = 20000

# (repository_id, commit_id) -> **整组**的拓扑位次（0 是最早的，祖先在前）。
# 与上面那张表分开存：那张是「平局组内第几」，这张是「整组里第几」，两者的数轴不同，
# 合并成一张表会让 `same_instant_rank`（排序键的兜底）读到另一个数轴的序号。
_TOPOLOGY_RANK: Dict[Tuple[Any, str], int] = {}
_TOPOLOGY_RANK_LIMIT = 20000


def order_commits_by_topology(git_service, commit_ids, timeout=180):
    """把一组提交按 git 拓扑排成「祖先在前」；排不出来就保持传入顺序。

    留在本模块而不是 `GitService` 类里：这是**定序判据**的一半，与
    `commit_merge_sort_key` 是同一件事的两端（一个有 I/O、一个纯函数），分开写迟早
    会被改成两套口径。`GitService.order_commits_by_topology` 只是它的三行委托。

    ## 为什么用一次 rev-list，而不是逐对 merge-base

    逐对 `--is-ancestor` 判完 k 个提交要 k(k-1)/2 次进程（实测单次 60ms，17 个提交
    就是 136 次）；`git rev-list --topo-order --reverse <ids>` 一次走完给出这些提交
    及其祖先的拓扑序，`--reverse` 保证父提交排在子提交之前，取每个 id 的位置即可
    （实测 17 个提交 82ms）。

    **只返回输入里有的 id**，不丢不重（输入即返回的一个排列）。拿不到位置的 id
    （对象缺失、克隆不全、根本不是 git 仓库）排在最后并保持传入顺序 —— 这里只负责
    让次序更接近真相，不负责报错：定序失败不该让一次周版本同步中断。

    `git_service` 只需要有 `local_path` 与 `_run_git_command`（跨模块取它的私有执行器，
    与本仓库 `weekly_file_sync` 取 `weekly_version_logic._get_git_service` 同一手法）。
    """
    incoming = list(commit_ids or [])
    if len(incoming) < 2:
        return incoming
    try:
        if not os.path.exists(getattr(git_service, "local_path", "")):
            return incoming
        result = git_service._run_git_command(
            ["git", "rev-list", "--topo-order", "--reverse", *[str(c) for c in incoming]],
            timeout=timeout,
        )
        if not result or result.returncode != 0 or not result.stdout:
            return incoming
        position: Dict[str, int] = {}
        for index, line in enumerate(str(result.stdout).splitlines()):
            position.setdefault(line.strip(), index)
        known = [c for c in incoming if str(c) in position]
        unknown = [c for c in incoming if str(c) not in position]
        known.sort(key=lambda c: position[str(c)])
        return known + unknown
    except Exception as exc:
        log_print(f"⚠️ 按拓扑排序提交失败（沿用传入次序）: {exc}", "GIT")
        return incoming


def _commit_key(commit) -> Optional[Tuple[Any, str]]:
    commit_id = str(getattr(commit, "commit_id", "") or "").strip()
    if not commit_id:
        return None
    return (getattr(commit, "repository_id", None), commit_id)


def has_same_instant_commits(commits: Iterable[Any]) -> bool:
    """这些提交里有没有「同一 commit_time，但不是同一条提交」。

    这是「要不要去问 git」的开关：没有平局时排序键已经足够定序，一次 git 都不该调。
    """
    seen = set()
    for commit in commits or ():
        stamp = getattr(commit, "commit_time", None)
        if stamp is None:
            continue
        if stamp in seen:
            return True
        seen.add(stamp)
    return False


def annotate_same_instant_order(git_service, commits: Iterable[Any]) -> int:
    """把平局组的 git 拓扑序记进缓存，返回标注了多少个提交。

    平局组 = 同一 `commit_time` 下的不同 commit_id。每组问一次 git
    （`GitService.order_commits_by_topology`），组内位置即次序。

    **不抛异常**：git 不可用（克隆缺失、仓库是 SVN）时返回 0，排序键退回行 id ——
    定序失败不该让一次周版本同步中断，那正是它要修的场景。
    """
    commit_list: List[Any] = [c for c in (commits or ())]
    if git_service is None or not commit_list or not has_same_instant_commits(commit_list):
        return 0

    groups: Dict[Any, List[Any]] = {}
    for commit in commit_list:
        stamp = getattr(commit, "commit_time", None)
        if stamp is None:
            continue
        groups.setdefault(stamp, []).append(commit)

    annotated = 0
    for group in groups.values():
        by_id: Dict[str, Any] = {}
        for commit in group:
            commit_id = str(getattr(commit, "commit_id", "") or "").strip()
            if commit_id and commit_id not in by_id:
                by_id[commit_id] = commit
        if len(by_id) < 2:      # 同一个提交被记了两次：不是平局
            continue
        try:
            ordered_ids = git_service.order_commits_by_topology(list(by_id))
        except Exception:
            # 单个组失败就跳过这一组，其余组继续 —— 与「定序失败不打断同步」同一条口径。
            continue
        for rank, commit_id in enumerate(ordered_ids or []):
            repository_id = getattr(by_id.get(commit_id), "repository_id", None)
            _remember_rank((repository_id, commit_id), rank)
            annotated += 1
    return annotated


def _remember_rank(key: Tuple[Any, str], rank: int) -> None:
    if len(_SAME_INSTANT_RANK) >= _SAME_INSTANT_RANK_LIMIT:
        _SAME_INSTANT_RANK.clear()
    _SAME_INSTANT_RANK[key] = rank


def same_instant_rank(commit) -> Optional[int]:
    """这条提交在它的平局组内排第几；没问过 git 返回 None。"""
    key = _commit_key(commit)
    if key is None:
        return None
    return _SAME_INSTANT_RANK.get(key)


def commit_timestamp(commit) -> float:
    """提交时间（epoch 秒）。取不到时是 -inf —— 与排序键原来的兜底一致。"""
    commit_time = getattr(commit, "commit_time", None)
    if not isinstance(commit_time, datetime):
        return float("-inf")
    try:
        if commit_time.tzinfo is None:
            commit_time = commit_time.replace(tzinfo=timezone.utc)
        return commit_time.timestamp()
    except (OverflowError, OSError, ValueError):
        return float("-inf")


def commit_merge_sort_key(commit) -> Tuple[float, int]:
    """合并 diff 用的排序键：先按提交时间，平局时按 git 拓扑序。

    平局且没问过 git 时落回数据库行 id —— 那是**改动前**的行为，保留它意味着
    「没标注」与「改动前」逐字相同，不会引入新的不确定性。

    **回填日期治不了**（时间是主键）：那一类要问过 git 之后走 `order_for_merge`。
    这个函数是它的兜底口径，两边不能分叉。
    """
    rank = same_instant_rank(commit)
    if rank is None:
        rank = getattr(commit, "id", 0) or 0
    return commit_timestamp(commit), rank


def annotate_topology_order(git_service, commits: Iterable[Any]) -> int:
    """把**整组提交**的 git 拓扑序记进缓存，返回标注了多少个。

    与 `annotate_same_instant_order` 的区别：那个只在平局组内定序，这个对整组做一次 ——
    回填日期的提交**没有平局**，可时间序仍然是反的。

    代价是每轮一次 `git rev-list --topo-order`（与平局那条路同一个调用），所以只该
    在「一次同步 / 一次读回退」这种**整组拿得到**的地方调，不要放进逐文件的循环里。

    **不抛异常**：git 不可用（克隆缺失、仓库是 SVN）时返回 0，排序退回原口径 ——
    与平局那条路同一条纪律，定序失败不该中断同步。
    """
    commit_list: List[Any] = [c for c in (commits or ())]
    if git_service is None or len(commit_list) < 2:
        return 0

    by_id: Dict[str, Any] = {}
    for commit in commit_list:
        commit_id = str(getattr(commit, "commit_id", "") or "").strip()
        if commit_id and commit_id not in by_id:
            by_id[commit_id] = commit
    if len(by_id) < 2:
        return 0

    try:
        ordered_ids = git_service.order_commits_by_topology(list(by_id))
    except Exception:
        return 0

    annotated = 0
    for rank, commit_id in enumerate(ordered_ids or []):
        repository_id = getattr(by_id.get(commit_id), "repository_id", None)
        _remember_topology_rank((repository_id, commit_id), rank)
        annotated += 1
    return annotated


def _remember_topology_rank(key: Tuple[Any, str], rank: int) -> None:
    if len(_TOPOLOGY_RANK) >= _TOPOLOGY_RANK_LIMIT:
        _TOPOLOGY_RANK.clear()
    _TOPOLOGY_RANK[key] = rank


def topology_rank(commit) -> Optional[int]:
    """这条提交在**整组**里的拓扑位次；没问过 git 返回 None。"""
    key = _commit_key(commit)
    if key is None:
        return None
    return _TOPOLOGY_RANK.get(key)


def order_for_merge(commits: Iterable[Any]) -> List[Any]:
    """合并 diff 该用的次序 —— **全有或全无**。

    组里每一条都问过 git 拓扑序才按拓扑排（祖先在前）；只要有一条没有，**整体**退回
    `commit_merge_sort_key`。不能混着来：两套序号（拓扑位次与时间戳）不可比，一部分按
    拓扑、一部分按时间丢进同一个 `sorted`，出来的次序是任意的，而且看起来完全正常。

    为什么需要它：时间序在图序之前**不成立**（回填日期），而合并 diff 取
    `commits[0]` 当 earliest、`[-1]` 当 latest —— 排反了就是自己和自己比。
    """
    items: List[Any] = [c for c in (commits or ())]
    if len(items) < 2:
        return items
    ranks = [topology_rank(commit) for commit in items]
    if all(rank is not None for rank in ranks):
        return [c for _rank, c in sorted(zip(ranks, items), key=lambda pair: pair[0])]
    return sorted(items, key=commit_merge_sort_key)
