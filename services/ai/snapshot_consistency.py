#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分析启动前的**来源一致性**核对：缓存里的提交还在不在当前 tip 上（P0）。

## 它补的是 `weekly_sync_gate` 那道闸门漏掉的那一格

现有闸门回答的是「**同步还在跑吗**」与「**有没有提交在上一轮同步之后才入库**」。
两条都成立，分析照样可以跑在一份**指向已被删除历史**的缓存上 —— 2026-09-23 实测正是
这个形态：测试仓库 master 被重建/强推成 3 个提交，`repository.last_synced_tip` 也已经
等于远端 tip，**而配置 3 的 `weekly_version_diff_cache` 仍指向 `e54c73df`、`aa3fd90b`、
`2971caf7`**（三行缓存并集 9 个提交）。AI 于是拿着一份远端已经不存在的代码做评审，
报告里还留下了「清单说 3 个提交、差异出处说 5/6 个」这条自相矛盾。

**只比 tip 是不够的**（这句话值得写在代码里，因为「tip 相等就算干净」看起来天经地义）：
上面那次 tip 早就相等了。判据必须是「**缓存行里的提交还在当前 tip 的可达集合里吗**」，
可达集合由 `services/repository_sync_window.reachable_commits` 现问 git。

## 核对哪四样（对应指引 §3.A）

| 项 | 从哪来 | 不等时 |
|---|---|---|
| 仓库 / 分支 | `WeeklyVersionConfig.repository` / `.branch` | 只记详情（见下「为什么不因分支不同就拦」） |
| tip | `git rev-parse HEAD`（本地克隆） | 进指纹；tip 变了指纹就变 |
| 可达集合指纹 | `reachable_commits(...).digest` | 进 `source_fingerprint`，供日志与人核对 |
| 缓存行的提交 | `weekly_version_diff_cache` 的 `latest/base_commit_id` | **任一不在可达集合 → 判该配置需要重新同步** |

配套的落地动作在 `services/weekly_window_reconcile.py`：那一层在同步时按可达集合
**原子替换**窗口缓存，所以本模块判出来的「需要同步」是真能被修好的 —— 不是一条
永远拦着、修不好的闸门。

## 未知一律不拦

没有本地克隆、git 不可用、对象库缺对象、拿不到 git 服务：`reachable_commits` 回
`known=False`，本模块**不判 stale**（只记一条日志）。这与平台别处「未知 ≠ 0」同一条
纪律：把「问不到」当成「历史被重写了」，代价是每一次分析都被无限期挡住，而
platform/agent 模式本来就**没有**本地克隆（平台被显式禁止 clone）。

## 为什么不因「配置声明的分支 ≠ 仓库当前分支」就拦

`WeeklyVersionConfig.branch` 是用户在配置页填的**标签**，全平台没有一处同步逻辑读它
（同步一律走 `repository.branch` 对应的本地克隆）。拿它当闸门会造出一条与数据来源
无关的新规则，误拦真实配置。所以分支只进指纹与详情 —— 它出现在日志里，人能看到，
但不单独构成「不许分析」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from services.repository_sync_window import (
    ReachableCommits,
    commit_reachable,
    reachable_commits,
)
from utils.logger import log_print

#: 一次核对最多问多少个提交的可达性。窗口里**已经确定不可达**的第一个就够判 stale，
#: 所以这个上限只影响「判不出来」那一堆的规模 —— 越过它剩下的按未知处理（不拦）。
MAX_COMMITS_PER_CHECK = 400


