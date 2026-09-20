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

## 缓存为什么按 (repository_id, commit_id) 存

不挂在 ORM 对象上：`db.session.commit()` 会 expire 实例，而被搬走/重查的提交
（`weekly_excel_merge_helpers` 会按缓存里的 commit_ids 重新查一遍）是**新的对象**，
挂在对象上的属性到了那边就没了。commit_id 在同一个仓库内唯一，跨仓库可能撞
（SVN 的 commit_id 是修订号），所以键里带上 repository_id。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

# (repository_id, commit_id) -> 平局组内的拓扑序（0 是最早的）
_SAME_INSTANT_RANK: Dict[Tuple[Any, str], int] = {}
# 缓存上限：超过就整体丢弃重建。一次同步涉及的提交是千级，这个上限只在长期不重启的
# 进程里才会碰到，而重建的代价只是下一次同步多问一次 git。
_SAME_INSTANT_RANK_LIMIT = 20000


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
    """
    rank = same_instant_rank(commit)
    if rank is None:
        rank = getattr(commit, "id", 0) or 0
    return commit_timestamp(commit), rank
