import uuid

import app as app_module
from app import app, create_tables, db
from models import Commit, Project, Repository
from sqlalchemy.exc import SQLAlchemyError


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _extract_response(result):
    if isinstance(result, tuple):
        response, status_code = result
        return status_code, response.get_json()
    return result.status_code, result.get_json()


def test_update_commit_status_returns_404_for_missing_commit():
    with app.app_context():
        create_tables()

    with app.test_request_context("/commits/999999/status", method="POST", json={"status": "pending"}):
        result = app_module.update_commit_status(999999)

    status_code, payload = _extract_response(result)
    assert status_code == 404
    assert payload["error_type"] == "commit_not_found"


def test_update_commit_status_rejects_non_object_json():
    with app.test_request_context("/commits/1/status", method="POST", json=["invalid"]):
        result = app_module.update_commit_status(1)

    status_code, payload = _extract_response(result)
    assert status_code == 400
    assert payload["error_type"] == "invalid_request"


def test_update_commit_status_returns_database_error_on_commit_failure(monkeypatch):
    with app.app_context():
        create_tables()
        project = Project(code=_uid("P"), name=_uid("proj"))
        db.session.add(project)
        db.session.flush()
        repository = Repository(
            project_id=project.id,
            name=_uid("repo"),
            type="git",
            url="ssh://git@example.com/group/repo.git",
            branch="master",
        )
        db.session.add(repository)
        db.session.flush()
        commit = Commit(
            repository_id=repository.id,
            commit_id=_uid("commit"),
            path="test/file.txt",
            status="pending",
        )
        db.session.add(commit)
        db.session.commit()
        commit_id = commit.id

    monkeypatch.setattr(app_module, "_ensure_commit_access_or_403", lambda _commit: (repository, project))
    monkeypatch.setattr(app_module.db.session, "commit", lambda: (_ for _ in ()).throw(SQLAlchemyError("commit failed")))

    with app.test_request_context(f"/commits/{commit_id}/status", method="POST", json={"status": "pending"}):
        result = app_module.update_commit_status(commit_id)

    status_code, payload = _extract_response(result)
    assert status_code == 500
    assert payload["error_type"] == "database_error"


def test_batch_update_commits_rejects_non_object_json():
    with app.test_request_context("/commits/batch-update", method="POST", json=["invalid"]):
        result = app_module.batch_update_commits_compat.__wrapped__()

    status_code, payload = _extract_response(result)
    assert status_code == 400
    assert payload["error_type"] == "invalid_request"


def test_batch_update_commits_returns_database_error_on_commit_failure(monkeypatch):
    monkeypatch.setattr(app_module.db.session, "commit", lambda: (_ for _ in ()).throw(SQLAlchemyError("commit failed")))
    monkeypatch.setattr(app_module.db.session, "get", lambda *_args, **_kwargs: None)

    with app.test_request_context(
        "/commits/batch-update",
        method="POST",
        json={"commit_ids": ["1"], "action": "confirm"},
    ):
        result = app_module.batch_update_commits_compat.__wrapped__()

    status_code, payload = _extract_response(result)
    assert status_code == 500
    assert payload["error_type"] == "database_error"
