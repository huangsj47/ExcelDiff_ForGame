#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent 运行主循环（增强版：失败回传重试）。"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
import time
import traceback
from collections import deque
from datetime import datetime

try:
    from .config import load_settings
    from .executor import execute_task
    from .http_client import post_json
    from .self_update import check_and_apply_update, get_local_release_version
    from .system_metrics import collect_agent_metrics
except ImportError:
    from config import load_settings
    from executor import execute_task
    from http_client import post_json
    from self_update import check_and_apply_update, get_local_release_version
    from system_metrics import collect_agent_metrics


_SHUTDOWN = False
_LAST_SIGNAL_NAME = ""
_LAST_SETTINGS = None
_LAST_COMMON_HEADERS = None
_LAST_AGENT_TOKEN = ""


def _log(message: str, verbose: bool = True):
    if not verbose:
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[AGENT][{ts}] {message}")


def _handle_signal(signum, frame):
    global _SHUTDOWN, _LAST_SIGNAL_NAME
    _SHUTDOWN = True
    try:
        _LAST_SIGNAL_NAME = signal.Signals(signum).name
    except Exception:
        _LAST_SIGNAL_NAME = f"SIGNAL_{signum}"


def _is_virtual_env() -> bool:
    if getattr(sys, "real_prefix", None):
        return True
    base_prefix = getattr(sys, "base_prefix", sys.prefix)
    return bool(base_prefix and base_prefix != sys.prefix)


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


def _report_task_result_once(settings, task_id, report_payload, common_headers):
    report_url = f"{settings.platform_base_url}/api/agents/tasks/{task_id}/result"
    return post_json(report_url, report_payload, headers=common_headers, timeout=15)


def _restart_process_after_update(settings):
    _log("自更新完成，准备重启 Agent 进程", settings.log_verbose)
    python_exe = sys.executable
    argv = [python_exe] + sys.argv
    os.execv(python_exe, argv)


def _read_agent_log_tail(max_lines: int = 100, max_chars: int = 32000) -> str:
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent.log")
    if not os.path.exists(log_path):
        return ""
    try:
        tail_lines = deque(maxlen=max(10, int(max_lines or 100)))
        with open(log_path, "r", encoding="utf-8", errors="replace") as fp:
            for line in fp:
                tail_lines.append(line.rstrip("\r\n"))
        if not tail_lines:
            return ""
        text = "\n".join(tail_lines)
        if len(text) > max_chars:
            return text[-max_chars:]
        return text
    except Exception:
        return ""


def _report_agent_incident(
    settings,
    common_headers: dict,
    agent_token: str,
    incident_type: str,
    title: str,
    message: str,
    error_detail: str = "",
):
    if not agent_token:
        return
    payload = {
        "agent_code": settings.agent_code,
        "agent_token": agent_token,
        "incident_type": str(incident_type or "runtime_error"),
        "title": str(title or "Agent运行异常"),
        "message": str(message or "")[:4000],
        "error_detail": str(error_detail or "")[:16000],
        "log_excerpt": _read_agent_log_tail(max_lines=100, max_chars=32000),
    }
    url = f"{settings.platform_base_url}/api/agents/incidents/report"
    status, data = post_json(url, payload, headers=common_headers, timeout=10)
    if status != 200 or not data.get("success"):
        _log(f"异常事件上报失败(status={status}): {data}", settings.log_verbose)


