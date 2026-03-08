from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import services.status_sync_handlers as handlers


class _Args:
    def __init__(self, mapping):
        self._mapping = mapping

    def get(self, key, default=None, type=None):  # noqa: A002
        value = self._mapping.get(key, default)
        if type is None:
            return value
        try:
            return type(value)
        except (TypeError, ValueError):
            return default


def test_status_sync_handler_exception_tuples_are_declared():
    assert hasattr(handlers, "STATUS_SYNC_CLEAR_ERRORS")
    assert hasattr(handlers, "STATUS_SYNC_MAPPING_ERRORS")
    assert hasattr(handlers, "STATUS_SYNC_CONFIG_LIST_ERRORS")
    assert hasattr(handlers, "STATUS_SYNC_WEEKLY_BATCH_CONFIRM_ERRORS")


def test_clear_all_confirmation_status_returns_500_on_known_error(monkeypatch):
    logs = []
    fake_db = SimpleNamespace()

    def _fake_get_runtime_models(*names):
        assert names == ("db", "log_print")
        return fake_db, (lambda message, *_args, **_kwargs: logs.append(str(message)))

    fake_status_sync_module = ModuleType("services.status_sync_service")

    class _StatusSyncService:
        def __init__(self, _db):
            pass

        def clear_all_confirmation_status(self):
            raise RuntimeError("clear failed")

    fake_status_sync_module.StatusSyncService = _StatusSyncService
    monkeypatch.setitem(sys.modules, "services.status_sync_service", fake_status_sync_module)
    monkeypatch.setattr(handlers, "get_runtime_models", _fake_get_runtime_models)
    monkeypatch.setattr(handlers, "jsonify", lambda payload: payload)

    payload, status = handlers.clear_all_confirmation_status.__wrapped__()

    assert status == 500
    assert payload["success"] is False
    assert any("清空确认状态失败" in item for item in logs)


def test_get_sync_mapping_info_returns_500_on_known_error(monkeypatch):
    logs = []
    fake_db = SimpleNamespace(session=SimpleNamespace(get=lambda *_args, **_kwargs: None))

    def _fake_get_runtime_models(*names):
        if names == ("db", "log_print"):
            return fake_db, (lambda message, *_args, **_kwargs: logs.append(str(message)))
        raise AssertionError(f"unexpected runtime models: {names}")

    fake_status_sync_module = ModuleType("services.status_sync_service")

    class _StatusSyncService:
        def __init__(self, _db):
            pass

        def get_sync_mapping_info(self, *_args, **_kwargs):
            raise RuntimeError("mapping failed")

    fake_status_sync_module.StatusSyncService = _StatusSyncService
    monkeypatch.setitem(sys.modules, "services.status_sync_service", fake_status_sync_module)
    monkeypatch.setattr(handlers, "get_runtime_models", _fake_get_runtime_models)
    monkeypatch.setattr(handlers, "request", SimpleNamespace(args=_Args({"config_id": None, "repository_id": None, "project_id": None})))
    monkeypatch.setattr(handlers, "_has_project_access", lambda _project_id: True)
    monkeypatch.setattr(handlers, "jsonify", lambda payload: payload)

    payload, status = handlers.get_sync_mapping_info()

    assert status == 500
    assert payload["success"] is False
    assert any("获取同步映射信息失败" in item for item in logs)


def test_get_sync_configs_returns_500_on_known_error(monkeypatch):
    logs = []

    class _ExplodingQuery:
        def filter(self, *_args, **_kwargs):
            return self

        def filter_by(self, *_args, **_kwargs):
            return self

        def order_by(self, *_args, **_kwargs):
            raise RuntimeError("order failed")

    weekly_model = SimpleNamespace(
        project_id=SimpleNamespace(in_=lambda _ids: ("in", _ids)),
        created_at=SimpleNamespace(desc=lambda: ("desc", "created_at")),
        query=_ExplodingQuery(),
    )

    def _fake_get_runtime_models(*names):
        if names == ("WeeklyVersionConfig", "log_print"):
            return weekly_model, (lambda message, *_args, **_kwargs: logs.append(str(message)))
        raise AssertionError(f"unexpected runtime models: {names}")

    monkeypatch.setattr(handlers, "get_runtime_models", _fake_get_runtime_models)
    monkeypatch.setattr(handlers, "request", SimpleNamespace(args=_Args({"project_id": None})))
    monkeypatch.setattr(handlers, "_get_accessible_project_ids", lambda: [1, 2])
    monkeypatch.setattr(handlers, "_has_project_access", lambda _project_id: True)
    monkeypatch.setattr(handlers, "jsonify", lambda payload: payload)

    payload, status = handlers.get_sync_configs()

    assert status == 500
    assert payload["success"] is False
    assert any("获取同步配置失败" in item for item in logs)


def test_weekly_version_batch_confirm_api_rolls_back_on_known_error(monkeypatch):
    logs = []
    rollback_calls = {"count": 0}

    class _Session:
        def commit(self):
            return None

        def rollback(self):
            rollback_calls["count"] += 1

    fake_db = SimpleNamespace(session=_Session())
    fake_config = SimpleNamespace(project_id=1)

    class _WeeklyVersionConfig:
        query = SimpleNamespace(get_or_404=lambda _config_id: fake_config)

    class _Field:
        def __eq__(self, other):
            return ("eq", other)

        def in_(self, values):
            return ("in", tuple(values))

    class _WeeklyVersionDiffCache:
        config_id = _Field()
        overall_status = _Field()
        file_path = _Field()
        query = SimpleNamespace(
            filter=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("query failed"))
        )

    def _fake_get_runtime_models(*names):
        if names == ("db", "WeeklyVersionConfig", "WeeklyVersionDiffCache", "log_print"):
            return fake_db, _WeeklyVersionConfig, _WeeklyVersionDiffCache, (
                lambda message, *_args, **_kwargs: logs.append(str(message))
            )
        raise AssertionError(f"unexpected runtime models: {names}")

    fake_security_module = ModuleType("utils.request_security")
    fake_security_module.can_current_user_operate_project_confirmation = lambda *_args, **_kwargs: (True, "")
    fake_security_module._get_current_user = lambda: None

    monkeypatch.setitem(sys.modules, "utils.request_security", fake_security_module)
    monkeypatch.setattr(handlers, "get_runtime_models", _fake_get_runtime_models)
    monkeypatch.setattr(handlers, "request", SimpleNamespace(get_json=lambda: {"file_paths": []}))
    monkeypatch.setattr(handlers, "jsonify", lambda payload: payload)

    payload, status = handlers.weekly_version_batch_confirm_api(10)

    assert status == 500
    assert payload["success"] is False
    assert rollback_calls["count"] == 1
    assert any("批量确认失败" in item for item in logs)
