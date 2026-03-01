#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
后台任务包
"""

# 导入主要的任务模块
from .background_tasks import (
    start_background_task_worker,
    stop_background_task_worker,
    add_task_to_queue,
    TaskWrapper
)

from .cache_cleanup import (
    clear_version_mismatch_cache,
    cleanup_old_cache_entries
)

from .cleanup_tasks import (
    cleanup_pending_deletions,
    schedule_cleanup_task
)

from .weekly_sync_tasks import (
    create_weekly_sync_task,
    schedule_weekly_sync_tasks
)

__all__ = [
    'start_background_task_worker',
    'stop_background_task_worker', 
    'add_task_to_queue',
    'TaskWrapper',
    'clear_version_mismatch_cache',
    'cleanup_old_cache_entries',
    'cleanup_pending_deletions',
    'schedule_cleanup_task',
    'create_weekly_sync_task',
    'schedule_weekly_sync_tasks'
]
