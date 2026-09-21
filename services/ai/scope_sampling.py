#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""这一轮分析**按什么范围跑**：变更清单怎么取样、要不要跑全量。

## 为什么单独一个文件

`services/ai_analysis_service.py` 贴着长度闸门（`scripts/check_file_length.py --strict`
在 2000 行报错），而这一块是那个文件里**纯计算最集中的一段**：去掉读数据库那几行之后，
它的全部输入是「变更清单 + 仓库清单」，输出是「列哪些文件 / 全量还是增量」。搬出来之后
「为什么这一轮只列了这些文件」只需要看这一个文件。

## 一件刻意留下的事

`_select_listed_files` **留在原文件**：它读模块级的 `MAX_LIST_CHARS`，而测试正是靠
`monkeypatch.setattr(ai_service, "MAX_LIST_CHARS", 50)` 逼出「清单长到列不下」那条退化
分支的 —— 搬到别处会让那个补丁失效，守卫变成假的绿。所以下面的常量也留在原文件。
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, Tuple

from models import Project, Repository, WeeklyVersionConfig, WeeklyVersionDiffCache, db
from services.ai.project_facts import (
    critical_path_facts,
    declared_important_tables_by_repo,
    scan_critical_paths,
)
from utils.logger import log_print

FULL_ANALYSIS_FILE_THRESHOLD = 50

FULL_ANALYSIS_RATIO_THRESHOLD = 0.30

def _repo_priority(repo: Repository) -> int:
    """取样时仓库的先手顺序：代码仓库优先于配表仓库。

    **判据只能是 `resource_type`，不能加上 `type == "git"`。** 线上两个仓库的
    `type` 都是 `git`，那一句会让「代码仓库」和「配表仓库」一起返回 2 —— 优先级
    形同虚设，`policy.sample_strategy` 写着 `priority_then_commit_count` 而实际
    只按 `commit_count` 排。

    注意这个值**只用于取样的发牌顺序**，不再被 `select_primary_weekly_config`
    复用（那里关心的是分组身份，不是取样偏好）。
    """
    resource_type = str(getattr(repo, "resource_type", "") or "").lower()
    return 2 if resource_type == "code" else 1

def _limit_items(items: List[dict], max_items: int) -> List[dict]:
    if max_items <= 0:
        return items
    return items[:max_items]

def _sample_with_repo_fairness(
    items: List[dict], max_items: int, *, repo_key: str = "repository_id"
) -> List[dict]:
    """按「各仓库轮流发牌」取样，保证没有仓库会被整个挤出清单。

    **不能只按全局排序截断。** 线上一次周版本有 767 个文件：748 个 lua（代码仓库）
    加 19 个配表，取前 200 时配表**一个都进不去** —— 而配表改的正是数值、ID、奖励
    这些评审最关心的东西。（当时的实际排序按 `commit_count` 降序，19 张配表因为
    改动次数少而排在后面。）

    做法是轮转发牌：按各仓库**优先级最高的那条**决定发牌顺序，然后每轮给每个仓库
    各发一条，直到取满或全部发完。文件少的仓库很快发完，剩下的名额自然全归文件多的
    仓库 —— 上例里配表 19 条全进，代码仓库拿走其余 181 条。

    `items` 必须**已按全局优先级降序排好**：桶内顺序、以及返回值的展示顺序都依赖它。
    """
    if max_items <= 0 or len(items) <= max_items:
        return list(items)

    buckets: Dict[object, List[int]] = {}
    for position, item in enumerate(items):
        buckets.setdefault(item.get(repo_key), []).append(position)

    # 每个仓库的第一条就是它优先级最高的那条（items 已全局排序），
    # 用它代表这个仓库的先手顺序。sorted 是稳定的，同优先级时保持首次出现的顺序。
    deal_order = sorted(
        buckets.values(),
        key=lambda positions: -int(items[positions[0]].get("priority") or 0),
    )

    chosen: List[int] = []
    round_index = 0
    while len(chosen) < max_items:
        dealt = False
        for positions in deal_order:
            if round_index >= len(positions):
                continue
            chosen.append(positions[round_index])
            dealt = True
            if len(chosen) >= max_items:
                break
        if not dealt:      # 所有仓库都发完了
            break
        round_index += 1

    chosen.sort()          # 回到全局优先级顺序，展示口径与改动前一致
    return [items[position] for position in chosen]

