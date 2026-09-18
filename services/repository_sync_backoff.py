#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同步连续失败的退避：仓库上一次同步失败后，定时调度先别碰它。

## 为什么需要

`schedule_repository_sync_tasks` 每 2 分钟调度一次，而失败路径只在「手动重试策略 =
重克隆」时才会把 `clone_status` 置成 `failed`（见 `_handle_auto_sync_task_inner` 的失败
分支）—— 一个地址写错、仓库被删、或本地目录被清掉的仓库会**每 2 分钟重试一次**，
每次都刷一屏「Git命令执行失败 / [RESET] … 失败 / 已记录仓库 X 的同步错误」，
永远刷下去（线上报障）。

`clone_status` 不能作为「别重试」的判据：它同时被「已克隆好、随时可用」这件事用着，
置成 failed 会让仓库从列表里掉出去。判据改用**失败时间**
（`last_sync_error_time`，`_record_sync_error` 失败时写、成功后清）。

退避**不等于放弃**：窗口过后仍会自动重试（网络抖动这类瞬时故障能自愈）；
手动「重试同步」（`services/repository_admin_handlers.py` 那条路）**不走这里**，
随时可用。

## 为什么单独一个文件

这段逻辑原先长在 `services/task_worker_service.py` 里，而那个文件已经贴着
`scripts/check_file_length.py` 的 2000 行硬线（1952 → 加这段就 2008，CI 直接红）。
脚本自己的建议就是「新增功能应写入新文件」，所以退避这一件事整个搬到这里：
判据 + 那句话都在本文件里，调度器只留一次调用。
"""
from __future__ import annotations

from datetime import datetime, timezone

# 退避窗口。30 分钟是「2 分钟一轮」的 15 倍：既让刷屏停下来，又不至于让一次网络抖动
# 把仓库晾太久；窗口结束后照旧自动重试。
SYNC_FAILURE_RETRY_BACKOFF_SECONDS = 30 * 60


def sync_failure_backoff_active(repository, now=None) -> bool:
    """这个仓库上一次同步失败得**太近**，本轮定时调度先跳过它。

    没有失败记录（`last_sync_error` 为空）→ 不跳过；有错误文本但没有时间
    （历史数据）→ **不跳过**：判据缺失时宁可重试，也不要把一个仓库的自动同步
    永久停掉。
    """
    if not getattr(repository, "last_sync_error", None):
        return False
    failed_at = getattr(repository, "last_sync_error_time", None)
    if failed_at is None:
        return False
    if now is None:
        now = datetime.now(timezone.utc)
    if failed_at.tzinfo is None:
        # 历史数据里可能是 naive（SQLite 取回来的 DateTime 不带时区）：按 UTC 解释，
        # 与写入时用的 datetime.now(timezone.utc) 同一口径。
        failed_at = failed_at.replace(tzinfo=timezone.utc)
    try:
        elapsed = (now - failed_at).total_seconds()
    except TypeError:
        return False
    # 未来时间（时钟回拨 / 写库时用了本地时间）算作「刚刚失败」，照样退避。
    return 0 <= elapsed < SYNC_FAILURE_RETRY_BACKOFF_SECONDS


def backoff_skip_message(count: int) -> str:
    """被跳过的仓库**只说一次**。

    原先每个仓库各自刷一屏失败日志，看日志的人分不清「一个仓库失败了一百次」还是
    「一百个仓库各失败一次」—— 这句话把数量说出来，明细仍在各仓库自己的失败记录里。
    """
    return (f"⏸️ 跳过 {count} 个近期同步失败的仓库"
            f"（{SYNC_FAILURE_RETRY_BACKOFF_SECONDS // 60} 分钟内不重试）")