@dataclass(frozen=True)
class ReachabilityCheck:
    """一次来源一致性核对的结果。

    `checked=False` 表示**这次没得出任何结论**（拿不到可达集合），调用方必须按
    「不拦」处理（见模块抬头的「未知一律不拦」）。
    """

    #: 缓存行指向不可达提交的 config id。
    stale: Tuple[int, ...] = ()
    #: 真算出结论了吗（False = 未知，不许当成「全都干净」）。
    checked: bool = False
    #: 参与核对的可达集合指纹（按 repository_id 排列），供日志/排查。
    source_fingerprints: Tuple[Tuple[int, str], ...] = ()
    #: 给人看的详情行（分支不一致、判不出来的提交等）。
    details: Tuple[str, ...] = ()
    reason: str = ""

    @property
    def stale_ids(self) -> List[int]:
        return [int(item) for item in self.stale]

    def describe(self) -> str:
        if not self.checked:
            return f"来源一致性核对未得出结论（{self.reason or '未知'}）"
        if not self.stale:
            return "来源一致性核对通过：缓存里的提交都在当前 tip 的可达集合内"
        return (
            f"缓存指向了当前 tip 上不存在的提交（config_id="
            f"{','.join(map(str, self.stale))}）：这批历史已被强推/重建，"
            "必须先按当前历史重做周版本同步"
        )


def _git_service_for(repository: Any):
    """仓库的 git 服务实例；拿不到返回 None（不抛）。

    优先用 `weekly_version_logic` 注入的那一份工厂（与同步路径**同一个**工厂，
    于是同一个仓库只有一个服务实例）；没有注入时退回 `vcs_content_service`
    的进程内缓存工厂。两者都按 (id, url) 缓存，不会每次核对都新建线程池。
    """
    if getattr(repository, "type", None) != "git":
        return None
    # **平台没有这个仓库的工作副本时连实例都不建。** 这一条既是语义（没有本地历史
    # 就没有可达性可谈），也是开销：`get_git_service` 会为每个仓库建一个带线程池的
    # 服务实例，而这道闸门在每次派发/每个 tick 都会被问一次。判据用 `clone_status`
    # 而不是「目录在不在」——前者是平台自己写的账，读它不需要碰文件系统。
    if str(getattr(repository, "clone_status", "") or "").strip().lower() != "completed":
        return None
    try:
        from services.weekly_version_logic import _get_git_service

        if _get_git_service is not None:
            return _get_git_service(repository)
    except Exception:  # noqa: BLE001 —— 退回下一个工厂
        pass
    try:
        from services.vcs_content_service import get_git_service

        return get_git_service(repository)
    except Exception:  # noqa: BLE001
        return None


def repository_reachability(repository: Any) -> ReachableCommits:
    """这个仓库当前本地 tip 的可达集合；拿不到就回 `known=False` 的对象。"""
    if str(getattr(repository, "clone_status", "") or "").strip().lower() != "completed":
        return ReachableCommits(reason="平台没有这个仓库的本地工作副本（clone_status != completed）")
    service = _git_service_for(repository)
    if service is None:
        return ReachableCommits(branch="", tip="", reason="拿不到该仓库的 git 服务")
    try:
        return reachable_commits(
            service, branch=str(getattr(repository, "branch", "") or "")
        )
    except Exception as exc:  # noqa: BLE001
        return ReachableCommits(reason=f"取可达集合失败: {exc}")


def source_fingerprint(repositories: Iterable[Any]) -> str:
    """`(仓库, 分支, tip, 可达集合指纹)` 的来源指纹（空串 = 算不出来）。

    用途是**可核对**：同一份指纹意味着「判据面对的是同一条历史」。它**不进**
    `weekly_snapshot_digest` —— 那个指纹被 `AiAnalysisRun.active_key` 当幂等键用，
    换算法会让所有历史活动运行的键不再匹配（见 `services/ai/snapshot_store.py` 抬头）。
    """
    import hashlib

    parts: List[str] = []
    for repository in repositories or ():
        try:
            reachable = repository_reachability(repository)
        except Exception:  # noqa: BLE001
            continue
        if not reachable.known:
            continue
        parts.append(
            "%s|%s|%s|%s"
            % (
                getattr(repository, "id", "") or "",
                str(getattr(repository, "branch", "") or ""),
                reachable.tip,
                reachable.digest,
            )
        )
    if not parts:
        return ""
    return hashlib.sha1("\n".join(sorted(parts)).encode("utf-8")).hexdigest()


