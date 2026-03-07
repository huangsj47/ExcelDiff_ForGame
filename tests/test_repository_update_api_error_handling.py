import uuid

import app as app_module
from app import app, create_tables, db
from models import Project, Repository
from sqlalchemy.exc import SQLAlchemyError


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _extract_response(result):
    if isinstance(result, tuple):
        response, status_code = result
        return status_code, response.get_json()
    return result.status_code, result.get_json()


def test_update_repository_and_cache_returns_404_when_repository_missing():
    with app.app_context():
        create_tables()

    with app.test_request_context(
        "/repositories/9999/update_and_cache",
        method="POST",
        json={"action": "pull_and_cache"},
    ):
        result = app_module.update_repository_and_cache.__wrapped__(9999)

    status_code, payload = _extract_response(result)
    assert status_code == 404
    assert payload["error_type"] == "repository_not_found"


def test_reuse_repository_and_update_returns_404_when_repository_missing():
    with app.app_context():
        create_tables()

    with app.test_request_context(
        "/repositories/9999/reuse_and_update",
        method="POST",
        json={"action": "pull_and_cache"},
    ):
        result = app_module.reuse_repository_and_update.__wrapped__(9999)

    status_code, payload = _extract_response(result)
    assert status_code == 404
    assert payload["error_type"] == "repository_not_found"


def test_batch_update_credentials_rejects_non_object_json():
    with app.test_request_context(
        "/repositories/batch-update-credentials",
        method="POST",
        json=["invalid", "payload"],
    ):
        result = app_module.batch_update_credentials.__wrapped__()

    status_code, payload = _extract_response(result)
    assert status_code == 400
    assert payload["error_type"] == "invalid_request"


def test_batch_update_credentials_returns_database_error_on_commit_failure(monkeypatch):
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
        db.session.commit()
        project_id = project.id

    def _raise_commit_error():
        raise SQLAlchemyError("commit failed")

    monkeypatch.setattr(app_module.db.session, "commit", _raise_commit_error)

    with app.test_request_context(
        "/repositories/batch-update-credentials",
        method="POST",
        json={"project_id": project_id, "repo_type": "git", "git_token": "token"},
    ):
        result = app_module.batch_update_credentials.__wrapped__()

    status_code, payload = _extract_response(result)
    assert status_code == 500
    assert payload["error_type"] == "database_error"
