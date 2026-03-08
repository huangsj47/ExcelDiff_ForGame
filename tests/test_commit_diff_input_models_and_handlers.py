from types import SimpleNamespace

import app as app_module
from app import app
import services.commit_operation_handlers as handlers
import services.background_task_service as bg_service
from services.commit_diff_input_models import CommitDiffQueryInput, MergeDiffRefreshInput


def _extract_response(result):
    if isinstance(result, tuple):
        response, status_code = result
        return status_code, response.get_json()
    return result.status_code, result.get_json()


def test_commit_diff_query_input_parses_truthy_force_retry():
    with app.test_request_context("/commits/1/diff-data?force_retry=true"):
        query = CommitDiffQueryInput.from_request(app_module.request)
    assert query.force_retry is True

    with app.test_request_context("/commits/1/diff-data?force_retry=0"):
        query = CommitDiffQueryInput.from_request(app_module.request)
    assert query.force_retry is False


def test_merge_diff_refresh_input_validates_payload():
    with app.test_request_context("/commits/merge-diff/refresh", method="POST", json={"commit_ids": ["1", 2, "x"]}):
        payload = MergeDiffRefreshInput.from_request_json(app_module.request)
    assert payload.commit_ids == [1, 2]

    with app.test_request_context("/commits/merge-diff/refresh", method="POST", json=["bad"]):
        try:
            MergeDiffRefreshInput.from_request_json(app_module.request)
            raised = False
        except ValueError:
            raised = True
    assert raised is True


def test_refresh_merge_diff_returns_invalid_request_for_bad_payload(monkeypatch):
    monkeypatch.setattr(handlers, "log_print", lambda *_args, **_kwargs: None)
    with app.test_request_context("/commits/merge-diff/refresh", method="POST", json=["bad"]):
        result = handlers.refresh_merge_diff()
    status_code, payload = _extract_response(result)
    assert status_code == 400
    assert payload["error_type"] == "invalid_request"


def test_get_commit_diff_data_agent_mode_uses_query_input_force_retry(monkeypatch):
    commit = SimpleNamespace(
        id=9,
        path="src/demo.lua",
        repository=SimpleNamespace(project_id=1),
    )

    captured = {}

    monkeypatch.setattr(handlers.db.session, "get", lambda *_args, **_kwargs: commit)
    monkeypatch.setattr(handlers, "_ensure_commit_project_access", lambda _c: (True, ""))
    monkeypatch.setattr(handlers, "is_agent_dispatch_mode", lambda: True)

    def _fake_dispatch(_commit, force_retry=False):
        captured["force_retry"] = force_retry
        return {"status": "pending", "message": "pending", "retry_after_seconds": 60, "task_id": 123}

    monkeypatch.setattr(handlers, "dispatch_or_get_commit_diff", _fake_dispatch)

    with app.test_request_context("/commits/9/diff-data?force_retry=1"):
        result = handlers.get_commit_diff_data(9)

    status_code, payload = _extract_response(result)
    assert status_code == 202
    assert payload["status"] == "pending"
    assert captured["force_retry"] is True


def test_get_commit_diff_data_returns_unexpected_error_for_fallback_exceptions(monkeypatch):
    class _FakeExpr:
        def __eq__(self, _other):
            return self

    fake_commit_model = SimpleNamespace(
        query=SimpleNamespace(
            filter=lambda *_args, **_kwargs: SimpleNamespace(
                order_by=lambda *_args, **_kwargs: SimpleNamespace(all=lambda: [])
            )
        ),
        repository_id=_FakeExpr(),
        path=_FakeExpr(),
    )
    commit = SimpleNamespace(
        id=9,
        path="src/demo.lua",
        repository=SimpleNamespace(id=1, project_id=1),
        repository_id=1,
        commit_id="abc123",
    )

    monkeypatch.setattr(handlers, "Commit", fake_commit_model)
    monkeypatch.setattr(handlers, "log_print", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(handlers.db.session, "get", lambda *_args, **_kwargs: commit)
    monkeypatch.setattr(handlers, "_ensure_commit_project_access", lambda _c: (True, ""))
    monkeypatch.setattr(handlers, "is_agent_dispatch_mode", lambda: False)
    monkeypatch.setattr(handlers.excel_cache_service, "is_excel_file", lambda _path: False)
    monkeypatch.setattr(
        handlers,
        "get_unified_diff_data",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyError("missing_diff_payload")),
    )

    with app.test_request_context("/commits/9/diff-data"):
        result = handlers.get_commit_diff_data(9)

    status_code, payload = _extract_response(result)
    assert status_code == 500
    assert payload["error_type"] == "unexpected_error"


def test_refresh_merge_diff_returns_unexpected_error_for_fallback_exceptions(monkeypatch):
    commit = SimpleNamespace(
        id=1,
        path="src/demo.xlsx",
        repository_id=1,
        commit_id="abc123",
        repository=SimpleNamespace(project_id=1),
    )

    monkeypatch.setattr(handlers, "log_print", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(handlers.db.session, "get", lambda *_args, **_kwargs: commit)
    monkeypatch.setattr(handlers, "_ensure_commit_project_access", lambda _c: (True, ""))
    monkeypatch.setattr(bg_service, "pause_background_tasks", lambda: None)
    monkeypatch.setattr(bg_service, "resume_background_tasks", lambda: None)
    monkeypatch.setattr(
        handlers.excel_cache_service,
        "is_excel_file",
        lambda _path: (_ for _ in ()).throw(KeyError("bad_excel_state")),
    )

    with app.test_request_context("/commits/merge-diff/refresh", method="POST", json={"commit_ids": [1]}):
        result = handlers.refresh_merge_diff()

    status_code, payload = _extract_response(result)
    assert status_code == 500
    assert payload["error_type"] == "unexpected_error"
