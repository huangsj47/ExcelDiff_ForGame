#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent 管理相关处理函数。"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from flask import jsonify, render_template, request, send_file

from services.model_loader import get_runtime_models
from services.agent_release_service import (
    get_release_package_path,
    list_release_manifests,
    load_latest_release_manifest,
    load_release_manifest,
    rollback_latest_release,
)
from services.repository_sync_status import clear_sync_error as clear_repository_sync_error
from services.repository_sync_status import record_sync_error as record_repository_sync_error
from utils.request_security import require_admin


_PROJECT_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{2,50}$")
_AGENT_CODE_RE = re.compile(r"[^A-Za-z0-9_-]+")
_TEMP_CACHE_CLEANUP_COOLDOWN_SECONDS = 600
_last_temp_cache_cleanup_at = 0.0
_INCIDENT_MAX_TITLE_LENGTH = 255
_INCIDENT_MAX_MESSAGE_LENGTH = 4000
_INCIDENT_MAX_ERROR_LENGTH = 16000
_INCIDENT_MAX_LOG_EXCERPT_LENGTH = 32000


def _bool_env(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _int_env(key: str, default: int, min_value: int | None = None, max_value: int | None = None) -> int:
    raw = os.environ.get(key)
    value = default
    if raw is not None:
        try:
            value = int(str(raw).strip())
        except Exception:
            value = default
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


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


def _normalize_identity_username(value: str) -> str:
    username = str(value or "").strip()
    if username.endswith("@corp.netease.com"):
        username = username.split("@", 1)[0]
    if "@" in username:
        username = username.split("@", 1)[0]
    return username.strip()


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


def _normalize_text(value, max_length: int, default: str = "") -> str:
    text = str(value or "").strip()
    if not text:
        return default
    if len(text) <= max_length:
        return text
    return text[:max_length]


def _to_iso(dt_value):
    if isinstance(dt_value, datetime):
        return dt_value.isoformat()
    return None



def _parse_host_and_port(raw_value):
    text = str(raw_value or "").strip()
    if not text:
        return "", None
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].strip()
    if not text:
        return "", None

    host = text
    port = None

    if text.startswith("[") and "]" in text:
        # Bracketed IPv6 format: [addr]:port
        end_idx = text.find("]")
        host = text[1:end_idx].strip()
        remainder = text[end_idx + 1 :].strip()
        if remainder.startswith(":"):
            parsed_port = _to_int_or_none(remainder[1:].strip(), min_value=1, max_value=65535)
            if parsed_port is not None:
                port = parsed_port
    elif text.count(":") == 1:
        host_part, port_part = text.rsplit(":", 1)
        parsed_port = _to_int_or_none(port_part.strip(), min_value=1, max_value=65535)
        if parsed_port is not None:
            host = host_part.strip()
            port = parsed_port

    return host.strip(), port


def _resolve_observed_remote_addr(payload: dict):
    x_forwarded_for = str(payload.get("_observed_forwarded_for") or "").strip()
    if x_forwarded_for:
        first_ip = x_forwarded_for.split(",", 1)[0].strip()
        host, _ = _parse_host_and_port(first_ip)
        if host:
            return host

    observed = str(payload.get("_observed_remote_addr") or "").strip()
    host, _ = _parse_host_and_port(observed)
    return host

def _extract_agent_metrics(payload: dict):
    metrics = {
        "cpu_cores": _to_int_or_none(payload.get("cpu_cores"), min_value=1, max_value=4096),
        "cpu_usage_percent": _to_float_or_none(payload.get("cpu_usage_percent"), min_value=0, max_value=100),
        "agent_cpu_usage_percent": _to_float_or_none(payload.get("agent_cpu_usage_percent"), min_value=0, max_value=100),
        "memory_total_bytes": _to_int_or_none(payload.get("memory_total_bytes"), min_value=0),
        "memory_available_bytes": _to_int_or_none(payload.get("memory_available_bytes"), min_value=0),
        "agent_memory_rss_bytes": _to_int_or_none(payload.get("agent_memory_rss_bytes"), min_value=0),
        "disk_free_bytes": _to_int_or_none(payload.get("disk_free_bytes"), min_value=0),
        "os_name": str(payload.get("os_name") or "").strip()[:100] or None,
        "os_version": str(payload.get("os_version") or "").strip()[:200] or None,
        "os_platform": str(payload.get("os_platform") or "").strip()[:300] or None,
    }
    return metrics


def _normalize_utc_datetime(value):
    if value is None:
        return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _agent_offline_timeout_seconds() -> int:
    return _int_env("AGENT_OFFLINE_TIMEOUT_SECONDS", 90, min_value=30, max_value=24 * 3600)


