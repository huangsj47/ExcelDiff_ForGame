#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""周版本窗口与「当前 tip 可达历史」的对账：过滤、原子替换、旧行清理（P0）。

## 这一层修的是哪一件事

2026-09-23 实测（`docs/AI分析全链路复测与强制改造指引-2026-09-23.md` §1）：测试仓库
`origin.git` 的 master 被重建/强推成 3 个提交，`repository.last_synced_tip` 也已经等于
远端 tip，**而配置 3 的 `weekly_version_diff_cache` 仍指向 `e54c73df`、`aa3fd90b`、
`2971caf7` 这些不在当前历史里的提交** —— 三行缓存的 `commit_ids` 并集有 9 个，单行
分别有 5/6/6 个。`commits_log` 同时留着新旧两段历史，而
`weekly_version_logic.process_weekly_version_sync` 按「仓库 ID + 时间窗口」取**全部**
行，没有任何按当前 branch tip 过滤的动作。

于是 AI 报告里出现了一个自相矛盾的事实：清单说「3 个提交」，差异的出处却说覆盖
「5/6 条提交」—— 两边说的不是同一个提交集合，而多出来的那些**在远端已经不存在了**。

**只比 tip 是不够的**（这正是本条记录里最容易做错的一步）：`last_synced_tip` 早就等于
远端 tip 了，缓存却还是旧的。必须真的问出「tip 上还有哪些提交」—— 见
`services/repository_sync_window.reachable_commits`。

## 做什么、不做什么

1. **过滤**：窗口内的提交按「当前 tip 可达」筛一遍。这是最小的一步，也是必须的一步。
2. **原子替换**（只在检测到历史被重写时才做）：把本窗口里不再可达的缓存行删掉，
   让它们由接下来的逐文件循环按新历史重建。删除与重建在**同一个事务**里收口
   （`db.session.commit()` 一次），不是「先删一半再慢慢补」。
3. **清掉混进来的旧提交行**（`commits_log`，限本窗口）：不清的话，下一轮同步、周版本
   页面、AI 的变更清单会**再次**读到它们 —— 过滤只影响这一次的内存结果。

   作用域**刻意限定在本窗口**：窗口外那些行属于提交列表 / 单提交 diff 这些别的功能，
   一次周版本同步不该顺手重写它们。历史被重写的仓库，窗口会随着时间推移自己覆盖过去。

4. **旧快照不在这里动**：`ai_diff_snapshot` / `ai_analysis_run` 里的历史运行**保留为
   冻结输入**（它们的 payload 里记着当时那份提交号）。历史运行是「当时看到了什么」的
   记录，改它等于伪造审计线索。本模块只动「当前这一份缓存」，不动历史账。

## 判不出来时不动作

`reachable_commits` 拿不到可达集合（没有本地副本、git 不可用、对象库缺对象）时，
本模块**一个字节都不写**，只回一句原因并让调用方按原口径继续。把「问不到」当成
「历史被重写了」的代价是删掉一整个好窗口的缓存 —— 那比不修更糟。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence, Tuple

from models import Commit, WeeklyVersionConfig, WeeklyVersionDiffCache, WeeklyVersionExcelCache, db
from services.repository_sync_window import (
    ReachableCommits,
    commit_reachable,
    reachable_commits,
)
from utils.logger import log_print


@dataclass(frozen=True)
class WindowReconciliation:
    """这一轮窗口对账做了什么。**结果对象是给调用方看的账**，不是过程。"""

    #: 过滤之后的窗口提交（未做任何对账时就是原样那一份）。
    commits: Tuple[Any, ...] = ()
    #: 是否真的按可达集合筛过（False = 可达集合未知，退回原口径）。
    filtered: bool = False
    #: 是否检测到历史被重写（有不可达的提交行或缓存行）。
    rewritten: bool = False
    #: 删掉的周版本 diff 缓存行数 / Excel 缓存行数 / 提交行数。
    dropped_cache_rows: int = 0
    dropped_excel_rows: int = 0
    dropped_commit_rows: int = 0
    #: 判不出来（未知）的提交号，只记日志。
    unknown_commits: Tuple[str, ...] = ()
    #: 给人看的一句话（进日志）。
    reason: str = ""
    reachable: Optional[ReachableCommits] = None

    def describe(self) -> str:
        if not self.reachable or not self.reachable.known:
            return f"窗口可达性对账跳过（{self.reason or '未知'}）"
        if not self.rewritten:
            return (
                f"窗口可达性对账通过（{self.reachable.describe()}），"
                f"{len(self.commits)} 个提交全部可达"
            )
        return (
            f"⚠️ 检测到当前分支历史被重写（{self.reachable.describe()}）："
            f"窗口 {len(self.commits)} 个提交可达，"
            f"清理缓存行 {self.dropped_cache_rows} 个、"
            f"Excel 缓存行 {self.dropped_excel_rows} 个、"
            f"旧提交行 {self.dropped_commit_rows} 条"
        )


