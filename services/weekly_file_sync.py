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
    「翻历史、必要时补一条 Commit」的逻辑。

## 调用方向

`weekly_version_logic` 从本模块导入（并在 `app.py` 的用法上保持同名可见）。
反向只有 `get_real_base_commit_from_vcs` 函数体内那一处延迟导入，见那里的说明。
"""

import traceback
from dataclasses import dataclass

from models import Commit, db
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
    if not parts:
        return f"{file_count} 个文件，无需更新"
    return f"{file_count} 个文件（{'、'.join(parts)}）"


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
        log_print(f"🔍 从{repository.type.upper()}获取文件提交历史: {file_path}", 'WEEKLY')
        if repository.type == 'git':
            # Git: 获取文件的提交历史
            commits_data = vcs_service.get_file_commit_history(file_path, limit=100)
        else:
            # SVN: 获取文件的提交历史
            commits_data = vcs_service.get_file_history(file_path, limit=100)
        if not commits_data:
            log_print(f"📭 {repository.type.upper()}中未找到文件 {file_path} 的提交历史", 'WEEKLY')
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
            log_print(f"📭 {repository.type.upper()}中未找到周版本开始前的提交", 'WEEKLY')
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
