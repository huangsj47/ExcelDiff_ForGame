"""Database schema migration helpers extracted from app.py."""

from __future__ import annotations

import re

from sqlalchemy import inspect, text as sa_text
from sqlalchemy.exc import SQLAlchemyError

from migrations.ai_run_claim_columns import apply as apply_ai_run_claim_columns

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
            # 比较口径的三项：`header_rows`（表头行数）/ `header_name_row`（名称行）/
            # `key_columns`（关键列）。后两项一直没列在这里 —— 老库上读它们会直接抛
            # `no such column`，而 `db.create_all()` 只建新表、不会给已存在的表补列。
            "header_rows": "header_rows INTEGER",
            "header_name_row": "header_name_row INTEGER",
            "key_columns": "key_columns VARCHAR(200)",
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
            # 比较口径版本。DiffCache / ExcelHtmlCache / WeeklyVersionExcelCache 都
            # 有这一列，本表原先没有 —— 于是 DIFF_LOGIC_VERSION 升级（合并口径变了）
            # 之后，旧合并 diff 仍会被 needs_merged_diff_cache() 判为可复用。
            # 合并 diff 的输入是窗口内多条提交，比单文件缓存更难靠人工发现口径过期。
            # 老库缺这一列时读侧会直接抛 `no such column: diff_version`。
            "diff_version": "diff_version VARCHAR(20)",
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

def _migrate_background_task_columns(db, log_print):
    """`background_tasks` 的任务身份与所有权列（复测文档 AI-P0-04）。

    这五列此前只活在内存载荷里（见 `models/task.py` 上那段注释）：进程重启、或去重
    命中一条更早创建的任务行，来源与模式就没了 —— 实测中一次**手工**发起的分析
    在库里被记成 `scheduled`，页面与账单都跟着错。

    不写 DEFAULT：老行读出来是 NULL，读侧按「未记录」处理（`trigger_source` 为 NULL
    时 `ai_analysis_service` 会归一到 `scheduled` —— 与改动前的行为一致，不会把
    历史任务说成手工发起的）。
    """
    _migrate_table_columns(
        db,
        "background_tasks",
        {
            "trigger_source": "trigger_source VARCHAR(20)",
            "requested_mode": "requested_mode VARCHAR(20)",
            "idempotency_key": "idempotency_key VARCHAR(120)",
            "job_id": "job_id INTEGER",
            "lease_expires_at": "lease_expires_at DATETIME",
        },
        log_print,
    )


def _migrate_ai_weekly_analysis_state_columns(db, log_print):
    _migrate_table_columns(
        db,
        "ai_weekly_analysis_state",
        {
            "last_triggered_at": "last_triggered_at DATETIME",
            # 「上一次分析的是哪一份快照」的内容指纹（见 services/ai/scope_sampling.py）。
            # 调度器用它拦掉「输入一字未变却还要再分析一遍」—— 时间水位线
            # （`last_analyzed_at`）做不到这件事：降级跑完的 run 按设计**不推进**水位线，
            # 于是同一份输入每小时都会被重新分析一次，白花钱。
            "last_snapshot_digest": "last_snapshot_digest VARCHAR(64)",
            # 两个基线指针（复测文档 AI-P0-02）：把「结论基线」与「完整覆盖快照」
            # 从那个三用的时间水位里拆出来。语义与分工见
            # `models/ai_analysis/weekly_state.py` 上那一段注释。
            "last_concluded_run_id": "last_concluded_run_id INTEGER",
            "last_complete_snapshot_id": "last_complete_snapshot_id INTEGER",
        },
        log_print,
    )


