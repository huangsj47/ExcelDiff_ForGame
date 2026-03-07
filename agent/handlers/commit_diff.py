#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent 本地 commit_diff 任务处理。"""

from __future__ import annotations

import hashlib
import json
import os
import sys

try:
    from ..local_temp_cache import save_local_temp_cache
except ImportError:
    from local_temp_cache import save_local_temp_cache


def _ensure_platform_runtime_import_path():
    agent_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(agent_dir, os.pardir, os.pardir))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    return project_root


def _execute_commit_diff_via_local_runtime(payload: dict):
    project_root = _ensure_platform_runtime_import_path()
    app_py_path = os.path.join(project_root, "app.py")
    if not os.path.exists(app_py_path):
        raise RuntimeError(f"platform runtime app.py not found: {app_py_path}")

    import app as app_module
    from services.task_worker_service import execute_task_inline_for_agent

    flask_app = getattr(app_module, "app", None)
    if flask_app is None:
        raise RuntimeError("app.app not found")

    with flask_app.app_context():
        return execute_task_inline_for_agent("commit_diff", payload)


def execute_commit_diff(task: dict, settings):
    payload = dict(task.get("payload") or {})
    repository_id = task.get("repository_id") or payload.get("repository_id")
    project_id = task.get("project_id") or payload.get("project_id")

    if repository_id and "repository_id" not in payload:
        payload["repository_id"] = repository_id
    if project_id and "project_id" not in payload:
        payload["project_id"] = project_id

    commit_record_id = payload.get("commit_record_id") or payload.get("commit_id")
    if not commit_record_id:
        raise ValueError("commit_diff payload 缺少 commit_record_id")

    result_payload = _execute_commit_diff_via_local_runtime(payload)
    payload_json = json.dumps(result_payload, ensure_ascii=False)
    payload_size = len(payload_json.encode("utf-8"))
    payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()

    cache_key = f"commit_diff:{commit_record_id}:{payload_hash[:16]}"
    expire_seconds = max(60, int(settings.temp_cache_expire_days) * 24 * 3600)

    save_local_temp_cache(
        settings,
        cache_key=cache_key,
        payload_json=payload_json,
        payload_hash=payload_hash,
        payload_size=payload_size,
        task_type="commit_diff",
        cache_kind="commit_diff_result",
        project_id=project_id,
        repository_id=repository_id,
        commit_id=str(payload.get("commit_sha") or payload.get("commit_id") or ""),
        file_path=str(payload.get("file_path") or ""),
        expire_seconds=expire_seconds,
    )

    prefetch_platform_cache = payload_size > int(settings.temp_cache_threshold_bytes or 0)

    report_payload = {
        "cache_key": cache_key,
        "task_type": "commit_diff",
        "cache_kind": "commit_diff_result",
        "project_id": project_id,
        "repository_id": repository_id,
        "commit_id": str(payload.get("commit_sha") or payload.get("commit_id") or ""),
        "file_path": str(payload.get("file_path") or ""),
        "payload_hash": payload_hash,
        "payload_size": payload_size,
        "expire_seconds": expire_seconds,
        "prefetch_platform_cache": bool(prefetch_platform_cache),
    }

    if not prefetch_platform_cache:
        report_payload["inline_payload_json"] = payload_json

    summary = {
        "message": "commit_diff completed",
        "cache_key": cache_key,
        "payload_size": payload_size,
        "source": "agent_local_cache",
        "inline": not prefetch_platform_cache,
    }

    return "completed", summary, None, report_payload
