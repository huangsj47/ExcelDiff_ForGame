# -*- coding: utf-8 -*-
"""Agent task result handling helper extracted from agent_management_handlers."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from flask import jsonify, request


def handle_agent_report_task_result(task_id):
    from services import agent_management_handlers as handlers

    db, AgentTask, BackgroundTask, Repository, log_print = handlers.get_runtime_models(
        "db",
        "AgentTask",
        "BackgroundTask",
        "Repository",
        "log_print",
    )
    try:
        ok, resp, code = handlers._validate_agent_shared_secret()
        if not ok:
            return resp, code

        payload = request.get_json(silent=True) or {}
        agent_code = str(payload.get("agent_code") or request.headers.get("X-Agent-Code") or "").strip()
        agent_token = str(payload.get("agent_token") or request.headers.get("X-Agent-Token") or "").strip()
        status = str(payload.get("status") or "").strip().lower()
        result_summary = payload.get("result_summary")
        result_payload = payload.get("result_payload")
        error_message = payload.get("error_message")

        if status not in {"completed", "failed"}:
            return jsonify({"success": False, "message": "status 仅支持 completed/failed"}), 400

        agent = handlers._get_agent_by_identity(agent_code, agent_token)
        if not agent:
            return jsonify({"success": False, "message": "Agent 身份无效"}), 401

        task = db.session.get(AgentTask, task_id)
        if not task:
            return jsonify({"success": False, "message": "任务不存在"}), 404
        if task.assigned_agent_id and task.assigned_agent_id != agent.id:
            return jsonify({"success": False, "message": "任务不属于当前 Agent"}), 403

        now_utc = datetime.now(timezone.utc)
        task.status = status
        task.completed_at = now_utc
        task.lease_expires_at = None
        effective_result_summary = result_summary

        if status == "completed" and task.task_type == "auto_sync" and isinstance(result_payload, dict):
            effective_result_summary = handlers._apply_auto_sync_result(task, result_payload)

        if status == "completed" and task.task_type == "commit_diff" and isinstance(result_payload, dict):
            cache_key = str(result_payload.get("cache_key") or "").strip()
            payload_hash = str(result_payload.get("payload_hash") or "").strip()
            payload_size = int(result_payload.get("payload_size") or 0)
            inline_payload_json = result_payload.get("inline_payload_json")
            summary_payload = {
                "message": "commit_diff completed",
                "cache_key": cache_key or None,
                "payload_hash": payload_hash or None,
                "payload_size": payload_size,
                "repository_id": result_payload.get("repository_id") or task.repository_id,
                "commit_id": result_payload.get("commit_id"),
                "file_path": result_payload.get("file_path"),
                "expire_seconds": result_payload.get("expire_seconds"),
            }
            if inline_payload_json is not None:
                if not isinstance(inline_payload_json, str):
                    inline_payload_json = json.dumps(inline_payload_json, ensure_ascii=False)
                summary_payload["inline_payload_json"] = inline_payload_json

            if cache_key and bool(result_payload.get("prefetch_platform_cache")):
                fetch_task_id = handlers._dispatch_agent_local_cache_fetch(
                    db=db,
                    cache_key=cache_key,
                    expected_hash=payload_hash,
                    project_id=task.project_id,
                    repository_id=result_payload.get("repository_id") or task.repository_id,
                )
                summary_payload["prefetch_task_id"] = fetch_task_id

            effective_result_summary = summary_payload

        if status == "completed" and task.task_type == "temp_cache_fetch" and isinstance(result_payload, dict):
            if result_payload.get("payload_json") is not None:
                payload_json = result_payload.get("payload_json")
                if not isinstance(payload_json, str):
                    payload_json = json.dumps(payload_json, ensure_ascii=False)
                payload_hash = str(result_payload.get("payload_hash") or "").strip()
                if not payload_hash:
                    payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
                payload_size = int(result_payload.get("payload_size") or len(payload_json.encode("utf-8")))
                cache_payload = {
                    "cache_key": str(result_payload.get("cache_key") or "").strip(),
                    "task_type": str(result_payload.get("task_type") or "").strip() or "excel_diff",
                    "cache_kind": str(result_payload.get("cache_kind") or "").strip() or "task_result_payload",
                    "project_id": task.project_id,
                    "repository_id": result_payload.get("repository_id") or task.repository_id,
                    "commit_id": result_payload.get("commit_id"),
                    "file_path": result_payload.get("file_path"),
                    "payload_json": payload_json,
                    "payload_hash": payload_hash,
                    "payload_size": payload_size,
                    "expire_seconds": result_payload.get("expire_seconds"),
                    "source_task_id": task.id,
                }
                row = handlers._upsert_agent_temp_cache_entry(db=db, agent=agent, payload=cache_payload)
                effective_result_summary = effective_result_summary or {
                    "message": "temp_cache_fetch applied",
                    "cache_key": row.cache_key,
                    "payload_size": row.payload_size,
                }
            elif result_payload.get("platform_cache_key"):
                effective_result_summary = effective_result_summary or {
                    "message": "temp_cache_fetch uploaded to platform cache",
                    "cache_key": result_payload.get("platform_cache_key"),
                    "payload_size": result_payload.get("payload_size"),
                }

        if isinstance(effective_result_summary, (dict, list)):
            task.result_summary = json.dumps(effective_result_summary, ensure_ascii=False)
        else:
            task.result_summary = None if effective_result_summary is None else str(effective_result_summary)
        if status == "failed":
            task.error_message = None if error_message is None else str(error_message)

        repository = db.session.get(Repository, task.repository_id) if task.repository_id else None
        if repository:
            if status == "failed":
                sync_error_message = f"Agent任务失败({task.task_type}): {task.error_message or '未知错误'}"
                if task.task_type == "auto_sync":
                    repository.clone_status = "failed"
                    repository.clone_error = task.error_message or "auto_sync failed"
                handlers.record_repository_sync_error(
                    db.session,
                    repository,
                    sync_error_message,
                    log_func=log_print,
                    log_type="AGENT",
                    commit=False,
                )
            elif status == "completed" and task.task_type == "auto_sync":
                handlers.clear_repository_sync_error(
                    db.session,
                    repository,
                    log_func=log_print,
                    log_type="AGENT",
                    commit=False,
                )

        if task.source_task_id:
            src_task = db.session.get(BackgroundTask, task.source_task_id)
            if src_task:
                src_task.status = status
                src_task.completed_at = now_utc
                src_task.error_message = task.error_message if status == "failed" else None
                if status == "failed":
                    src_task.retry_count = (src_task.retry_count or 0) + 1

        db.session.commit()
        if str(task.task_type or "").strip().lower() == "commit_diff":
            log_print(
                (
                    f"Agent 回传 commit_diff 结果: task_id={task.id}, agent={agent.agent_code}, "
                    f"status={status}, error={task.error_message or 'N/A'}"
                ),
                "AGENT",
                force=(status == "failed"),
            )
        return jsonify({"success": True}), 200
    except Exception as exc:
        db.session.rollback()
        log_print(f"Agent 回传结果失败: {exc}", "AGENT", force=True)
        return jsonify({"success": False, "message": str(exc)}), 500

