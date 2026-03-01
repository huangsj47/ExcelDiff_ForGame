#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Database backend configuration helpers.

This module keeps sqlite/mysql bootstrap logic isolated from app runtime code.
"""

from __future__ import annotations

import os
from typing import Mapping, MutableMapping, Optional, Tuple
from urllib.parse import quote_plus

from sqlalchemy.engine import make_url


SUPPORTED_BACKENDS = {"sqlite", "mysql"}
DEFAULT_DB_BACKEND = "sqlite"
DEFAULT_SQLITE_PATH = os.path.abspath(os.path.join("instance", "diff_platform.db"))


def _get_env(env: Mapping[str, str], *keys: str, default: Optional[str] = None) -> Optional[str]:
    for key in keys:
        value = env.get(key)
        if value is not None:
            value = str(value).strip()
            if value != "":
                return value
    return default


def normalize_backend(value: Optional[str]) -> str:
    if not value:
        return DEFAULT_DB_BACKEND

    backend = str(value).strip().lower()
    alias = {
        "sqlite3": "sqlite",
        "mariadb": "mysql",
        "mysql+pymysql": "mysql",
        "mysql+mysqlconnector": "mysql",
    }
    backend = alias.get(backend, backend)

    if backend not in SUPPORTED_BACKENDS:
        raise ValueError(f"Unsupported DB_BACKEND: {value}. Supported: sqlite, mysql")
    return backend


def infer_backend_from_uri(database_uri: str) -> str:
    if not database_uri:
        return DEFAULT_DB_BACKEND

    uri = database_uri.strip().lower()
    if uri.startswith("sqlite"):
        return "sqlite"
    if uri.startswith("mysql"):
        return "mysql"
    return DEFAULT_DB_BACKEND


def sanitize_database_uri(database_uri: str) -> str:
    if not database_uri:
        return "<empty>"
    try:
        return make_url(database_uri).render_as_string(hide_password=True)
    except Exception:
        return "<invalid-database-uri>"


def build_sqlite_uri(env: Mapping[str, str]) -> Tuple[str, str]:
    db_path = _get_env(env, "SQLITE_DB_PATH", "DB_PATH", default=DEFAULT_SQLITE_PATH)
    assert db_path is not None

    if not os.path.isabs(db_path):
        db_path = os.path.abspath(db_path)

    instance_dir = os.path.dirname(db_path)
    if instance_dir:
        os.makedirs(instance_dir, exist_ok=True)

    return f"sqlite:///{db_path}", db_path


def build_mysql_uri(env: Mapping[str, str]) -> str:
    host = _get_env(env, "DB_HOST", "MYSQL_HOST")
    port = _get_env(env, "DB_PORT", "MYSQL_PORT", default="3306")
    user = _get_env(env, "DB_USER", "MYSQL_USER")
    password = _get_env(env, "DB_PASSWORD", "MYSQL_PASSWORD", default="")
    database = _get_env(env, "DB_NAME", "MYSQL_DATABASE")
    charset = _get_env(env, "DB_CHARSET", "MYSQL_CHARSET", default="utf8mb4")

    missing = []
    if not host:
        missing.append("DB_HOST")
    if not user:
        missing.append("DB_USER")
    if not database:
        missing.append("DB_NAME")
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"MySQL backend missing required env vars: {joined}")

    user_part = quote_plus(user)
    password_part = quote_plus(password)
    return (
        f"mysql+pymysql://{user_part}:{password_part}@{host}:{port}/{database}"
        f"?charset={charset}"
    )


def build_database_settings(env: Optional[Mapping[str, str]] = None) -> dict:
    if env is None:
        env = os.environ

    database_url = _get_env(env, "DATABASE_URL")
    if database_url:
        backend = infer_backend_from_uri(database_url)
        settings = {
            "backend": backend,
            "database_uri": database_url,
            "engine_options": build_engine_options(backend, env),
            "display_uri": sanitize_database_uri(database_url),
            "sqlite_db_path": None,
        }
        if backend == "sqlite":
            settings["sqlite_db_path"] = get_sqlite_path_from_uri(database_url)
        return settings

    backend = normalize_backend(_get_env(env, "DB_BACKEND", "DATABASE_BACKEND", "DATABASE_TYPE"))
    sqlite_db_path = None
    if backend == "mysql":
        database_uri = build_mysql_uri(env)
    else:
        database_uri, sqlite_db_path = build_sqlite_uri(env)

    return {
        "backend": backend,
        "database_uri": database_uri,
        "engine_options": build_engine_options(backend, env),
        "display_uri": sanitize_database_uri(database_uri),
        "sqlite_db_path": sqlite_db_path,
    }


def build_engine_options(backend: str, env: Mapping[str, str]) -> dict:
    if backend != "mysql":
        return {}

    options = {
        "pool_pre_ping": _parse_bool(_get_env(env, "DB_POOL_PRE_PING", default="true")),
        "pool_recycle": _safe_int(_get_env(env, "DB_POOL_RECYCLE", default="1800"), default=1800),
    }

    pool_size = _get_env(env, "DB_POOL_SIZE")
    max_overflow = _get_env(env, "DB_MAX_OVERFLOW")
    pool_timeout = _get_env(env, "DB_POOL_TIMEOUT")
    if pool_size:
        options["pool_size"] = _safe_int(pool_size, default=5)
    if max_overflow:
        options["max_overflow"] = _safe_int(max_overflow, default=10)
    if pool_timeout:
        options["pool_timeout"] = _safe_int(pool_timeout, default=30)
    return options


def apply_database_settings(app_config: MutableMapping[str, object], env: Optional[Mapping[str, str]] = None) -> dict:
    settings = build_database_settings(env)
    app_config["SQLALCHEMY_DATABASE_URI"] = settings["database_uri"]
    app_config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app_config["DB_BACKEND"] = settings["backend"]
    if settings["sqlite_db_path"]:
        app_config["SQLITE_DB_PATH"] = settings["sqlite_db_path"]

    engine_options = settings["engine_options"]
    if engine_options:
        app_config["SQLALCHEMY_ENGINE_OPTIONS"] = engine_options
    else:
        app_config.pop("SQLALCHEMY_ENGINE_OPTIONS", None)

    return settings


def get_database_backend_from_config(config: Mapping[str, object]) -> str:
    raw_backend = config.get("DB_BACKEND")
    if raw_backend:
        return normalize_backend(str(raw_backend))
    uri = str(config.get("SQLALCHEMY_DATABASE_URI", "") or "")
    return infer_backend_from_uri(uri)


def get_sqlite_path_from_uri(database_uri: str) -> Optional[str]:
    if not database_uri or not database_uri.lower().startswith("sqlite"):
        return None
    try:
        db_path = make_url(database_uri).database
    except Exception:
        return None
    if not db_path:
        return None
    if not os.path.isabs(db_path):
        db_path = os.path.abspath(db_path)
    return db_path


def _parse_bool(value: Optional[str]) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _safe_int(value: Optional[str], default: int) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default
