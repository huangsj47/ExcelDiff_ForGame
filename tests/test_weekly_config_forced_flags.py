import uuid

from app import app, create_tables, db
from models import Project, Repository, WeeklyVersionConfig


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def test_weekly_config_api_forces_auto_sync_and_is_active(monkeypatch):
    admin_token = _uid("admin-token")
    monkeypatch.setenv("ADMIN_API_TOKEN", admin_token)

    with app.app_context():
        create_tables()
        project = Project(code=_uid("P"), name="weekly-force-flags")
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
        db.session.commit()

        with app.test_client() as client:
            resp = client.post(
                f"/projects/{project.id}/weekly-version-config/api",
                json={
                    "name": "W1",
                    "branch": "main",
                    "repository_id": repository.id,
                    "start_time": "2026-03-01T10:00",
                    "end_time": "2026-03-01T12:00",
                    "is_active": False,
                    "auto_sync": False,
                },
                headers={"X-Admin-Token": admin_token},
            )
            assert resp.status_code == 200, resp.get_data(as_text=True)
            payload = resp.get_json() or {}
            assert payload.get("success") is True

        created = WeeklyVersionConfig.query.filter_by(project_id=project.id).order_by(WeeklyVersionConfig.id.desc()).first()
        assert created is not None
        assert created.is_active is True
        assert created.auto_sync is True

        with app.test_client() as client:
            update_resp = client.put(
                f"/projects/{project.id}/weekly-version-config/api/{created.id}",
                json={
                    "is_active": False,
                    "auto_sync": False,
                    "name": "W1-updated",
                },
                headers={"X-Admin-Token": admin_token},
            )
            assert update_resp.status_code == 200, update_resp.get_data(as_text=True)
            update_payload = update_resp.get_json() or {}
            assert update_payload.get("success") is True

        db.session.refresh(created)
        assert created.is_active is True
        assert created.auto_sync is True
