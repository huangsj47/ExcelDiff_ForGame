#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent 任务执行器（可扩展）。"""

from __future__ import annotations

import os
import sys

try:
    from .handlers.auto_sync import execute_auto_sync
    from .handlers.commit_diff import execute_commit_diff
    from .handlers.temp_cache_fetch import execute_temp_cache_fetch
except ImportError:
    from handlers.auto_sync import execute_auto_sync
    from handlers.commit_diff import execute_commit_diff
    from handlers.temp_cache_fetch import execute_temp_cache_fetch


def _ensure_platform_runtime_import_path():
    """Ensure parent project root is importable when launched from agent/ directory."""
    agent_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(agent_dir, os.pardir))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    return project_root


def _execute_task_via_local_runtime(task_type: str, task: dict):
    payload = dict(task.get("payload") or {})
    repository_id = task.get("repository_id")
    if repository_id and "repository_id" not in payload:
        payload["repository_id"] = repository_id

    if task_type == "weekly_sync" and "config_id" not in payload:
        commit_id = payload.get("commit_id") or task.get("commit_id")
        if isinstance(commit_id, str) and commit_id.isdigit():
            payload["config_id"] = int(commit_id)

    try:
        project_root = _ensure_platform_runtime_import_path()
        app_py_path = os.path.join(project_root, "app.py")
        if not os.path.exists(app_py_path):
            raise RuntimeError(f"platform runtime app.py not found: {app_py_path}")
        import app as app_module
        from services.task_worker_service import execute_task_inline_for_agent
    except Exception as exc:
        return (
            "failed",
            None,
            f"agent local runtime unavailable for task_type={task_type}: {exc}",
            None,
        )

    try:
        flask_app = getattr(app_module, "app", None)
        if flask_app is None:
            raise RuntimeError("app.app not found")
        with flask_app.app_context():
            result_summary = execute_task_inline_for_agent(task_type, payload)
        return "completed", result_summary, None, None
    except Exception as exc:
        return (
            "failed",
            None,
            f"agent local runtime execute failed for task_type={task_type}: {exc}",
            None,
        )


def execute_task(task: dict, settings):
    """执行任务并返回 (status, result_summary, error_message, result_payload)。"""
    task_type = str(task.get("task_type") or "").strip().lower()
    local_task_types = set(settings.local_task_types or [])

    try:
        if task_type == "noop" and "noop" in local_task_types:
            return "completed", "noop task completed", None, {}

        if task_type == "auto_sync" and "auto_sync" in local_task_types:
            return execute_auto_sync(task, settings)

        if task_type == "temp_cache_fetch" and "temp_cache_fetch" in local_task_types:
            return execute_temp_cache_fetch(task, settings)

        if task_type == "commit_diff" and "commit_diff" in local_task_types:
            return execute_commit_diff(task, settings)

        if task_type in {"excel_diff", "weekly_sync", "weekly_excel_cache"} and task_type in local_task_types:
            return _execute_task_via_local_runtime(task_type, task)
    except Exception as exc:
        return (
            "failed",
            None,
            f"agent local executor crashed for task_type={task_type}: {exc}",
            None,
        )

    # 任务类型未在本地启用，直接失败并由平台记录。
    return (
        "failed",
        None,
        f"agent local executor disabled or unsupported task_type={task_type}",
        None,
    )