def _migrate_ai_analysis_columns(db, log_print):
    """AI 分析相关表的新增列。

    **注意这里没有写 DEFAULT 子句**（`_migrate_table_columns` 的 DDL 只带列名与类型），
    所以已部署库里的老行在新列上是 NULL。`AiProjectAnalysisConfig.resolved()` 负责把
    NULL 读成默认值 —— 不要假设数据库会替我们补上。

    新表（`ai_analysis_anomaly` / `ai_analysis_trace`）原本不在这里出现：`db.create_all()`
    在启动时会创建它们（它只建不存在的表，不会动已有的表）。只有**给已存在的表加列**
    才需要走这里。若将来给这两张新表补索引，则要同时加进 `REQUIRED_INDEXES`
    （老库里表已建好，`create_all` 不会再给它补索引）。

    `ai_analysis_trace` 现在**必须**出现在这里：它已经不是「新表」了 —— 用量采集上线时
    它已经是既有的表，而那次要给它的每轮记录补两列缓存 token。`create_all` 对已存在的
    表什么都不做，所以漏了这一段，老库上就会是「代码写 `trace.cache_read_tokens`、
    库里没这一列」的启动期报错。
    """
    _migrate_table_columns(
        db,
        "ai_analysis_job",
        {
            "progress_json": "progress_json TEXT",
            "progress_updated_at": "progress_updated_at DATETIME",
        },
        log_print,
    )
    _migrate_table_columns(
        db,
        "ai_project_analysis_config",
        {
            "api_base_url": "api_base_url VARCHAR(500)",
            "api_model": "api_model VARCHAR(200)",
            "max_analysis_rounds": "max_analysis_rounds INTEGER",
            "max_tool_requests": "max_tool_requests INTEGER",
            "prompt_char_budget": "prompt_char_budget INTEGER",
            "request_timeout_seconds": "request_timeout_seconds INTEGER",
            # 提示词缓存标记（2026-09）：这个端点接不接受 `cache_control`、接受哪一种。
            # 同样**没有 DEFAULT 子句**，老行是 NULL —— 而 NULL 经 `resolved()` 读出来
            # 正是「不发标记」那个保守默认值，所以不需要回填。
            "prompt_cache_mode": "prompt_cache_mode VARCHAR(20)",
            "prompt_cache_format": "prompt_cache_format VARCHAR(20)",
            "min_severity": "min_severity VARCHAR(20)",
            "min_confidence": "min_confidence VARCHAR(20)",
            "max_anomalies_per_run": "max_anomalies_per_run INTEGER",
            "project_knowledge": "project_knowledge TEXT",
            # 费用估算用的单价表（JSON 文本）。平台出厂不带单价，见 services/ai/pricing.py。
            "model_price_table": "model_price_table TEXT",
            # 预算闸门（2026-09）：超了就不让 AI 分析跑起来。
            # 这两列**没有 DEFAULT 子句**，老行是 NULL —— 而 NULL 的语义正好是
            # 「不限制」，所以不需要回填，也不该回填成 0（0 = 一个 token 都不许花）。
            "budget_period": "budget_period VARCHAR(20)",
            "budget_token_limit": "budget_token_limit BIGINT",
            "budget_cost_limit": "budget_cost_limit VARCHAR(40)",
            # 子代理模式（2026-09，见 services/ai/subagent.py）。同样没有 DEFAULT：
            # 老行是 NULL，而 NULL 经 `resolved()` 读出来正是「关闭 + 3 个成员」——
            # 「关闭」是唯一安全的默认值（打开它会让模型调用次数变成 n+1 倍）。
            "subagent_enabled": "subagent_enabled BOOLEAN",
            "subagent_count": "subagent_count INTEGER",
            "subagent_verify": "subagent_verify BOOLEAN",
        },
        log_print,
    )
    _migrate_table_columns(
        db,
        "ai_analysis_run",
        {
            "analysis_revision": "analysis_revision VARCHAR(80)",
            "model": "model VARCHAR(200)",
            "prompt_version": "prompt_version VARCHAR(80)",
            "skill_version": "skill_version VARCHAR(80)",
            "rules_version": "rules_version VARCHAR(80)",
            "rounds_used": "rounds_used INTEGER",
            "tool_requests_used": "tool_requests_used INTEGER",
            "tokens_input": "tokens_input INTEGER",
            "tokens_output": "tokens_output INTEGER",
            "anomalies_found": "anomalies_found INTEGER",
            "dropped_count": "dropped_count INTEGER",
            "context_chars": "context_chars INTEGER",
            # 用量面板要读的列（2026-09-18）。老行在这几列上是 NULL —— 读取侧按
            # 「未采集」处理，**不当成 0**（见 services/ai/usage.py）。
            "cache_read_tokens": "cache_read_tokens INTEGER",
            "cache_write_tokens": "cache_write_tokens INTEGER",
            "cache_source": "cache_source VARCHAR(40)",
            "duration_ms": "duration_ms INTEGER",
            "tool_stats_json": "tool_stats_json TEXT",
            "pricing_version": "pricing_version VARCHAR(40)",
            # 子代理模式（2026-09）。一家子（n 个分片 + 1 次汇总）只落**这一条**运行，
            # 所以这两列记的是「这次是按子代理模式跑的、几个分片」。NULL = 没开
            # （也是所有老行的情形），读取侧据此显示成常规运行。
            "subagent_mode": "subagent_mode VARCHAR(20)",
            "subagent_count": "subagent_count INTEGER",
            # 结论形态（2026-09-20）：模型按协议给了结构化结论 → 1；只留下一份 markdown
            # 报告 → 0。**没有 DEFAULT 子句，老行是 NULL**，而 NULL 的语义正好是保守的
            # 那一边：`_previous_run` 用 `is_(True)` 筛，NULL 一律不当基线。
            # 为什么必须有这一列，见 `models/ai_analysis/analysis_run.py` 上那段说明。
            "conclusion_structured": "conclusion_structured BOOLEAN",
        },
        log_print,
    )
    _migrate_table_columns(
        db,
        "ai_analysis_trace",
        {
            # 逐轮的缓存 token。这张表已经有 tokens_input / tokens_output /
            # request_chars / context_chars / duration_ms（一直是 NULL，用量采集上线
            # 后才开始写），所以这里只补两个真正缺的列。
            "cache_read_tokens": "cache_read_tokens INTEGER",
            "cache_write_tokens": "cache_write_tokens INTEGER",
            # 子代理模式（2026-09）：这一轮是哪个分片代理跑的、它在那个成员内部是第几轮。
            # `round_index` 是**家族内全局递增**的序号（一家子只落一条运行，两个成员的
            # 「第 1 轮」必须在库里分得开，否则撞 `uq_ai_trace_run_round`）。
            # NULL/空 = 主代理自己的轮次，也是所有老行的情形 —— 不需要回填。
            "agent": "agent VARCHAR(20)",
            "agent_round": "agent_round INTEGER",
        },
        log_print,
    )


