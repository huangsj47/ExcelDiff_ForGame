import uuid

from app import app, create_tables
from models import Project, db


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def test_weekly_list_empty_state_quick_config_link_uses_open_create(monkeypatch):
    admin_token = _uid("admin-token")
    monkeypatch.setenv("ADMIN_API_TOKEN", admin_token)

    with app.app_context():
        create_tables()
        project = Project(code=_uid("P"), name="周版本列表空配置项目", department="QA")
        db.session.add(project)
        db.session.commit()

        with app.test_client() as client:
            resp = client.get(
                f"/projects/{project.id}/weekly-version",
                headers={"X-Admin-Token": admin_token},
            )
            assert resp.status_code == 200
            html = resp.get_data(as_text=True)
            assert f"/projects/{project.id}/weekly-version-config?open_create=1" in html
