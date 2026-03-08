"""App bootstrap database/cache routines extracted from app.py."""

from __future__ import annotations

import os

from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError

from utils.db_config import (
    get_database_backend_from_config,
    get_sqlite_path_from_uri,
    sanitize_database_uri,
)
from utils.db_safety import collect_sqlite_runtime_diagnostics

DB_STARTUP_DIR_CREATE_ERRORS = (OSError, ValueError, TypeError)
DB_STARTUP_INSPECT_ERRORS = (SQLAlchemyError, AttributeError, ValueError, TypeError, RuntimeError)
DB_STARTUP_CREATE_ALL_ERRORS = (SQLAlchemyError, AttributeError, ValueError, TypeError, RuntimeError)
DB_STARTUP_DIAGNOSTIC_ERRORS = (OSError, SQLAlchemyError, ValueError, TypeError, AttributeError, RuntimeError)
DB_STARTUP_SIZE_FORMAT_ERRORS = (ValueError, TypeError, ArithmeticError)
DB_STARTUP_CACHE_CLEANUP_ERRORS = (
    SQLAlchemyError,
    OSError,
    ValueError,
    TypeError,
    AttributeError,
    RuntimeError,
)


def create_tables_with_runtime_checks(*, app, db, log_print, apply_schema_migrations):
    """Create tables and run lightweight startup diagnostics."""
    with app.app_context():
        backend = get_database_backend_from_config(app.config)
        database_uri = str(app.config.get("SQLALCHEMY_DATABASE_URI", "") or "")
        sqlite_db_path = app.config.get("SQLITE_DB_PATH") or get_sqlite_path_from_uri(database_uri)
        if backend == "sqlite" and sqlite_db_path:
            instance_dir = os.path.dirname(sqlite_db_path)
            if instance_dir and not os.path.exists(instance_dir):
                try:
                    os.makedirs(instance_dir, exist_ok=True)
                    log_print(f"✅ 创建instance目录: {os.path.abspath(instance_dir)}", "DB")
                except DB_STARTUP_DIR_CREATE_ERRORS as exc:
                    log_print(f"❌ 创建instance目录失败: {exc}", "DB", force=True)
                    return

            elif instance_dir:
                log_print(f"ℹ️ instance目录已存在: {os.path.abspath(instance_dir)}", "DB")
            if not os.path.exists(sqlite_db_path):
                log_print(f"ℹ️ 数据库文件不存在，将创建新数据库: {os.path.abspath(sqlite_db_path)}", "DB")
        else:
            log_print(
                f"ℹ️ 使用 {backend.upper()} 数据库: {sanitize_database_uri(database_uri)}",
                "DB",
            )
        existing_tables = []
        try:
            existing_tables = inspect(db.engine).get_table_names()
        except DB_STARTUP_INSPECT_ERRORS as exc:
            log_print(f"检查现有表失败: {exc}", "DB", force=True)
        log_print(f"创建前的数据库表: {existing_tables}", "DB")
        try:
            db.create_all()
            log_print("✅ db.create_all() 执行完成", "DB")
        except DB_STARTUP_CREATE_ALL_ERRORS as exc:
            log_print(f"❌ 创建表失败: {exc}", "DB", force=True)
            return

        apply_schema_migrations(db, log_print)

        try:
            final_tables = inspect(db.engine).get_table_names()
            log_print(f"创建后的数据库表: {final_tables}", "DB")
            expected_tables = [
                "project",
                "repository",
                "commits_log",
                "background_tasks",
                "global_repository_counter",
                "diff_cache",
                "excel_html_cache",
                "weekly_version_config",
                "weekly_version_diff_cache",
                "weekly_version_excel_cache",
                "merged_diff_cache",
                "operation_log",
                "agent_nodes",
                "agent_project_bindings",
                "agent_tasks",
                "agent_default_admins",
                "agent_incidents",
            ]
            missing_tables = [table_name for table_name in expected_tables if table_name not in final_tables]
            if missing_tables:
                log_print(f"⚠️ 仍然缺失的表: {missing_tables}", "DB", force=True)
            else:
                log_print("✅ 所有必需的表都已创建", "DB")
        except DB_STARTUP_INSPECT_ERRORS as exc:
            log_print(f"检查最终表状态失败: {exc}", "DB", force=True)

        try:
            diag = collect_sqlite_runtime_diagnostics(database_uri)
            if diag.get("backend") == "sqlite":

                def _fmt_mb(num_bytes):
                    try:
                        return f"{(float(num_bytes) / (1024 * 1024)):.2f}MB"
                    except DB_STARTUP_SIZE_FORMAT_ERRORS:
                        return "0.00MB"

                log_print(
                    "SQLite诊断: "
                    f"path={diag.get('sqlite_path')}, "
                    f"size={_fmt_mb(diag.get('db_size_bytes', 0))}, "
                    f"wal={_fmt_mb(diag.get('wal_size_bytes', 0))}, "
                    f"journal={diag.get('journal_mode')}, "
                    f"pages={diag.get('page_count')}, "
                    f"free_pages={diag.get('freelist_count')}, "
                    f"free_ratio={float(diag.get('free_ratio', 0.0)):.2%}",
                    "DB",
                    force=True,
                )
                if float(diag.get("free_ratio", 0.0)) >= 0.80:
                    log_print(
                        "⚠️ SQLite空闲页占比超过80%，可能发生过大规模删除且未VACUUM；"
                        "若出现数据缺失请优先核查是否误执行 drop_all/清库脚本。",
                        "DB",
                        force=True,
                    )
            if diag.get("error"):
                log_print(f"SQLite诊断失败: {diag.get('error')}", "DB", force=True)
        except DB_STARTUP_DIAGNOSTIC_ERRORS as exc:
            log_print(f"SQLite启动诊断异常: {exc}", "DB", force=True)


def clear_startup_version_mismatch_cache(
    *,
    log_print,
    diff_logic_version,
    excel_cache_service,
    excel_html_cache_service,
    db,
):
    """Clear startup cache entries that do not match the current diff logic version."""
    try:
        log_print(f"检查并清理版本不匹配的缓存 (当前版本: {diff_logic_version})", "CACHE")
        total_diff_cleaned = excel_cache_service.cleanup_version_mismatch_cache()
        total_html_cleaned = excel_html_cache_service.cleanup_old_version_cache()

        if total_diff_cleaned > 0 or total_html_cleaned > 0:
            log_print(f"清理完成：{total_diff_cleaned} 条数据缓存，{total_html_cleaned} 条HTML缓存", "CACHE")
        else:
            log_print("无需清理版本不匹配的缓存", "CACHE")
            log_print("启动成功！", "APP")
    except DB_STARTUP_CACHE_CLEANUP_ERRORS as exc:
        log_print(f"清理版本不匹配缓存失败: {exc}", "CACHE", force=True)
        try:
            db.session.rollback()
        except SQLAlchemyError:
            pass
