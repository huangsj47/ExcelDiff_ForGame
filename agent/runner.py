#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent 运行主循环。"""

from __future__ import annotations

import hashlib
import json
import signal
import sys
import time
from datetime import datetime

try:
    from .config import load_settings
    from .executor import execute_task
    from .http_client import post_json
    from .system_metrics import collect_agent_metrics
except ImportError:
    from config import load_settings
    from executor import execute_task
    from http_client import post_json
    from system_metrics import collect_agent_metrics


_SHUTDOWN = False


def _log(message: str, verbose: bool = True):
    if not verbose:
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[AGENT][{ts}] {message}")


def _handle_signal(signum, frame):
    global _SHUTDOWN
    _SHUTDOWN = True


def _maybe_upload_large_temp_cache(task, task_id, task_type, result_payload, settings, common_headers, agent_token):
    if not settings.temp_cache_upload_enabled:
        return result_payload
    if task_type not in {"excel_diff", "weekly_sync", "weekly_excel_cache"}:
        return result_payload
    if result_payload is None:
        return result_payload
    if not isinstance(result_payload, (dict, list)):
        return result_payload

    try:
        payload_json = json.dumps(result_payload, ensure_ascii=False)
    except Exception:
        return result_payload

    payload_size = len(payload_json.encode("utf-8"))
    if payload_size <= int(settings.temp_cache_threshold_bytes or 0):
        return result_payload

    payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    payload_dict = task.get("payload") or {}
    cache_key = f"{task_type}:{task_id}:{payload_hash[:16]}"
    upsert_url = f"{settings.platform_base_url}/api/agents/cache/upsert"
    upsert_payload = {
        "agent_code": settings.agent_code,
        "agent_token": agent_token,
        "cache_key": cache_key,
        "cache_kind": "task_result_payload",
        "task_type": task_type,
        "source_task_id": task_id,
        "project_id": task.get("project_id") or payload_dict.get("project_id"),
        "repository_id": task.get("repository_id") or payload_dict.get("repository_id"),
        "commit_id": payload_dict.get("commit_id"),
        "file_path": payload_dict.get("file_path"),
        "payload_json": payload_json,
        "payload_hash": payload_hash,
        "payload_size": payload_size,
        "expire_seconds": int(settings.temp_cache_expire_days) * 24 * 3600,
    }
    status, data = post_json(upsert_url, upsert_payload, headers=common_headers, timeout=30)
    if status == 200 and data.get("success"):
        return {
            "platform_cache_key": cache_key,
            "payload_hash": payload_hash,
            "payload_size": payload_size,
            "source": "platform_temp_cache",
        }
    return result_payload


