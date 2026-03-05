import uuid

from app import app, create_tables


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def test_merged_view_missing_project_shows_friendly_page(monkeypatch):
    admin_token = _uid("admin-token")
    monkeypatch.setenv("ADMIN_API_TOKEN", admin_token)

    with app.app_context():
        create_tables()
        with app.test_client() as client:
            resp = client.get(
                "/projects/999999/merged-view",
                headers={"X-Admin-Token": admin_token},
            )
            assert resp.status_code == 404
            body = resp.get_data(as_text=True)
            assert "当前项目或页面不存在" in body
            assert "返回主页" in body

