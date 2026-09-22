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

from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Dict, List, Optional, Sequence, Tuple

from models import Project, Repository, WeeklyVersionConfig, WeeklyVersionDiffCache, db
from services.ai import snapshot_store
from services.ai.header_scope import header_scope_fingerprint, scoped_version
from services.ai.project_facts import (
    critical_path_facts,
    declared_important_tables_by_repo,
    scan_critical_paths,
)
from utils.logger import log_print

FULL_ANALYSIS_FILE_THRESHOLD = 50

FULL_ANALYSIS_RATIO_THRESHOLD = 0.30
DEPENDENCY_MAX_FILES = 50


def _same_stem_dependency_entries(entries, roots, *, already, limit=DEPENDENCY_MAX_FILES):
    """为变更根补齐同名生成物/源表；只做一跳，且不把它冒充本轮改动。"""
    stems = {
        PurePosixPath(str(getattr(item, "file_path", "") or "").replace("\\", "/")).stem.lower()
        for item in roots
        if getattr(item, "file_path", None)
    }
    if not stems or limit <= 0:
        return []
    result = []
    for entry in entries:
        key = (entry.config_id, entry.file_path)
        if key in already:
            continue
        stem = PurePosixPath(str(entry.file_path or "").replace("\\", "/")).stem.lower()
        if stem and stem in stems:
            result.append(entry)
            if len(result) >= limit:
                break
    return result

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

def _as_naive_utc(value: Optional[datetime]) -> Optional[datetime]:
    """带时区的值折成 naive UTC。**不能直接与库里的值相减**（一个有 tz 一个没有会抛
    `TypeError`），而 SQLite 读回来的行是 naive 的。"""
    if value is None:
        return None
    if getattr(value, "tzinfo", None) is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)

def _identity_of(entry, scope: str = "") -> tuple:
    """一条缓存行的**内容身份** —— 做差比的就是这三个（不是 `updated_at`）。

    `scope` 是这批配置的比较口径（表头坐标 / 多套表头方案）。**必须与快照那一侧
    在同一个位置并上**（`services/ai/header_scope.scoped_version`）：快照条目在写入时
    已经并过，这里不并的话两边永远不相等 —— 那不是「漏报一次」，而是**每一个文件**
    都被判成变了（或都没变），整个增量做差失真。
    """
    return (entry.base_commit_id, entry.latest_commit_id,
            scoped_version(entry.diff_version, scope))

def _select_delta_entries(entries: List, baseline, scope: str = "") -> List:
    """按基准挑出「这次要装进输入」的那些条目。

    ## 基准的三种形态（见 `snapshot_store.BaselineSnapshot`）

    * `None`：没有任何基线 —— 首跑，整个窗口都是「新变化」；
    * `BaselineSnapshot`：按 **manifest 做差**（正常路径）。逐个比内容身份，`updated_at`
      不参与 —— 那是写库时刻，同步每 2~3 分钟跑一遍，任何一次重算都会顶高它；
    * `datetime`：**过渡态**，这一组还没有任何冻结快照、但有一条历史时间水位线。
      判据退回 `updated_at > 水位线`（改动前的行为）。快照一旦建起来就走不到这里。
    """
    if baseline is None:
        return list(entries)
    items = getattr(baseline, "items", None)
    if not isinstance(items, dict):
        watermark = _as_naive_utc(baseline)
        return [
            entry for entry in entries
            if entry.updated_at is not None
            and watermark is not None
            and _as_naive_utc(entry.updated_at) > watermark
        ]
    delta = []
    for entry in entries:
        if items.get((entry.config_id, entry.file_path)) != _identity_of(entry, scope):
            delta.append(entry)
    return delta

