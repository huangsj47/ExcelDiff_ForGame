#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent 配置读取。"""

from __future__ import annotations

import ast
import re
import socket
import os
from dataclasses import dataclass


def _try_load_dotenv():
    try:
        from dotenv import load_dotenv

        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        if os.path.exists(env_path):
            load_dotenv(env_path, override=False)
    except Exception:
        pass


def _bool_env(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _split_csv(raw: str):
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


_AGENT_TASK_TYPE_ALLOWED = (
    "auto_sync",
    "excel_diff",
    "weekly_sync",
    "weekly_excel_cache",
    "temp_cache_fetch",
)
_AGENT_TASK_TYPE_REQUIRED = (
    "auto_sync",
    "excel_diff",
    "weekly_sync",
    "weekly_excel_cache",
    "temp_cache_fetch",
)


def _safe_eval_int_expression(raw: str, default: int) -> int:
    """Safely evaluate simple integer expressions like `1*1024_1024`."""
    text = str(raw or "").strip()
    if not text:
        return default
    # 兼容业务习惯写法：`1*1024_1024` 解释为 `1*1024*1024`
    if "*" in text:
        text = re.sub(r"(?<=\d)_(?=\d)", "*", text)

    try:
        node = ast.parse(text, mode="eval")
    except Exception:
        return default

    def _eval_expr(expr_node):
        if isinstance(expr_node, ast.Expression):
            return _eval_expr(expr_node.body)
        if isinstance(expr_node, ast.Constant) and isinstance(expr_node.value, int):
            return int(expr_node.value)
        if isinstance(expr_node, ast.UnaryOp) and isinstance(expr_node.op, (ast.UAdd, ast.USub)):
            value = _eval_expr(expr_node.operand)
            return value if isinstance(expr_node.op, ast.UAdd) else -value
        if isinstance(expr_node, ast.BinOp) and isinstance(expr_node.op, (ast.Add, ast.Sub, ast.Mult)):
            left = _eval_expr(expr_node.left)
            right = _eval_expr(expr_node.right)
            if isinstance(expr_node.op, ast.Add):
                return left + right
            if isinstance(expr_node.op, ast.Sub):
                return left - right
            return left * right
        raise ValueError("unsupported expression")

    try:
        return int(_eval_expr(node))
    except Exception:
        return default


def _normalize_local_task_types(raw: str):
    items = [item.lower() for item in _split_csv(raw)]
    if not items:
        return list(_AGENT_TASK_TYPE_REQUIRED)
    if "none" in items:
        return list(_AGENT_TASK_TYPE_REQUIRED)
    if "all" in items:
        return list(_AGENT_TASK_TYPE_REQUIRED)

    normalized = []
    for item in items:
        if item in _AGENT_TASK_TYPE_ALLOWED and item not in normalized:
            normalized.append(item)
    for required in _AGENT_TASK_TYPE_REQUIRED:
        if required not in normalized:
            normalized.append(required)
    return normalized


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "").strip()).strip("-").lower()


def _detect_local_ip(default_ip: str = "127.0.0.1") -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0] or default_ip)
    except Exception:
        return default_ip


def _build_agent_code(agent_name: str, agent_host: str) -> str:
    base = _slug(agent_name) or "agent"
    host = _slug(str(agent_host or "").replace(".", "-"))
    if host:
        return f"{base}-{host}"[:100]
    return base[:100]


@dataclass
class AgentSettings:
    platform_base_url: str
    agent_shared_secret: str
    agent_code: str
    agent_name: str
    agent_host: str
    agent_port: int
    default_admin_username: str
    project_codes: list[str]
    heartbeat_interval_seconds: int
    register_retry_interval_seconds: int
    task_poll_interval_seconds: int
    metrics_interval_seconds: int
    local_task_types: list[str]
    repos_base_dir: str
    log_verbose: bool
    temp_cache_upload_enabled: bool
    temp_cache_threshold_bytes: int
    temp_cache_expire_days: int
    auto_update_enabled: bool
    auto_update_check_interval_seconds: int
    auto_update_request_timeout_seconds: int
    auto_update_download_timeout_seconds: int
    auto_update_install_deps: bool
    auto_update_pip_timeout_seconds: int


