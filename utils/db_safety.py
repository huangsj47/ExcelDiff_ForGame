#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Database safety guards for destructive operations.

This module prevents accidental destructive actions (drop_all, truncate, etc.)
from running against non-test databases.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from typing import Any, Dict, Optional

from utils.db_config import get_sqlite_path_from_uri, infer_backend_from_uri


def _is_truthy(value: Optional[str]) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def is_temp_sqlite_path(path: Optional[str]) -> bool:
    """Return True when sqlite path looks like a temp/test database."""
    if not path:
        return False
    raw_path = str(path)
    raw_lower = raw_path.lower().replace("\\", "/")
    abs_path = os.path.abspath(raw_path)
    abs_lower = abs_path.lower()
    tmp_root = os.path.abspath(tempfile.gettempdir()).lower()
    base = os.path.basename(abs_lower)

    # 1) OS temp directory
    if abs_lower.startswith(tmp_root):
        return True

    # 1b) Posix-style temp paths when running tests in mixed environments
    if raw_lower.startswith("/tmp/") or "/pytest-" in raw_lower or "/pytest_of_" in raw_lower:
        return True

    # 2) Common test naming conventions
    return (
        base.startswith("tmp")
        or "pytest" in base
        or "diff_platform_test" in base
        or "codex_test" in base
    )


def assert_destructive_db_allowed(
    *,
    database_uri: str,
    action_name: str,
    testing: bool = False,
    allow_env_var: str = "ALLOW_DESTRUCTIVE_DB_OPS",
) -> None:
    """Guard destructive DB actions.

    Allowed when either:
    - Explicit env override is enabled (`ALLOW_DESTRUCTIVE_DB_OPS=true`)
    - Running in test mode and sqlite target is a temporary database file
    """
    if _is_truthy(os.environ.get(allow_env_var)):
        return

    backend = infer_backend_from_uri(database_uri or "")
    if backend != "sqlite":
        raise RuntimeError(
            f"拒绝执行破坏性数据库操作[{action_name}]：当前数据库后端={backend}，"
            f"请设置 {allow_env_var}=true 后重试。"
        )

    sqlite_path = get_sqlite_path_from_uri(database_uri or "")
    if testing and is_temp_sqlite_path(sqlite_path):
        return

    raise RuntimeError(
        f"拒绝执行破坏性数据库操作[{action_name}]：目标数据库不是测试临时库。"
        f" uri={database_uri} path={sqlite_path}. "
        f"如确认操作，请显式设置 {allow_env_var}=true。"
    )


def reset_sqlalchemy_engine_cache(app) -> None:
    """Clear Flask-SQLAlchemy engine cache for current app.

    Needed when tests switch SQLALCHEMY_DATABASE_URI at runtime.
    """
    ext = app.extensions.get("sqlalchemy")
    app_engines = getattr(ext, "_app_engines", None)
    if isinstance(app_engines, dict):
        app_engines.pop(app, None)


def collect_sqlite_runtime_diagnostics(database_uri: str) -> Dict[str, Any]:
    """Collect lightweight sqlite diagnostics for startup troubleshooting."""
    result: Dict[str, Any] = {
        "backend": infer_backend_from_uri(database_uri or ""),
        "database_uri": database_uri,
        "sqlite_path": None,
        "db_size_bytes": 0,
        "wal_size_bytes": 0,
        "shm_size_bytes": 0,
        "exists": False,
        "page_size": 0,
        "page_count": 0,
        "freelist_count": 0,
        "used_page_count": 0,
        "free_ratio": 0.0,
        "journal_mode": "",
        "error": "",
    }
    if result["backend"] != "sqlite":
        return result

    sqlite_path = get_sqlite_path_from_uri(database_uri or "")
    result["sqlite_path"] = sqlite_path
    if not sqlite_path:
        result["error"] = "sqlite_path_empty"
        return result

    result["exists"] = os.path.exists(sqlite_path)
    if not result["exists"]:
        return result

    try:
        result["db_size_bytes"] = os.path.getsize(sqlite_path)
        wal_path = f"{sqlite_path}-wal"
        shm_path = f"{sqlite_path}-shm"
        result["wal_size_bytes"] = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0
        result["shm_size_bytes"] = os.path.getsize(shm_path) if os.path.exists(shm_path) else 0

        conn = sqlite3.connect(sqlite_path)
        cur = conn.cursor()
        cur.execute("PRAGMA page_size")
        page_size = int(cur.fetchone()[0] or 0)
        cur.execute("PRAGMA page_count")
        page_count = int(cur.fetchone()[0] or 0)
        cur.execute("PRAGMA freelist_count")
        freelist_count = int(cur.fetchone()[0] or 0)
        cur.execute("PRAGMA journal_mode")
        journal_mode = str(cur.fetchone()[0] or "")
        conn.close()

        used_page_count = max(page_count - freelist_count, 0)
        free_ratio = (float(freelist_count) / float(page_count)) if page_count else 0.0

        result.update(
            {
                "page_size": page_size,
                "page_count": page_count,
                "freelist_count": freelist_count,
                "used_page_count": used_page_count,
                "free_ratio": round(free_ratio, 6),
                "journal_mode": journal_mode,
            }
        )
    except Exception as exc:  # pragma: no cover - best effort diagnostics
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result