def _derive_agent_status(agent, now_utc: datetime | None = None) -> str:
    status = str(getattr(agent, "status", "") or "").strip().lower() or "offline"
    if status != "online":
        return "offline"

    heartbeat_at = _normalize_utc_datetime(getattr(agent, "last_heartbeat", None))
    if heartbeat_at is None:
        return "offline"

    now = now_utc or datetime.now(timezone.utc)
    if (now - heartbeat_at).total_seconds() > _agent_offline_timeout_seconds():
        return "offline"
    return "online"


def _serialize_incident_brief(row):
    if row is None:
        return None
    return {
        "id": row.id,
        "incident_type": row.incident_type,
        "title": row.title,
        "message": row.message,
        "created_at": _to_iso(row.created_at),
    }


def _build_active_incident_maps(agent_ids):
    AgentIncident = get_runtime_models("AgentIncident")[0]
    if not agent_ids:
        return {}, {}

    rows = (
        AgentIncident.query.filter(
            AgentIncident.agent_id.in_(agent_ids),
            AgentIncident.is_ignored.is_(False),
        )
        .order_by(AgentIncident.created_at.desc())
        .all()
    )

    latest_by_agent_id = {}
    count_by_agent_id = {}
    for row in rows:
        count_by_agent_id[row.agent_id] = int(count_by_agent_id.get(row.agent_id, 0)) + 1
        if row.agent_id not in latest_by_agent_id:
            latest_by_agent_id[row.agent_id] = row
    return latest_by_agent_id, count_by_agent_id


def _apply_agent_runtime_fields(agent, payload: dict):
    now_utc = datetime.now(timezone.utc)
    if "status" in payload:
        status = str(payload.get("status") or "").strip()
        agent.status = status or agent.status or "online"

    reported_host, reported_host_port = _parse_host_and_port(payload.get("ip") or payload.get("host"))
    observed_host = _resolve_observed_remote_addr(payload)
    platform_host, _ = _parse_host_and_port(payload.get("_platform_host"))
    platform_host = platform_host.lower()

    reported_host_lower = reported_host.lower()
    suspicious_host = reported_host_lower in {"", "127.0.0.1", "0.0.0.0", "localhost"}
    if platform_host and reported_host_lower == platform_host:
        suspicious_host = True

    resolved_host = observed_host if (observed_host and suspicious_host) else (reported_host or observed_host)
    if resolved_host:
        agent.host = resolved_host

    port = None
    if "port" in payload:
        port = _to_int_or_none(payload.get("port"), min_value=1, max_value=65535)
    if port is None:
        port = reported_host_port
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


def _format_agent_rows(
    agents,
    bindings_by_agent_id,
    default_admins_by_agent_id,
    latest_incident_by_agent_id,
    incident_count_by_agent_id,
):
    now_utc = datetime.now(timezone.utc)
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
        default_admins = sorted(default_admins_by_agent_id.get(agent.id, set()))
        latest_default_admin = _normalize_identity_username(agent.default_admin_username or "")
        if latest_default_admin and latest_default_admin not in default_admins:
            default_admins.append(latest_default_admin)
            default_admins = sorted(set(default_admins))
        status = _derive_agent_status(agent, now_utc=now_utc)
        active_incident = latest_incident_by_agent_id.get(agent.id)
        incident_count = int(incident_count_by_agent_id.get(agent.id, 0))
        has_offline_alert = status == "offline"
        has_incident_alert = active_incident is not None
        is_abnormal = bool(has_offline_alert or has_incident_alert)
        abnormal_reasons = []
        if has_offline_alert:
            abnormal_reasons.append("offline")
        if has_incident_alert:
            abnormal_reasons.append("incident")
        rows.append(
            {
                "agent_code": agent.agent_code,
                "agent_name": raw_name,
                "display_name": display_name,
                "host": agent.host,
                "port": agent.port,
                "status": status,
                "last_heartbeat": agent.last_heartbeat.isoformat() if agent.last_heartbeat else None,
                "default_admin_username": latest_default_admin,
                "default_admin_usernames": default_admins,
                "project_codes": [binding.project_code for binding in binding_rows],
                "project_count": len(binding_rows),
                "cpu_cores": agent.cpu_cores,
                "cpu_usage_percent": agent.cpu_usage_percent,
                "agent_cpu_usage_percent": agent.agent_cpu_usage_percent,
                "memory_total_bytes": agent.memory_total_bytes,
                "memory_available_bytes": agent.memory_available_bytes,
                "agent_memory_rss_bytes": agent.agent_memory_rss_bytes,
                "disk_free_bytes": agent.disk_free_bytes,
                "os_name": agent.os_name,
                "os_version": agent.os_version,
                "os_platform": agent.os_platform,
                "metrics_updated_at": agent.metrics_updated_at.isoformat() if agent.metrics_updated_at else None,
                "is_abnormal": is_abnormal,
                "abnormal_reasons": abnormal_reasons,
                "active_incident_count": incident_count,
                "active_incident": _serialize_incident_brief(active_incident),
            }
        )
    return rows


