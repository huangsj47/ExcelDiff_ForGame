#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent 任务执行器（可扩展）。"""

from __future__ import annotations

try:
    from .handlers.auto_sync import execute_auto_sync
except ImportError:
    from handlers.auto_sync import execute_auto_sync


def execute_task(task: dict, settings):
    """执行任务并返回 (status, result_summary, error_message, result_payload)。"""
    task_type = str(task.get("task_type") or "").strip()
    local_task_types = set(settings.local_task_types or [])

    if task_type == "noop" and "noop" in local_task_types:
        return "completed", "noop task completed", None, {}

    if task_type == "auto_sync" and "auto_sync" in local_task_types:
        return execute_auto_sync(task, settings)

    # 任务类型未在本地启用，交由上层走 proxy。
    return (
        "failed",
        None,
        f"agent local executor disabled or unsupported task_type={task_type}",
        None,
    )
