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
    """丢弃本 app 已缓存的引擎，使下一次 `db.engine` 按**当前** config 重建。

    用途：测试在运行时改掉 `SQLALCHEMY_DATABASE_URI`（例如
    `tests/test_auth_e2e.py` 想把连接切到临时库再 `drop_all`）。

    【原实现的缺陷】判断写的是 `isinstance(app_engines, dict)`，而 Flask-SQLAlchemy
    3.0.x 里 `ext._app_engines` 是 `weakref.WeakKeyDictionary` —— 它**不是** dict
    的子类，于是这个判断恒为假，函数**静默什么都不做**。

    实测（改前）：
        URI in config AFTER : sqlite:///...\\Temp\\tmpXXXX.db
        engine.url AFTER    : sqlite:///...\\instance\\diff_platform.db   ← 没换过去

    危害不只是「测试跑不起来」：调用方以为已经切到临时库了，接着就会对**真实
    生产库**执行 `drop_all()`。本项目里唯一挡住它的是 `assert_destructive_db_allowed`
    的那道保险 —— 而一道「靠另一处守卫侥幸没出事」的静默失败，迟早会出事。

    【必须清内层而不是 pop 掉 app】Flask-SQLAlchemy 3.0.x 把 `_app_engines` 同时当作
    **注册记录**用：`engines` 属性先查 `if app not in self._app_engines` 再抛
    「The current Flask app is not registered with this 'SQLAlchemy' instance」。
    直接 `pop(app)` 会把 app 的注册一并删掉；而清空内层 dict 又会让 `engines[None]`
    抛 KeyError —— 因为 FSA 3.0.x **只在 `init_app` 里创建引擎**（extension.py:314
    `engines = self._app_engines.setdefault(app, {})`），访问时不会惰性重建。

    所以「按新 config 重建引擎」的可行路径只有一条：摘掉注册 → 再跑一次 `init_app`。
    `init_app` 自己就支持这种重入（源码注释：`Dispose existing engines in case
    init_app is called again.`），只是它开头会拦「已注册」：
    `if "sqlalchemy" in app.extensions: raise RuntimeError(...)`。

    【调用时机】必须在 app **处理第一个请求之前**调用。`init_app` 内部会调用
    `app.shell_context_processor(...)` 这类 Flask 的 setup 方法，而 Flask 禁止在
    首个请求之后再调用（会抛「The setup method 'shell_context_processor' can no
    longer be called...」）。这里提前给出中文提示，免得读到那句难懂的英文。
    """
    ext = app.extensions.get("sqlalchemy")
    if ext is None:
        return
    init_app = getattr(ext, "init_app", None)
    if not callable(init_app):
        return
    if getattr(app, "_got_first_request", False):
        raise RuntimeError(
            "reset_sqlalchemy_engine_cache 必须在 app 处理第一个请求之前调用："
            "重建引擎要重跑 Flask-SQLAlchemy 的 init_app，而 Flask 不允许在首个请求"
            "之后再调用 setup 方法。请在导入后、发起任何请求前切换数据库连接。"
        )
    app.extensions.pop("sqlalchemy", None)
    init_app(app)


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
