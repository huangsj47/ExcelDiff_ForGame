import uuid

from app import app, create_tables
from models import Project, Repository, db


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def test_merged_view_empty_state_quick_config_link_uses_open_create(monkeypatch):
    admin_token = _uid("admin-token")
    monkeypatch.setenv("ADMIN_API_TOKEN", admin_token)

    with app.app_context():
        create_tables()
        project = Project(code=_uid("P"), name="周版本空配置项目", department="QA")
        db.session.add(project)
        db.session.flush()
        repository = Repository(
            project_id=project.id,
            name="repo_for_weekly_empty_state",
            type="git",
            url="https://example.com/repo.git",
            branch="main",
            resource_type="table",
            clone_status="completed",
        )
        db.session.add(repository)
        db.session.commit()

        with app.test_client() as client:
            merged_resp = client.get(
                f"/projects/{project.id}/merged-view",
                headers={"X-Admin-Token": admin_token},
            )
            assert merged_resp.status_code == 200
            merged_html = merged_resp.get_data(as_text=True)
            assert f"/projects/{project.id}/weekly-version-config?open_create=1" in merged_html

            config_resp = client.get(
                f"/projects/{project.id}/weekly-version-config?open_create=1",
                headers={"X-Admin-Token": admin_token},
            )
            assert config_resp.status_code == 200
            config_html = config_resp.get_data(as_text=True)
            assert "maybeAutoOpenCreateModal" in config_html
            assert "open_create" in config_html