def load_settings() -> AgentSettings:
    _try_load_dotenv()

    platform_base_url = (os.environ.get("PLATFORM_BASE_URL") or "http://127.0.0.1:8002").strip().rstrip("/")
    project_codes = _split_csv(os.environ.get("AGENT_PROJECT_CODES") or "")
    local_task_types = _normalize_local_task_types(
        os.environ.get("AGENT_LOCAL_TASK_TYPES")
        or "auto_sync,excel_diff,weekly_sync,weekly_excel_cache,temp_cache_fetch"
    )
    repos_base_dir = (os.environ.get("AGENT_REPOS_BASE_DIR") or "agent_repos").strip()
    configured_host = (os.environ.get("AGENT_HOST") or "").strip()
    resolved_host = configured_host or _detect_local_ip()
    configured_name = (os.environ.get("AGENT_NAME") or "").strip()
    configured_code = (os.environ.get("AGENT_CODE") or "").strip()
    resolved_name = configured_name or configured_code or f"agent-{resolved_host}"
    resolved_code = configured_code or _build_agent_code(resolved_name, resolved_host)
    return AgentSettings(
        platform_base_url=platform_base_url,
        agent_shared_secret=(os.environ.get("AGENT_SHARED_SECRET") or "").strip(),
        agent_code=resolved_code,
        agent_name=resolved_name,
        agent_host=resolved_host,
        agent_port=int((os.environ.get("AGENT_PORT") or "9010").strip()),
        default_admin_username=(os.environ.get("AGENT_DEFAULT_ADMIN_USERNAME") or "admin").strip(),
        project_codes=project_codes,
        heartbeat_interval_seconds=max(5, int((os.environ.get("AGENT_HEARTBEAT_INTERVAL_SECONDS") or "20").strip())),
        register_retry_interval_seconds=max(
            3,
            int((os.environ.get("AGENT_REGISTER_RETRY_INTERVAL_SECONDS") or "10").strip()),
        ),
        task_poll_interval_seconds=max(1, int((os.environ.get("AGENT_TASK_POLL_INTERVAL_SECONDS") or "3").strip())),
        metrics_interval_seconds=max(60, int((os.environ.get("AGENT_METRICS_INTERVAL_SECONDS") or "300").strip())),
        local_task_types=[t.lower() for t in local_task_types],
        repos_base_dir=repos_base_dir,
        log_verbose=_bool_env("AGENT_LOG_VERBOSE", True),
        temp_cache_upload_enabled=_bool_env("AGENT_TEMP_CACHE_UPLOAD_ENABLED", True),
        temp_cache_threshold_bytes=max(
            64 * 1024,
            _safe_eval_int_expression(
                os.environ.get("AGENT_TEMP_CACHE_THRESHOLD_BYTES") or "1*1024_1024",
                1024 * 1024,
            ),
        ),
        temp_cache_expire_days=max(
            1,
            int((os.environ.get("AGENT_TEMP_CACHE_EXPIRE_DAYS") or "90").strip()),
        ),
        auto_update_enabled=_bool_env("AGENT_AUTO_UPDATE_ENABLED", True),
        auto_update_check_interval_seconds=max(
            30,
            int((os.environ.get("AGENT_AUTO_UPDATE_CHECK_INTERVAL_SECONDS") or "300").strip()),
        ),
        auto_update_request_timeout_seconds=max(
            5,
            int((os.environ.get("AGENT_AUTO_UPDATE_REQUEST_TIMEOUT_SECONDS") or "15").strip()),
        ),
        auto_update_download_timeout_seconds=max(
            30,
            int((os.environ.get("AGENT_AUTO_UPDATE_DOWNLOAD_TIMEOUT_SECONDS") or "120").strip()),
        ),
        auto_update_install_deps=_bool_env("AGENT_AUTO_UPDATE_INSTALL_DEPS", True),
        auto_update_pip_timeout_seconds=max(
            60,
            int((os.environ.get("AGENT_AUTO_UPDATE_PIP_TIMEOUT_SECONDS") or "900").strip()),
        ),
    )