def _repository_git_service(repository: Any):
    """这个仓库的 git 服务；非 git / 拿不到注入的工厂时返回 None。

    工厂由 `weekly_version_logic.configure_weekly_version_logic` 注入
    （`_get_git_service`），在函数体里读是为了断开环 —— 与 `weekly_file_sync`
    取同一份注入值的手法相同。
    """
    if getattr(repository, "type", None) != "git":
        return None
    try:
        from services.weekly_version_logic import _get_git_service

        if _get_git_service is None:
            return None
        return _get_git_service(repository)
    except Exception:  # noqa: BLE001 —— 拿不到服务只该退回原口径
        return None


def reconcile_weekly_window(
    config: WeeklyVersionConfig,
    commits_in_range: Sequence[Any],
    *,
    git_service: Any = None,
) -> WindowReconciliation:
    """把「窗口内的提交」与「当前 tip 可达的提交」对一次账。

    返回的 `commits` 是**过滤之后**的窗口提交，调用方必须用它继续（用它分组、算合并
    diff、定 latest），不能再用原始那份 —— 否则旧提交会经由缓存行再次回到页面与
    AI 的变更清单里。

    不抛异常：任何失败都退回「不过滤、不写库」，并把原因放进 `reason`。
    """
    original = tuple(commits_in_range or ())
    repository = getattr(config, "repository", None)
    if repository is None or not original:
        return WindowReconciliation(commits=original, reason="没有提交或拿不到仓库")

    try:
        service = git_service or _repository_git_service(repository)
        if service is None:
            return WindowReconciliation(commits=original, reason="拿不到该仓库的 git 服务")
        reachable = reachable_commits(
            service, branch=str(getattr(repository, "branch", "") or "")
        )
    except Exception as exc:  # noqa: BLE001 —— 对账失败不该让同步起不来
        return WindowReconciliation(commits=original, reason=f"取可达集合失败: {exc}")
    if not reachable.known:
        log_print(
            f"ℹ️ 周版本窗口可达性对账跳过（{reachable.reason}）：本轮按时间窗口口径继续，"
            "缓存里可能残留上一段历史的提交（历史被重写时会看到）",
            "WEEKLY",
        )
        return WindowReconciliation(commits=original, reason=reachable.reason, reachable=reachable)

    known: list = []
    unreachable: list = []
    unknown_ids: list = []
    for commit in original:
        commit_id = str(getattr(commit, "commit_id", "") or "")
        verdict = commit_reachable(service, reachable, commit_id)
        if verdict is False:
            unreachable.append(commit)
            continue
        # 判不出来（None）的**留下**：宁可多带一条，也不要因为一次读不到对象库
        # 就把真实的改动从窗口里抹掉。同时把它记下来，日志里如实说。
        known.append(commit)
        if verdict is None and commit_id and commit_id not in unknown_ids:
            unknown_ids.append(commit_id)

    if not unreachable:
        return WindowReconciliation(
            commits=tuple(known), filtered=True, reachable=reachable,
            unknown_commits=tuple(unknown_ids), reason="窗口内全部提交都可达",
        )

    return _replace_window(
        config, repository, reachable, tuple(known), unreachable, tuple(unknown_ids)
    )


