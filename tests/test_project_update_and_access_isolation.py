import uuid
from datetime import datetime, timezone

import app as app_module
import services.core_navigation_handlers as core_navigation_handlers
import services.repository_compare_helpers as repository_compare_helpers
import services.weekly_version_logic as weekly_version_logic
from app import app, create_tables, db
from models import Commit, Project, Repository, WeeklyVersionConfig


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _set_logged_in_session(client, *, is_admin=False):
    with client.session_transaction() as sess:
        sess["_csrf_token"] = "test-csrf-token"
        if is_admin:
            sess["is_admin"] = True
            sess["admin_user"] = "admin"
        else:
            sess["auth_user_id"] = 10001
            sess["auth_username"] = "member_user"


def _create_project_repo_commit():
    project = Project(code=_uid("P"), name="old-name", department="old-dept")
    db.session.add(project)
    db.session.flush()

    repository = Repository(
        project_id=project.id,
        name=_uid("repo"),
        type="git",
        url="https://example.com/repo.git",
        branch="main",
        clone_status="completed",
    )
    db.session.add(repository)
    db.session.flush()

    commit = Commit(
        repository_id=repository.id,
        commit_id=_uid("c"),
        path="src/demo.txt",
        operation="M",
        author="alice",
        commit_time=datetime.now(timezone.utc),
        message="msg",
    )
    db.session.add(commit)
    db.session.commit()
    return project, repository, commit


def test_update_project_allowed_for_platform_admin():
    with app.app_context():
        create_tables()
        project = Project(code=_uid("P"), name="origin", department="dept-a")
        db.session.add(project)
        db.session.commit()

        with app.test_client() as client:
            _set_logged_in_session(client, is_admin=True)
            resp = client.post(
                f"/projects/{project.id}/update",
                json={"name": "updated-by-admin", "department": "dept-b"},
                headers={"X-CSRF-Token": "test-csrf-token"},
            )
            assert resp.status_code == 200, resp.get_data(as_text=True)
            body = resp.get_json()
            assert body["success"] is True

        refreshed = db.session.get(Project, project.id)
        assert refreshed.name == "updated-by-admin"
        assert refreshed.department == "dept-b"


def test_update_project_allowed_for_project_admin(monkeypatch):
    with app.app_context():
        create_tables()
        project = Project(code=_uid("P"), name="origin", department="dept-a")
        db.session.add(project)
        db.session.commit()

        monkeypatch.setattr(core_navigation_handlers, "_has_admin_access", lambda: False)
        monkeypatch.setattr(
            core_navigation_handlers,
            "_has_project_admin_access",
            lambda project_id: int(project_id) == int(project.id),
        )
        monkeypatch.setattr(app_module, "_is_logged_in", lambda: True)

        with app.test_client() as client:
            _set_logged_in_session(client, is_admin=False)
            resp = client.post(
                f"/projects/{project.id}/update",
                json={"name": "updated-by-project-admin", "department": "dept-c"},
                headers={"X-CSRF-Token": "test-csrf-token"},
            )
            assert resp.status_code == 200, resp.get_data(as_text=True)
            assert resp.get_json()["success"] is True

        refreshed = db.session.get(Project, project.id)
        assert refreshed.name == "updated-by-project-admin"
        assert refreshed.department == "dept-c"


def test_update_project_denied_for_non_project_admin(monkeypatch):
    with app.app_context():
        create_tables()
        project = Project(code=_uid("P"), name="origin", department="dept-a")
        db.session.add(project)
        db.session.commit()

        monkeypatch.setattr(core_navigation_handlers, "_has_admin_access", lambda: False)
        monkeypatch.setattr(core_navigation_handlers, "_has_project_admin_access", lambda _project_id: False)
        monkeypatch.setattr(app_module, "_is_logged_in", lambda: True)

        with app.test_client() as client:
            _set_logged_in_session(client, is_admin=False)
            resp = client.post(
                f"/projects/{project.id}/update",
                json={"name": "should-not-pass", "department": "dept-x"},
                headers={"X-CSRF-Token": "test-csrf-token"},
            )
            assert resp.status_code == 403

        refreshed = db.session.get(Project, project.id)
        assert refreshed.name == "origin"
        assert refreshed.department == "dept-a"


def test_commit_diff_denies_cross_project_access(monkeypatch):
    with app.app_context():
        create_tables()
        _project, _repository, commit = _create_project_repo_commit()

        monkeypatch.setattr(app_module, "_has_project_access", lambda _project_id: False)
        monkeypatch.setattr(app_module, "_is_logged_in", lambda: True)

        with app.test_client() as client:
            _set_logged_in_session(client, is_admin=False)
            resp = client.get(f"/commits/{commit.id}/diff")
            assert resp.status_code == 403


def test_weekly_files_api_denies_cross_project_access(monkeypatch):
    with app.app_context():
        create_tables()
        project, repository, _commit = _create_project_repo_commit()
        config = WeeklyVersionConfig(
            project_id=project.id,
            repository_id=repository.id,
            name="W1",
            description="",
            branch="main",
            start_time=datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 7, 0, 0, 0, tzinfo=timezone.utc),
            cycle_type="custom",
            is_active=True,
            auto_sync=True,
            status="active",
        )
        db.session.add(config)
        db.session.commit()

        monkeypatch.setattr(weekly_version_logic, "_has_project_access", lambda _project_id: False)
        monkeypatch.setattr(app_module, "_is_logged_in", lambda: True)

        with app.test_client() as client:
            _set_logged_in_session(client, is_admin=False)
            resp = client.get(f"/weekly-version-config/{config.id}/files")
            assert resp.status_code == 403


def test_get_commits_by_file_denies_cross_project_access(monkeypatch):
    with app.app_context():
        create_tables()
        _project, repository, commit = _create_project_repo_commit()
        monkeypatch.setattr(repository_compare_helpers, "_has_project_access", lambda _project_id: False)
        monkeypatch.setattr(app_module, "_is_logged_in", lambda: True)

        with app.test_client() as client:
            _set_logged_in_session(client, is_admin=False)
            resp = client.get(
                f"/repositories/{repository.id}/commits/by-file",
                query_string={"path": commit.path},
            )
            assert resp.status_code == 403
