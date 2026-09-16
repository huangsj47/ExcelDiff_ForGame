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
    DEFAULT_MAX_ANALYSIS_ROUNDS,
    DEFAULT_MAX_ANOMALIES_PER_RUN,
    DEFAULT_MAX_FILES_PER_RUN,
    DEFAULT_MAX_TOOL_REQUESTS,
    DEFAULT_MIN_CONFIDENCE,
    DEFAULT_MIN_SEVERITY,
    DEFAULT_PROMPT_CHAR_BUDGET,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_WEEKLY_INTERVAL_MINUTES,
)

# 迁移前就存在的那两张表（只保留新列加入之前的样子）。
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

CONFIG_NEW_COLUMNS = (
    "api_base_url",
    "api_model",
    "max_analysis_rounds",
    "max_tool_requests",
    "prompt_char_budget",
    "request_timeout_seconds",
    "min_severity",
    "min_confidence",
    "max_anomalies_per_run",
    "project_knowledge",
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

    stub = _DbStub(engine)
    try:
        _migrate_ai_analysis_columns(stub, lambda *a, **k: None)
        assert set(CONFIG_NEW_COLUMNS).issubset(_columns(stub, "ai_project_analysis_config"))
        assert set(RUN_NEW_COLUMNS).issubset(_columns(stub, "ai_analysis_run"))
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


def test_resolved_never_returns_none_for_any_key():
    """逐键断言，防止将来加了列忘了在 `resolved()` 里补。

    `None` 是这里最危险的返回值：`min_severity=None` 会让规则层「什么都过滤不掉」，
    `max_analysis_rounds=None` 会让循环条件比较时报错或永不收敛。
    """
    resolved = AiProjectAnalysisConfig(project_id=1).resolved()
    for key, value in resolved.items():
        assert value is not None, f"{key} 读出来是 None"


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
