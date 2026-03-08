from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

from sqlalchemy.exc import SQLAlchemyError

import services.app_bootstrap_db_service as bootstrap_db


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
    "agent_nodes",
    "agent_project_bindings",
    "agent_tasks",
    "agent_default_admins",
    "agent_incidents",
]


class _AppStub:
    def __init__(self, config):
        self.config = config

    def app_context(self):
        return nullcontext()


def _collector():
    logs = []

    def _log_print(message, *_args, **_kwargs):
        logs.append(str(message))

    return logs, _log_print


def test_db_bootstrap_exception_tuples_are_declared():
    assert hasattr(bootstrap_db, "DB_STARTUP_DIR_CREATE_ERRORS")
    assert hasattr(bootstrap_db, "DB_STARTUP_INSPECT_ERRORS")
    assert hasattr(bootstrap_db, "DB_STARTUP_CREATE_ALL_ERRORS")
    assert hasattr(bootstrap_db, "DB_STARTUP_DIAGNOSTIC_ERRORS")
    assert hasattr(bootstrap_db, "DB_STARTUP_SIZE_FORMAT_ERRORS")
    assert hasattr(bootstrap_db, "DB_STARTUP_CACHE_CLEANUP_ERRORS")


def test_create_tables_returns_when_instance_dir_creation_fails(monkeypatch):
    logs, log_print = _collector()
    app = _AppStub(
        {
            "SQLALCHEMY_DATABASE_URI": "sqlite:///bootstrap-test.db",
            "SQLITE_DB_PATH": "missing_dir/bootstrap.db",
        }
    )
    db = SimpleNamespace(engine=object(), create_all=lambda: None)
    migrations_called = {"value": False}

    monkeypatch.setattr(bootstrap_db.os.path, "exists", lambda _path: False)
    monkeypatch.setattr(
        bootstrap_db.os,
        "makedirs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("no permission")),
    )

    def _apply_schema_migrations(*_args, **_kwargs):
        migrations_called["value"] = True

    bootstrap_db.create_tables_with_runtime_checks(
        app=app,
        db=db,
        log_print=log_print,
        apply_schema_migrations=_apply_schema_migrations,
    )

    assert migrations_called["value"] is False
    assert any("创建instance目录失败" in message for message in logs)


def test_create_tables_continues_when_initial_inspect_raises_sqlalchemy(monkeypatch):
    logs, log_print = _collector()
    app = _AppStub({"SQLALCHEMY_DATABASE_URI": "mysql://user:pwd@localhost/db"})
    create_all_called = {"value": False}
    migrations_called = {"value": False}

    db = SimpleNamespace(engine=object())

    def _create_all():
        create_all_called["value"] = True

    db.create_all = _create_all

    inspect_call_count = {"value": 0}

    def _fake_inspect(_engine):
        inspect_call_count["value"] += 1
        call_index = inspect_call_count["value"]
        if call_index == 1:
            raise SQLAlchemyError("inspect boom")
        return SimpleNamespace(get_table_names=lambda: EXPECTED_TABLES)

    monkeypatch.setattr(bootstrap_db, "inspect", _fake_inspect)
    monkeypatch.setattr(
        bootstrap_db,
        "collect_sqlite_runtime_diagnostics",
        lambda _uri: {"backend": "mysql"},
    )

    def _apply_schema_migrations(*_args, **_kwargs):
        migrations_called["value"] = True

    bootstrap_db.create_tables_with_runtime_checks(
        app=app,
        db=db,
        log_print=log_print,
        apply_schema_migrations=_apply_schema_migrations,
    )

    assert create_all_called["value"] is True
    assert migrations_called["value"] is True
    assert any("检查现有表失败" in message for message in logs)


def test_create_tables_uses_size_formatter_fallback_for_invalid_diagnostic_bytes(monkeypatch):
    logs, log_print = _collector()
    app = _AppStub(
        {
            "SQLALCHEMY_DATABASE_URI": "sqlite:///bootstrap-test.db",
            "SQLITE_DB_PATH": "existing_dir/bootstrap.db",
        }
    )
    db = SimpleNamespace(engine=object(), create_all=lambda: None)

    monkeypatch.setattr(bootstrap_db.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(
        bootstrap_db,
        "inspect",
        lambda _engine: SimpleNamespace(get_table_names=lambda: EXPECTED_TABLES),
    )
    monkeypatch.setattr(
        bootstrap_db,
        "collect_sqlite_runtime_diagnostics",
        lambda _uri: {
            "backend": "sqlite",
            "sqlite_path": "existing_dir/bootstrap.db",
            "db_size_bytes": "bad",
            "wal_size_bytes": None,
            "journal_mode": "wal",
            "page_count": 123,
            "freelist_count": 1,
            "free_ratio": 0.1,
        },
    )

    bootstrap_db.create_tables_with_runtime_checks(
        app=app,
        db=db,
        log_print=log_print,
        apply_schema_migrations=lambda *_args, **_kwargs: None,
    )

    assert any("SQLite诊断:" in message and "size=0.00MB" in message for message in logs)


def test_clear_startup_cache_cleanup_handles_runtime_error_and_rollback_failure():
    logs, log_print = _collector()
    rollback_called = {"value": 0}

    class _Session:
        def rollback(self):
            rollback_called["value"] += 1
            raise SQLAlchemyError("rollback failed")

    db = SimpleNamespace(session=_Session())
    excel_cache_service = SimpleNamespace(
        cleanup_version_mismatch_cache=lambda: (_ for _ in ()).throw(RuntimeError("cleanup failed"))
    )
    excel_html_cache_service = SimpleNamespace(cleanup_old_version_cache=lambda: 0)

    bootstrap_db.clear_startup_version_mismatch_cache(
        log_print=log_print,
        diff_logic_version="v-test",
        excel_cache_service=excel_cache_service,
        excel_html_cache_service=excel_html_cache_service,
        db=db,
    )

    assert rollback_called["value"] == 1
    assert any("清理版本不匹配缓存失败" in message for message in logs)