def run_agent():
    global _SHUTDOWN, _LAST_SETTINGS, _LAST_COMMON_HEADERS, _LAST_AGENT_TOKEN
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
    _LAST_SETTINGS = settings
    _LAST_COMMON_HEADERS = common_headers
    agent_token = ""
    _LAST_AGENT_TOKEN = ""
    last_heartbeat_at = 0.0
    last_metrics_at = 0.0
    cached_metrics = {}
    heartbeat_online_logged = False
    heartbeat_offline_logged = False
    pending_report = None
    last_auto_update_check_at = 0.0

    _log(
        f"启动 Agent: code={settings.agent_code}, projects={','.join(settings.project_codes)}, "
        f"platform={settings.platform_base_url}, version={get_local_release_version()}",
        settings.log_verbose,
    )
    _log(
        f"运行时Python: exe={sys.executable}, mode={'venv' if _is_virtual_env() else 'system'}",
        settings.log_verbose,
    )
    if settings.auto_update_install_deps:
        _log(
            "自更新依赖安装已启用：将使用当前解释器执行 `python -m pip install -r requirements.txt`",
            settings.log_verbose,
        )
        if not _is_virtual_env():
            _log(
                "提示：当前为系统Python环境，依赖将安装到系统环境；生产建议使用venv。",
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
                    "supports_commit_diff": True,
                    "supports_excel_diff": True,
                    "supports_weekly_diff": True,
                    "supports_self_update": True,
                    "runtime": "python",
                },
                **cached_metrics,
            }
            status, data = post_json(register_url, register_payload, headers=common_headers, timeout=15)
            if status == 200 and data.get("success"):
                agent_token = str(data.get("agent_token") or "").strip()
                _LAST_AGENT_TOKEN = agent_token
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

        if pending_report:
            now_retry_ts = time.time()
            retry_after_ts = float(pending_report.get("retry_after_ts") or 0.0)
            if now_retry_ts >= retry_after_ts:
                rep_status, rep_data = _report_task_result_once(
                    settings,
                    pending_report["task_id"],
                    pending_report["payload"],
                    common_headers,
                )
                if rep_status == 200 and rep_data.get("success"):
                    _log(
                        f"任务补回传成功 id={pending_report['task_id']}, status={pending_report['payload'].get('status')}",
                        settings.log_verbose,
                    )
                    pending_report = None
                else:
                    pending_report["attempt"] = int(pending_report.get("attempt") or 0) + 1
                    backoff_seconds = min(30, max(2, pending_report["attempt"] * 2))
                    pending_report["retry_after_ts"] = time.time() + backoff_seconds
                    _log(
                        f"任务补回传失败 id={pending_report['task_id']}, status={rep_status}, "
                        f"attempt={pending_report['attempt']}, body={rep_data}",
                        settings.log_verbose,
                    )
                    if rep_status in (401, 403):
                        agent_token = ""
                        _LAST_AGENT_TOKEN = ""
                        heartbeat_online_logged = False
                        heartbeat_offline_logged = False
                        time.sleep(settings.register_retry_interval_seconds)
                        continue
            if pending_report:
                time.sleep(max(1, settings.task_poll_interval_seconds))
                continue

        now_ts = time.time()
        if settings.auto_update_enabled and (now_ts - last_auto_update_check_at) >= settings.auto_update_check_interval_seconds:
            last_auto_update_check_at = now_ts
            try:
                updated, update_message = check_and_apply_update(
                    settings=settings,
                    common_headers=common_headers,
                    agent_token=agent_token,
                    log_func=lambda m: _log(m, settings.log_verbose),
                )
                if updated:
                    _log(f"自更新成功: {update_message}", settings.log_verbose)
                    _restart_process_after_update(settings)
                else:
                    if update_message not in {"no update", ""}:
                        _log(f"自更新检查结果: {update_message}", settings.log_verbose)
            except Exception as update_exc:
                _log(f"自更新失败: {update_exc}", settings.log_verbose)

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
                    if settings.auto_update_enabled:
                        # 断线恢复后触发下一轮立即检查，避免等待完整周期。
                        last_auto_update_check_at = 0.0
                last_heartbeat_at = now_ts
            else:
                if not heartbeat_offline_logged:
                    _log(f"心跳断线(status={status}): {data}", settings.log_verbose)
                    heartbeat_offline_logged = True
                    heartbeat_online_logged = False
                if status in (401, 403):
                    agent_token = ""
                    _LAST_AGENT_TOKEN = ""
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
                _LAST_AGENT_TOKEN = ""
            time.sleep(settings.register_retry_interval_seconds)
            continue

        task = data.get("task")
        if not task:
            time.sleep(settings.task_poll_interval_seconds)
            continue

        task_id = task.get("id")
        task_type = str(task.get("task_type") or "").strip().lower()
        _log(f"领取任务成功 id={task_id}, type={task_type}", settings.log_verbose)

        try:
            exec_status, result_summary, error_message, result_payload = execute_task(task, settings)
        except Exception as task_exc:
            exec_status, result_summary, error_message, result_payload = (
                "failed",
                None,
                f"execute_task crashed for task_type={task_type}: {task_exc}",
                None,
            )
        try:
            result_payload = _maybe_upload_large_temp_cache(
                task=task,
                task_id=task_id,
                task_type=task_type,
                result_payload=result_payload,
                settings=settings,
                common_headers=common_headers,
                agent_token=agent_token,
            )
        except Exception as cache_exc:
            _log(f"临时缓存上报异常，已跳过: {cache_exc}", settings.log_verbose)

        report_payload = {
            "agent_code": settings.agent_code,
            "agent_token": agent_token,
            "status": exec_status,
            "result_summary": result_summary,
            "error_message": error_message,
            "result_payload": result_payload,
        }
        rep_status, rep_data = _report_task_result_once(settings, task_id, report_payload, common_headers)
        if rep_status == 200 and rep_data.get("success"):
            _log(f"任务回传完成 id={task_id}, status={exec_status}", settings.log_verbose)
        else:
            _log(f"任务回传失败 id={task_id}, status={rep_status}, body={rep_data}", settings.log_verbose)
            pending_report = {
                "task_id": task_id,
                "payload": report_payload,
                "attempt": 0,
                "retry_after_ts": time.time() + 2,
            }

        time.sleep(settings.task_poll_interval_seconds)

    if _LAST_SIGNAL_NAME and agent_token:
        _report_agent_incident(
            settings=settings,
            common_headers=common_headers,
            agent_token=agent_token,
            incident_type="interrupted",
            title="Agent收到中断信号",
            message=f"进程收到 {_LAST_SIGNAL_NAME} 并退出",
            error_detail="",
        )

    _log("收到退出信号，Agent 已停止", settings.log_verbose)


def main():
    try:
        run_agent()
    except Exception as exc:
        if _LAST_SETTINGS and _LAST_COMMON_HEADERS and _LAST_AGENT_TOKEN:
            _report_agent_incident(
                settings=_LAST_SETTINGS,
                common_headers=_LAST_COMMON_HEADERS,
                agent_token=_LAST_AGENT_TOKEN,
                incident_type="runtime_fatal",
                title="Agent运行异常退出",
                message=str(exc),
                error_detail=traceback.format_exc(),
            )
        print(f"[AGENT][FATAL] {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
