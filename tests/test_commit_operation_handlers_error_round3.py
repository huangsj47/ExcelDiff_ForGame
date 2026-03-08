from types import SimpleNamespace

from sqlalchemy.exc import SQLAlchemyError

import services.commit_operation_handlers as handlers
from app import app


def _extract_response(result):
    if isinstance(result, tuple):
        response, status_code = result
        return status_code, response.get_json()
    return result.status_code, result.get_json()


def test_batch_approve_commits_rejects_non_object_json(monkeypatch):
    monkeypatch.setattr(handlers, "log_print", lambda *_args, **_kwargs: None)
    with app.test_request_context("/commits/batch-approve", method="POST", json=["bad"]):
        result = handlers.batch_approve_commits()
    status_code, payload = _extract_response(result)
    assert status_code == 400
    assert payload["message"] == "请求体必须为JSON对象"


def test_batch_reject_commits_rejects_non_object_json(monkeypatch):
    monkeypatch.setattr(handlers, "log_print", lambda *_args, **_kwargs: None)
    with app.test_request_context("/commits/batch-reject", method="POST", json=["bad"]):
        result = handlers.batch_reject_commits()
    status_code, payload = _extract_response(result)
    assert status_code == 400
    assert payload["message"] == "请求体必须为JSON对象"


def test_reject_commit_rejects_non_object_json(monkeypatch):
    monkeypatch.setattr(handlers, "log_print", lambda *_args, **_kwargs: None)
    with app.test_request_context("/commits/reject", method="POST", json=["bad"]):
        result = handlers.reject_commit()
    status_code, payload = _extract_response(result)
    assert status_code == 400
    assert payload["message"] == "请求体必须为JSON对象"


def test_request_priority_diff_returns_database_error(monkeypatch):
    monkeypatch.setattr(handlers, "log_print", lambda *_args, **_kwargs: None)
    fake_commit_model = SimpleNamespace(
        query=SimpleNamespace(get_or_404=lambda _cid: (_ for _ in ()).throw(SQLAlchemyError("boom")))
    )
    monkeypatch.setattr(handlers, "Commit", fake_commit_model)

    with app.test_request_context("/commits/9/priority-diff", method="POST"):
        result = handlers.request_priority_diff(9)
    status_code, payload = _extract_response(result)
    assert status_code == 500
    assert "数据库操作异常" in payload["message"]


def test_update_commit_fields_route_returns_database_error(monkeypatch):
    class _FakeExpr:
        def is_(self, *_args, **_kwargs):
            return self

        def __or__(self, _other):
            return self

    monkeypatch.setattr(
        handlers,
        "Commit",
        SimpleNamespace(
            query=SimpleNamespace(
                filter=lambda *_args, **_kwargs: SimpleNamespace(all=lambda: [])
            ),
            version=_FakeExpr(),
            operation=_FakeExpr(),
        ),
    )
    monkeypatch.setattr(handlers.db.session, "commit", lambda: (_ for _ in ()).throw(SQLAlchemyError("fail")))

    with app.test_request_context("/update_commit_fields"):
        result = handlers.update_commit_fields_route.__wrapped__()
    status_code, payload = _extract_response(result)
    assert status_code == 500
    assert "数据库操作异常" in payload["message"]