def _summarize_weekly_files(
    configs: List[WeeklyVersionConfig],
    last_analyzed_at: Optional[datetime],
) -> Tuple[dict, dict, Optional[str]]:
    config_ids = [cfg.id for cfg in configs]
    total_query = WeeklyVersionDiffCache.query.filter(
        WeeklyVersionDiffCache.config_id.in_(config_ids)
    )
    total_files = total_query.count()

    if last_analyzed_at:
        delta_query = total_query.filter(WeeklyVersionDiffCache.updated_at > last_analyzed_at)
    else:
        delta_query = total_query

    delta_entries = delta_query.all()
    delta_count = len(delta_entries)

    if last_analyzed_at and delta_count == 0:
        return {}, {}, "no_change"

    repo_lookup = {cfg.repository_id: cfg.repository for cfg in configs}
    repo_summaries: Dict[int, dict] = {}
    delta_files: List[dict] = []
    total_files_by_repo: Dict[int, int] = {}

    # 关键路径的事实源有两处（语义不重合，命中任一）：项目知识包里的**路径模式**
    # （取不到＝平台默认），与**每个仓库自己声明的「重点表名」**（界面上那一栏）。
    project = db.session.get(Project, configs[0].project_id) if configs else None
    facts = critical_path_facts(getattr(project, "code", None))
    if facts.warning:
        log_print(f"⚠️ AI 分析：关键路径声明有问题 —— {facts.warning}", "AI", force=True)
    tables_by_repo = declared_important_tables_by_repo(repo_lookup)

    for entry in delta_entries:
        repo = repo_lookup.get(entry.repository_id)
        repo_name = repo.name if repo else f"repo-{entry.repository_id}"
        repo_priority = _repo_priority(repo) if repo else 1
        total_files_by_repo[entry.repository_id] = total_files_by_repo.get(entry.repository_id, 0) + 1

        delta_files.append(
            {
                "repository_id": entry.repository_id,
                "repository_name": repo_name,
                "priority": repo_priority,
                "file_path": entry.file_path,
                "file_type": entry.file_type,
                "latest_commit_id": entry.latest_commit_id,
                "commit_count": entry.commit_count,
                "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
            }
        )

    for cfg in configs:
        repo = cfg.repository
        total_count = WeeklyVersionDiffCache.query.filter_by(config_id=cfg.id).count()
        repo_summaries[cfg.repository_id] = {
            "repository_id": cfg.repository_id,
            "repository_name": repo.name,
            "resource_type": getattr(repo, "resource_type", None),
            "priority": _repo_priority(repo),
            "total_files": total_count,
            "delta_files": total_files_by_repo.get(cfg.repository_id, 0),
        }

    delta_files.sort(
        key=lambda item: (item.get("priority", 1), item.get("commit_count", 0), item.get("file_path", "")),
        reverse=True,
    )

    # 扫一遍关键路径。这一行日志是这次要补的「留痕」本身：在这之前，升级为全量的理由
    # （甚至「一条都没命中」这件事）在任何地方都看不到。
    scan = scan_critical_paths(
        (
            (entry.file_path, tables_by_repo.get(entry.repository_id, ()))
            for entry in delta_entries
        ),
        facts=facts,
    )
    if scan.hit:
        log_print(scan.log_line(), "AI", force=True)

    summary = {
        # `total_files` = **窗口总数**（这个周版本一共有过多少改动文件），`delta_files` =
        # 其中这一次装进输入的。`scope=incremental` 时两者差很多（实测 847 vs 19），
        # 所以给模型的「本次变更共 N 个」必须单独一个键（见 `batch_files`）。
        "total_files": total_files,
        "delta_files": delta_count,
        # **给模型的那一份总数**：本批次（这次装进输入）的文件数。喂错它，提示词那句
        # 「还有 M 个的名字没列出来，但你可以读到它们的 diff」就会宣称一批白名单里根本
        # 没有的文件「读得到」，模型点名索取时请求被 `protocol` 静默丢掉（只进 trace，
        # 没有任何回执），于是它把一个不存在的取数缺口写成免责声明。
        "batch_files": delta_count,
        # 窗口总数**另存一份**：`focus` 会把 `total_files` 改写成筛后的条数，之后就没有
        # 任何一处还记着「这个版本一共改了多少」—— 而「这次输入覆盖了多少分之多少」
        # 正是靠它（见 `change_set._scope_note`）。
        "window_files": total_files,
        "critical_paths": scan.hit,
        # 命中的前几条（含理由）与模式来源：`_decide_scope` 只回一个原因串，具体是哪条
        # 路径、依据是谁声明的，只有这里才有。来源那一条哪怕没命中也要记 —— 否则
        # 「项目声明了『没有路径模式』」与「项目什么都没声明」在结果里分不开。
        "critical_path_hits": list(scan.reasons),
        "critical_path_source": scan.source,
    }
    return summary, {
        "repos": list(repo_summaries.values()),
        "delta_files": delta_files,
    }, None