def _cache_commits(config_ids: Sequence[int]) -> Dict[int, List[str]]:
    """每个 config 的缓存行引用到的提交号（`latest` 与 `base` 都要）。

    `base_commit_id` 也算：合并 diff 是「base → latest」算出来的，base 指向一个不存在
    的提交时，那份 diff 已经没有对应的两端了。

    **只取三列并去重**，不水合 ORM 对象：这道闸门每次派发、每个 tick 都会被问一次，
    而大仓库的一个配置有上千行缓存 —— 逐行建对象在这里是纯开销（判定只需要那两列）。
    """
    from models import WeeklyVersionDiffCache

    result: Dict[int, List[str]] = {}
    try:
        rows = (
            WeeklyVersionDiffCache.query
            .filter(WeeklyVersionDiffCache.config_id.in_(list(config_ids)))
            .with_entities(
                WeeklyVersionDiffCache.config_id,
                WeeklyVersionDiffCache.latest_commit_id,
                WeeklyVersionDiffCache.base_commit_id,
            )
            .distinct()
            .all()
        )
    except Exception:  # noqa: BLE001 —— 查不动就是「没有可核对的输入」
        return result
    for row in rows:
        config_id = row[0]
        if config_id is None:
            continue
        bucket = result.setdefault(int(config_id), [])
        for value in (row[1], row[2]):
            text = str(value or "").strip()
            if text and text not in bucket:
                bucket.append(text)
    return result


def _baseline_source_notes(config: Any, reachable: ReachableCommits) -> List[str]:
    """上一份**做差基准快照**的来源核对结果（只记详情，**不据此拦截**）。

    ## 为什么它只报不拦

    基准快照指向的提交被强推掉之后，`resolve_baseline` 仍会拿它做差 —— 而条目身份
    不同，于是每个文件都被判成「变了」，结果是一次**全量重算**（正确的方向，只是贵）。
    想「拦到修好为止」的话会撞上死锁：本平台**刻意不删历史快照**（它们是历史运行的
    冻结输入，见 `services/weekly_window_reconcile.py` 的抬头），所以重做同步也不会
    让这份旧基准消失 —— 那条闸门会永远拦住每一次分析。

    所以这里回一行**说清楚现状**的详情：它进日志、进核对结果，人能看到「这一轮为什么
    贵」；而真正会挡分析的只有「当前缓存行指向不可达提交」那一条（那个重做同步能修好）。
    """
    try:
        from services.ai import snapshot_store
        from services.ai.project_config_source import build_weekly_group_key

        group_key = build_weekly_group_key(config)
        snapshot = snapshot_store.latest_sealed_snapshot(group_key)
        if snapshot is None:
            return []
        outside = snapshot_store.entries_outside_history(snapshot, reachable.commits)
    except Exception as exc:  # noqa: BLE001 —— 核对不动只是少一条详情
        return [f"配置 {getattr(config, 'id', None)} 的基准快照来源核对失败：{exc}"]
    if not outside:
        return []
    note = (
        f"⚠️ 配置 {getattr(config, 'id', None)} 的做差基准（快照 {snapshot.id}，"
        f"{snapshot.item_count} 项）有 {len(outside)} 项指向当前历史上不存在的提交："
        "下一轮按它对差会把每个文件都判成「变了」（全量重算，不是错，但贵）。"
        "**不据此拦截** —— 历史快照刻意不删，拦了会永远修不好"
    )
    log_print(f"⛔ AI 分析：{note}", "AI", force=True)
    return [note]


