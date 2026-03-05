#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Qkit auth runtime configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _first_env(keys: tuple[str, ...], default: str = "") -> str:
    for key in keys:
        value = os.environ.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except Exception:
        return default


@dataclass(frozen=True)
class QkitSettings:
    local_host: str
    login_host: str
    public_base_url: str
    jwt_secret: str
    local_jwt_cache: bool
    lock_project_id: str
    my_project_api: str
    change_project_api: str
    auth_check_jwt_api: str
    login_service: str
    login_service_explicit: bool
    logout_service: str
    logout_service_explicit: bool
    request_timeout_seconds: int
    redmine_api_url: str


def load_qkit_settings() -> QkitSettings:
    local_host = _first_env(("QKIT_LOCAL_HOST", "LOCAL_HOST"), "10.226.98.33:8002")
    login_host = _first_env(("QKIT_LOGIN_HOST", "LOGIN_HOST"), local_host)
    public_base_url = _first_env(("QKIT_PUBLIC_BASE_URL", "PUBLIC_BASE_URL"), "").rstrip("/")
    jwt_secret = _first_env(("QKIT_JWT_SECRET", "JWT_SECRET"), "")
    local_jwt_cache = _bool_env("QKIT_LOCAL_JWT_CACHE", _bool_env("LOCAL_JWT_CACHE", True))
    lock_project_id = _first_env(("QKIT_LOCK_PROJECT_ID", "LOCK_PROJECT_ID"), "")
    request_timeout_seconds = max(
        1,
        _int_env(
            "QKIT_REQUEST_TIMEOUT_SECONDS",
            _int_env("REQUEST_TIMEOUT_SECONDS", 5),
        ),
    )

    my_project_api = _first_env(
        ("QKIT_MY_PROJECT_API", "MY_PROJECT_API"),
        f"http://{login_host}/api/v1/projects/myprojects/",
    )
    change_project_api = _first_env(
        ("QKIT_CHANGE_PROJECT_API", "CHANGE_PROJECT_API"),
        f"http://{login_host}/api/v1/projects/change_project_custom/",
    )
    auth_check_jwt_api = _first_env(
        ("QKIT_AUTH_CHECK_JWT_API", "AUTH_CHECK_JWT_API"),
        f"http://{login_host}/api/v1/users/jwt_ver/",
    )
    login_service_raw = _first_env(("QKIT_LOGIN_SERVICE", "LOGIN_SERVICE"), "")
    logout_service_raw = _first_env(("QKIT_LOGOUT_SERVICE", "LOGOUT_SERVICE"), "")
    login_service_explicit = bool(login_service_raw)
    logout_service_explicit = bool(logout_service_raw)
    login_service = login_service_raw or f"http://{login_host}/openid/login?next=http://{local_host}/qkit_auth/after_login"
    logout_service = logout_service_raw or f"http://{login_host}/openid/logout?next=http://{local_host}"
    redmine_api_url = _first_env(
        ("QKIT_REDMINE_API_URL", "REDMINE_API_URL"),
        "http://redmineapi.nie.netease.com/api/user",
    )

    return QkitSettings(
        local_host=local_host,
        login_host=login_host,
        public_base_url=public_base_url,
        jwt_secret=jwt_secret,
        local_jwt_cache=local_jwt_cache,
        lock_project_id=lock_project_id,
        my_project_api=my_project_api,
        change_project_api=change_project_api,
        auth_check_jwt_api=auth_check_jwt_api,
        login_service=login_service,
        login_service_explicit=login_service_explicit,
        logout_service=logout_service,
        logout_service_explicit=logout_service_explicit,
        request_timeout_seconds=request_timeout_seconds,
        redmine_api_url=redmine_api_url,
    )
