#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提交状态变更的唯一写法：写状态 + 写操作者 + 同步周版本（+ 批量权限预检）。

## 为什么需要这个模块

同一个业务动作「确认 / 拒绝一条提交」在本仓库有 6 个入口：

    handle_update_commit_status          单条 /commits/<id>/status
    handle_batch_update_commits_compat   兼容批量 /commits/batch-update
    batch_approve_commits                批量通过
    batch_reject_commits                 批量拒绝
    approve_all_files                    一次提交的「所有文件」
    reject_commit                        merge diff 页的拒绝

每个入口都各自抄了一遍上面三步，于是行为逐条漂移（以下均为实测，不是推测）：

  * `approve_all_files` / `reject_commit` 既不写 `status_changed_by`，也**完全不调用
    周版本同步** → 从这两个入口确认/拒绝之后，周版本页仍显示「未确认」、
    `weekly_version_stats_api` 统计不到，结果与从提交列表确认不一致；
  * 批量入口把权限校验放在循环内，且 `sync_commit_to_weekly` 默认在循环内逐条
    `commit()` → 跨项目批量时第二条没有权限，第一条却已经落库：403 只是「看起来
    失败了」，用户重试即重复副作用；
  * 周版本同步失败被吞成 `success=False`，响应照样说「已通过 N 个提交」，
    失败条数在界面上不可见。

把这三步收敛成同一个函数、所有入口都调它，是唯一能避免「第五种写法」的办法。

## 事务边界（重要）

本模块的函数**都不提交事务**：由调用方在「全部目标都处理完」之后提交一次。
`StatusSyncService.sync_commit_to_weekly` 为此提供了 `auto_commit=False`
（默认 True，既有调用方行为不变）。批量入口因此可以先做完权限预检再动手，
预检失败时库里一条都没改。
"""
from __future__ import annotations

#: 「不改写操作者」的哨兵，与「置为 None」区分开：'reviewed' 等非确认终态保持原有
#: 操作者 —— 它既不是确认也不是拒绝，不该被当成「谁点了已查看」写进去。
_KEEP_OPERATOR = object()


def resolve_status_operator(new_status: str, current_username):
    """状态 → 该写进 `status_changed_by` 的值。

    pending 清空、confirmed/rejected 记当前用户、其余（如 reviewed）保持原值。
    """
    if new_status == 'pending':
        return None
    if new_status in ('confirmed', 'rejected'):
        return current_username
    return _KEEP_OPERATOR


def apply_commit_status(commit, new_status: str, *, current_username, sync_service):
    """写提交状态与操作者，并在有实际变化时同步到周版本。**不提交事务**。

    返回 `{'changed': bool, 'sync_result': dict | None}`：

      - `changed` —— 状态或操作者至少有一项真的变了，批量入口据此计数；
      - `sync_result` —— `sync_commit_to_weekly` 的原始返回（未触发同步时为 None），
        调用方用 `summarize_sync_results` 汇总失败条数。
    """
    operator = resolve_status_operator(new_status, current_username)
    status_changed = commit.status != new_status
    operator_changed = operator is not _KEEP_OPERATOR and commit.status_changed_by != operator

    commit.status = new_status
    if operator is not _KEEP_OPERATOR:
        commit.status_changed_by = operator

    if not (status_changed or operator_changed):
        return {'changed': False, 'sync_result': None}

    # 顺序很重要：先落状态与操作者，再同步。`sync_commit_to_weekly` 拿
    # `commit.status_changed_by` 当周版本的操作者（services/status_sync_service.py），
    # 反过来的话周版本会记在上一个操作者头上。
    sync_result = sync_service.sync_commit_to_weekly(commit.id, new_status, auto_commit=False)
    return {'changed': True, 'sync_result': sync_result}


def precheck_batch_permissions(commits, *, action: str, can_operate, permission_cache=None):
    """批量操作前对**全部**目标提交做权限预检，返回 `(allowed, message)`。

    必须在任何写入之前调用：原先的写法在循环里边写边判，第二条提交没有权限时
    前面的提交已经落库，返回 403 也无法回滚（用户重试即重复副作用）。

    `can_operate(project_id, action)` 由调用方注入（两个入口的取用方式不同），
    `permission_cache` 可跨次复用，避免同一项目重复判权。
    """
    cache = {} if permission_cache is None else permission_cache
    for commit in commits:
        project_id = commit.repository.project_id if commit.repository else None
        if project_id not in cache:
            cache[project_id] = can_operate(project_id, action)
        allowed, message = cache[project_id]
        if not allowed:
            return False, message
    return True, None


def summarize_sync_results(sync_results):
    """汇总 `sync_commit_to_weekly` 的结果 → `(周版本更新数, 失败条数)`。

    失败条数必须对外可见：原先只统计 `success` 的那些，同步失败被静默吞掉，
    响应照样是「已通过 N 个提交」。
    """
    updated = 0
    failed = 0
    for result in sync_results:
        if not result:
            continue
        if result.get('success'):
            updated += result.get('updated_count', 0) or 0
        else:
            failed += 1
    return updated, failed


def build_batch_result_message(count: int, weekly_updated: int, sync_failed: int) -> str:
    """批量入口的统一措辞：条数、周版本同步数，失败时显式追加失败条数。"""
    message = f'已更新 {count} 个提交，同步更新了 {weekly_updated} 个周版本记录'
    if sync_failed:
        message += f'（{sync_failed} 条周版本同步失败）'
    return message