def build_agent_node_items():
    AgentNode, AgentProjectBinding, AgentDefaultAdmin = get_runtime_models(
        "AgentNode",
        "AgentProjectBinding",
        "AgentDefaultAdmin",
    )
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

    default_admins_by_agent_id = {}
    admin_rows = AgentDefaultAdmin.query.filter(AgentDefaultAdmin.agent_id.in_(agent_ids)).all()
    for row in admin_rows:
        username = _normalize_identity_username(row.username or "")
        if not username:
            continue
        default_admins_by_agent_id.setdefault(row.agent_id, set()).add(username)

    latest_incident_by_agent_id, incident_count_by_agent_id = _build_active_incident_maps(agent_ids)

    return _format_agent_rows(
        agents,
        bindings_by_agent_id,
        default_admins_by_agent_id,
        latest_incident_by_agent_id,
        incident_count_by_agent_id,
    )


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

    auth_backend = "local"
    try:
        from auth import get_auth_backend

        auth_backend = get_auth_backend()
    except Exception:
        auth_backend = "local"

    try:
        if auth_backend == "qkit":
            from qkit_auth.models import (
                QkitAuthProjectPreAssignment as ProjectPreAssignmentModel,
                QkitAuthUser as UserModel,
                QkitAuthUserProject as UserProjectModel,
            )
        else:
            from auth.models import (
                AuthProjectPreAssignment as ProjectPreAssignmentModel,
                AuthUser as UserModel,
                AuthUserProject as UserProjectModel,
            )
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

    existing_user = UserModel.query.filter_by(username=username).first()
    if existing_user:
        # 不降级已有平台角色；普通用户提升为项目管理员角色（平台级标识）
        if hasattr(existing_user, "role"):
            current_role = str(getattr(existing_user, "role") or "").strip()
            if current_role in {"", "normal"}:
                existing_user.role = "project_admin"
        for project_id in normalized_project_ids:
            membership = UserProjectModel.query.filter_by(
                user_id=existing_user.id,
                project_id=project_id,
            ).first()
            if membership:
                if membership.role != target_role:
                    membership.role = target_role
                    if hasattr(membership, "import_sync_locked"):
                        membership.import_sync_locked = True
                    updated_memberships += 1
                continue

            db.session.add(
                UserProjectModel(
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
        pre = ProjectPreAssignmentModel.query.filter_by(
            username=username,
            project_id=project_id,
        ).first()
        if pre:
            if pre.role != target_role:
                pre.role = target_role
                updated_preassignments += 1
            continue
        db.session.add(
            ProjectPreAssignmentModel(
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


def _temp_cache_retention_days() -> int:
    return _int_env("PLATFORM_TEMP_CACHE_EXPIRE_DAYS", 90, min_value=1, max_value=365)


def _temp_cache_max_payload_bytes() -> int:
    return _int_env("PLATFORM_TEMP_CACHE_MAX_PAYLOAD_BYTES", 20 * 1024 * 1024, min_value=1024 * 100)


def _cleanup_expired_agent_temp_cache(db):
    AgentTempCache = get_runtime_models("AgentTempCache")[0]
    now_utc = datetime.now(timezone.utc)
    deleted = (
        AgentTempCache.query.filter(
            AgentTempCache.expire_at.isnot(None),
            AgentTempCache.expire_at < now_utc,
        ).delete(synchronize_session=False)
    )
    return int(deleted or 0)


def _cleanup_expired_agent_temp_cache_if_needed(db):
    global _last_temp_cache_cleanup_at
    now_ts = datetime.now().timestamp()
    if (now_ts - _last_temp_cache_cleanup_at) < _TEMP_CACHE_CLEANUP_COOLDOWN_SECONDS:
        return 0
    deleted = _cleanup_expired_agent_temp_cache(db)
    _last_temp_cache_cleanup_at = now_ts
    return deleted


def _upsert_agent_temp_cache_entry(*, db, agent, payload: dict):
    AgentTempCache, AgentTask = get_runtime_models("AgentTempCache", "AgentTask")

    cache_key = str(payload.get("cache_key") or "").strip()
    if not cache_key:
        raise ValueError("缺少 cache_key")
    if len(cache_key) > 255:
        raise ValueError("cache_key 超长")

    payload_json = payload.get("payload_json")
    if payload_json is None:
        raise ValueError("缺少 payload_json")
    if not isinstance(payload_json, str):
        payload_json = json.dumps(payload_json, ensure_ascii=False)

    payload_size = int(payload.get("payload_size") or len(payload_json.encode("utf-8")))
    if payload_size <= 0:
        raise ValueError("payload_size 非法")
    max_payload_bytes = _temp_cache_max_payload_bytes()
    if payload_size > max_payload_bytes:
        raise ValueError(f"payload_size 超过限制({payload_size} > {max_payload_bytes})")

    payload_hash = str(payload.get("payload_hash") or "").strip()
    if not payload_hash:
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()

    expire_seconds = _to_int_or_none(payload.get("expire_seconds"), min_value=60, max_value=365 * 24 * 3600)
    if expire_seconds is not None:
        expire_at = datetime.now(timezone.utc) + timedelta(seconds=expire_seconds)
    else:
        expire_at = datetime.now(timezone.utc) + timedelta(days=_temp_cache_retention_days())

    source_task_id = _to_int_or_none(payload.get("source_task_id"), min_value=1)
    if source_task_id is None:
        source_task_id = _to_int_or_none(payload.get("task_id"), min_value=1)
    source_task = db.session.get(AgentTask, source_task_id) if source_task_id else None

    project_id = _to_int_or_none(payload.get("project_id"), min_value=1)
    if project_id is None and source_task is not None:
        project_id = source_task.project_id

    repository_id = _to_int_or_none(payload.get("repository_id"), min_value=1)
    if repository_id is None and source_task is not None:
        repository_id = source_task.repository_id

    commit_id = str(payload.get("commit_id") or "").strip() or None
    file_path = str(payload.get("file_path") or "").strip() or None
    task_type = str(payload.get("task_type") or "").strip() or (source_task.task_type if source_task else None)
    cache_kind = str(payload.get("cache_kind") or "").strip() or None

    row = AgentTempCache.query.filter_by(cache_key=cache_key).first()
    if row is None:
        row = AgentTempCache(
            cache_key=cache_key,
            source_agent_id=agent.id,
        )
        db.session.add(row)

    row.task_type = task_type
    row.cache_kind = cache_kind
    row.project_id = project_id
    row.repository_id = repository_id
    row.commit_id = commit_id
    row.file_path = file_path
    row.payload_json = payload_json
    row.payload_hash = payload_hash
    row.payload_size = payload_size
    row.source_agent_id = agent.id
    row.source_task_id = source_task.id if source_task else source_task_id
    row.expire_at = expire_at
    return row


def _dispatch_agent_local_cache_fetch(*, db, cache_key: str, expected_hash: str, project_id: int, repository_id: int | None):
    task_payload = {
        "cache_key": cache_key,
        "expected_hash": expected_hash or None,
        "project_id": project_id,
        "repository_id": repository_id,
    }
    task = enqueue_agent_task(
        task_type="temp_cache_fetch",
        project_id=project_id,
        repository_id=repository_id,
        source_task_id=None,
        priority=2,
        payload=task_payload,
    )
    db.session.flush()
    return task.id if task else None


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
        if raw_value.tzinfo is None:
            return raw_value.replace(tzinfo=timezone.utc)
        return raw_value.astimezone(timezone.utc)
    text = str(raw_value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _apply_auto_sync_result(task, result_payload):
    db, Commit, Repository, WeeklyVersionConfig = get_runtime_models(
        "db",
        "Commit",
        "Repository",
        "WeeklyVersionConfig",
    )
    repository = db.session.get(Repository, task.repository_id) if task.repository_id else None
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
    weekly_sync_tasks_added = 0
    if new_commit_objects:
        from services.task_worker_service import add_excel_diff_task, create_weekly_sync_task

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

        weekly_configs = WeeklyVersionConfig.query.filter_by(
            repository_id=repository.id,
            is_active=True,
            auto_sync=True,
            status="active",
        ).all()
        for config in weekly_configs:
            task_id = create_weekly_sync_task(config.id)
            if task_id:
                weekly_sync_tasks_added += 1

    repository.last_sync_time = datetime.now(timezone.utc)
    if latest_commit_id:
        repository.last_sync_commit_id = latest_commit_id
    repository.clone_status = "completed"
    repository.clone_error = None

    return {
        "message": "auto_sync result applied",
        "commits_added": len(new_commit_objects),
        "excel_tasks_added": excel_tasks_added,
        "weekly_sync_tasks_added": weekly_sync_tasks_added,
        "latest_commit_id": latest_commit_id,
    }


def register_agent_node():
    """Agent 注册并申请绑定项目代号。"""
    db, AgentNode, AgentProjectBinding, AgentDefaultAdmin, Project, log_print = get_runtime_models(
        "db",
        "AgentNode",
        "AgentProjectBinding",
        "AgentDefaultAdmin",
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
        default_admin_username = _normalize_identity_username(payload.get("default_admin_username") or "") or None
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
                "_observed_remote_addr": request.remote_addr,
                "_observed_forwarded_for": request.headers.get("X-Forwarded-For"),
                "_platform_host": request.host,
            },
        )
        agent.default_admin_username = default_admin_username
        agent.capabilities = None if capabilities is None else str(capabilities)
        agent.last_error = None
        default_admin_record_created = False
        if default_admin_username:
            existed = AgentDefaultAdmin.query.filter_by(
                agent_id=agent.id,
                username=default_admin_username,
            ).first()
            if existed is None:
                db.session.add(
                    AgentDefaultAdmin(
                        agent_id=agent.id,
                        username=default_admin_username,
                    )
                )
                default_admin_record_created = True

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

        existing_bindings = AgentProjectBinding.query.filter_by(agent_id=agent.id).all()
        all_bound_project_ids = sorted(
            {
                int(pid)
                for pid in ([row.project_id for row in existing_bindings] + bound_project_ids)
                if pid
            }
        )

        default_admin_assignment = _ensure_default_admin_for_projects(
            db,
            default_admin_username,
            all_bound_project_ids,
        )

        default_admin_usernames = sorted(
            {
                _normalize_identity_username(row.username or "")
                for row in AgentDefaultAdmin.query.filter_by(agent_id=agent.id).all()
                if _normalize_identity_username(row.username or "")
            }
        )
        if default_admin_username and default_admin_username not in default_admin_usernames:
            default_admin_usernames.append(default_admin_username)
            default_admin_usernames = sorted(set(default_admin_usernames))

        db.session.commit()
        log_print(
            f"Agent 注册成功: {agent_code}, 创建项目={len(created_projects)}, 幂等项目={len(idempotent_projects)}, "
            f"总绑定项目={len(all_bound_project_ids)}",
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
                "all_bound_project_count": len(all_bound_project_ids),
                "default_admin_record_created": default_admin_record_created,
                "default_admin_usernames": default_admin_usernames,
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
                "_observed_remote_addr": request.remote_addr,
                "_observed_forwarded_for": request.headers.get("X-Forwarded-For"),
                "_platform_host": request.host,
            },
        )
        _cleanup_expired_agent_temp_cache_if_needed(db)

        db.session.commit()
        return jsonify({"success": True, "server_time": datetime.now(timezone.utc).isoformat()})
    except Exception as exc:
        db.session.rollback()
        log_print(f"Agent 心跳失败: {exc}", "AGENT", force=True)
        return jsonify({"success": False, "message": str(exc)}), 500


def agent_report_incident():
    """Agent 主动上报运行异常/中断事件。"""
    db, AgentNode, AgentIncident, log_print = get_runtime_models(
        "db",
        "AgentNode",
        "AgentIncident",
        "log_print",
    )
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

        incident_type = _normalize_text(payload.get("incident_type"), 40, default="runtime_error").lower()
        title = _normalize_text(payload.get("title"), _INCIDENT_MAX_TITLE_LENGTH, default="Agent运行异常")
        message = _normalize_text(payload.get("message"), _INCIDENT_MAX_MESSAGE_LENGTH, default="")
        error_detail = _normalize_text(payload.get("error_detail"), _INCIDENT_MAX_ERROR_LENGTH, default="")
        log_excerpt = _normalize_text(payload.get("log_excerpt"), _INCIDENT_MAX_LOG_EXCERPT_LENGTH, default="")

        row = AgentIncident(
            agent_id=agent.id,
            incident_type=incident_type or "runtime_error",
            title=title,
            message=message or None,
            error_detail=error_detail or None,
            log_excerpt=log_excerpt or None,
            is_ignored=False,
        )
        db.session.add(row)

        agent.last_error = message or title
        if incident_type in {"interrupted", "fatal", "runtime_fatal"}:
            agent.status = "offline"

        db.session.commit()
        return jsonify({"success": True, "incident_id": row.id}), 200
    except Exception as exc:
        db.session.rollback()
        log_print(f"Agent 异常上报失败: {exc}", "AGENT", force=True)
        return jsonify({"success": False, "message": str(exc)}), 500


def agent_upsert_temp_cache():
    """Agent 上传临时加速缓存（平台侧仅作可过期加速，不作为主业务数据）。"""
    db, log_print = get_runtime_models("db", "log_print")
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

        row = _upsert_agent_temp_cache_entry(db=db, agent=agent, payload=payload)
        _cleanup_expired_agent_temp_cache_if_needed(db)
        db.session.commit()
        return jsonify(
            {
                "success": True,
                "cache_key": row.cache_key,
                "payload_size": row.payload_size,
                "expire_at": row.expire_at.isoformat() if row.expire_at else None,
            }
        ), 200
    except Exception as exc:
        db.session.rollback()
        log_print(f"Agent 临时缓存写入失败: {exc}", "AGENT", force=True)
        return jsonify({"success": False, "message": str(exc)}), 400


@require_admin
def get_agent_temp_cache(cache_key):
    """管理员读取平台临时加速缓存。"""
    db, AgentTempCache, log_print = get_runtime_models("db", "AgentTempCache", "log_print")
    try:
        if not cache_key:
            return jsonify({"success": False, "message": "缺少 cache_key"}), 400
        _cleanup_expired_agent_temp_cache_if_needed(db)
        row = AgentTempCache.query.filter_by(cache_key=cache_key).first()
        if row is None:
            return jsonify({"success": False, "message": "缓存不存在"}), 404
        now_utc = datetime.now(timezone.utc)
        expire_at = row.expire_at
        if expire_at is not None and getattr(expire_at, "tzinfo", None) is None:
            expire_at = expire_at.replace(tzinfo=timezone.utc)
        if expire_at and expire_at < now_utc:
            db.session.delete(row)
            db.session.commit()
            return jsonify({"success": False, "message": "缓存已过期"}), 404
        return jsonify(
            {
                "success": True,
                "cache_key": row.cache_key,
                "task_type": row.task_type,
                "cache_kind": row.cache_kind,
                "project_id": row.project_id,
                "repository_id": row.repository_id,
                "commit_id": row.commit_id,
                "file_path": row.file_path,
                "payload_json": row.payload_json,
                "payload_hash": row.payload_hash,
                "payload_size": row.payload_size,
                "expire_at": row.expire_at.isoformat() if row.expire_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
        ), 200
    except Exception as exc:
        log_print(f"读取平台临时缓存失败: {exc}", "AGENT", force=True)
        return jsonify({"success": False, "message": str(exc)}), 500


@require_admin
def resolve_agent_temp_cache(cache_key):
    """解析平台临时缓存；未命中或哈希不一致时可触发重算任务。"""
    db, AgentTempCache, Repository, log_print = get_runtime_models("db", "AgentTempCache", "Repository", "log_print")
    try:
        if not cache_key:
            return jsonify({"success": False, "message": "缺少 cache_key"}), 400

        expected_hash = str(request.args.get("expected_hash") or "").strip()
        try_agent_fetch = str(request.args.get("try_agent_fetch") or "1").strip().lower() in {"1", "true", "yes"}
        trigger_recompute = str(request.args.get("trigger_recompute") or "").strip().lower() in {"1", "true", "yes"}
        repository_id = _to_int_or_none(request.args.get("repository_id"), min_value=1)
        project_id = _to_int_or_none(request.args.get("project_id"), min_value=1)
        _cleanup_expired_agent_temp_cache_if_needed(db)

        row = AgentTempCache.query.filter_by(cache_key=cache_key).first()
        now_utc = datetime.now(timezone.utc)
        fallback_project_id = None
        fallback_repository_id = repository_id
        if row is not None:
            fallback_project_id = row.project_id
            if fallback_repository_id is None and row.repository_id:
                fallback_repository_id = row.repository_id
            expire_at = row.expire_at
            if expire_at is not None and getattr(expire_at, "tzinfo", None) is None:
                expire_at = expire_at.replace(tzinfo=timezone.utc)
            if expire_at and expire_at < now_utc:
                db.session.delete(row)
                db.session.commit()
                row = None
            else:
                if expected_hash and row.payload_hash and expected_hash != row.payload_hash:
                    row = None
                else:
                    return (
                        jsonify(
                            {
                                "success": True,
                                "status": "hit",
                                "source": "platform_temp_cache",
                                "cache_key": cache_key,
                                "payload_json": row.payload_json,
                                "payload_hash": row.payload_hash,
                                "payload_size": row.payload_size,
                                "expire_at": expire_at.isoformat() if expire_at else None,
                            }
                        ),
                        200,
                    )

        effective_repository_id = fallback_repository_id
        effective_project_id = project_id or fallback_project_id
        if effective_project_id is None and effective_repository_id:
            repo = db.session.get(Repository, effective_repository_id)
            if repo:
                effective_project_id = repo.project_id

        if try_agent_fetch and effective_project_id:
            fetch_task_id = _dispatch_agent_local_cache_fetch(
                db=db,
                cache_key=cache_key,
                expected_hash=expected_hash,
                project_id=effective_project_id,
                repository_id=effective_repository_id,
            )
            db.session.commit()
            return (
                jsonify(
                    {
                        "success": True,
                        "status": "pending_agent_fetch",
                        "cache_key": cache_key,
                        "project_id": effective_project_id,
                        "repository_id": effective_repository_id,
                        "task_id": fetch_task_id,
                    }
                ),
                202,
            )

        if trigger_recompute and effective_repository_id:
            from services.task_worker_service import create_auto_sync_task

            task_id = create_auto_sync_task(effective_repository_id)
            if task_id:
                return (
                    jsonify(
                        {
                            "success": True,
                            "status": "pending_recompute",
                            "cache_key": cache_key,
                            "repository_id": effective_repository_id,
                            "task_id": task_id,
                        }
                    ),
                    202,
                )
            return (
                jsonify(
                    {
                        "success": False,
                        "status": "recompute_dispatch_failed",
                        "cache_key": cache_key,
                        "repository_id": effective_repository_id,
                    }
                ),
                409,
            )

        return jsonify({"success": False, "status": "miss", "cache_key": cache_key}), 404
    except Exception as exc:
        log_print(f"解析平台临时缓存失败: {exc}", "AGENT", force=True)
        return jsonify({"success": False, "message": str(exc)}), 500


def agent_get_latest_release():
    """Agent 查询最新可用 release。"""
    log_print = get_runtime_models("log_print")[0]
    try:
        ok, resp, code = _validate_agent_shared_secret()
        if not ok:
            return resp, code

        payload = request.get_json(silent=True) or {}
        agent_code = str(payload.get("agent_code") or request.headers.get("X-Agent-Code") or "").strip()
        agent_token = str(payload.get("agent_token") or request.headers.get("X-Agent-Token") or "").strip()
        current_version = str(payload.get("current_version") or "").strip()
        if not agent_code or not agent_token:
            return jsonify({"success": False, "message": "缺少 agent_code 或 agent_token"}), 400

        agent = _get_agent_by_identity(agent_code, agent_token)
        if not agent:
            return jsonify({"success": False, "message": "Agent 身份无效"}), 401

        latest = load_latest_release_manifest()
        if not latest:
            return (
                jsonify(
                    {
                        "success": True,
                        "has_update": False,
                        "status": "no_release",
                        "current_version": current_version,
                    }
                ),
                200,
            )

        latest_version = str(latest.get("version") or "").strip()
        if not latest_version:
            return (
                jsonify(
                    {
                        "success": True,
                        "has_update": False,
                        "status": "invalid_release",
                        "current_version": current_version,
                    }
                ),
                200,
            )

        has_update = latest_version != current_version
        return (
            jsonify(
                {
                    "success": True,
                    "has_update": has_update,
                    "status": "update_available" if has_update else "up_to_date",
                    "current_version": current_version,
                    "latest_version": latest_version,
                    "release": {
                        "version": latest_version,
                        "commit_id": latest.get("commit_id"),
                        "created_at": latest.get("created_at"),
                        "notes": latest.get("notes"),
                        "package_size": latest.get("package_size"),
                        "package_sha256": latest.get("package_sha256"),
                        "download_path": f"/api/agents/releases/{latest_version}/package",
                    },
                }
            ),
            200,
        )
    except Exception as exc:
        log_print(f"读取 Agent release 信息失败: {exc}", "AGENT", force=True)
        return jsonify({"success": False, "message": str(exc)}), 500


def agent_download_release_package(version: str):
    """Agent 下载指定版本 release 包。"""
    log_print = get_runtime_models("log_print")[0]
    try:
        ok, resp, code = _validate_agent_shared_secret()
        if not ok:
            return resp, code

        agent_code = str(
            request.args.get("agent_code")
            or request.headers.get("X-Agent-Code")
            or ""
        ).strip()
        agent_token = str(
            request.args.get("agent_token")
            or request.headers.get("X-Agent-Token")
            or ""
        ).strip()
        if not agent_code or not agent_token:
            return jsonify({"success": False, "message": "缺少 agent_code 或 agent_token"}), 400

        agent = _get_agent_by_identity(agent_code, agent_token)
        if not agent:
            return jsonify({"success": False, "message": "Agent 身份无效"}), 401

        manifest = load_release_manifest(version)
        if not manifest:
            return jsonify({"success": False, "message": "release 不存在"}), 404
        package_path = get_release_package_path(version)
        if not package_path:
            return jsonify({"success": False, "message": "release 包不存在"}), 404

        package_name = str(manifest.get("package_file") or os.path.basename(package_path))
        return send_file(
            package_path,
            mimetype="application/zip",
            as_attachment=True,
            download_name=package_name,
            conditional=True,
        )
    except Exception as exc:
        log_print(f"Agent 下载 release 包失败: {exc}", "AGENT", force=True)
        return jsonify({"success": False, "message": str(exc)}), 500


@require_admin
def list_agent_releases():
    """管理员查看 release 列表。"""
    log_print = get_runtime_models("log_print")[0]
    try:
        latest = load_latest_release_manifest()
        latest_version = str((latest or {}).get("version") or "").strip()
        items = []
        for item in list_release_manifests():
            version = str(item.get("version") or "").strip()
            if not version:
                continue
            items.append(
                {
                    "version": version,
                    "commit_id": item.get("commit_id"),
                    "created_at": item.get("created_at"),
                    "notes": item.get("notes"),
                    "package_file": item.get("package_file"),
                    "package_size": item.get("package_size"),
                    "package_sha256": item.get("package_sha256"),
                    "is_latest": bool(version and version == latest_version),
                }
            )
        return jsonify(
            {
                "success": True,
                "latest_version": latest_version or None,
                "items": items,
                "count": len(items),
            }
        )
    except Exception as exc:
        log_print(f"读取 Agent release 列表失败: {exc}", "AGENT", force=True)
        return jsonify({"success": False, "message": str(exc)}), 500


@require_admin
def rollback_agent_release():
    """管理员一键回滚 latest release（默认回滚到上一版）。"""
    log_print = get_runtime_models("log_print")[0]
    try:
        payload = request.get_json(silent=True) or {}
        target_version = str(payload.get("target_version") or "").strip() or None
        steps = int(payload.get("steps") or 1)
        steps = max(1, min(50, steps))

        result = rollback_latest_release(target_version=target_version, steps=steps)
        latest = result.get("latest") if isinstance(result, dict) else None
        latest_version = str((latest or {}).get("version") or "").strip() or None
        return jsonify(
            {
                "success": True,
                "changed": bool(result.get("changed")),
                "from_version": result.get("from_version"),
                "to_version": result.get("to_version"),
                "latest_version": latest_version,
                "latest_release": latest or None,
            }
        )
    except Exception as exc:
        log_print(f"回滚 Agent release 失败: {exc}", "AGENT", force=True)
        return jsonify({"success": False, "message": str(exc)}), 400


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
    db, AgentTask, BackgroundTask, Repository, log_print = get_runtime_models(
        "db",
        "AgentTask",
        "BackgroundTask",
        "Repository",
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
            effective_result_summary = _apply_auto_sync_result(task, result_payload)
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
                row = _upsert_agent_temp_cache_entry(db=db, agent=agent, payload=cache_payload)
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
                record_repository_sync_error(
                    db.session,
                    repository,
                    sync_error_message,
                    log_func=log_print,
                    log_type="AGENT",
                    commit=False,
                )
            elif status == "completed" and task.task_type == "auto_sync":
                clear_repository_sync_error(
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
        return jsonify({"success": True}), 200
    except Exception as exc:
        db.session.rollback()
        log_print(f"Agent 回传结果失败: {exc}", "AGENT", force=True)
        return jsonify({"success": False, "message": str(exc)}), 500


@require_admin
def list_agent_nodes():
    """查看 Agent 节点状态（管理员）。"""
    rows = build_agent_node_items()
    response = jsonify(
        {
            "success": True,
            "items": rows,
            "count": len(rows),
            "server_time": datetime.now(timezone.utc).isoformat(),
        }
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@require_admin
def get_agent_abnormal_summary():
    """返回异常Agent数量，供导航角标展示。"""
    rows = build_agent_node_items()
    abnormal_rows = [row for row in rows if bool(row.get("is_abnormal"))]
    offline_count = 0
    incident_count = 0
    for row in abnormal_rows:
        reasons = set(row.get("abnormal_reasons") or [])
        if "offline" in reasons:
            offline_count += 1
        if "incident" in reasons:
            incident_count += 1
    return jsonify(
        {
            "success": True,
            "abnormal_count": len(abnormal_rows),
            "offline_count": offline_count,
            "incident_count": incident_count,
        }
    )


@require_admin
def list_agent_incidents(agent_code: str):
    """查看指定 Agent 的异常事件。"""
    AgentNode, AgentIncident = get_runtime_models("AgentNode", "AgentIncident")
    limit = max(1, min(200, int(request.args.get("limit") or 50)))

    agent = AgentNode.query.filter_by(agent_code=str(agent_code or "").strip()).first()
    if not agent:
        return jsonify({"success": False, "message": "Agent 不存在"}), 404

    rows = (
        AgentIncident.query.filter_by(agent_id=agent.id)
        .order_by(AgentIncident.created_at.desc())
        .limit(limit)
        .all()
    )
    items = []
    for row in rows:
        items.append(
            {
                "id": row.id,
                "incident_type": row.incident_type,
                "title": row.title,
                "message": row.message,
                "error_detail": row.error_detail,
                "log_excerpt": row.log_excerpt,
                "is_ignored": bool(row.is_ignored),
                "ignored_by": row.ignored_by,
                "ignored_at": _to_iso(row.ignored_at),
                "created_at": _to_iso(row.created_at),
                "updated_at": _to_iso(row.updated_at),
            }
        )

    return jsonify(
        {
            "success": True,
            "agent_code": agent.agent_code,
            "agent_name": agent.agent_name,
            "items": items,
            "count": len(items),
        }
    )


@require_admin
def ignore_agent_incident(incident_id: int):
    """忽略或恢复 Agent 异常事件。"""
    db, AgentIncident = get_runtime_models("db", "AgentIncident")
    payload = request.get_json(silent=True) or {}
    ignored = bool(payload.get("ignored", True))

    row = db.session.get(AgentIncident, incident_id)
    if not row:
        return jsonify({"success": False, "message": "异常事件不存在"}), 404

    username = _normalize_identity_username(request.headers.get("X-Auth-Username") or "") or None
    if ignored:
        row.is_ignored = True
        row.ignored_by = username or "admin"
        row.ignored_at = datetime.now(timezone.utc)
    else:
        row.is_ignored = False
        row.ignored_by = None
        row.ignored_at = None

    db.session.commit()
    return jsonify(
        {
            "success": True,
            "incident_id": row.id,
            "is_ignored": bool(row.is_ignored),
            "ignored_by": row.ignored_by,
            "ignored_at": _to_iso(row.ignored_at),
        }
    )


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









