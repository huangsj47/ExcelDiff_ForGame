from types import SimpleNamespace

import app as app_module
from app import app
import services.commit_operation_handlers as handlers
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
