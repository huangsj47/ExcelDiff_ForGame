#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""周版本「文件确认状态」的取值域 —— 单一事实来源。

## 为什么需要这个模块

周版本的文件状态会落到**两处**：

    WeeklyVersionDiffCache.overall_status / confirmation_status["dev"]
    → 再经 StatusSyncService.sync_weekly_to_commit 同步到 Commit.status

而 `POST /weekly-version-config/<id>/file-status` 过去只判了「非空」：

    if not file_path or not status:      # 只挡 None / ""
        return ..., 400
    diff_cache.overall_status = status   # 任意字符串原样落库

于是 `"banana"` 会被 200 接受并写进上面两处。后果不是「多了一个字段值」：

- 周版本页的筛选只有三态（templates/weekly_version_diff.html 的 statusFilter /
  statusNames），`weekly_version_stats_api` 也按三态分别 count —— 脏状态的文件
  同时从筛选和统计里**消失**（total 与三项之和对不上，实测 total=1 而三态各为 0）。
- "操作人"的写入是 if/elif 分支（非三态不进入任何分支），于是留下
  「状态是脏值、操作人还是上一个确认者」的自相矛盾记录。
- Commit 侧同样落脏值，提交列表/比较页只认 pending/reviewed/confirmed/rejected，
  脏值渲染成「其他」（danger 徽章）且无法通过界面纠正。

## 取值域证据（不是猜的）

- 模型注释：`models/weekly_version.py` 的 `overall_status` 与 `models/commit.py`
  的 `status` 都写着 `# 'pending', 'confirmed', 'rejected'`。
- 前端调用点：`templates/weekly_version_diff.html` / `weekly_version_full_diff.html`
  的 `updateFileStatusQuick()` / `updateFileStatus()` 只传这三个值。
- 统计与筛选：`weekly_version_stats_api` 只 count 这三个；周版本页筛选器只有三项。

注意**提交状态域比周版本文件状态域大**：`services/commit_status_api_service.py`
额外接受 `'reviewed'`（界面「已查看」）。它是合法的**提交**状态，但不是周版本
文件状态 —— 反向同步（commit → weekly）必须把它挡在周版本缓存之外，见
`StatusSyncService.sync_commit_to_weekly`。
"""
from __future__ import annotations

#: 周版本文件状态的全部合法取值。顺序与界面（待确认 / 已确认 / 已拒绝）一致。
WEEKLY_FILE_STATUSES = ("pending", "confirmed", "rejected")

#: 集合形式，供 O(1) 判定使用。
WEEKLY_FILE_STATUS_SET = frozenset(WEEKLY_FILE_STATUSES)


def is_valid_weekly_file_status(value) -> bool:
    """判断 `value` 是否为合法的周版本文件状态。

    刻意**只做精确匹配**（不 strip、不 lower、不做同义词映射）：

    - 前端传的就是精确的三态字面量，`"PENDING"` / `" done "` / `"已确认"` 只会
      来自脚本或手工构造的请求；
    - 静默归一化会把「调用方传错」变成「看起来成功」，正是本次要消除的问题；
    - `value` 可能是数字 / 字典 / 列表（请求体是任意 JSON），所以不能直接
      拿它做 dict key —— 用元组成员判定，任何类型都不会抛异常。
    """
    return isinstance(value, str) and value in WEEKLY_FILE_STATUS_SET
