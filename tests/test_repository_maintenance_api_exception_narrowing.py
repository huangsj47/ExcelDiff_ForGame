from __future__ import annotations

from types import SimpleNamespace

import services.repository_maintenance_api_service as maintenance_service


def _collect_logs():
    logs = []

    def _log_print(message, *_args, **_kwargs):
        logs.append(str(message))

    return logs, _log_print


def test_repository_maintenance_exception_tuples_are_declared():
    assert hasattr(maintenance_service, "REPOSITORY_MAINTENANCE_CACHE_REBUILD_ERRORS")
    assert hasattr(maintenance_service, "REPOSITORY_MAINTENANCE_CACHE_STATUS_ERRORS")
    assert hasattr(maintenance_service, "REPOSITORY_MAINTENANCE_SYNC_ERRORS")


def test_handle_regenerate_cache_returns_500_on_known_error():
    logs, log_print = _collect_logs()
    repository = SimpleNamespace(id=1, name="repo")
    repository_model = SimpleNamespace(query=SimpleNamespace(get_or_404=lambda _id: repository))
    exploding_cache_service = SimpleNamespace(
        get_recent_excel_commits=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cache read failed")),
        log_cache_operation=lambda *_args, **_kwargs: None,
    )

    result = maintenance_service.handle_regenerate_cache(
        repository_id=1,
        Repository=repository_model,
        DiffCache=SimpleNamespace(query=SimpleNamespace(filter_by=lambda **_k: SimpleNamespace(delete=lambda: None))),
        ExcelHtmlCache=SimpleNamespace(query=SimpleNamespace(filter_by=lambda **_k: SimpleNamespace(delete=lambda: None))),
        db=SimpleNamespace(session=SimpleNamespace(commit=lambda: None)),
        excel_cache_service=exploding_cache_service,
        add_excel_diff_task=lambda *_args, **_kwargs: None,
        jsonify=lambda payload: payload,
        log_print=log_print,
    )

    payload, status = result
    assert status == 500
    assert payload["success"] is False
    assert "重新生成缓存失败" in payload["message"]
    assert any("重新生成缓存失败" in message for message in logs)


def test_handle_get_cache_status_returns_500_on_known_error():
    logs, log_print = _collect_logs()
    repository_model = SimpleNamespace(
        query=SimpleNamespace(get_or_404=lambda _id: (_ for _ in ()).throw(ValueError("not found")))
    )

    result = maintenance_service.handle_get_cache_status(
        repository_id=1,
        Repository=repository_model,
        DiffCache=SimpleNamespace(query=SimpleNamespace(filter_by=lambda **_k: SimpleNamespace(count=lambda: 0))),
        excel_cache_service=SimpleNamespace(get_recent_excel_commits=lambda *_args, **_kwargs: []),
        ensure_repository_access_or_403=lambda *_args, **_kwargs: None,
        jsonify=lambda payload: payload,
        log_print=log_print,
    )

    payload, status = result
    assert status == 500
    assert payload["success"] is False
    assert "获取缓存状态失败" in payload["message"]
    assert any("获取缓存状态失败" in message for message in logs)


def test_handle_sync_repository_returns_500_when_commit_payload_missing_commit_id():
    logs, log_print = _collect_logs()
    recorded_errors = []
    repository = SimpleNamespace(id=1, name="repo", type="git", start_date=None, clone_status="completed")

    class _Session:
        def get(self, _model, _repository_id):
            return repository

        def add(self, _model):
            return None

        def commit(self):
            return None

    class _CommitQuery:
        def filter_by(self, **kwargs):
            if "repository_id" in kwargs and "commit_id" not in kwargs:
                return SimpleNamespace(order_by=lambda *_a, **_k: SimpleNamespace(first=lambda: None))
            return SimpleNamespace(first=lambda: None)

    class _CommitModel:
        commit_time = SimpleNamespace(desc=lambda: None)
        query = _CommitQuery()

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    git_service = SimpleNamespace(
        clone_or_update_repository=lambda: (True, "ok"),
        get_commits_threaded=lambda **_kwargs: [{"path": "a.xlsx"}],
    )

    result = maintenance_service.handle_sync_repository(
        repository_id=1,
        db=SimpleNamespace(session=_Session()),
        Repository=SimpleNamespace(),
        Commit=_CommitModel,
        get_git_service=lambda _repo: git_service,
        get_svn_service=lambda _repo: None,
        dispatch_auto_sync_task_when_agent_mode=lambda _id: (False, None),
        record_repository_sync_error=lambda *_args, **_kwargs: recorded_errors.append("recorded"),
        clear_repository_sync_error=lambda *_args, **_kwargs: None,
        add_excel_diff_task=lambda *_args, **_kwargs: None,
        excel_cache_service=SimpleNamespace(is_excel_file=lambda _path: True),
        jsonify=lambda payload: payload,
        log_print=log_print,
    )

    payload, status = result
    assert status == 500
    assert payload["status"] == "error"
    assert "同步失败" in payload["message"]
    assert recorded_errors == ["recorded"]
    assert any("手动同步失败" in message for message in logs)
