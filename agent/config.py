#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent 配置读取。"""

from __future__ import annotations

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
    local_task_types: list[str]
    repos_base_dir: str
    log_verbose: bool


def load_settings() -> AgentSettings:
    _try_load_dotenv()

    platform_base_url = (os.environ.get("PLATFORM_BASE_URL") or "http://127.0.0.1:8002").strip().rstrip("/")
    project_codes = _split_csv(os.environ.get("AGENT_PROJECT_CODES") or "")
    local_task_types = _split_csv(os.environ.get("AGENT_LOCAL_TASK_TYPES") or "auto_sync")
    repos_base_dir = (os.environ.get("AGENT_REPOS_BASE_DIR") or "agent_repos").strip()
    return AgentSettings(
        platform_base_url=platform_base_url,
        agent_shared_secret=(os.environ.get("AGENT_SHARED_SECRET") or "").strip(),
        agent_code=(os.environ.get("AGENT_CODE") or "").strip(),
        agent_name=(os.environ.get("AGENT_NAME") or "").strip() or (os.environ.get("AGENT_CODE") or "").strip(),
        agent_host=(os.environ.get("AGENT_HOST") or "127.0.0.1").strip(),
        agent_port=int((os.environ.get("AGENT_PORT") or "9010").strip()),
        default_admin_username=(os.environ.get("AGENT_DEFAULT_ADMIN_USERNAME") or "admin").strip(),
        project_codes=project_codes,
        heartbeat_interval_seconds=max(5, int((os.environ.get("AGENT_HEARTBEAT_INTERVAL_SECONDS") or "20").strip())),
        register_retry_interval_seconds=max(
            3,
            int((os.environ.get("AGENT_REGISTER_RETRY_INTERVAL_SECONDS") or "10").strip()),
        ),
        task_poll_interval_seconds=max(1, int((os.environ.get("AGENT_TASK_POLL_INTERVAL_SECONDS") or "3").strip())),
        local_task_types=[t.lower() for t in local_task_types],
        repos_base_dir=repos_base_dir,
        log_verbose=_bool_env("AGENT_LOG_VERBOSE", True),
    )
