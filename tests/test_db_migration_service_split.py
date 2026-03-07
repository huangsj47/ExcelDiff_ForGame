import app as app_module
from app import clear_version_mismatch_cache, create_tables


def test_create_tables_delegates_schema_migrations(monkeypatch):
    called = {"count": 0}

    def _fake_create_tables_with_runtime_checks(**kwargs):
        assert kwargs["app"] is app_module.app
        assert kwargs["db"] is app_module.db
        assert kwargs["log_print"] is app_module.log_print
        assert kwargs["apply_schema_migrations"] is app_module.apply_schema_migrations
        called["count"] += 1

    monkeypatch.setattr(app_module, "create_tables_with_runtime_checks", _fake_create_tables_with_runtime_checks)
    create_tables()

    assert called["count"] == 1


def test_clear_version_mismatch_cache_delegates_service(monkeypatch):
    called = {"count": 0}

    def _fake_clear_startup_version_mismatch_cache(**kwargs):
        assert kwargs["log_print"] is app_module.log_print
        assert kwargs["diff_logic_version"] == app_module.DIFF_LOGIC_VERSION
        assert kwargs["excel_cache_service"] is app_module.excel_cache_service
        assert kwargs["excel_html_cache_service"] is app_module.excel_html_cache_service
        assert kwargs["db"] is app_module.db
        called["count"] += 1

    monkeypatch.setattr(
        app_module,
        "clear_startup_version_mismatch_cache",
        _fake_clear_startup_version_mismatch_cache,
    )
    clear_version_mismatch_cache()

    assert called["count"] == 1