def _compensation_entries(
    entries: List,
    *,
    base_run,
    already: set,
    facts,
    tables_by_repo: Dict[int, Sequence[str]],
    repo_lookup: Dict[int, Repository],
    limit: int,
) -> List:
    """**补偿集**：上一轮装了、却一个证据都没取到的那些文件。

    复测文档 AI-P0-02：「本次需要分析 = target - base + base_run 中未覆盖且按风险策略
    需要补偿的项目」。降级运行的结论可以当基线，但它那 94% 没取到证据的文件**不许**就
    这么算了 —— 它们按风险排序后回到这一轮的输入里，而且会在 payload 里**单独列出来**
    （`compensation_files`），不混进「变化了什么」那本账。

    ## 风险策略

    命不命中关键路径（项目声明的重点表 / 路径模式）是第一判据，其次才是仓库优先级与
    提交次数 —— 补偿名额有限，先补「改得多、又落在危险路径上」的那些。

    ## 未知就是不补

    `uncovered_paths` 在**没有逐轮明细**时返回 `None`（未知）。那时不补任何东西：
    把未知当成「全都没看到」会把整份窗口再喂一遍 —— 那正是这次要修的那件事，
    只是换了个名字。
    """
    if base_run is None or limit <= 0:
        return []
    uncovered = snapshot_store.uncovered_paths(base_run)
    if not uncovered:
        return []
    candidates = [
        entry for entry in entries
        if entry.file_path in uncovered
        and (entry.config_id, entry.file_path) not in already
    ]
    if not candidates:
        return []

    def _priority(entry) -> int:
        repo = repo_lookup.get(entry.repository_id)
        return _repo_priority(repo) if repo else 1

    def _order(entry):
        tables = tables_by_repo.get(entry.repository_id, ())
        hit = bool(facts.why(entry.file_path, tables))
        return (hit, _priority(entry), int(entry.commit_count or 0), entry.file_path)

    # 风险排序（`_order` 的第一项就是「命不命中关键路径」）。候选可能有上千条（线上
    # 1009 个文件里 967 个没取到证据），但这一步只做一次比较排序，代价与候选数同阶。
    candidates.sort(key=_order, reverse=True)
    return candidates[:limit]

