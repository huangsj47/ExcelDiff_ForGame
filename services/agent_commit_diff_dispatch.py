"""Agent commit diff dispatch/orchestration helpers.

Platform+agent mode:
- Validate project-agent binding.
- Check agent online status.
- Dispatch commit_diff task to agent.
- Resolve ready payload from inline task summary or platform temp cache.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from models import AgentNode, AgentProjectBinding, AgentTask, AgentTempCache, db
from services.agent_management_handlers import enqueue_agent_task
from utils.logger import log_print

_PENDING_STATUSES = {"pending", "processing"}


def is_agent_dispatch_mode() -> bool:
    return (os.environ.get("DEPLOYMENT_MODE") or "single").strip().lower() in {"platform", "agent"}


def _int_env(name: str, default: int, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        value = int(str(os.environ.get(name, default)).strip())
    except Exception:
        value = int(default)
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def _normalize_utc_datetime(value):
    if value is None:
        return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _agent_online(agent) -> bool:
    if agent is None:
        return False
    status = str(getattr(agent, "status", "") or "").strip().lower()
    if status != "online":
        return False
    heartbeat_at = _normalize_utc_datetime(getattr(agent, "last_heartbeat", None))
    if heartbeat_at is None:
        return False
    timeout_seconds = _int_env("AGENT_OFFLINE_TIMEOUT_SECONDS", 90, min_value=30, max_value=24 * 3600)
    now_utc = datetime.now(timezone.utc)
    return (now_utc - heartbeat_at).total_seconds() <= timeout_seconds


def _safe_json_loads(raw: Any):
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _extract_task_payload(task):
    return _safe_json_loads(getattr(task, "payload", None)) or {}


def _payload_matches_commit(task_payload: dict, commit_record_id: int) -> bool:
    try:
        payload_commit_id = int(task_payload.get("commit_record_id") or 0)
    except Exception:
        payload_commit_id = 0
    return payload_commit_id == int(commit_record_id)


def _find_latest_commit_diff_task(project_id: int, repository_id: int, commit_record_id: int):
    # 按最新任务优先，避免误用旧任务摘要。
    candidates = (
        AgentTask.query.filter_by(
            task_type="commit_diff",
            project_id=project_id,
            repository_id=repository_id,
        )
        .order_by(AgentTask.id.desc())
        .limit(120)
        .all()
    )
    for task in candidates:
        payload = _extract_task_payload(task)
        if _payload_matches_commit(payload, commit_record_id):
            return task, payload
    return None, None


def _ensure_commit_diff_task(commit, project_id: int, repository_id: int, priority: int = 3):
    existing_task, _ = _find_latest_commit_diff_task(project_id, repository_id, commit.id)
    if existing_task and str(existing_task.status or "").lower() in _PENDING_STATUSES:
        return existing_task, False

    payload = {
        "commit_record_id": int(commit.id),
        "repository_id": int(repository_id),
        "project_id": int(project_id),
        "commit_sha": str(commit.commit_id or ""),
        "file_path": str(commit.path or ""),
        "operation": str(commit.operation or "M"),
        "request_key": f"commit_diff:{int(commit.id)}",
    }
    task = enqueue_agent_task(
        task_type="commit_diff",
        project_id=project_id,
        repository_id=repository_id,
        source_task_id=None,
        priority=priority,
        payload=payload,
    )
    db.session.flush()
    return task, True


def _ensure_temp_cache_fetch_task(project_id: int, repository_id: int | None, cache_key: str, expected_hash: str | None):
    key = str(cache_key or "").strip()
    if not key:
        return None, False

    expected_hash = str(expected_hash or "").strip() or None
    existing_tasks = (
        AgentTask.query.filter(
            AgentTask.task_type == "temp_cache_fetch",
            AgentTask.project_id == project_id,
            AgentTask.status.in_(list(_PENDING_STATUSES)),
        )
        .order_by(AgentTask.id.desc())
        .limit(80)
        .all()
    )
    for task in existing_tasks:
        payload = _extract_task_payload(task)
        if str(payload.get("cache_key") or "").strip() == key:
            return task, False

    task_payload = {
        "cache_key": key,
        "expected_hash": expected_hash,
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
    return task, True


def _load_platform_cache_payload(cache_key: str, expected_hash: str | None = None):
    key = str(cache_key or "").strip()
    if not key:
        return None

    row = AgentTempCache.query.filter_by(cache_key=key).first()
    if not row:
        return None

    now_utc = datetime.now(timezone.utc)
    expire_at = _normalize_utc_datetime(getattr(row, "expire_at", None))
    if expire_at and expire_at < now_utc:
        try:
            db.session.delete(row)
            db.session.flush()
        except Exception:
            pass
        return None

    expected = str(expected_hash or "").strip()
    if expected and row.payload_hash and str(row.payload_hash).strip() != expected:
        return None

    payload_obj = _safe_json_loads(row.payload_json)
    if not isinstance(payload_obj, dict):
        return None
    return payload_obj


def _extract_ready_payload_from_summary(summary: dict):
    if not isinstance(summary, dict):
        return None

    inline_payload = summary.get("inline_payload")
    if isinstance(inline_payload, dict):
        return inline_payload

    inline_payload_json = summary.get("inline_payload_json")
    if inline_payload_json:
        parsed = _safe_json_loads(inline_payload_json)
        if isinstance(parsed, dict):
            return parsed

    cache_key = str(summary.get("cache_key") or "").strip()
    if cache_key:
        expected_hash = str(summary.get("payload_hash") or "").strip() or None
        cached_payload = _load_platform_cache_payload(cache_key, expected_hash=expected_hash)
        if isinstance(cached_payload, dict):
            return cached_payload

    return None


def dispatch_or_get_commit_diff(commit, *, force_retry: bool = False):
    """Agent 模式下：获取或派发提交diff任务。"""
    repository = getattr(commit, "repository", None)
    project = getattr(repository, "project", None) if repository else None
    if repository is None or project is None:
        return {
            "status": "error",
            "message": "提交仓库或项目信息缺失",
        }

    binding = AgentProjectBinding.query.filter_by(project_id=project.id).first()
    if binding is None:
        return {
            "status": "unbound",
            "message": f"项目 {project.code or project.id} 未绑定 Agent，无法在 platform+agent 模式下计算diff",
            "project_id": project.id,
            "repository_id": repository.id,
        }

    agent = db.session.get(AgentNode, binding.agent_id) if binding.agent_id else None
    online = _agent_online(agent)

    task, _task_payload = _find_latest_commit_diff_task(project.id, repository.id, commit.id)
    if task is not None:
        task_status = str(task.status or "").strip().lower()

        if task_status == "completed":
            summary = _safe_json_loads(task.result_summary)
            payload = _extract_ready_payload_from_summary(summary if isinstance(summary, dict) else {})
            if isinstance(payload, dict):
                return {
                    "status": "ready",
                    "task_id": task.id,
                    "payload": payload,
                    "project_id": project.id,
                    "repository_id": repository.id,
                    "agent_online": online,
                }

            summary_dict = summary if isinstance(summary, dict) else {}
            cache_key = str(summary_dict.get("cache_key") or "").strip()
            payload_hash = str(summary_dict.get("payload_hash") or "").strip() or None
            if cache_key:
                if online:
                    fetch_task, created = _ensure_temp_cache_fetch_task(
                        project.id,
                        repository.id,
                        cache_key,
                        payload_hash,
                    )
                    if created:
                        db.session.commit()
                    return {
                        "status": "pending",
                        "message": "正在从 Agent 拉取缓存结果",
                        "task_id": task.id,
                        "fetch_task_id": fetch_task.id if fetch_task else None,
                        "cache_key": cache_key,
                        "retry_after_seconds": 60,
                        "project_id": project.id,
                        "repository_id": repository.id,
                        "agent_online": True,
                    }

                return {
                    "status": "pending_offline",
                    "message": "Agent 离线，等待上线后可继续拉取diff结果",
                    "task_id": task.id,
                    "cache_key": cache_key,
                    "retry_after_seconds": 60,
                    "project_id": project.id,
                    "repository_id": repository.id,
                    "agent_online": False,
                }

            if not force_retry:
                return {
                    "status": "error",
                    "message": "diff任务已完成但结果不可用，请手动重试",
                    "task_id": task.id,
                    "project_id": project.id,
                    "repository_id": repository.id,
                    "agent_online": online,
                }

        if task_status in _PENDING_STATUSES:
            return {
                "status": "pending" if online else "pending_offline",
                "message": "Agent 正在处理diff任务" if online else "Agent 离线，任务处理中断",
                "task_id": task.id,
                "retry_after_seconds": 60,
                "project_id": project.id,
                "repository_id": repository.id,
                "agent_online": online,
            }

        if task_status == "failed" and not force_retry:
            if not online:
                return {
                    "status": "pending_offline",
                    "message": "Agent 离线，暂无法自动重试",
                    "task_id": task.id,
                    "retry_after_seconds": 60,
                    "project_id": project.id,
                    "repository_id": repository.id,
                    "agent_online": False,
                }
            # 失败后允许自动补派一次新任务
            new_task, _created = _ensure_commit_diff_task(commit, project.id, repository.id, priority=2)
            db.session.commit()
            return {
                "status": "pending",
                "message": "diff任务失败，已重新派发",
                "task_id": new_task.id if new_task else None,
                "retry_after_seconds": 60,
                "project_id": project.id,
                "repository_id": repository.id,
                "agent_online": True,
            }

    if not online:
        return {
            "status": "pending_offline",
            "message": "Agent 离线，等待上线后可执行diff",
            "task_id": task.id if task else None,
            "retry_after_seconds": 60,
            "project_id": project.id,
            "repository_id": repository.id,
            "agent_online": False,
        }

    try:
        new_task, _created = _ensure_commit_diff_task(commit, project.id, repository.id, priority=2 if force_retry else 3)
        db.session.commit()
        return {
            "status": "pending",
            "message": "已派发 Agent diff 任务",
            "task_id": new_task.id if new_task else None,
            "retry_after_seconds": 60,
            "project_id": project.id,
            "repository_id": repository.id,
            "agent_online": True,
        }
    except Exception as exc:
        db.session.rollback()
        log_print(f"派发 commit_diff 任务失败: {exc}", "AGENT", force=True)
        return {
            "status": "error",
            "message": f"派发 Agent diff 任务失败: {exc}",
            "project_id": project.id,
            "repository_id": repository.id,
            "agent_online": True,
        }
