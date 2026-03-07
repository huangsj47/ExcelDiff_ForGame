"""Database schema migration helpers extracted from app.py."""

from __future__ import annotations

from sqlalchemy import inspect, text as sa_text
from sqlalchemy.exc import SQLAlchemyError


def _migrate_table_columns(db, table_name, desired_cols, log_print):
    """Add missing columns for existing tables using ALTER TABLE."""
    try:
        insp = inspect(db.engine)
        if table_name not in insp.get_table_names():
            return
        existing_cols = {col["name"] for col in insp.get_columns(table_name)}
        added = []
        for col_name, col_ddl in desired_cols.items():
            if col_name not in existing_cols:
                db.session.execute(sa_text(f"ALTER TABLE {table_name} ADD COLUMN {col_ddl}"))
                added.append(col_name)
        if added:
            db.session.commit()
            log_print(f"✅ 自动迁移 {table_name} 表，新增列: {', '.join(added)}", "DB")
        else:
            log_print(f"ℹ️ {table_name} 表列已完整，无需迁移", "DB")
    except Exception as exc:
        log_print(f"⚠️ {table_name} 表自动迁移失败: {exc}", "DB", force=True)
        try:
            db.session.rollback()
        except SQLAlchemyError:
            pass


def _migrate_repository_columns(db, log_print):
    _migrate_table_columns(
        db,
        "repository",
        {
            "last_sync_error": "last_sync_error TEXT",
            "last_sync_error_time": "last_sync_error_time DATETIME",
        },
        log_print,
    )


def _migrate_commits_log_columns(db, log_print):
    _migrate_table_columns(
        db,
        "commits_log",
        {
            "status_changed_by": "status_changed_by VARCHAR(100)",
        },
        log_print,
    )


def _migrate_weekly_version_diff_cache_columns(db, log_print):
    _migrate_table_columns(
        db,
        "weekly_version_diff_cache",
        {
            "status_changed_by": "status_changed_by VARCHAR(100)",
        },
        log_print,
    )


def _migrate_agent_nodes_columns(db, log_print):
    _migrate_table_columns(
        db,
        "agent_nodes",
        {
            "cpu_cores": "cpu_cores INTEGER",
            "cpu_usage_percent": "cpu_usage_percent FLOAT",
            "agent_cpu_usage_percent": "agent_cpu_usage_percent FLOAT",
            "memory_total_bytes": "memory_total_bytes BIGINT",
            "memory_available_bytes": "memory_available_bytes BIGINT",
            "agent_memory_rss_bytes": "agent_memory_rss_bytes BIGINT",
            "disk_free_bytes": "disk_free_bytes BIGINT",
            "os_name": "os_name VARCHAR(100)",
            "os_version": "os_version VARCHAR(200)",
            "os_platform": "os_platform VARCHAR(300)",
            "metrics_updated_at": "metrics_updated_at DATETIME",
        },
        log_print,
    )


def apply_schema_migrations(db, log_print):
    """Apply all lightweight runtime schema migrations."""
    _migrate_repository_columns(db, log_print)
    _migrate_commits_log_columns(db, log_print)
    _migrate_weekly_version_diff_cache_columns(db, log_print)
    _migrate_agent_nodes_columns(db, log_print)