def _summarize_weekly_files(
    configs: List[WeeklyVersionConfig],
    baseline,
    *,
    base_run=None,
    compensation_max: int = snapshot_store.DEFAULT_COMPENSATION_MAX_FILES,
) -> Tuple[dict, dict, Optional[str]]:
    """这一轮的输入账：窗口里有多少、这次装进输入多少、关键路径命中没有。

    ## `baseline` 是**做差基准**，不是时间水位线

    改动前这里收的是 `last_analyzed_at` 并筛 `updated_at > 水位线` —— 而那是**写库时刻**，
    同步每 2~3 分钟跑一遍。实测 Run 20 相对 Run 15 只有 45 个路径身份变化，却因为水位线
    为空（降级运行不推进它）而把 1009 个文件全放进了白名单。

    现在按 manifest 做差（见 `_select_delta_entries`），再加上补偿集
    （`_compensation_entries`）。**summary 的键一个都没改**：`total_files` / `delta_files` /
    `batch_files` / `window_files` / `critical_paths` / `critical_path_hits` /
    `critical_path_source` 的语义与拼写都是既有读侧（`change_set._scope_note`、
    `coverage_ledger._inventory`、`_decide_scope`）按名字取的。
    """
    config_ids = [cfg.id for cfg in configs]
    total_query = WeeklyVersionDiffCache.query.filter(
        WeeklyVersionDiffCache.config_id.in_(config_ids)
    )
    total_files = total_query.count()

    entries = total_query.all()
    delta_entries = _select_delta_entries(
        entries, baseline, header_scope_fingerprint([cfg.id for cfg in configs]),
    )

    repo_lookup = {cfg.repository_id: cfg.repository for cfg in configs}
    repo_summaries: Dict[int, dict] = {}
    total_files_by_repo: Dict[int, int] = {}

    # 关键路径的事实源有两处（语义不重合，命中任一）：项目知识包里的**路径模式**
    # （取不到＝平台默认），与**每个仓库自己声明的「重点表名」**（界面上那一栏）。
    project = db.session.get(Project, configs[0].project_id) if configs else None
    facts = critical_path_facts(getattr(project, "code", None))
    if facts.warning:
        log_print(f"⚠️ AI 分析：关键路径声明有问题 —— {facts.warning}", "AI", force=True)
    tables_by_repo = declared_important_tables_by_repo(repo_lookup)

    delta_keys = {(entry.config_id, entry.file_path) for entry in delta_entries}
    compensation = _compensation_entries(
        entries,
        base_run=base_run,
        already=delta_keys,
        facts=facts,
        tables_by_repo=tables_by_repo,
        repo_lookup=repo_lookup,
        limit=int(compensation_max or 0),
    )
    if compensation:
        delta_entries = delta_entries + compensation

    dependency = _same_stem_dependency_entries(
        entries,
        delta_entries,
        already={(entry.config_id, entry.file_path) for entry in delta_entries},
    )
    if dependency:
        delta_entries = delta_entries + dependency

    if baseline is not None and not delta_entries:
        return {}, {}, "no_change"

    delta_files: List[dict] = []
    compensated_keys = {(entry.config_id, entry.file_path) for entry in compensation}
    dependency_keys = {(entry.config_id, entry.file_path) for entry in dependency}
    for entry in delta_entries:
        repo = repo_lookup.get(entry.repository_id)
        repo_name = repo.name if repo else f"repo-{entry.repository_id}"
        repo_priority = _repo_priority(repo) if repo else 1
        total_files_by_repo[entry.repository_id] = total_files_by_repo.get(entry.repository_id, 0) + 1

        item = {
            "repository_id": entry.repository_id,
            "repository_name": repo_name,
            "priority": repo_priority,
            "file_path": entry.file_path,
            "file_type": entry.file_type,
            "latest_commit_id": entry.latest_commit_id,
            "commit_count": entry.commit_count,
            "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
        }
        if (entry.config_id, entry.file_path) in compensated_keys:
            # **明确列出来**：这一条不是「这次变了」，而是「上一轮没看到，这轮补上」。
            # 两条不分开写，读者会把补偿项当成新变更，于是「这周改了什么」那本账就错了。
            item["source"] = "compensation"
            item["reason"] = "上一轮装进了输入但没有取到任何证据"
        elif (entry.config_id, entry.file_path) in dependency_keys:
            item["source"] = "dependency"
            item["reason"] = "与本轮变更文件同名的源表或生成物，作为一跳依赖核查项"
        delta_files.append(item)

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
    #
    # **只扫「变化了的那部分」**，不含补偿项：`critical_paths` 是「这一轮的变化里有没有
    # 落在危险路径上的」，把它算成「输入里有没有危险文件」会让每一次补偿都升级为全量。
    scan = scan_critical_paths(
        (
            (entry.file_path, tables_by_repo.get(entry.repository_id, ()))
            for entry in delta_entries
            if (entry.config_id, entry.file_path) not in compensated_keys
            and (entry.config_id, entry.file_path) not in dependency_keys
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
        "delta_files": len(delta_files),
        # **给模型的那一份总数**：本批次（这次装进输入）的文件数。喂错它，提示词那句
        # 「还有 M 个的名字没列出来，但你可以读到它们的 diff」就会宣称一批白名单里根本
        # 没有的文件「读得到」，模型点名索取时请求被 `protocol` 静默丢掉（只进 trace，
        # 没有任何回执），于是它把一个不存在的取数缺口写成免责声明。
        "batch_files": len(delta_files),
        # 窗口总数**另存一份**：`focus` 会把 `total_files` 改写成筛后的条数，之后就没有
        # 任何一处还记着「这个版本一共改了多少」—— 而「这次输入覆盖了多少分之多少」
        # 正是靠它（见 `change_set._scope_note`）。
        "window_files": total_files,
        # 这轮输入里有多少条是**补偿项**（上一轮没取到证据的那些）。单独一个计数是为了
        # 让「这周真的改了多少」与「补了多少」在报告里能分开读。
        "compensation_files": len(compensation),
        "dependency_files": len(dependency),
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
        "compensation_files": [
            item for item in delta_files if item.get("source") == "compensation"
        ],
        "dependency_files": [
            item for item in delta_files if item.get("source") == "dependency"
        ],
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
    # **比较口径也要进身份**（见 `services/ai/header_scope.py`）：上面那四项全在说
    # 「文件变了没有」，没有一项说「这些文件该怎么读」。改了表头配置之后缓存会重算，
    # 而文件确实没变、四元组逐字相同 ⇒ 这份判据会说「输入一字未变」而跳过自动分析 ——
    # 可模型能看到的东西（列名、哪些行算数据）已经换了一套。
    scope = header_scope_fingerprint(config_ids)
    parts = sorted(
        "%s|%s|%s|%s|%s" % (path or "", base or "", latest or "", version or "", scope)
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


def _decide_scope(summary: dict, baseline) -> Tuple[str, str]:
    """这一轮跑全量还是增量，以及**为什么**（第二返回值进 run 的 `policy.reason`）。

    `baseline` 是**做差基准**（`snapshot_store.BaselineSnapshot`），不是 `last_analyzed_at`。

    改动前这里判的是 `if not last_analyzed_at`，于是三件事被绑在同一个值上：降级运行
    不推进水位线（有意），代价是**首次/增量分流**跟着一起废掉 —— 手工路径看到 `None`
    就按 `first_run` 把约 1000 个文件全放进白名单。现在判的是「有没有可用的基线」：
    快照在就按 manifest 做差，没有才叫首跑。

    第二参数仍然是「能判真假」的（既有调用点与测试按那个形状传），只是**它的含义变了**。
    """
    if not baseline:
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