def has_weekly_changes(config_ids: List[int], last_analyzed_at: Optional[datetime]) -> bool:
    if not config_ids:
        return False
    query = WeeklyVersionDiffCache.query.filter(WeeklyVersionDiffCache.config_id.in_(config_ids))
    if last_analyzed_at:
        query = query.filter(WeeklyVersionDiffCache.updated_at > last_analyzed_at)
    return query.first() is not None


def weekly_snapshot_digest(config_ids: List[int]) -> str:
    """这一批配置**当前这一份快照**的内容指纹（「分析的是什么」的纯函数）。

    ## 它解决的是时间水位线解决不了的一件事

    `has_weekly_changes(config_ids, last_analyzed_at)` 用的是时间：缓存行的
    `updated_at` 晚于上次分析就算「有新变化」。而 `last_analyzed_at` **只在跑完整了
    的 run 上推进**（见 `_update_weekly_state`：降级的 run 不推进，否则模型没真读到的
    变更会被标成「已看过」，线上真出过 767 个文件里 748 个没读到却被标成已分析）。
    于是「降级跑完 → 水位线不动 → 下一个周期又判有新变化 → 同一份输入再分析一遍」
    这条循环会一直转，每小时烧一次全量分析，而输入一字未变。

    指纹取自同一条查询的**内容身份**：每个文件的 `(base_commit_id, latest_commit_id,
    diff_version)` 排序后取 sha1。这三者决定合并 diff 的输入，也就决定了这一轮分析
    能看到的全部内容；它们没变，再跑一遍只会得到同一份结果。只哈希**这个三元组**，
    不哈希 `updated_at`：后者是「什么时候写的」，不是「写了什么」。

    用途见 `services/task_worker_service.schedule_weekly_ai_analysis_tasks`：指纹与
    `AiWeeklyAnalysisState.last_snapshot_digest` 相同就跳过这一轮自动分析。
    **只拦自动路径** —— 手动触发是用户的明确动作，照跑。
    """
    import hashlib

    if not config_ids:
        return ""
    rows = (
        WeeklyVersionDiffCache.query
        .filter(WeeklyVersionDiffCache.config_id.in_(list(config_ids)))
        .with_entities(
            WeeklyVersionDiffCache.file_path,
            WeeklyVersionDiffCache.base_commit_id,
            WeeklyVersionDiffCache.latest_commit_id,
            WeeklyVersionDiffCache.diff_version,
        )
        .all()
    )
    if not rows:
        return ""
    parts = sorted(
        "%s|%s|%s|%s" % (path or "", base or "", latest or "", version or "")
        for path, base, latest, version in rows
    )
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()

def snapshot_already_analyzed(state, config_ids: List[int]) -> bool:
    """这一份快照**就是**上次分析过的那一份吗（自动分析据此跳过）。

    调用方是调度器（`services/task_worker_service.schedule_weekly_ai_analysis_tasks`）
    与它的测试。三条都要成立才算「分析过了」：

    * 有状态行、且指纹非空 —— 老库里的行没有这一列（读出来是 NULL），
      此时**判不了**，必须按「没分析过」走，否则会把该跑的分析全跳过；
    * 指纹相同 —— 输入逐字未变；
    * 不涉及「上次跑得怎么样」—— 降级也算分析过了（同一份输入重跑一遍不会有不同结果，
      这一条正是为了让「降级 → 水位线不动 → 每小时重跑」那条循环停下来）。

    **手动触发不经过这里**：那是用户的明确动作，照跑。
    """
    if state is None:
        return False
    stored = str(getattr(state, "last_snapshot_digest", "") or "").strip()
    if not stored:
        return False
    return stored == weekly_snapshot_digest(config_ids)


def _decide_scope(summary: dict, last_analyzed_at: Optional[datetime]) -> Tuple[str, str]:
    if not last_analyzed_at:
        return "full", "first_run"
    delta_count = int(summary.get("delta_files") or 0)
    total_count = int(summary.get("total_files") or 0)
    if total_count <= 0:
        return "full", "empty_total"
    ratio = delta_count / max(total_count, 1)
    critical_hit = bool(summary.get("critical_paths"))
    if delta_count >= FULL_ANALYSIS_FILE_THRESHOLD:
        return "full", "delta_count_high"
    if ratio >= FULL_ANALYSIS_RATIO_THRESHOLD:
        return "full", "delta_ratio_high"
    if critical_hit:
        return "full", "critical_path_detected"
    return "incremental", "delta_small"