def _replace_window(
    config: WeeklyVersionConfig,
    repository: Any,
    reachable: ReachableCommits,
    kept: Tuple[Any, ...],
    dropped: Sequence[Any],
    unknown_ids: Sequence[str],
) -> WindowReconciliation:
    """原子替换本窗口的提交索引与缓存行。**一个事务，失败整体回滚。**

    删什么（只删「已经确定不可达」的，不删「判不出来」的）：

    * `commits_log`：本窗口内、`commit_id` 不可达的行 —— 它们是旧历史混进来的那一段；
    * `weekly_version_diff_cache`：本配置里 `latest_commit_id` 不可达的行 —— 它们
      指向的提交已经不存在了，保留只会让页面与 AI 继续读到旧内容；
    * 同一批文件在本配置的 `weekly_version_excel_cache`：Excel 缓存的键含
      `(base, latest, diff_version)`，diff 缓存行都删了，这一份留着就是孤儿。

    **不动 `ai_diff_snapshot` / `ai_analysis_run`**：历史运行是冻结输入（见模块抬头）。
    """
    _win_start, _win_end = _window_bounds(config)
    unreachable_ids = {str(getattr(commit, "commit_id", "") or "") for commit in dropped}
    unreachable_ids.discard("")

    dropped_cache_rows = 0
    dropped_excel_rows = 0
    try:
        if _win_start is not None and _win_end is not None:
            dropped_commit_rows = (
                Commit.query
                .filter(
                    Commit.repository_id == repository.id,
                    Commit.commit_id.in_(sorted(unreachable_ids)),
                    Commit.commit_time >= _win_start,
                    Commit.commit_time <= _win_end,
                )
                .delete(synchronize_session=False)
            )
            # 窗口外**故意不动**（见模块抬头第 3 条）。
        else:
            dropped_commit_rows = 0

        stale_rows = [
            row for row in _config_cache_rows(config.id)
            if str(getattr(row, "latest_commit_id", "") or "") in unreachable_ids
        ]
        stale_paths = sorted({str(getattr(row, "file_path", "") or "") for row in stale_rows})
        for row in stale_rows:
            db.session.delete(row)
        dropped_cache_rows = len(stale_rows)
        if stale_paths:
            dropped_excel_rows = (
                WeeklyVersionExcelCache.query
                .filter(
                    WeeklyVersionExcelCache.config_id == config.id,
                    WeeklyVersionExcelCache.file_path.in_(stale_paths),
                )
                .delete(synchronize_session=False)
            )
        db.session.commit()
    except Exception as exc:  # noqa: BLE001 —— 对账失败退回原口径，别把同步拖垮
        db.session.rollback()
        log_print(f"⚠️ 周版本窗口对账失败，本轮不改动缓存: {exc}", "WEEKLY", force=True)
        return WindowReconciliation(
            commits=tuple(kept) or tuple(dropped),
            reason=f"对账失败: {exc}",
            reachable=reachable,
            unknown_commits=tuple(unknown_ids),
        )

    result = WindowReconciliation(
        commits=tuple(kept),
        filtered=True,
        rewritten=True,
        dropped_cache_rows=dropped_cache_rows,
        dropped_excel_rows=dropped_excel_rows,
        dropped_commit_rows=int(dropped_commit_rows or 0),
        unknown_commits=tuple(unknown_ids),
        reason="当前分支历史被重写，已按当前 tip 的可达集合替换本窗口",
        reachable=reachable,
    )
    log_print(f"🧹 [WEEKLY] {result.describe()}", "WEEKLY", force=True)
    if unknown_ids:
        # 判不出来的**如实说**：它们既没被删也没被证伪，报告里不该被当成「已确认可达」。
        log_print(
            f"⚠️ [WEEKLY] 有 {len(unknown_ids)} 个提交的可达性判不出来"
            f"（{', '.join(item[:8] for item in unknown_ids[:5])}）："
            "本轮保留了它们，未据此删除任何缓存",
            "WEEKLY",
            force=True,
        )
    return result


def group_reachable_window_files(
    config: WeeklyVersionConfig, commits_in_range: Sequence[Any], *, git_service: Any = None
) -> dict:
    """`process_weekly_version_sync` 的分组步骤：先对账，再按文件路径分组。

    **返回的就是这一轮真正要处理的文件 → 提交列表。** 调用方拿它当唯一输入 ——
    对账之后的 `commits_in_range` 在那边已经不再被任何代码读到，所以这里直接做分组、
    不把过滤后的列表回传，同步那一处因此**一行都不用多**（`weekly_version_logic.py`
    有 1800 行的硬预算，见 `tests/test_todo_split_followup_round2.py`）。

    失败方向是安全的：对账拿不到可达集合时，本函数分组的就是原样那一份（与改动前
    逐字相同的行为）。
    """
    reconciliation = reconcile_weekly_window(config, commits_in_range, git_service=git_service)
    if reconciliation.rewritten:
        # 只有「真的替换了」才打日志：对账通过是稳态（每 2 分钟一轮），逐轮打会把
        # 「历史被重写」这条**必须被看见**的记录淹掉 —— 它已经在 `_replace_window`
        # 里带 force 打过一次，这里再打会重复。
        log_print(reconciliation.describe(), "WEEKLY")
    grouped: dict = {}
    for commit in reconciliation.commits:
        grouped.setdefault(getattr(commit, "path", None), []).append(commit)
    return grouped


def _window_bounds(config: Any) -> Tuple[Optional[Any], Optional[Any]]:
    """本窗口的 (start_utc, end_utc)。换算走 `weekly_window_in_utc`（唯一实现）。"""
    try:
        from services.weekly_version_logic import weekly_window_in_utc

        return weekly_window_in_utc(config)
    except Exception:  # noqa: BLE001
        return getattr(config, "start_time", None), getattr(config, "end_time", None)


def _config_cache_rows(config_id: Any):
    """本配置现在的全部 diff 缓存行。查不动时返回空表（宁可少删，不要多删）。"""
    try:
        return WeeklyVersionDiffCache.query.filter_by(config_id=config_id).all()
    except Exception:  # noqa: BLE001
        return []