def run_agent():
    global _SHUTDOWN
    settings = load_settings()

    if not settings.agent_shared_secret:
        raise RuntimeError("缺少 AGENT_SHARED_SECRET")

    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_signal)

    register_url = f"{settings.platform_base_url}/api/agents/register"
    heartbeat_url = f"{settings.platform_base_url}/api/agents/heartbeat"
    claim_url = f"{settings.platform_base_url}/api/agents/tasks/claim"
    common_headers = {"X-Agent-Secret": settings.agent_shared_secret}
    agent_token = ""
    last_heartbeat_at = 0.0
    last_metrics_at = 0.0
    cached_metrics = {}
    heartbeat_online_logged = False
    heartbeat_offline_logged = False

    _log(
        f"启动 Agent: code={settings.agent_code}, projects={','.join(settings.project_codes)}, "
        f"platform={settings.platform_base_url}",
        settings.log_verbose,
    )

    while not _SHUTDOWN:
        if not agent_token:
            if not cached_metrics:
                cached_metrics = collect_agent_metrics(settings.repos_base_dir)
                last_metrics_at = time.time()
            register_payload = {
                "agent_code": settings.agent_code,
                "agent_name": settings.agent_name,
                "host": settings.agent_host,
                "port": settings.agent_port,
                "default_admin_username": settings.default_admin_username,
                "project_codes": settings.project_codes,
                "capabilities": {
                    "supports_excel_diff": True,
                    "supports_weekly_diff": True,
                    "runtime": "python",
                },
                **cached_metrics,
            }
            status, data = post_json(register_url, register_payload, headers=common_headers, timeout=15)
            if status == 200 and data.get("success"):
                agent_token = str(data.get("agent_token") or "").strip()
                created = data.get("created_project_codes") or []
                idem = data.get("idempotent_project_codes") or []
                _log(
                    f"注册成功，created={created}, idempotent={idem}",
                    settings.log_verbose,
                )
                heartbeat_online_logged = False
                heartbeat_offline_logged = False
                time.sleep(1)
                continue

            _log(f"注册失败(status={status}): {data}", settings.log_verbose)
            time.sleep(settings.register_retry_interval_seconds)
            continue

        now_ts = time.time()
        should_push_metrics = (not cached_metrics) or (now_ts - last_metrics_at >= settings.metrics_interval_seconds)
        if should_push_metrics:
            cached_metrics = collect_agent_metrics(settings.repos_base_dir)
            last_metrics_at = now_ts
        if now_ts - last_heartbeat_at >= settings.heartbeat_interval_seconds:
            heartbeat_payload = {
                "agent_code": settings.agent_code,
                "agent_token": agent_token,
                "status": "online",
                "host": settings.agent_host,
                "port": settings.agent_port,
            }
            if should_push_metrics:
                heartbeat_payload.update(cached_metrics)
            status, data = post_json(heartbeat_url, heartbeat_payload, headers=common_headers, timeout=10)
            if status == 200 and data.get("success"):
                if not heartbeat_online_logged:
                    _log("心跳连接成功", settings.log_verbose)
                    heartbeat_online_logged = True
                    heartbeat_offline_logged = False
                last_heartbeat_at = now_ts
            else:
                if not heartbeat_offline_logged:
                    _log(f"心跳断线(status={status}): {data}", settings.log_verbose)
                    heartbeat_offline_logged = True
                    heartbeat_online_logged = False
                if status in (401, 403):
                    agent_token = ""
                    heartbeat_online_logged = False
                    heartbeat_offline_logged = False
                    time.sleep(settings.register_retry_interval_seconds)
                    continue

        claim_payload = {
            "agent_code": settings.agent_code,
            "agent_token": agent_token,
            "lease_seconds": max(settings.heartbeat_interval_seconds * 3, 120),
        }
        status, data = post_json(claim_url, claim_payload, headers=common_headers, timeout=15)
        if status != 200 or not data.get("success"):
            _log(f"claim 失败(status={status}): {data}", settings.log_verbose)
            if status in (401, 403):
                agent_token = ""
            time.sleep(settings.register_retry_interval_seconds)
            continue

        task = data.get("task")
        if not task:
            time.sleep(settings.task_poll_interval_seconds)
            continue

        task_id = task.get("id")
        task_type = str(task.get("task_type") or "").strip().lower()
        _log(f"领取任务成功 id={task_id}, type={task_type}", settings.log_verbose)

        exec_status, result_summary, error_message, result_payload = execute_task(task, settings)
        if (
            settings.allow_execute_proxy
            and exec_status == "failed"
            and task_type not in set(settings.local_task_types or [])
        ):
            exec_proxy_url = f"{settings.platform_base_url}/api/agents/tasks/{task_id}/execute-proxy"
            proxy_payload = {
                "agent_code": settings.agent_code,
                "agent_token": agent_token,
            }
            proxy_status, proxy_data = post_json(exec_proxy_url, proxy_payload, headers=common_headers, timeout=300)
            if proxy_status == 200 and proxy_data.get("success"):
                exec_status = str(proxy_data.get("status") or "completed")
                result_summary = proxy_data.get("result_summary")
                error_message = proxy_data.get("error_message")
                result_payload = proxy_data.get("result_payload")
            else:
                _log(
                    f"代理执行失败: status={proxy_status}, body={proxy_data}",
                    settings.log_verbose,
                )

        result_payload = _maybe_upload_large_temp_cache(
            task=task,
            task_id=task_id,
            task_type=task_type,
            result_payload=result_payload,
            settings=settings,
            common_headers=common_headers,
            agent_token=agent_token,
        )

        report_payload = {
            "agent_code": settings.agent_code,
            "agent_token": agent_token,
            "status": exec_status,
            "result_summary": result_summary,
            "error_message": error_message,
            "result_payload": result_payload,
        }
        report_url = f"{settings.platform_base_url}/api/agents/tasks/{task_id}/result"
        rep_status, rep_data = post_json(report_url, report_payload, headers=common_headers, timeout=15)
        if rep_status == 200 and rep_data.get("success"):
            _log(f"任务回传完成 id={task_id}, status={exec_status}", settings.log_verbose)
        else:
            _log(f"任务回传失败 id={task_id}, status={rep_status}, body={rep_data}", settings.log_verbose)

        time.sleep(settings.task_poll_interval_seconds)

    _log("收到退出信号，Agent 已停止", settings.log_verbose)


def main():
    try:
        run_agent()
    except Exception as exc:
        print(f"[AGENT][FATAL] {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
