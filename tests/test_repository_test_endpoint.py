import uuid

from app import app, create_tables, db
from models import Project, Repository
import services.repository_admin_handlers as repository_admin_handlers


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _create_git_repository():
    project = Project(code=_uid("P"), name=_uid("proj"))
    db.session.add(project)
    db.session.flush()
    repository = Repository(
        project_id=project.id,
        name=_uid("repo"),
        type="git",
        url="ssh://git@example.com/group/repo.git",
        server_url="https://example.com",
        branch="master",
        clone_status="pending",
    )
    db.session.add(repository)
    db.session.commit()
    return repository


def test_repository_test_endpoint_ajax_returns_error_when_ssh_check_failed(monkeypatch):
    admin_token = _uid("admin-token")
    monkeypatch.setenv("ADMIN_API_TOKEN", admin_token)

    class _FakeService:
        local_path = "C:/fake/repo"

        @staticmethod
        def test_ssh_connection():
            return False

    original_get_runtime_model = repository_admin_handlers.get_runtime_model

    def _fake_get_runtime_model(name):
        if name == "get_git_service":
            return lambda repository: _FakeService()
        return original_get_runtime_model(name)

    monkeypatch.setattr(repository_admin_handlers, "get_runtime_model", _fake_get_runtime_model)

    with app.app_context():
        create_tables()
        repository = _create_git_repository()

        with app.test_client() as client:
            resp = client.post(
                f"/repositories/{repository.id}/test",
                headers={
                    "X-Admin-Token": admin_token,
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json",
                },
            )
            assert resp.status_code == 400, resp.get_data(as_text=True)
            data = resp.get_json() or {}
            assert data.get("success") is False
            assert data.get("scope") == "platform_local"
            assert "SSH连接测试失败" in str(data.get("message") or "")


def test_repository_test_endpoint_ajax_returns_success_when_clone_ok(monkeypatch):
    admin_token = _uid("admin-token")
    monkeypatch.setenv("ADMIN_API_TOKEN", admin_token)

    class _FakeService:
        local_path = "C:/fake/repo"

        @staticmethod
        def test_ssh_connection():
            return True

        @staticmethod
        def clone_or_update_repository():
            return True, "仓库更新成功"

    original_get_runtime_model = repository_admin_handlers.get_runtime_model

    def _fake_get_runtime_model(name):
        if name == "get_git_service":
            return lambda repository: _FakeService()
        return original_get_runtime_model(name)

    monkeypatch.setattr(repository_admin_handlers, "get_runtime_model", _fake_get_runtime_model)

    with app.app_context():
        create_tables()
        repository = _create_git_repository()

        with app.test_client() as client:
            resp = client.post(
                f"/repositories/{repository.id}/test",
                headers={
                    "X-Admin-Token": admin_token,
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json",
                },
            )
            assert resp.status_code == 200, resp.get_data(as_text=True)
            data = resp.get_json() or {}
            assert data.get("success") is True
            assert data.get("status") == "success"
            assert data.get("scope") == "platform_local"
            assert "仓库连接测试成功" in str(data.get("message") or "")

