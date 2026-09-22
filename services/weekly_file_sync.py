#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""周版本「按文件」这一步的产物：结果聚合 + 基准版本回查。

## 为什么单独一个模块

`services/weekly_version_logic.py` 有一道硬性行数预算（1800 行，钉在
`tests/test_todo_split_followup_round2.py` 里）。为了让日志不再按文件数刷屏，那边
新增了「把逐文件结果聚合成一行」的逻辑，正好越线。于是把这两件**与路由、与缓存
编排都无关**的事情挪出来：

  * `WeeklyFileSyncResult` / `describe_weekly_file_totals`：纯数据 + 纯格式化，
    没有任何外部依赖；
  * `get_real_base_commit_from_vcs`：一次独立的 VCS 回查 —— 文件里唯一一处
    「翻历史、必要时补一条 Commit」的逻辑；
  * `weekly_cache_is_unchanged` / `annotate_same_instant_order`：同样是**逐文件**这一步
    才有的两件事 —— 「这次算出来的东西和库里那行一样吗」与「同刻提交谁在前」。
    两者都在「读缓存行 / 问 git」的边上，与路由和缓存编排无关。

## 调用方向

`weekly_version_logic` 从本模块导入（并在 `app.py` 的用法上保持同名可见）。
反向只有 `get_real_base_commit_from_vcs` 函数体内那一处延迟导入，见那里的说明。
"""

import traceback
from dataclasses import dataclass

from models import Commit, db
from services.log_sampling import (
    OUTCOME_MISS,
    flush_log_sampling,
    log_sampled,
)
from utils.logger import log_print


@dataclass(frozen=True)
class WeeklyFileSyncResult:
    """**一个文件**这次同步干了什么。只用于日志聚合，不参与任何判断。

    为什么要有它：这一层原本把「更新了哪个文件的缓存」「跳过了哪个文件的 Excel 缓存」
    逐条打出来，而它是**按文件调用**的 —— 条数与仓库里的文件数成正比（实测 833 个
    文件约 4,165 行）。把结果回给调用方、由它**聚合成一行**，明细才能在默认配置下
    安静下来（见 `utils/logger._DEFAULT_OFF_CATEGORIES`）。

    字段是**几条互相独立的轴**，不是单选：一个文件可以同时「更新了缓存」且「需要
    Excel 缓存」且「回查了 VCS 基准」，用一个枚举标签表达不了。
    """

    created: bool = False
    updated: bool = False
    # 该文件的 Excel 缓存这一轮的去向：'created' / 'failed' / 'skipped'。
    excel: str = 'skipped'
    # 基准版本是从 VCS 回查来的（库里没有）。**这一条要计数**：它意味着这个文件多走了
    # 一次 Git/SVN 查询，是同步慢的主因之一 —— 逐条打是刷屏，完全不记则是把线索丢了。
    vcs_base_lookup: bool = False


def describe_weekly_file_totals(totals: dict, file_count: int) -> str:
    """把逐文件的结果聚合成**一行**。

    这一行的信息量比它替代掉的那几千行更大：它回答的是「这次同步干了什么」
    （几个新建、几个更新、几个要生成 Excel 缓存、几个回查了 VCS），而那几千行回答的
    是「第 517 个文件叫什么名字」—— 后者只有在排查具体某个文件时才需要，
    所以它们降级到 `DETAIL`（默认关），而不是消失。

    零值的项不出现：`（新建 0 / 更新 833）` 里的「新建 0」没有信息量，而这一行会被
    每次同步打一遍。全部为零时说明这轮什么都没做，直接说清楚。

    **它同时是这一轮采样的收口点。** 本模块里那几条逐文件的日志（`weekly.vcs_*`）
    只是累计，没有作用域就不会自己冒出来；本函数每次同步**恰好被调用一次**（在逐文件
    循环之后、同一个线程里），所以在这里 `flush_log_sampling()` 就能把「这轮扫了多少、
    命中多少」结清一次，而不必去动 task_worker 那边的入口。
    返回值不受影响 —— 这是写日志，不是算结果。
    """
    parts = []
    if totals.get('created'):
        parts.append(f"新建 {totals['created']}")
    if totals.get('updated'):
        parts.append(f"更新 {totals['updated']}")
    if totals.get('excel'):
        parts.append(f"生成 Excel 缓存 {totals['excel']}")
    if totals.get('excel_failed'):
        parts.append(f"❌ Excel 缓存失败 {totals['excel_failed']}")
    # VCS 回查单独说：它是「同步为什么慢」的答案，而正常情况下它应该是 0 ——
    # 库里查不到基准版本意味着历史被截断或这是个新文件。
    if totals.get('vcs'):
        parts.append(f"回查 VCS 基准 {totals['vcs']}")
    # 收口这一轮被采样掉的逐文件日志（见 docstring）：计数不结清就等于把日志删了。
    flush_log_sampling()
    if not parts:
        return f"{file_count} 个文件，无需更新"
    return f"{file_count} 个文件（{'、'.join(parts)}）"


def weekly_cache_is_unchanged(
    existing_cache, payload_json, base_commit, latest_commit, commits, diff_version
):
    """这一行缓存与本次同步算出来的内容**逐字相同**吗（用于跳过无谓的写入）。

    比的是「重算一遍会不会得到同样的东西」，所以除了 payload 本身，还要比输入：
    base / latest / 口径版本 / 提交条数。任何一项对不上都返回 False（照旧写库）——
    这个判据只用来**跳过没必要的写**，判错的方向必须落在「多写一次」上。

    为什么要跳过：本表逐文件写一次，`updated_at`（`onupdate`）就被顶高一次，而 AI
    分析的变更判据读的正是它（`services/ai/scope_sampling._summarize_weekly_files`
    筛 `updated_at > last_analyzed_at`）。同步每 2~3 分钟跑一遍，于是平台**永远**认为
    「有新变化」：既不停调度新的自动分析，手动触发也总是真跑而不是回放上次结论。
    跳过写入之后，那一列恢复它字面上的语义 ——「内容最后变化的时刻」。

    `diff_version` 由调用方传入（不在这里 import）：它是比较口径的版本号，来源是
    `weekly_version_logic._current_diff_logic_version`，而那边要从本模块取
    `WeeklyFileSyncResult`，反向再 import 就成环了。
    """
    if existing_cache.cache_status != 'completed':
        return False
    if existing_cache.diff_version != diff_version:
        return False
    if (existing_cache.base_commit_id or None) != (base_commit.commit_id if base_commit else None):
        return False
    if existing_cache.latest_commit_id != latest_commit.commit_id:
        return False
    if (existing_cache.commit_count or 0) != len(commits):
        return False
    return (existing_cache.merged_diff_data or '') == payload_json


def annotate_same_instant_order(repository, commits):
    """给「同一 commit_time 的多个提交」按 git 拓扑定序（写进 commit_ordering 的缓存）。

    没有平局时一次 git 都不调（见 `commit_ordering.has_same_instant_commits`），所以逐文件
    的读侧回退路径也能安全地带上它。**任何失败都不抛**：定序失败沿用数据库次序，
    与改动前一致 —— 这条路只用来更接近真相，不用来报错。

    为什么需要它：机器人提交会把 committer date 打成同一时刻，平局时原先落回数据库行序，
    而那是**入库顺序**，实测就出现「父提交被当成 latest_commit_id」。判据与实测记录见
    `services/commit_ordering.py`。
    """
    # `_get_git_service` 是 weekly_version_logic 那边 `configure_weekly_version_logic`
    # 注入的运行时依赖，在函数体里导入是为了断开环（那边要从本模块取 WeeklyFileSyncResult）。
    # 延迟到调用时读，拿到的也是注入后的最新值 —— 与 `get_real_base_commit_from_vcs`
    # 取 `_get_svn_service` 是同一个理由。
    from services.commit_ordering import annotate_same_instant_order as _annotate
    from services.weekly_version_logic import _get_git_service

    try:
        if getattr(repository, 'type', None) != 'git' or _get_git_service is None:
            return 0
        return _annotate(_get_git_service(repository), commits)
    except Exception as exc:
        log_print(f"⚠️ 同刻提交定序失败（沿用数据库次序）: {exc}", 'WEEKLY', force=True)
        return 0


def annotate_topology_order(repository, commits):
    """给**整组提交**按 git 拓扑定序（写进 commit_ordering 的缓存）。

    与 `annotate_same_instant_order` 的分工：那个治「同一时刻的多个提交」，这个治
    **回填日期** —— 工具提交时带上原始日期，子提交可能比父提交「旧」，没有平局也照样排反。
    排反的后果是合并 diff 自己和自己比（见 `services/commit_ordering.py`）。

    代价是每轮一次 `git rev-list --topo-order`，所以只在这里（一次同步、一次读回退）
    调用，不要放进逐文件的循环。**任何失败都不抛**：定序失败退回时间序，同步照跑。
    """
    from services.commit_ordering import annotate_topology_order as _annotate
    from services.weekly_version_logic import _get_git_service

    try:
        if getattr(repository, 'type', None) != 'git' or _get_git_service is None:
            return 0
        return _annotate(_get_git_service(repository), commits)
    except Exception as exc:
        log_print(f"⚠️ 提交拓扑定序失败（沿用时间序）: {exc}", 'WEEKLY', force=True)
        return 0


def get_real_base_commit_from_vcs(config, file_path):
    """从Git/SVN获取文件的真实基准版本提交"""
    # 这两个名字还在 weekly_version_logic 里：`weekly_window_in_utc` 是窗口换算的
    # **唯一**实现（在这里重写一遍就是把当初那个 8 小时偏移的 bug 再种一次），
    # `_get_svn_service` 是那边 `configure_weekly_version_logic` 注入的运行时依赖。
    # 在函数体里导入是为了断开环：那边要从本模块取 `WeeklyFileSyncResult`。
    # 延迟到调用时读，拿到的也是注入后的最新值（本仓库 `ThreadedGitService` 同法）。
    from services.weekly_version_logic import _get_svn_service, weekly_window_in_utc

    try:
        repository = config.repository
        # 根据仓库类型选择相应的服务
        if repository.type == 'git':
            from services.threaded_git_service import ThreadedGitService
            vcs_service = ThreadedGitService(
                repository.url,
                repository.root_directory,
                repository.username,
                repository.token,
                repository
            )
        elif repository.type == 'svn':
            vcs_service = _get_svn_service(repository)
        else:
            log_print(f"不支持的仓库类型: {repository.type}", 'WEEKLY', force=True)
            return None

        # 获取文件的完整提交历史
        #
        # 这一行是**按文件**打的（本函数按文件调用），实测一次同步 272 行；下面两条
        # 「未找到」同理。三条一起采样之后，本轮扫了多少、命中多少由三行汇总回答：
        #   获取文件提交历史: 处理 N 次
        #   未找到文件提交历史: 处理 A 次，其中 A 次未命中
        #   未找到周版本开始前的提交: 处理 B 次，其中 B 次未命中
        # 于是「回查到了基准」= N − A − B（命中那一次另有 `📝 创建新的基准提交记录`
        # 明细，不必从这三个数里推）。计数点在这个同步**末尾**由
        # `describe_weekly_file_totals` 收口，见那里的说明。
        log_sampled(
            'weekly.vcs_history', f'{repository.type.upper()}获取文件提交历史',
            f"🔍 从{repository.type.upper()}获取文件提交历史: {file_path}",
            log_type='WEEKLY',
        )
        if repository.type == 'git':
            # Git: 获取文件的提交历史
            commits_data = vcs_service.get_file_commit_history(file_path, limit=100)
        else:
            # SVN: 获取文件的提交历史
            commits_data = vcs_service.get_file_history(file_path, limit=100)
        if not commits_data:
            log_sampled(
                'weekly.vcs_no_history', '未找到文件提交历史',
                f"📭 {repository.type.upper()}中未找到文件 {file_path} 的提交历史",
                outcome=OUTCOME_MISS, log_type='WEEKLY',
            )
            return None

        # 查找周版本开始时间之前的最后一个提交
        from datetime import timezone
        base_commit_data = None
        for commit_data in commits_data:
            commit_time = commit_data.get('commit_time')
            if commit_time:
                if commit_time.tzinfo is None:
                    commit_time = commit_time.replace(tzinfo=timezone.utc)
                # config.start_time 是北京墙钟，不能按 UTC 解释（原注释「假设为UTC」正是窗口
                # 偏移 8 小时的根源）。这里只取起点，故走 weekly_window_in_utc 解包 —— 直接调
                # beijing_window_to_utc_naive 会因少传 end_time 抛 TypeError 而被末尾 except 吞掉。
                config_start_time, _ = weekly_window_in_utc(config)
                if config_start_time is None:
                    continue
                config_start_time = config_start_time.replace(tzinfo=timezone.utc)
                if commit_time < config_start_time:
                    base_commit_data = commit_data
                    break

        if not base_commit_data:
            log_sampled(
                'weekly.vcs_no_window_base', '未找到周版本开始前的提交',
                f"📭 {repository.type.upper()}中未找到周版本开始前的提交",
                outcome=OUTCOME_MISS, log_type='WEEKLY',
            )
            return None

        # 检查数据库中是否已存在这个提交记录
        existing_commit = Commit.query.filter_by(
            repository_id=repository.id,
            commit_id=base_commit_data['commit_id'],
            path=file_path
        ).first()
        if existing_commit:
            log_print(f"✅ 数据库中已存在基准提交: {existing_commit.commit_id[:8]}", 'WEEKLY')
            return existing_commit

        # 如果数据库中不存在，创建新的提交记录
        log_print(f"📝 创建新的基准提交记录: {base_commit_data['commit_id'][:8]}", 'WEEKLY')
        new_commit = Commit(
            repository_id=repository.id,
            commit_id=base_commit_data['commit_id'],
            path=file_path,
            author=base_commit_data.get('author', 'Unknown'),
            commit_time=base_commit_data['commit_time'],
            message=base_commit_data.get('message', ''),
            operation=base_commit_data.get('operation', 'M')
        )
        db.session.add(new_commit)
        db.session.commit()
        log_print(f"✅ 成功创建基准提交记录: {new_commit.commit_id[:8]} ({new_commit.commit_time})", 'WEEKLY')
        return new_commit

    except Exception as e:
        log_print(f"❌ 从{repository.type.upper()}获取基准版本失败: {e}", 'WEEKLY', force=True)
        traceback.print_exc()
        return None