def check_configs(config_ids: Sequence[int]) -> ReachabilityCheck:
    """这批配置的周版本缓存，还落在当前 tip 的可达历史上吗。

    **不抛异常、不做模型调用、不写库**（会问 git）。任何读不动的地方都退回「未知」，
    由调用方按「不拦」处理。
    """

    ids = sorted({int(item) for item in config_ids if item is not None})
    if not ids:
        return ReachabilityCheck(checked=False, reason="没有配置")

    try:
        from models import WeeklyVersionConfig

        configs = WeeklyVersionConfig.query.filter(WeeklyVersionConfig.id.in_(ids)).all()
    except Exception as exc:  # noqa: BLE001
        return ReachabilityCheck(checked=False, reason=f"读配置失败: {exc}")
    if not configs:
        return ReachabilityCheck(checked=False, reason="配置不存在")

    cache_commits = _cache_commits(ids)
    stale: List[int] = []
    fingerprints: List[Tuple[int, str]] = []
    details: List[str] = []
    any_checked = False

    # 一个仓库只问一次可达集合：同一批里的多个配置常常共用仓库。
    by_repository: Dict[Any, List[Any]] = {}
    for config in configs:
        by_repository.setdefault(getattr(config, "repository_id", None), []).append(config)

    for repository_id, group in by_repository.items():
        repository = getattr(group[0], "repository", None)
        if repository is None:
            details.append(f"仓库 {repository_id} 读不到（配置与仓库对不上）")
            continue
        reachable = repository_reachability(repository)
        if not reachable.known:
            details.append(
                f"仓库 {getattr(repository, 'name', repository_id)} 的可达集合未知"
                f"（{reachable.reason}）：本次不据此拦截"
            )
            continue
        any_checked = True
        fingerprints.append((int(repository_id), reachable.digest))
        # 一个仓库只取一次服务实例（同一个实例被所有判定复用，见 `_git_service_for`）。
        service = _git_service_for(repository)
        declared_branch = str(getattr(group[0], "branch", "") or "").strip()
        if declared_branch and declared_branch != str(getattr(repository, "branch", "") or ""):
            # 只记详情（理由见模块抬头）：同步用的是仓库分支的本地克隆。
            details.append(
                f"⚠️ 配置声明的分支（{declared_branch}）与仓库当前分支"
                f"（{getattr(repository, 'branch', '')}）不同：按仓库分支核对"
            )

        for config in group:
            commits = cache_commits.get(int(config.id), [])
            if not commits:
                continue
            verdicts = [
                commit_reachable(service, reachable, item)
                for item in commits[:MAX_COMMITS_PER_CHECK]
            ]
            unknown = [item for item, verdict in zip(commits, verdicts) if verdict is None]
            if any(verdict is False for verdict in verdicts):
                stale.append(int(config.id))
                details.append(
                    f"配置 {config.id}（{getattr(config, 'name', '')}）的缓存指向了"
                    f"{sum(1 for v in verdicts if v is False)} 个不在可达集合里的提交"
                )
            if unknown:
                details.append(
                    f"配置 {config.id} 有 {len(unknown)} 个提交的可达性判不出来"
                    f"（{', '.join(item[:8] for item in unknown[:3])}）：未据此判为过期"
                )
            details.extend(_baseline_source_notes(config, reachable))

    if not any_checked:
        return ReachabilityCheck(
            checked=False,
            details=tuple(details),
            reason="；".join(details) or "所有仓库的可达集合都未知",
        )
    result = ReachabilityCheck(
        stale=tuple(stale),
        checked=True,
        source_fingerprints=tuple(fingerprints),
        details=tuple(details),
    )
    if stale:
        log_print(f"⛔ AI 分析：{result.describe()}", "AI", force=True)
    return result


def stale_config_ids(config_ids: Sequence[int]) -> List[int]:
    """`check_configs` 的薄封装：只要「哪些配置必须重新同步」这一份名单。

    这就是 `weekly_sync_gate` 要的那个口径 —— 它会被拿去**排一条 weekly_sync 任务**
    （`job_service._dispatch` 拿到 needed 就建任务），所以这里只回确定过期的那些，
    判不出来的一个都不回。
    """
    return check_configs(config_ids).stale_ids
