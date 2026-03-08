from types import SimpleNamespace

import services.app_blueprint_bootstrap_service as blueprint_bootstrap
import services.commit_diff_new_page_service as commit_diff_new_page
import services.db_migration_service as db_migration


def test_register_blueprint_with_trace_swallows_known_runtime_errors():
    logs = []

    class _FakeApp:
        def register_blueprint(self, _blueprint):
            raise RuntimeError("blueprint boom")

    blueprint_bootstrap._register_blueprint_with_trace(
        app=_FakeApp(),
        blueprint=object(),
        label="demo_bp",
        log_print=lambda msg, *_args, **_kwargs: logs.append(msg),
    )

    assert any("demo_bp FAILED" in msg for msg in logs)


def test_migrate_table_columns_rolls_back_on_runtime_error(monkeypatch):
    rollback_called = {"value": False}
    logs = []

    class _FakeInspector:
        def get_table_names(self):
            return ["demo_table"]

        def get_columns(self, _table_name):
            return []

    class _FakeSession:
        def execute(self, _sql):
            raise RuntimeError("execute failed")

        def commit(self):
            raise AssertionError("should not commit on failure")

        def rollback(self):
            rollback_called["value"] = True

    fake_db = SimpleNamespace(engine=object(), session=_FakeSession())
    monkeypatch.setattr(db_migration, "inspect", lambda _engine: _FakeInspector())

    db_migration._migrate_table_columns(
        fake_db,
        "demo_table",
        {"new_col": "new_col TEXT"},
        lambda msg, *_args, **_kwargs: logs.append(msg),
    )

    assert rollback_called["value"] is True
    assert any("自动迁移失败" in msg for msg in logs)


def test_commit_diff_new_page_falls_back_when_author_mapping_fails():
    logs = []
    commit = SimpleNamespace(
        id=10,
        path="src/a.lua",
        repository_id=1,
        repository=SimpleNamespace(id=1),
    )

    class _Field:
        def __eq__(self, _other):
            return self

        def desc(self):
            return self

    class _FakeQuery:
        def get_or_404(self, _commit_id):
            return commit

        def filter(self, *_args, **_kwargs):
            return self

        def order_by(self, *_args, **_kwargs):
            return self

        def all(self):
            return [commit]

    class _FakeCommit:
        query = _FakeQuery()
        repository_id = _Field()
        path = _Field()
        commit_time = _Field()

    result = commit_diff_new_page.handle_commit_diff_new_page(
        commit_id=10,
        Commit=_FakeCommit,
        resolve_previous_commit=lambda *_args, **_kwargs: None,
        attach_author_display=lambda *_args, **_kwargs: (_ for _ in ()).throw(LookupError("author map missing")),
        get_unified_diff_data=lambda *_args, **_kwargs: {"type": "text"},
        ensure_commit_access_or_403=lambda _commit: (SimpleNamespace(id=1), SimpleNamespace(id=2)),
        render_template=lambda _tpl, **kwargs: kwargs,
        log_print=lambda msg, *_args, **_kwargs: logs.append(msg),
    )

    assert isinstance(result, dict)
    assert result["diff_data"]["type"] == "text"
    assert any("作者姓名映射失败" in msg for msg in logs)
