#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据库初始化脚本（支持 sqlite / mysql）"""

import os

from sqlalchemy import inspect

from services.model_loader import get_runtime_models
from utils.db_config import (
    get_database_backend_from_config,
    get_sqlite_path_from_uri,
    sanitize_database_uri,
)


EXPECTED_TABLES = [
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
    # 账号系统表 (auth_ 前缀)
    "auth_users",
    "auth_functions",
    "auth_user_functions",
    "auth_user_projects",
    "auth_project_join_requests",
    "auth_project_create_requests",
]


def _safe_get_table_names(db):
    try:
        return inspect(db.engine).get_table_names()
    except Exception as exc:  # pragma: no cover
        print(f"⚠️ 获取数据库表列表失败: {exc}")
        return []


def check_and_create_all_tables():
    """检查并创建所有必需的数据表。"""
    app, db = get_runtime_models("app", "db")

    backend = get_database_backend_from_config(app.config)
    database_uri = str(app.config.get("SQLALCHEMY_DATABASE_URI", "") or "")
    sqlite_db_path = app.config.get("SQLITE_DB_PATH") or get_sqlite_path_from_uri(database_uri)

    print(f"数据库后端: {backend}")
    print(f"数据库连接: {sanitize_database_uri(database_uri)}")

    if backend == "sqlite" and sqlite_db_path:
        instance_dir = os.path.dirname(sqlite_db_path)
        if instance_dir and not os.path.exists(instance_dir):
            os.makedirs(instance_dir, exist_ok=True)
            print(f"✅ 创建instance目录: {os.path.abspath(instance_dir)}")
        if not os.path.exists(sqlite_db_path):
            print(f"ℹ️ 数据库文件不存在，将创建新数据库: {os.path.abspath(sqlite_db_path)}")

    with app.app_context():
        existing_tables = _safe_get_table_names(db)
        print(f"创建前的数据库表: {existing_tables}")
        missing_tables = [name for name in EXPECTED_TABLES if name not in existing_tables]
        if missing_tables:
            print(f"创建前缺失表: {missing_tables}")

    try:
        with app.app_context():
            print("正在执行 db.create_all() ...")
            db.create_all()
            print("✅ db.create_all() 执行完成")
    except Exception as exc:
        print(f"❌ 创建表失败: {exc}")
        return False

    with app.app_context():
        final_tables = _safe_get_table_names(db)
    print(f"创建后的数据库表: {final_tables}")

    still_missing = [name for name in EXPECTED_TABLES if name not in final_tables]
    if still_missing:
        print(f"\n❌ 仍然缺失的表: {still_missing}")
        return False

    print("\n✅ 所有必需表均已创建")
    return True


if __name__ == "__main__":
    success = check_and_create_all_tables()
    if success:
        print("\n🎉 数据库初始化完成!")
    else:
        print("\n❌ 数据库初始化失败!")
