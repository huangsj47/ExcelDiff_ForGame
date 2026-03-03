#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent 管理相关处理函数。"""

from __future__ import annotations

from collections import Counter
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from flask import jsonify, render_template, request

from services.model_loader import get_runtime_models
from utils.request_security import require_admin


_PROJECT_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{2,50}$")
_AGENT_CODE_RE = re.compile(r"[^A-Za-z0-9_-]+")


def _agent_shared_secret() -> str:
    return (os.environ.get("AGENT_SHARED_SECRET") or "").strip()


def _validate_agent_shared_secret():
    expected = _agent_shared_secret()
    provided = (request.headers.get("X-Agent-Secret") or "").strip()
    if not expected:
        return False, jsonify({"success": False, "message": "平台未配置 AGENT_SHARED_SECRET"}), 503
    if not provided or provided != expected:
        return False, jsonify({"success": False, "message": "Agent 鉴权失败"}), 401
    return True, None, None


def _normalize_project_specs(raw_projects):
    specs = []
    if isinstance(raw_projects, str):
        raw_projects = [item.strip() for item in raw_projects.split(",") if item.strip()]

    if not isinstance(raw_projects, list):
        return specs

    seen = set()
    for item in raw_projects:
        if isinstance(item, str):
            code = item.strip()
            name = code
            department = None
        elif isinstance(item, dict):
            code = str(item.get("code", "")).strip()
            name = str(item.get("name") or code).strip()
            department = item.get("department")
        else:
            continue

        if not code or code in seen:
            continue
        if not _PROJECT_CODE_RE.match(code):
            continue

        specs.append(
            {
                "code": code,
                "name": name or code,
                "department": str(department).strip() if department else None,
            }
        )
        seen.add(code)
    return specs


def _normalize_agent_code(agent_code: str, agent_name: str, host: str) -> str:
    raw = str(agent_code or "").strip()
    if not raw:
        base = str(agent_name or "agent").strip()
        host_part = str(host or "").strip().replace(".", "-")
        raw = f"{base}-{host_part}" if host_part else base

    normalized = _AGENT_CODE_RE.sub("-", raw).strip("-").lower()
    if not normalized:
        normalized = f"agent-{secrets.token_hex(4)}"
    return normalized[:100]


def _to_int_or_none(value, min_value=None, max_value=None):
    try:
        iv = int(value)
    except Exception:
        return None
    if min_value is not None and iv < min_value:
        return None
    if max_value is not None and iv > max_value:
        return None
    return iv


def _to_float_or_none(value, min_value=None, max_value=None):
    try:
        fv = float(value)
    except Exception:
        return None
    if min_value is not None and fv < min_value:
        return None
    if max_value is not None and fv > max_value:
        return None
    return fv


def _extract_agent_metrics(payload: dict):
    metrics = {
        "cpu_cores": _to_int_or_none(payload.get("cpu_cores"), min_value=1, max_value=4096),
        "cpu_usage_percent": _to_float_or_none(payload.get("cpu_usage_percent"), min_value=0, max_value=100),
        "memory_total_bytes": _to_int_or_none(payload.get("memory_total_bytes"), min_value=0),
        "memory_available_bytes": _to_int_or_none(payload.get("memory_available_bytes"), min_value=0),
        "disk_free_bytes": _to_int_or_none(payload.get("disk_free_bytes"), min_value=0),
        "os_name": str(payload.get("os_name") or "").strip()[:100] or None,
        "os_version": str(payload.get("os_version") or "").strip()[:200] or None,
        "os_platform": str(payload.get("os_platform") or "").strip()[:300] or None,
    }
    return metrics


