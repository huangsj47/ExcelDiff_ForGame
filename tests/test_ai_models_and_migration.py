"""AI 分析的数据模型与加列迁移。

两条最要紧的性质：

1. **老库上的新列是 NULL，默认值必须在读取侧补齐。** `_migrate_table_columns` 发的
   `ALTER TABLE ... ADD COLUMN` 不带 DEFAULT 子句，所以已部署库里老行的新列全是 NULL。
   如果代码直接读 `config.min_severity`，拿到的是 `None`，下游把 `None` 当门槛的后果是
   「什么都过滤不掉」—— 报出一堆低置信度条目，而且不报任何错。
2. **默认值只有一份。** 这里有一条用例专门断言模型层的默认值与规则层/预算层的默认值
   一致 —— 旧工具的 `.env.example` 与代码默认值漂移过 8 项，那类不一致没有任何报错，
   只能靠人肉眼比对。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from models.ai_analysis import (
    DISPOSITION_LABELS,
    DISPOSITIONS,
    RUN_STATUSES,
    TRACE_OUTCOMES,
    AiAnalysisAnomaly,
    AiAnalysisRun,
    AiAnalysisTrace,
    AiProjectAnalysisConfig,
)
from models.ai_analysis.project_config import (
    DEFAULT_BUDGET_PERIOD,
    DEFAULT_MAX_ANALYSIS_ROUNDS,
    DEFAULT_MAX_ANOMALIES_PER_RUN,
    DEFAULT_MAX_FILES_PER_RUN,
    DEFAULT_MAX_TOOL_REQUESTS,
    DEFAULT_MIN_CONFIDENCE,
    DEFAULT_MIN_SEVERITY,
    DEFAULT_PROMPT_CACHE_FORMAT,
    DEFAULT_PROMPT_CACHE_MODE,
    DEFAULT_PROMPT_CHAR_BUDGET,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_WEEKLY_INTERVAL_MINUTES,
    NULLABLE_RESOLVED_KEYS,
    PROMPT_CACHE_FORMAT_CHOICES,
    PROMPT_CACHE_MODE_CHOICES,
)

# 迁移前就存在的表（只保留新列加入之前的样子）。
_OLD_CONFIG_DDL = """
CREATE TABLE ai_project_analysis_config (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    auto_weekly_enabled BOOLEAN,
    weekly_interval_minutes INTEGER,
    max_files_per_run INTEGER,
    prompt_template TEXT,
    updated_by VARCHAR(100),
    created_at DATETIME,
    updated_at DATETIME
)
"""

_OLD_RUN_DDL = """
CREATE TABLE ai_analysis_run (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    target_type VARCHAR(20) NOT NULL,
    status VARCHAR(20),
    request_payload TEXT,
    response_text TEXT,
    error_message TEXT,
    created_at DATETIME
)
"""

# `ai_analysis_trace` 是后加进迁移的：它先作为「新表」由 create_all 建出来，用量采集
# 上线时又给它补了两列缓存 token。这里的老 DDL 就是「加了那两列之前」的样子。
_OLD_TRACE_DDL = """
CREATE TABLE ai_analysis_trace (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    round_index INTEGER NOT NULL,
    outcome VARCHAR(30),
    parsed_ok BOOLEAN,
    response_text TEXT,
    request_chars INTEGER,
    tokens_input INTEGER,
    tokens_output INTEGER,
    context_chars INTEGER,
    duration_ms INTEGER,
    created_at DATETIME
)
"""

CONFIG_NEW_COLUMNS = (
    "api_base_url",
    "api_model",
    "max_analysis_rounds",
    "max_tool_requests",
    "prompt_char_budget",
    "request_timeout_seconds",
    # 提示词缓存标记（2026-09）。老行在这两列上是 NULL，`resolved()` 把 NULL 读成
    # 「不发标记」那个保守默认值（见 `_cache_choice_or_default`），不需要回填。
    "prompt_cache_mode",
    "prompt_cache_format",
    "min_severity",
    "min_confidence",
    "max_anomalies_per_run",
    "project_knowledge",
    "model_price_table",
    # 预算闸门（2026-09）。老行在这三列上是 NULL，而 NULL 的语义就是「不限制」——
    # 这正是这个功能要的默认行为，所以没有回填这一步。
    "budget_period",
    "budget_token_limit",
    "budget_cost_limit",
)

RUN_NEW_COLUMNS = (
    "analysis_revision",
    "model",
    "prompt_version",
    "skill_version",
    "rules_version",
    "rounds_used",
    "tool_requests_used",
    "tokens_input",
    "tokens_output",
    "anomalies_found",
    "dropped_count",
    "context_chars",
    # 用量采集（2026-09-18）
    "cache_read_tokens",
    "cache_write_tokens",
    "cache_source",
    "duration_ms",
    "tool_stats_json",
    "pricing_version",
)

# `ai_analysis_trace` 已有 tokens_input / tokens_output / request_chars / context_chars /
# duration_ms（一直是 NULL，采集上线后才开始写），所以只补这两个。
TRACE_NEW_COLUMNS = (
    "cache_read_tokens",
    "cache_write_tokens",
)


class _DbStub:
    """`_migrate_table_columns` 只用到 `db.engine` 与 `db.session`，所以给一个最小的替身。

    用替身而不是给 Flask-SQLAlchemy 的 `db` 换引擎：后者的引擎在创建时就绑死了，而
    把测试指向真实库再删列会污染整个会话共用的隔离库。
    """

    def __init__(self, engine):
        self.engine = engine
        self.session = Session(engine)


@pytest.fixture()
def old_db(tmp_path):
    """一个「迁移前」的库：两张表都只有老列。"""
    path = tmp_path / "old_ai.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    with engine.begin() as connection:
        connection.exec_driver_sql(_OLD_CONFIG_DDL)
        connection.exec_driver_sql(_OLD_RUN_DDL)
        connection.exec_driver_sql(_OLD_TRACE_DDL)
    stub = _DbStub(engine)
    yield stub
    stub.session.close()
    engine.dispose()


def _columns(stub, table: str) -> set[str]:
    return {column["name"] for column in inspect(stub.engine).get_columns(table)}


# ==========================================================================
# 加列迁移
# ==========================================================================


def test_migration_adds_every_new_column(old_db):
    from services.db_migration_service import _migrate_ai_analysis_columns

    _migrate_ai_analysis_columns(old_db, lambda *a, **k: None)

    assert set(CONFIG_NEW_COLUMNS).issubset(_columns(old_db, "ai_project_analysis_config"))
    assert set(RUN_NEW_COLUMNS).issubset(_columns(old_db, "ai_analysis_run"))
    assert set(TRACE_NEW_COLUMNS).issubset(_columns(old_db, "ai_analysis_trace"))


def test_migration_is_idempotent(old_db):
    """启动时每次都会跑一遍，重复执行必须无害。

    这里额外断言**第二遍没有报失败**。`_migrate_table_columns` 用字典的**键**判断列
    是否已存在、用字典的**值**（DDL 文本）真正加列。两者一旦不一致（例如键写成
    `rounds_used_typo` 而 DDL 写 `rounds_used`），第一遍能成功，之后**每次启动都会再
    ALTER 一次并因「列已存在」报错** —— 错误被兜住只打个告警，于是没人发现，日志里永远
    躺着一条迁移失败。变异验证时正是这个位置暴露了它。
    """
    from services.db_migration_service import _migrate_ai_analysis_columns

    messages: list[str] = []

    def _log(*args, **_kwargs):
        messages.append(" ".join(str(arg) for arg in args))

    _migrate_ai_analysis_columns(old_db, _log)
    before = _columns(old_db, "ai_analysis_run")
    messages.clear()

    _migrate_ai_analysis_columns(old_db, _log)

    assert _columns(old_db, "ai_analysis_run") == before
    failures = [message for message in messages if "失败" in message]
    assert not failures, f"第二遍迁移报了失败：{failures}"


def test_migration_keeps_existing_rows_and_leaves_new_columns_null(old_db):
    """**这是「默认值必须在读取侧补齐」的实证。**

    老行在加列之后不会凭空获得默认值 —— 它们就是 NULL。模型层的 `resolved()` 之所以
    存在，就是因为数据库不会替我们补。
    """
    from services.db_migration_service import _migrate_ai_analysis_columns

    with old_db.engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO ai_project_analysis_config (id, project_id, max_files_per_run)"
            " VALUES (1, 7, 50)"
        )

    _migrate_ai_analysis_columns(old_db, lambda *a, **k: None)

    row = old_db.session.query(AiProjectAnalysisConfig).filter_by(id=1).first()
    assert row is not None
    assert row.max_files_per_run == 50, "老数据不能被改动"
    assert row.min_severity is None, "新列在老行上就是 NULL"
    assert row.max_analysis_rounds is None


def test_migration_skips_a_missing_table(tmp_path):
    """库更老、表还没建出来时不能抛异常（`create_all` 会先建表，但顺序不该被依赖）。"""
    from services.db_migration_service import _migrate_ai_analysis_columns

    engine = create_engine(f"sqlite:///{(tmp_path / 'empty.db').as_posix()}")
    stub = _DbStub(engine)
    try:
        _migrate_ai_analysis_columns(stub, lambda *a, **k: None)
    finally:
        stub.session.close()
        engine.dispose()


def test_migration_adds_only_the_missing_columns(tmp_path):
    """部分迁移过的库（例如上一次迁移中途失败）要能续上。"""
    from services.db_migration_service import _migrate_ai_analysis_columns

    engine = create_engine(f"sqlite:///{(tmp_path / 'partial.db').as_posix()}")
    with engine.begin() as connection:
        connection.exec_driver_sql(_OLD_CONFIG_DDL)
        connection.exec_driver_sql(
            "ALTER TABLE ai_project_analysis_config ADD COLUMN min_severity VARCHAR(20)"
        )
        connection.exec_driver_sql(_OLD_RUN_DDL)
        connection.exec_driver_sql(_OLD_TRACE_DDL)

    stub = _DbStub(engine)
    try:
        _migrate_ai_analysis_columns(stub, lambda *a, **k: None)
        assert set(CONFIG_NEW_COLUMNS).issubset(_columns(stub, "ai_project_analysis_config"))
        assert set(RUN_NEW_COLUMNS).issubset(_columns(stub, "ai_analysis_run"))
        assert set(TRACE_NEW_COLUMNS).issubset(_columns(stub, "ai_analysis_trace"))
    finally:
        stub.session.close()
        engine.dispose()


def test_the_migration_is_wired_into_startup():
    """光有函数不算数，必须挂在 `apply_schema_migrations` 上。"""
    import inspect as pyinspect

    from services import db_migration_service

    source = pyinspect.getsource(db_migration_service.apply_schema_migrations)
    assert "_migrate_ai_analysis_columns" in source


def test_migrations_run_clean_against_the_real_schema():
    """在真实（已建好表的）库上跑一遍：注册顺序、SQL 方言、索引迁移都不能炸。"""
    import app as app_module
    from services.db_migration_service import apply_schema_migrations

    with app_module.app.app_context():
        apply_schema_migrations(app_module.db, lambda *a, **k: None)
        apply_schema_migrations(app_module.db, lambda *a, **k: None)


def test_new_tables_exist_with_their_columns():
    """新表靠 `create_all()` 创建，所以它们必须真的出现在元数据里。

    新表不需要加列迁移（`create_all` 直接按模型建），所以**模型的列定义就是唯一来源**；
    这条用例守着「列别被误删」。
    """
    expected = {
        AiAnalysisAnomaly: (
            "run_id",
            "fingerprint",
            "title",
            "category",
            "severity",
            "confidence",
            "evidence",
            "commit_ref",
            "file_path",
            "disposition",
            "disposition_by",
            "disposition_at",
        ),
        AiAnalysisTrace: (
            "run_id",
            "round_index",
            "outcome",
            "parsed_ok",
            "response_text",
            "requests_json",
            "executed_json",
            "dropped_json",
            "tokens_input",
        ),
    }
    for model, columns in expected.items():
        names = set(model.__table__.columns.keys())
        assert set(columns).issubset(names), (
            f"{model.__tablename__} 缺少列 {set(columns) - names}"
        )


def test_trace_has_a_unique_constraint_per_round():
    """一轮一行。它挡住的是「重试逻辑把同一轮写了两遍」——那种情况会留下两条自相矛盾的
    trace，比直接报错难查得多。"""
    names = {constraint.name for constraint in AiAnalysisTrace.__table__.constraints}
    assert "uq_ai_trace_run_round" in names


def test_anomaly_has_no_unique_constraint_on_fingerprint():
    """去重是规则层的职责，它有自己的记账。

    再加一道唯一约束不会让去重更正确，却会让「去重逻辑回归」从「报告里多一条重复项」
    升级成「整个分析在写库时 IntegrityError 失败」—— 用一个更严重的故障去兜一个更轻的
    问题。
    """
    names = {constraint.name for constraint in AiAnalysisAnomaly.__table__.constraints}
    assert not any(name and name.startswith("uq_") for name in names)


# ==========================================================================
# 读取侧的默认值
# ==========================================================================


def test_resolved_fills_every_null_with_a_default():
    """**核心用例**：老库上全部新列都是 NULL 时，读出来必须是可用的一套配置。"""
    config = AiProjectAnalysisConfig(project_id=1)

    resolved = config.resolved()

    assert resolved["max_analysis_rounds"] == DEFAULT_MAX_ANALYSIS_ROUNDS
    assert resolved["max_tool_requests"] == DEFAULT_MAX_TOOL_REQUESTS
    assert resolved["prompt_char_budget"] == DEFAULT_PROMPT_CHAR_BUDGET
    assert resolved["request_timeout_seconds"] == DEFAULT_REQUEST_TIMEOUT_SECONDS
    assert resolved["min_severity"] == DEFAULT_MIN_SEVERITY
    assert resolved["min_confidence"] == DEFAULT_MIN_CONFIDENCE
    assert resolved["max_anomalies_per_run"] == DEFAULT_MAX_ANOMALIES_PER_RUN
    assert resolved["max_files_per_run"] == DEFAULT_MAX_FILES_PER_RUN
    assert resolved["weekly_interval_minutes"] == DEFAULT_WEEKLY_INTERVAL_MINUTES
    assert resolved["auto_weekly_enabled"] is True
    assert resolved["prompt_template"] == ""
    assert resolved["api_base_url"] == ""
    assert resolved["api_model"] == ""
    assert resolved["project_knowledge"] == ""
    assert resolved["model_price_table"] == ""


def test_resolved_never_returns_none_for_any_key():
    """逐键断言，防止将来加了列忘了在 `resolved()` 里补。

    `None` 是这里最危险的返回值：`min_severity=None` 会让规则层「什么都过滤不掉」，
    `max_analysis_rounds=None` 会让循环条件比较时报错或永不收敛。

    **例外**是 `NULLABLE_RESOLVED_KEYS` 里那两栏：它们的 `None` 是一个有意义的取值
    （预算「不限制」），不是漏补的默认值。例外清单本身也在这里被校验 ——
    它必须真的在 `resolved()` 的返回体里，也不能被随手扩大成「谁的 None 都放行」：
    下面两条反向断言把这件事钉死。
    """
    resolved = AiProjectAnalysisConfig(project_id=1).resolved()
    assert set(NULLABLE_RESOLVED_KEYS).issubset(resolved), (
        "例外清单里出现了 resolved() 根本不返回的键 —— 那这条例外就白开了"
    )
    for key, value in resolved.items():
        if key in NULLABLE_RESOLVED_KEYS:
            continue
        assert value is not None, f"{key} 读出来是 None"


def test_the_nullable_resolved_keys_are_only_the_budget_limits():
    """**反向守卫**：例外清单只许装预算那两栏。

    没有这一条，将来有人加了一栏新配置、又不想补默认值，最省事的做法就是把它塞进
    这个清单 —— 上面那条用例照样全绿，而 `resolved()` 又开始返回 None 了。
    """
    assert tuple(NULLABLE_RESOLVED_KEYS) == ("budget_token_limit", "budget_cost_limit")


def test_resolved_treats_an_unset_budget_as_unlimited():
    """**未配置 = 不限制**，不是 0、也不是任何默认值。

    0 会被预算闸门读成「一个 token 都不许花」，把 AI 分析整个锁死；而界面上
    找不到任何解释（那一栏是空的）。
    """
    resolved = AiProjectAnalysisConfig(project_id=1).resolved()
    assert resolved["budget_token_limit"] is None
    assert resolved["budget_cost_limit"] is None
    assert resolved["budget_period"] == DEFAULT_BUDGET_PERIOD


def test_resolved_keeps_a_configured_budget():
    """反向自检：配了的值不能被「不限制」那条口径吃掉。"""
    config = AiProjectAnalysisConfig(
        project_id=1,
        budget_period="weekly",
        budget_token_limit=5_000_000,
        budget_cost_limit="12.50",
    )
    resolved = config.resolved()
    assert resolved["budget_period"] == "weekly"
    assert resolved["budget_token_limit"] == 5_000_000
    # 金额读回来仍是十进制字符串（走 float 会在费用那一套 Decimal 计算里引入误差）。
    assert resolved["budget_cost_limit"] == "12.50"


def test_resolved_treats_a_zero_or_garbage_budget_as_unset():
    """0 / 负数 / 空串 / 乱码一律读成「不限制」。

    0 尤其要紧：它要么是「用户手动填的 0」（= 不许花钱，那是另一件事，应该报错让他
    改），要么是「某个上游把 NULL 写成了 0」。这两种在库里分不开，而把功能锁死的
    代价远大于放过一次 —— 所以按「不限制」处理，并在配置界面那一栏写明留空即不限制。
    """
    for raw in (0, -5, "", "   ", None, "abc"):
        config = AiProjectAnalysisConfig(
            project_id=1, budget_token_limit=raw, budget_cost_limit=raw
        )
        resolved = config.resolved()
        assert resolved["budget_token_limit"] is None, f"budget_token_limit={raw!r}"
        assert resolved["budget_cost_limit"] is None, f"budget_cost_limit={raw!r}"


def test_resolved_prefers_stored_values():
    """反向自检：不能无差别覆盖成默认值。"""
    config = AiProjectAnalysisConfig(
        project_id=1,
        min_severity="critical",
        max_analysis_rounds=3,
        api_model="gpt-x",
        project_knowledge="只看配表",
        auto_weekly_enabled=False,
    )
    resolved = config.resolved()

    assert resolved["min_severity"] == "critical"
    assert resolved["max_analysis_rounds"] == 3
    assert resolved["api_model"] == "gpt-x"
    assert resolved["project_knowledge"] == "只看配表"
    assert resolved["auto_weekly_enabled"] is False


def test_resolved_normalizes_case_and_whitespace():
    """门槛值来自表单，可能带空格或大写；规则层按小写比较。"""
    config = AiProjectAnalysisConfig(project_id=1, min_severity=" CRITICAL ", api_model=" x ")
    resolved = config.resolved()
    assert resolved["min_severity"] == "critical"
    assert resolved["api_model"] == "x"


def test_resolved_survives_a_string_in_an_integer_column():
    """某些后端/驱动会把整数列读成字符串。读不动时退回默认值，而不是抛异常。"""
    config = AiProjectAnalysisConfig(project_id=1)
    config.max_analysis_rounds = "abc"
    config.max_tool_requests = "7"

    assert config.resolved()["max_analysis_rounds"] == DEFAULT_MAX_ANALYSIS_ROUNDS
    assert config.resolved()["max_tool_requests"] == 7


# ==========================================================================
# 默认值的唯一事实源
# ==========================================================================


def test_model_defaults_agree_with_the_rule_layer():
    """**防漂移**：门槛与封顶的默认值在模型层与规则层必须一致。

    两处不一致的后果是静默的：界面显示「严重度门槛：高」，实际生效的是另一套值，
    谁都不会发现。旧工具的默认值就漂移过 8 项。
    """
    from services.ai.rules import RuleThresholds

    resolved = AiProjectAnalysisConfig(project_id=1).resolved()
    thresholds = RuleThresholds.from_config(resolved)

    assert thresholds.min_severity == resolved["min_severity"]
    assert thresholds.min_confidence == resolved["min_confidence"]
    assert thresholds.max_anomalies == resolved["max_anomalies_per_run"]
    assert thresholds == RuleThresholds()


def test_model_defaults_agree_with_the_budget_layer():
    from services.ai.budget import DEFAULT_TOTAL_CHARS

    assert DEFAULT_PROMPT_CHAR_BUDGET == DEFAULT_TOTAL_CHARS


def test_the_prompt_cache_value_domains_do_not_drift():
    """**防漂移**：缓存标记的值域在模型层与行为层各写了一份，必须一致。

    模型层不 import 服务层（与其它默认值一样的处理方式），所以这两份是**故意**重复的。
    不一致的后果是静默的：界面/校验允许填 `on`、而行为层只认 `explicit`，于是用户
    打开了一个永远不生效的开关 —— 没有报错，也没有日志。
    """
    from services.ai import prompt_cache

    assert set(PROMPT_CACHE_MODE_CHOICES) == set(prompt_cache.PROMPT_CACHE_MODES)
    assert set(PROMPT_CACHE_FORMAT_CHOICES) == set(prompt_cache.PROMPT_CACHE_FORMATS)
    assert DEFAULT_PROMPT_CACHE_MODE == prompt_cache.DEFAULT_PROMPT_CACHE_MODE
    assert DEFAULT_PROMPT_CACHE_FORMAT == prompt_cache.DEFAULT_PROMPT_CACHE_FORMAT
    # 默认值的组合必须是「不发标记」：`auto` + `none`（见 prompt_cache.resolve_cache_marker）。
    assert prompt_cache.resolve_cache_marker(
        DEFAULT_PROMPT_CACHE_MODE, DEFAULT_PROMPT_CACHE_FORMAT
    ) is None


def test_resolved_reads_an_unknown_prompt_cache_value_as_the_conservative_default():
    """读不出来的值一律退回「不发标记」那一侧，而不是退成「发一个没人认识的标记」。

    老库上的新列是 NULL、有人手工改过库、将来删掉一个选项 —— 这几种都会走到这里。
    """
    config = AiProjectAnalysisConfig(project_id=1)
    config.prompt_cache_mode = "ON"  # 大小写与值域都不对
    config.prompt_cache_format = "openai"

    resolved = config.resolved()

    assert resolved["prompt_cache_mode"] == DEFAULT_PROMPT_CACHE_MODE
    assert resolved["prompt_cache_format"] == DEFAULT_PROMPT_CACHE_FORMAT
    assert resolved["prompt_cache_mode"] == "auto" and resolved["prompt_cache_format"] == "none"


def test_resolved_keeps_a_declared_prompt_cache_switch():
    """反向自检：声明过的值不能被默认值吃掉 —— 那这个功能就永远打不开。"""
    config = AiProjectAnalysisConfig(
        project_id=1,
        prompt_cache_mode="explicit",
        prompt_cache_format=" ANTHROPIC ",
    )
    resolved = config.resolved()

    assert resolved["prompt_cache_mode"] == "explicit"
    assert resolved["prompt_cache_format"] == "anthropic"


def test_disposition_labels_cover_exactly_the_dispositions():
    assert set(DISPOSITION_LABELS) == set(DISPOSITIONS)
    assert DISPOSITIONS == ("pending", "confirmed", "ignored")


def test_trace_outcomes_cover_the_protocol_statuses():
    """trace 的 outcome 是我们观察到的结果，必须包含协议里的两种终态。"""
    from services.ai.protocol import STATUSES

    assert set(STATUSES).issubset(TRACE_OUTCOMES)


# ==========================================================================
# 运行状态
# ==========================================================================


def test_run_statuses_include_failed():
    """原实现没有 failed，`error_message` 列存在却从未被写入过。

    于是「进程中断」「模型返回不可用」都只能留下一条永远 running 的记录，用户看到的
    是「一直在分析中」，无从判断到底出了什么事。
    """
    assert "failed" in RUN_STATUSES
    assert "succeeded" in RUN_STATUSES


@pytest.mark.parametrize("status", RUN_STATUSES)
def test_every_status_is_a_non_empty_label(status):
    assert status and isinstance(status, str)


def test_a_fresh_running_record_is_not_stale():
    from datetime import datetime, timezone

    run = AiAnalysisRun(
        project_id=1,
        target_type="commit",
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    assert not run.is_stale_running
    assert run.effective_status == "running"


def test_a_long_running_record_reads_as_failed():
    """进程被杀留下的僵尸记录不能一直显示「分析中」。

    只在读取侧判定，**不改库里的值** —— 那个进程可能只是慢，还没死。
    """
    from datetime import datetime, timedelta, timezone

    run = AiAnalysisRun(
        project_id=1,
        target_type="commit",
        status="running",
        started_at=datetime.now(timezone.utc) - timedelta(hours=3),
    )
    assert run.is_stale_running
    assert run.effective_status == "failed"
    assert run.status == "running", "库里的原值不该被就地改掉"


def test_a_naive_started_at_is_treated_as_utc():
    """sqlite 上读回来的 datetime 可能没有时区，直接与 aware 的 now 相减会抛 TypeError。

    用一个固定的朴素时间而不是 `utcnow() - 3h`：后者在 3.12 起就带弃用告警，
    而这里要的只是「一个很久以前的朴素时间」。
    """
    from datetime import datetime

    run = AiAnalysisRun(
        project_id=1,
        target_type="commit",
        status="running",
        started_at=datetime(2020, 1, 1, 12, 0, 0),
    )
    assert run.is_stale_running


def test_a_succeeded_record_is_never_stale():
    from datetime import datetime, timedelta, timezone

    run = AiAnalysisRun(
        project_id=1,
        target_type="commit",
        status="succeeded",
        started_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    assert not run.is_stale_running
    assert run.effective_status == "succeeded"


def test_a_run_without_timestamps_is_not_reported_as_stale():
    run = AiAnalysisRun(project_id=1, target_type="commit", status="running")
    assert not run.is_stale_running


def test_run_to_dict_is_json_safe():
    run = AiAnalysisRun(project_id=1, target_type="commit", status="pending")
    payload = run.to_dict()
    assert payload["status"] == "pending"
    assert payload["stored_status"] == "pending"
    assert payload["created_at"] is None
    assert payload["tokens_input"] is None

# 变更清单**全量列出**时的最坏字符数。**这是实测值**，不是估的：
# 用 `render_change_summary` 渲染一份「刚好顶到 `ai_analysis_service.MAX_LIST_CHARS`
# （60,000）的清单」—— 约 1,016 个文件、240 个提交 —— 得到 87,055 字符。
# 提交数越多，每个提交的头部（提交号/信息/作者/时间/文件数）重复得越多，所以最坏情况
# 取提交数大的那一档。清单现在默认全列（见 `_select_listed_files`），所以这个数取代了
# 原来「600 个文件、取样 200 个」的 39,283。
_LARGE_VERSION_SUMMARY_CHARS = 87_055

# 平台内置 SKILL.md（约 9,600 字符）加项目知识包，按 12,000 算常驻开销。
# references 是按需索取的，索取时占用的是上下文那部分预算，所以不重复计入这里。
_SYSTEM_PROMPT_CHARS = 12_000


def test_the_context_item_cap_never_wastes_a_paid_request():
    """**`max_items` 不许小于 `max_tool_requests`。**

    这两个数字分在两个模块里（`budget.DEFAULT_MAX_ITEMS` 与
    `context_tools.DEFAULT_MAX_TOOL_REQUESTS`），谁也不知道对方存在。旧默认值是 8 与
    12：模型索要 12 个文件、平台老老实实取了 12 次，然后 `enforce_budget` 按条数上限
    把其中 4 条**丢掉**并写一句「有 4 条因条数上限未提供给你」。也就是说三分之一的
    索取额度被买了又扔 —— 用户为此付了 token 与时间，模型却以为自己没要过。

    上限关系必须反过来：条数上限**不小于**索取次数，取回来的都要带得走（带不走的原因是
    字符预算不足时，那由 `enforce_budget` 逐级压缩处理，并且会如实说明被压缩了）。
    """
    from services.ai.budget import DEFAULT_MAX_ITEMS
    from services.ai.context_tools import DEFAULT_MAX_TOOL_REQUESTS

    assert DEFAULT_MAX_ITEMS >= DEFAULT_MAX_TOOL_REQUESTS, (
        f"条数上限 {DEFAULT_MAX_ITEMS} 小于索取次数 {DEFAULT_MAX_TOOL_REQUESTS}："
        f"每次分析都会白白丢掉 {DEFAULT_MAX_TOOL_REQUESTS - DEFAULT_MAX_ITEMS} 条已取回的内容"
    )


def test_the_default_budget_gives_each_shard_a_workable_allowance():
    """**分片拿到几次是「算」出来的，所以默认值必须按分片之后再验一遍。**

    `max_tool_requests` 是**整次分析的总数**，而子代理模式下它会被 `subagent.plan_family`
    按 `max(2, 总数 ÷ (分片数 + 1))` 分给每个成员。于是「默认 20 次」在默认的 3 个分片下
    等于**每片 5 次** —— 线上真实的一次周版本分析里，一个分片跑完 3 次就报「本轮上下文
    额度已用尽」，取不到导出配置与消费侧代码，`config_id` 那一整个维度只能写成信息缺口。

    这不是「配置填错了」，是**两个默认值乘出来的结果**：改上限、改分片数、改分摊公式，
    三处任何一处动了都会让它变小，而三处分别在三个文件里。所以这里把「默认配置下每个
    分片够不够用」算出来直接断言 —— 它挡的是下一次「只调了一个数」的改动。

    10 这个下限来自那次线上数据：一个分片要覆盖它那几组维度（含跨模块引用），
    3~5 次会在跑到第二个维度时就断粮。
    """
    from models.ai_analysis.project_config import (
        DEFAULT_MAX_TOOL_REQUESTS,
        DEFAULT_SUBAGENT_COUNT,
        DEFAULT_SUBAGENT_ENABLED,
    )

    assert DEFAULT_SUBAGENT_ENABLED, (
        "前提：默认开着子代理。关掉的话下面这条断言就没有意义了（每人拿到全部额度），"
        "那种情况下这条用例该跟着一起改，而不是继续在这里空转"
    )
    per_shard = DEFAULT_MAX_TOOL_REQUESTS // (DEFAULT_SUBAGENT_COUNT + 1)
    assert per_shard >= 10, (
        f"默认配置下每个分片只有 {per_shard} 次上下文索取（总上限 {DEFAULT_MAX_TOOL_REQUESTS}"
        f" ÷ ({DEFAULT_SUBAGENT_COUNT} + 1)）—— 线上 3 次就会在报告里写「本轮上下文额度"
        "已用尽」，那一整个维度只能写成信息缺口。调大上限或调小分片数，两处一起看"
    )


def test_the_prompt_budget_can_honor_the_request_budget():
    """**五个数字必须互相自洽，不能各看各的。**

    真实关系是：

        prompt_char_budget ≥ 系统提示词 + 变更摘要 + 历史结论基线 + 索取次数 × 单条上限

    旧默认值（120,000 / 12 次 / 14,000）在 150 个提交的版本上，右边是
    12,000 + 39,283 + 12 × 14,000 = 219,283，**超了 1.8 倍**。超了不会报错：模型照样
    索要 12 个文件，其中一半被截断，而它无法区分「预算不够」与「文件就这么大」。

    增量分析上线后，历史结论基线摘要（`baseline.DEFAULT_BASELINE_CHARS`）也从同一份
    预算里出。它**必须**算进来：每轮都带着它，而被它挤掉的正是本轮上下文条目的空间 ——
    漏算的后果是「分析跑得越勤，每次能看到的新代码反而越少」，而且没人会把它和基线
    联系起来（两个数字在代码里离得很远）。

    这条测试锁的是「改其中任何一个数字都要同时看另外几个」——只调预算会浪费模型的窗口，
    只调单条上限会丢掉 diff 细节，只调索取次数会让模型一次要不够，
    只调基线会让增量分析悄悄吃掉上下文预算。
    """
    from services.ai.baseline import DEFAULT_BASELINE_CHARS
    from services.ai.budget import DEFAULT_TOTAL_CHARS
    from services.ai.context_tools import DEFAULT_TOOL_LIMITS

    per_item = max(DEFAULT_TOOL_LIMITS.values())
    needed = (
        _SYSTEM_PROMPT_CHARS
        + _LARGE_VERSION_SUMMARY_CHARS
        + DEFAULT_BASELINE_CHARS
        + DEFAULT_MAX_TOOL_REQUESTS * per_item
    )

    assert DEFAULT_BASELINE_CHARS < _LARGE_VERSION_SUMMARY_CHARS, (
        "基线只是历史结论的索引，不该比本轮的变更摘要还大 —— 那会让模型盯着旧结论看"
    )
    assert DEFAULT_PROMPT_CHAR_BUDGET >= needed, (
        f"提示词预算 {DEFAULT_PROMPT_CHAR_BUDGET:,} 装不下：系统提示词 "
        f"{_SYSTEM_PROMPT_CHARS:,} + 变更摘要 {_LARGE_VERSION_SUMMARY_CHARS:,} + "
        f"历史结论基线 {DEFAULT_BASELINE_CHARS:,} + "
        f"{DEFAULT_MAX_TOOL_REQUESTS} 次 × {per_item:,} = {needed:,}。"
        "要么调大预算，要么调小单条上限或索取次数"
    )
    assert DEFAULT_TOTAL_CHARS == DEFAULT_PROMPT_CHAR_BUDGET, "预算两处不一致"

    # 上界 = **默认窗口的水位**（1M token × 60%）。理由见下面这条：它不再是「超了会报错」
    # （那个口子已经由 `effective_prompt_budget` 的水位压掉了），而是「再大也没有意义」。
    from services.ai.budget import DEFAULT_CONTEXT_TOKENS, context_watermark_chars

    assert DEFAULT_PROMPT_CHAR_BUDGET <= context_watermark_chars(DEFAULT_CONTEXT_TOKENS), (
        f"预算 {DEFAULT_PROMPT_CHAR_BUDGET:,} 超过了默认窗口的水位 —— 窗口问不到时"
        "（`DEFAULT_CONTEXT_TOKENS` = 1M）它本来就会被压回水位，写更大的数只是让人以为"
        "自己配得下。要真的更大，先确认端点声明的窗口确实够大。"
    )


def test_the_reference_limit_is_not_larger_than_the_diff_limit():
    """读参考文档与读 diff 用的是同一档上限。

    参考文档比 diff 小得多，单独给更大的额度只会让「12 次索取」里最不值钱的那类
    占掉最多预算。
    """
    from services.ai.context_tools import DEFAULT_TOOL_LIMITS

    assert DEFAULT_TOOL_LIMITS["read_reference"] <= DEFAULT_TOOL_LIMITS["file_diff"]
    assert DEFAULT_TOOL_LIMITS["commit_detail"] <= DEFAULT_TOOL_LIMITS["file_diff"]
