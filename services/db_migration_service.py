"""Database schema migration helpers extracted from app.py."""

from __future__ import annotations

import re

from sqlalchemy import inspect, text as sa_text
from sqlalchemy.exc import SQLAlchemyError

DB_MIGRATION_RUNTIME_ERRORS = (
    SQLAlchemyError,
    RuntimeError,
    TypeError,
    ValueError,
    AttributeError,
    LookupError,
)

# ---------------------------------------------------------------------------
# 索引迁移
# ---------------------------------------------------------------------------
# 背景：`db.create_all()` 只对**新建**的表建索引；已经部署出去的库，
# commits_log 永远不会得到后来才加进模型的索引。SQLite 也不支持
# `ALTER TABLE ... ADD CONSTRAINT`，所以索引必须单独走 CREATE INDEX 这条路。
#
# 清单里的表名 / 列名全部是本文件写死的字面量，**绝不拼接任何外部输入**，
# 因此拼出来的 DDL 不可能被注入；拼之前还会再用 _INDEX_IDENTIFIER_RE 校验一遍。
#
# 这份清单与模型层 models/commit.py、models/task.py 的 __table_args__ 是同一批
# 索引（一个管新库，一个管老库）。tests/test_commit_indexes_and_bulk_update.py
# 会断言两份清单逐项一致，防止哪天只改一边导致新老库索引不一致。
REQUIRED_INDEXES = (
    ("idx_commits_log_repo_commit_time", "commits_log", ("repository_id", "commit_time")),
    ("idx_commits_log_repo_commit_id", "commits_log", ("repository_id", "commit_id")),
    ("idx_commits_log_repo_status", "commits_log", ("repository_id", "status")),
    ("idx_commits_log_commit_time", "commits_log", ("commit_time",)),
    ("idx_background_tasks_status_priority", "background_tasks", ("status", "priority", "created_at")),
    ("idx_background_tasks_type_repo_status", "background_tasks", ("task_type", "repository_id", "status")),
)

# 索引相关标识符的合法字符集（表名 / 列名 / 索引名都必须长这样）。
_INDEX_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# MySQL 错误码 1061 = ER_DUP_KEYNAME（"Duplicate key name 'xxx'"）。
MYSQL_DUPLICATE_KEY_NAME_ERRNO = 1061


def _is_index_already_exists_error(exc) -> bool:
    """判断异常是不是「索引已存在」。

    MySQL 走 CREATE INDEX（无 IF NOT EXISTS）时重复建会抛 1061，这里按错误码识别；
    兜底再看消息文本，覆盖驱动把 errno 放在别处 / SQLite 用到非 IF NOT EXISTS 写法的情况。
    """
    orig = getattr(exc, "orig", None)
    args = getattr(orig, "args", None) or ()
    if args and args[0] == MYSQL_DUPLICATE_KEY_NAME_ERRNO:
        return True
    message = str(exc).lower()
    return "duplicate key name" in message or "already exists" in message


def _build_create_index_sql(dialect_name, table_name, index_name, columns):
    """按后端拼 CREATE INDEX 语句（入参只允许来自 REQUIRED_INDEXES 常量）。"""
    columns_sql = ", ".join(columns)
    if dialect_name == "mysql":
        # MySQL **不支持** `CREATE INDEX IF NOT EXISTS`（MySQL 8.0 也不支持，
        # 写了直接 1064 语法错误）。这里选的是「照建 + 捕获 1061 后忽略」，
        # 而不是「先查 information_schema.statistics 再建」，原因是：
        #   1) 先查后建是 check-then-act：两个进程同时启动会双双查到「不存在」
        #      然后一起建，其中一个必然报错 → 启动链路炸掉。直接建 + 吞 1061
        #      没有这个竞态。
        #   2) 少一次 information_schema 往返。代价是每次启动会对已存在的索引
        #      各发一条必然失败（1061）的 DDL；这类失败在拿到元数据锁之前就返回，
        #      开销可以忽略。
        return f"CREATE INDEX {index_name} ON {table_name} ({columns_sql})"
    # SQLite / PostgreSQL 支持 IF NOT EXISTS，重复执行由数据库自己保证幂等。
    return f"CREATE INDEX IF NOT EXISTS {index_name} ON {table_name} ({columns_sql})"


def _columns_log_text(columns):
    """把列名元组渲染成日志用的可读文本。"""
    return ", ".join(str(column) for column in columns)


def apply_index_migrations(db, log_print):
    """为已存在的表补建 REQUIRED_INDEXES 里的索引。

    幂等：重复执行不报错、不重复建。已有数据的库照样能建成功（CREATE INDEX
    本来就不要求表为空，只是会随数据量花时间）。
    """
    try:
        existing_tables = set(inspect(db.engine).get_table_names())
    except DB_MIGRATION_RUNTIME_ERRORS as exc:
        log_print(f"⚠️ 索引迁移跳过：读取数据库结构失败: {exc}", "DB", force=True)
        return

    dialect_name = db.engine.dialect.name
    created_indexes = []
    for index_name, table_name, columns in REQUIRED_INDEXES:
        if table_name not in existing_tables:
            # 表还没建出来（例如某些精简测试库）——交给 create_all，不在这里造表。
            continue
        identifiers = (table_name, index_name, *columns)
        if not all(_INDEX_IDENTIFIER_RE.match(part) for part in identifiers):
            log_print(f"⚠️ 索引定义含非法标识符，已跳过: {table_name}.{index_name}", "DB", force=True)
            continue

        create_sql = _build_create_index_sql(dialect_name, table_name, index_name, columns)
        try:
            db.session.execute(sa_text(create_sql))
            db.session.commit()
            created_indexes.append(f"{table_name}.{index_name}")
        except SQLAlchemyError as exc:
            # 每条成功的 CREATE INDEX 都已单独 commit，所以这里的 rollback
            # 只会清掉这条失败语句残留的事务状态，不会影响已建好的索引。
            try:
                db.session.rollback()
            except SQLAlchemyError:
                pass
            if _is_index_already_exists_error(exc):
                continue  # 幂等路径：索引已经在，不算失败，也不打日志噪音
            log_print(
                f"⚠️ 创建索引失败 {table_name}.{index_name}"
                f"（{_columns_log_text(columns)}）: {exc}",
                "DB",
                force=True,
            )

    if created_indexes:
        log_print(f"✅ 自动补建索引 {len(created_indexes)} 个: {', '.join(created_indexes)}", "DB")
    else:
        log_print("ℹ️ 索引已完整，无需补建", "DB")


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
    except DB_MIGRATION_RUNTIME_ERRORS as exc:
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

def _migrate_ai_weekly_analysis_state_columns(db, log_print):
    _migrate_table_columns(
        db,
        "ai_weekly_analysis_state",
        {
            "last_triggered_at": "last_triggered_at DATETIME",
        },
        log_print,
    )


def apply_schema_migrations(db, log_print):
    """Apply all lightweight runtime schema migrations."""
    _migrate_repository_columns(db, log_print)
    _migrate_commits_log_columns(db, log_print)
    _migrate_weekly_version_diff_cache_columns(db, log_print)
    _migrate_agent_nodes_columns(db, log_print)
    _migrate_ai_weekly_analysis_state_columns(db, log_print)
    # 索引放在最后：列迁移先跑完，避免出现「列还没 add 就要给它建索引」。
    apply_index_migrations(db, log_print)