def _apply_agent_runtime_fields(agent, payload: dict):
    now_utc = datetime.now(timezone.utc)
    if "status" in payload:
        status = str(payload.get("status") or "").strip()
        agent.status = status or agent.status or "online"

    host = str(payload.get("ip") or payload.get("host") or "").strip()
    if host:
        agent.host = host

    if "port" in payload:
        port = _to_int_or_none(payload.get("port"), min_value=1, max_value=65535)
        if port is not None:
            agent.port = port

    if "agent_name" in payload:
        incoming_name = str(payload.get("agent_name") or "").strip()
        if incoming_name:
            agent.agent_name = incoming_name

    if "last_error" in payload:
        last_error = str(payload.get("last_error") or "").strip()
        agent.last_error = last_error or None

    metrics = _extract_agent_metrics(payload)
    metrics_updated = False
    for key, value in metrics.items():
        if value is None:
            continue
        setattr(agent, key, value)
        metrics_updated = True

    if metrics_updated:
        agent.metrics_updated_at = now_utc

    agent.last_heartbeat = now_utc


def _format_agent_rows(agents, bindings_by_agent_id):
    name_counter = Counter(
        (str(item.agent_name or "").strip().lower() or "agent")
        for item in agents
    )
    rows = []
    for agent in agents:
        raw_name = str(agent.agent_name or "").strip() or str(agent.agent_code or "").strip() or "agent"
        name_key = raw_name.lower()
        if name_counter.get(name_key, 0) > 1 and agent.host:
            display_name = f"{raw_name}_{agent.host}"
        else:
            display_name = raw_name

        binding_rows = bindings_by_agent_id.get(agent.id, [])
        rows.append(
            {
                "agent_code": agent.agent_code,
                "agent_name": raw_name,
                "display_name": display_name,
                "host": agent.host,
                "port": agent.port,
                "status": agent.status,
                "last_heartbeat": agent.last_heartbeat.isoformat() if agent.last_heartbeat else None,
                "default_admin_username": agent.default_admin_username,
                "project_codes": [binding.project_code for binding in binding_rows],
                "project_count": len(binding_rows),
                "cpu_cores": agent.cpu_cores,
                "cpu_usage_percent": agent.cpu_usage_percent,
                "memory_total_bytes": agent.memory_total_bytes,
                "memory_available_bytes": agent.memory_available_bytes,
                "disk_free_bytes": agent.disk_free_bytes,
                "os_name": agent.os_name,
                "os_version": agent.os_version,
                "os_platform": agent.os_platform,
                "metrics_updated_at": agent.metrics_updated_at.isoformat() if agent.metrics_updated_at else None,
            }
        )
    return rows


def build_agent_node_items():
    AgentNode, AgentProjectBinding = get_runtime_models("AgentNode", "AgentProjectBinding")
    agents = AgentNode.query.order_by(AgentNode.updated_at.desc()).all()
    if not agents:
        return []

    agent_ids = [item.id for item in agents]
    bindings = (
        AgentProjectBinding.query.filter(AgentProjectBinding.agent_id.in_(agent_ids))
        .order_by(AgentProjectBinding.project_code.asc())
        .all()
    )
    bindings_by_agent_id = {}
    for row in bindings:
        bindings_by_agent_id.setdefault(row.agent_id, []).append(row)
    return _format_agent_rows(agents, bindings_by_agent_id)


