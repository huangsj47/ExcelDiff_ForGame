import app as app_module
from app import app, create_tables


def test_create_tables_delegates_schema_migrations(monkeypatch):
    called = {"count": 0}

    def _fake_apply_schema_migrations(db_obj, log_func):
        assert db_obj is app_module.db
        assert callable(log_func)
        called["count"] += 1

    monkeypatch.setattr(app_module, "apply_schema_migrations", _fake_apply_schema_migrations)

    with app.app_context():
        create_tables()

    assert called["count"] >= 1
