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

from services.db_migration_service import missing_columns

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

        # **列**也要自己量一遍：表在、列缺是启动时唯一看不出来的那一类 —— 模型加了列而
        # 迁移清单里漏写，进程照常起来，直到某条查询用到那一列才炸（实测代价：一轮 22
        # 分钟的周版本分析作废在 `no such column: ai_analysis_anomaly.claims`）。
        # 只报不拦：列缺了也要能起平台来排查，但报错信息必须指向这一句。
        try:
            absent = missing_columns(db)
        except DB_STARTUP_INSPECT_ERRORS as exc:
            log_print(f"检查缺失列失败: {exc}", "DB", force=True)
        else:
            if absent:
                detail = "；".join(f"{name} 缺 {', '.join(cols)}" for name, cols in absent.items())
                log_print(
                    f"⚠️ 模型上有、库里没有的列：{detail} —— "
                    "`services/db_migration_service.py` 的迁移清单漏了它，"
                    "用到这一列的查询会直接报错",
                    "DB",
                    force=True,
                )
            else:
                log_print("✅ 模型列与库一致，无缺失列", "DB")

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
                "ai_project_api_key",
                "ai_project_analysis_config",
                "ai_analysis_run",
                "ai_weekly_analysis_state",
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
    weekly_excel_cache_service,
    db,
):
    """Clear startup cache entries that do not match the current diff logic version.

    `weekly_excel_cache_service` 是**必填**而不是可选：漏传一次就意味着周版本 Excel
    缓存的版本清理永远不跑，而它正是三张表里最占空间的那张（每行一整份 HTML/CSS/JS）。
    「可选参数忘了传」的失败形态与「清理本来就没东西可清」在日志上完全一样 ——
    必填参数则会当场抛 TypeError。
    """
    try:
        log_print(f"检查并清理版本不匹配的缓存 (当前版本: {diff_logic_version})", "CACHE")
        results = (
            ("数据缓存", excel_cache_service.cleanup_version_mismatch_cache()),
            ("HTML缓存", excel_html_cache_service.cleanup_old_version_cache()),
            ("周版本Excel缓存", weekly_excel_cache_service.cleanup_version_mismatch_cache()),
        )

        # **不能写成 `count > 0`**：这几家的清理方法用 None 表示「执行失败」，
        # 0 才是「本来就没东西可清」。而 `None > 0` 在 Python 3 里直接抛 TypeError，
        # 于是「清理失败」会被外层 except 包装成一句笼统的启动告警，
        # 既看不出是哪张表，也和「没东西可清」分不开。
        cleaned = [f"{count} 条{label}" for label, count in results if count]
        failed = [label for label, count in results if count is None]

        if cleaned:
            log_print(f"清理完成：{'，'.join(cleaned)}", "CACHE")
        if failed:
            log_print(
                f"❌ 版本清理执行失败（不是「没东西可清」）：{'、'.join(failed)}",
                "CACHE",
                force=True,
            )
        if not cleaned and not failed:
            log_print("无需清理版本不匹配的缓存", "CACHE")
            log_print("启动成功！", "APP")
    except DB_STARTUP_CACHE_CLEANUP_ERRORS as exc:
        log_print(f"清理版本不匹配缓存失败: {exc}", "CACHE", force=True)
        try:
            db.session.rollback()
        except SQLAlchemyError:
            pass