def _ensure_default_admin_for_projects(db, default_admin_username: str | None, project_ids: list[int] | None):
    """确保默认管理员用户拥有目标项目管理员权限（不存在则预分配）。"""
    username = (default_admin_username or "").strip()
    normalized_project_ids = sorted({int(pid) for pid in (project_ids or []) if pid})
    if not username or not normalized_project_ids:
        return {
            "mode": "skipped",
            "username": username,
            "project_count": len(normalized_project_ids),
            "added_memberships": 0,
            "updated_memberships": 0,
            "created_preassignments": 0,
            "updated_preassignments": 0,
        }

    try:
        from auth.models import AuthProjectPreAssignment, AuthUser, AuthUserProject
    except Exception as exc:
        return {
            "mode": "auth_unavailable",
            "username": username,
            "project_count": len(normalized_project_ids),
            "added_memberships": 0,
            "updated_memberships": 0,
            "created_preassignments": 0,
            "updated_preassignments": 0,
            "error": str(exc),
        }

    target_role = "admin"
    added_memberships = 0
    updated_memberships = 0
    created_preassignments = 0
    updated_preassignments = 0

    existing_user = AuthUser.query.filter_by(username=username).first()
    if existing_user:
        for project_id in normalized_project_ids:
            membership = AuthUserProject.query.filter_by(
                user_id=existing_user.id,
                project_id=project_id,
            ).first()
            if membership:
                if membership.role != target_role:
                    membership.role = target_role
                    updated_memberships += 1
                continue

            db.session.add(
                AuthUserProject(
                    user_id=existing_user.id,
                    project_id=project_id,
                    role=target_role,
                )
            )
            added_memberships += 1

        return {
            "mode": "existing_user",
            "username": username,
            "project_count": len(normalized_project_ids),
            "added_memberships": added_memberships,
            "updated_memberships": updated_memberships,
            "created_preassignments": 0,
            "updated_preassignments": 0,
        }

    for project_id in normalized_project_ids:
        pre = AuthProjectPreAssignment.query.filter_by(
            username=username,
            project_id=project_id,
        ).first()
        if pre:
            if pre.role != target_role:
                pre.role = target_role
                updated_preassignments += 1
            continue
        db.session.add(
            AuthProjectPreAssignment(
                username=username,
                project_id=project_id,
                role=target_role,
                applied=False,
            )
        )
        created_preassignments += 1

    return {
        "mode": "pre_assignment",
        "username": username,
        "project_count": len(normalized_project_ids),
        "added_memberships": 0,
        "updated_memberships": 0,
        "created_preassignments": created_preassignments,
        "updated_preassignments": updated_preassignments,
    }


def _get_agent_by_identity(agent_code: str, agent_token: str):
    AgentNode = get_runtime_models("AgentNode")[0]
    return AgentNode.query.filter_by(agent_code=agent_code, agent_token=agent_token).first()


def enqueue_agent_task(*, task_type, project_id, repository_id=None, source_task_id=None, priority=10, payload=None):
    """平台内部调用：创建 Agent 任务。"""
    db, AgentTask = get_runtime_models("db", "AgentTask")
    agent_task = AgentTask(
        task_type=task_type,
        project_id=project_id,
        repository_id=repository_id,
        source_task_id=source_task_id,
        priority=priority,
        payload=json.dumps(payload or {}, ensure_ascii=False),
        status="pending",
    )
    db.session.add(agent_task)
    return agent_task