def _migrate_ai_run_claim_columns(db, log_print):
    """给 `ai_analysis_run` 补「活动运行认领」那一列与它的唯一索引。

    **为什么必须接上**：那道 UNIQUE 索引是**并发幂等**的地基 —— 手工与定时同时触发、
    或同页连按两次时，「同一目标 + 同一份输入同时只允许一条活动运行」由数据库裁决
    （写入侧接住 `IntegrityError`）。漏了这一段，保留数据的老库上那一列与索引都不会
    生成 → 撞不出 `IntegrityError` → 幂等**静默失效**（行为退回改动前，且没有任何报错）。
    `create_all()` 只对**新建**的表建索引，对已存在的表什么都不做，正是这个缺口。

    实现在 `migrations/ai_run_claim_columns.py`（那边也能单跑：
    `python -m migrations.ai_run_claim_columns [--apply]`）。它自己已经是幂等的
    （列 / 索引已存在就跳过、失败只记日志不抛），所以这里只负责把它接上，
    不再自己判一遍「要不要跑」。
    """
    report = apply_ai_run_claim_columns(db, log_print)
    if not report["added_columns"] and not report["added_indexes"]:
        # 没动库也要留一句（与 `_migrate_table_columns` 的「无需迁移」同一条口径）：
        # 否则「接上了但一次都没生效」与「压根没接」在日志上分不开。
        log_print("ℹ️ ai_analysis_run 认领列与唯一索引已完整，无需迁移", "DB")


def apply_schema_migrations(db, log_print):
    """Apply all lightweight runtime schema migrations."""
    _migrate_repository_columns(db, log_print)
    _migrate_commits_log_columns(db, log_print)
    _migrate_weekly_version_diff_cache_columns(db, log_print)
    _migrate_agent_nodes_columns(db, log_print)
    # 任务身份与所有权（trigger_source / requested_mode / idempotency_key / job_id /
    # lease_expires_at）。**少了它，老库上的任务来源与租约就是「代码在写、库里没有这一列」
    # 的启动期报错** —— `create_all()` 只建新表，不会给已存在的表补列。
    _migrate_background_task_columns(db, log_print)
    _migrate_ai_weekly_analysis_state_columns(db, log_print)
    _migrate_ai_analysis_columns(db, log_print)
    # 认领那一列与它的唯一索引（`migrations/ai_run_claim_columns.py`）。**少了它，老库上
    # 就没有 UNIQUE，并发幂等静默失效**。它在一次调用里既加列又加索引、各自自判存在与否，
    # 所以放在列迁移这一组，索引迁移之前。
    _migrate_ai_run_claim_columns(db, log_print)
    # 索引放在最后：列迁移先跑完，避免出现「列还没 add 就要给它建索引」。
    apply_index_migrations(db, log_print)