def _parse_commit_time(raw_value):
    if raw_value is None:
        return None
    if isinstance(raw_value, datetime):
        return raw_value
    text = str(raw_value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _apply_auto_sync_result(task, result_payload):
    db, Commit, Repository = get_runtime_models("db", "Commit", "Repository")
    repository = Repository.query.get(task.repository_id) if task.repository_id else None
    if not repository:
        return {"message": "repository missing", "commits_added": 0, "excel_tasks_added": 0}

    commit_items = result_payload.get("commits") if isinstance(result_payload, dict) else None
    if not isinstance(commit_items, list):
        return {"message": "invalid commits payload", "commits_added": 0, "excel_tasks_added": 0}

    commit_ids = {str(item.get("commit_id") or "").strip() for item in commit_items if isinstance(item, dict)}
    file_paths = {str(item.get("path") or "").strip() for item in commit_items if isinstance(item, dict)}
    commit_ids.discard("")
    file_paths.discard("")

    existing_pairs = set()
    if commit_ids and file_paths:
        existing_rows = Commit.query.filter(
            Commit.repository_id == repository.id,
            Commit.commit_id.in_(list(commit_ids)),
            Commit.path.in_(list(file_paths)),
        ).all()
        existing_pairs = {(row.commit_id, row.path) for row in existing_rows}

    new_commit_objects = []
    latest_commit_id = None
    latest_commit_time = None
    for item in commit_items:
        if not isinstance(item, dict):
            continue
        commit_id = str(item.get("commit_id") or "").strip()
        file_path = str(item.get("path") or "").strip()
        if not commit_id or not file_path:
            continue

        pair = (commit_id, file_path)
        if pair in existing_pairs:
            continue
        existing_pairs.add(pair)

        commit_time = _parse_commit_time(item.get("commit_time"))
        if commit_time and (latest_commit_time is None or commit_time > latest_commit_time):
            latest_commit_time = commit_time
            latest_commit_id = commit_id

        operation = str(item.get("operation") or "M").strip().upper()
        if operation not in {"A", "M", "D"}:
            operation = "M"

        new_commit_objects.append(
            Commit(
                repository_id=repository.id,
                commit_id=commit_id,
                path=file_path,
                version=str(item.get("version") or commit_id[:8])[:50],
                operation=operation,
                author=str(item.get("author") or "")[:100],
                commit_time=commit_time,
                message=str(item.get("message") or ""),
                status="pending",
            )
        )

    if new_commit_objects:
        db.session.bulk_save_objects(new_commit_objects)

    excel_tasks_added = 0
    if new_commit_objects:
        from services.task_worker_service import add_excel_diff_task

        for commit in new_commit_objects:
            file_path_lower = (commit.path or "").lower()
            if file_path_lower.endswith((".xlsx", ".xls", ".xlsm", ".xlsb", ".csv")):
                task_id = add_excel_diff_task(
                    repository_id=repository.id,
                    commit_id=commit.commit_id,
                    file_path=commit.path,
                    priority=8,
                    auto_commit=False,
                )
                if task_id:
                    excel_tasks_added += 1

    repository.last_sync_time = datetime.now(timezone.utc)
    if latest_commit_id:
        repository.last_sync_commit_id = latest_commit_id

    return {
        "message": "auto_sync result applied",
        "commits_added": len(new_commit_objects),
        "excel_tasks_added": excel_tasks_added,
        "latest_commit_id": latest_commit_id,
    }


def register_agent_node():
    """Agent 注册并申请绑定项目代号。"""
    db, AgentNode, AgentProjectBinding, Project, log_print = get_runtime_models(
        "db",
        "AgentNode",
        "AgentProjectBinding",
        "Project",
        "log_print",
    )
    try:
        ok, resp, code = _validate_agent_shared_secret()
        if not ok:
            return resp, code

        payload = request.get_json(silent=True) or {}
        host = str(payload.get("ip") or payload.get("host") or "").strip() or None
        agent_name = str(payload.get("agent_name") or "").strip()
        agent_code = _normalize_agent_code(
            str(payload.get("agent_code") or "").strip(),
            agent_name,
            host or "",
        )
        default_admin_username = str(payload.get("default_admin_username", "")).strip() or None
        capabilities = payload.get("capabilities")

        project_specs = _normalize_project_specs(
            payload.get("projects")
            or payload.get("project_codes")
            or payload.get("project_code_list")
        )

        agent = AgentNode.query.filter_by(agent_code=agent_code).first()
        if not agent:
            agent = AgentNode(
                agent_code=agent_code,
                agent_name=agent_name or agent_code,
                agent_token=secrets.token_hex(24),
            )
            db.session.add(agent)
            db.session.flush()
        else:
            agent.agent_name = agent_name or agent.agent_name or agent_code

        _apply_agent_runtime_fields(
            agent,
            {
                **payload,
                "agent_name": agent_name or agent.agent_name or agent_code,
                "host": host or payload.get("host"),
                "status": "online",
            },
        )
        agent.default_admin_username = default_admin_username
        agent.capabilities = None if capabilities is None else str(capabilities)
        agent.last_error = None

        created_projects = []
        idempotent_projects = []
        conflict_projects = []
        bound_project_ids = []

        for spec in project_specs:
            project_code = spec["code"]
            project = Project.query.filter_by(code=project_code).first()
            if project is None:
                project = Project(
                    code=project_code,
                    name=spec.get("name") or project_code,
                    department=spec.get("department"),
                )
                db.session.add(project)
                db.session.flush()

                binding = AgentProjectBinding(
                    agent_id=agent.id,
                    project_id=project.id,
                    project_code=project.code,
                )
                db.session.add(binding)
                created_projects.append(project_code)
                bound_project_ids.append(project.id)
                continue

            binding = AgentProjectBinding.query.filter_by(project_id=project.id).first()
            if binding and binding.agent_id == agent.id:
                idempotent_projects.append(project_code)
                bound_project_ids.append(project.id)
                continue

            # 严格遵循需求：项目代号已存在且非该 Agent 已绑定场景，禁止覆盖
            conflict_projects.append(project_code)

        if conflict_projects:
            db.session.rollback()
            return (
                jsonify(
                    {
                        "success": False,
                        "message": "存在已占用项目代号，禁止覆盖",
                        "conflict_project_codes": conflict_projects,
                    }
                ),
                409,
            )

        default_admin_assignment = _ensure_default_admin_for_projects(
            db,
            default_admin_username,
            bound_project_ids,
        )

        db.session.commit()
        log_print(
            f"Agent 注册成功: {agent_code}, 创建项目={len(created_projects)}, 幂等项目={len(idempotent_projects)}",
            "AGENT",
        )
        return jsonify(
            {
                "success": True,
                "agent_code": agent.agent_code,
                "agent_token": agent.agent_token,
                "project_binding_count": len(project_specs),
                "created_project_codes": created_projects,
                "idempotent_project_codes": idempotent_projects,
                "default_admin_assignment": default_admin_assignment,
            }
        )
    except Exception as exc:
        db.session.rollback()
        log_print(f"Agent 注册失败: {exc}", "AGENT", force=True)
        return jsonify({"success": False, "message": str(exc)}), 500


def agent_heartbeat():
    """Agent 心跳上报。"""
    db, AgentNode, log_print = get_runtime_models("db", "AgentNode", "log_print")
    try:
        ok, resp, code = _validate_agent_shared_secret()
        if not ok:
            return resp, code

        payload = request.get_json(silent=True) or {}
        agent_code = str(payload.get("agent_code") or request.headers.get("X-Agent-Code") or "").strip()
        agent_token = str(payload.get("agent_token") or request.headers.get("X-Agent-Token") or "").strip()

        if not agent_code or not agent_token:
            return jsonify({"success": False, "message": "缺少 agent_code 或 agent_token"}), 400

        agent = _get_agent_by_identity(agent_code, agent_token)
        if not agent:
            return jsonify({"success": False, "message": "Agent 身份无效"}), 401

        _apply_agent_runtime_fields(
            agent,
            {
                **payload,
                "status": str(payload.get("status") or "online").strip() or "online",
            },
        )

        db.session.commit()
        return jsonify({"success": True, "server_time": datetime.now(timezone.utc).isoformat()})
    except Exception as exc:
        db.session.rollback()
        log_print(f"Agent 心跳失败: {exc}", "AGENT", force=True)
        return jsonify({"success": False, "message": str(exc)}), 500


def agent_claim_task():
    """Agent 拉取可执行任务（按优先级）。"""
    db, AgentProjectBinding, AgentTask, log_print = get_runtime_models(
        "db",
        "AgentProjectBinding",
        "AgentTask",
        "log_print",
    )
    try:
        ok, resp, code = _validate_agent_shared_secret()
        if not ok:
            return resp, code

        payload = request.get_json(silent=True) or {}
        agent_code = str(payload.get("agent_code") or request.headers.get("X-Agent-Code") or "").strip()
        agent_token = str(payload.get("agent_token") or request.headers.get("X-Agent-Token") or "").strip()
        lease_seconds = int(payload.get("lease_seconds") or 120)
        lease_seconds = max(30, min(600, lease_seconds))

        if not agent_code or not agent_token:
            return jsonify({"success": False, "message": "缺少 agent_code 或 agent_token"}), 400

        agent = _get_agent_by_identity(agent_code, agent_token)
        if not agent:
            return jsonify({"success": False, "message": "Agent 身份无效"}), 401

        project_ids = [
            row.project_id
            for row in AgentProjectBinding.query.filter_by(agent_id=agent.id).all()
        ]
        if not project_ids:
            return jsonify({"success": True, "task": None, "message": "当前 Agent 未绑定项目"}), 200

        now_utc = datetime.now(timezone.utc)
        reclaimable_processing = AgentTask.query.filter(
            AgentTask.status == "processing",
            AgentTask.project_id.in_(project_ids),
            AgentTask.lease_expires_at.isnot(None),
            AgentTask.lease_expires_at < now_utc,
        ).all()
        for item in reclaimable_processing:
            item.status = "pending"
            item.assigned_agent_id = None
            item.started_at = None
            item.lease_expires_at = None
            item.retry_count = (item.retry_count or 0) + 1

        task = (
            AgentTask.query.filter(
                AgentTask.status == "pending",
                AgentTask.project_id.in_(project_ids),
            )
            .order_by(AgentTask.priority.asc(), AgentTask.created_at.asc())
            .first()
        )

        if not task:
            db.session.commit()
            return jsonify({"success": True, "task": None}), 200

        task.status = "processing"
        task.assigned_agent_id = agent.id
        task.started_at = now_utc
        task.lease_expires_at = now_utc + timedelta(seconds=lease_seconds)
        agent.status = "online"
        agent.last_heartbeat = now_utc
        db.session.commit()

        response_task = {
            "id": task.id,
            "task_type": task.task_type,
            "priority": task.priority,
            "project_id": task.project_id,
            "repository_id": task.repository_id,
            "source_task_id": task.source_task_id,
            "payload": json.loads(task.payload or "{}"),
            "lease_expires_at": task.lease_expires_at.isoformat() if task.lease_expires_at else None,
        }
        return jsonify({"success": True, "task": response_task}), 200
    except Exception as exc:
        db.session.rollback()
        log_print(f"Agent claim 失败: {exc}", "AGENT", force=True)
        return jsonify({"success": False, "message": str(exc)}), 500


def agent_report_task_result(task_id):
    """Agent 回传任务结果。"""
    db, AgentTask, BackgroundTask, log_print = get_runtime_models(
        "db",
        "AgentTask",
        "BackgroundTask",
        "log_print",
    )
    try:
        ok, resp, code = _validate_agent_shared_secret()
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

        agent = _get_agent_by_identity(agent_code, agent_token)
        if not agent:
            return jsonify({"success": False, "message": "Agent 身份无效"}), 401

        task = AgentTask.query.get(task_id)
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
            effective_result_summary = _apply_auto_sync_result(task, result_payload)

        if isinstance(effective_result_summary, (dict, list)):
            task.result_summary = json.dumps(effective_result_summary, ensure_ascii=False)
        else:
            task.result_summary = None if effective_result_summary is None else str(effective_result_summary)
        if status == "failed":
            task.error_message = None if error_message is None else str(error_message)

        if task.source_task_id:
            src_task = BackgroundTask.query.get(task.source_task_id)
            if src_task:
                src_task.status = status
                src_task.completed_at = now_utc
                src_task.error_message = task.error_message if status == "failed" else None
                if status == "failed":
                    src_task.retry_count = (src_task.retry_count or 0) + 1

        db.session.commit()
        return jsonify({"success": True}), 200
    except Exception as exc:
        db.session.rollback()
        log_print(f"Agent 回传结果失败: {exc}", "AGENT", force=True)
        return jsonify({"success": False, "message": str(exc)}), 500


def agent_execute_task_proxy(task_id):
    """平台代理执行任务（过渡方案，便于 platform + agent 模式先跑通）。"""
    db, AgentTask, log_print = get_runtime_models("db", "AgentTask", "log_print")
    try:
        ok, resp, code = _validate_agent_shared_secret()
        if not ok:
            return resp, code

        payload = request.get_json(silent=True) or {}
        agent_code = str(payload.get("agent_code") or request.headers.get("X-Agent-Code") or "").strip()
        agent_token = str(payload.get("agent_token") or request.headers.get("X-Agent-Token") or "").strip()
        if not agent_code or not agent_token:
            return jsonify({"success": False, "message": "缺少 agent_code 或 agent_token"}), 400

        agent = _get_agent_by_identity(agent_code, agent_token)
        if not agent:
            return jsonify({"success": False, "message": "Agent 身份无效"}), 401

        task = AgentTask.query.get(task_id)
        if not task:
            return jsonify({"success": False, "message": "任务不存在"}), 404
        if task.assigned_agent_id != agent.id:
            return jsonify({"success": False, "message": "任务不属于当前 Agent"}), 403
        if task.status != "processing":
            return jsonify({"success": False, "message": f"任务状态不是 processing: {task.status}"}), 409

        task_payload = json.loads(task.payload or "{}")
        if "config_id" not in task_payload and task.task_type == "weekly_sync":
            if isinstance(task_payload.get("commit_id"), str) and task_payload.get("commit_id").isdigit():
                task_payload["config_id"] = int(task_payload["commit_id"])
        if "repository_id" not in task_payload and task.repository_id:
            task_payload["repository_id"] = task.repository_id

        from services.task_worker_service import execute_task_inline_for_agent

        result_summary = execute_task_inline_for_agent(task.task_type, task_payload)
        return jsonify(
            {
                "success": True,
                "status": "completed",
                "result_summary": result_summary,
                "result_payload": None,
            }
        ), 200
    except Exception as exc:
        db.session.rollback()
        log_print(f"Agent 代理执行失败: task_id={task_id}, error={exc}", "AGENT", force=True)
        return jsonify(
            {
                "success": True,
                "status": "failed",
                "error_message": str(exc),
            }
        ), 200


@require_admin
def list_agent_nodes():
    """查看 Agent 节点状态（管理员）。"""
    rows = build_agent_node_items()
    return jsonify({"success": True, "items": rows, "count": len(rows)})


@require_admin
def agent_overview_page():
    """Agent 节点信息总览页面。"""
    return render_template("admin_agents.html")


@require_admin
def list_agent_tasks():
    """查看 Agent 任务状态（管理员）。"""
    AgentTask = get_runtime_models("AgentTask")[0]
    status_filter = str(request.args.get("status") or "").strip().lower()
    limit = max(1, min(200, int(request.args.get("limit") or 50)))

    query = AgentTask.query
    if status_filter in {"pending", "processing", "completed", "failed"}:
        query = query.filter_by(status=status_filter)

    rows = query.order_by(AgentTask.id.desc()).limit(limit).all()
    items = []
    for row in rows:
        items.append(
            {
                "id": row.id,
                "task_type": row.task_type,
                "status": row.status,
                "priority": row.priority,
                "project_id": row.project_id,
                "repository_id": row.repository_id,
                "source_task_id": row.source_task_id,
                "assigned_agent_id": row.assigned_agent_id,
                "retry_count": row.retry_count,
                "error_message": row.error_message,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "started_at": row.started_at.isoformat() if row.started_at else None,
                "completed_at": row.completed_at.isoformat() if row.completed_at else None,
            }
        )

    return jsonify({"success": True, "items": items, "count": len(items)})
