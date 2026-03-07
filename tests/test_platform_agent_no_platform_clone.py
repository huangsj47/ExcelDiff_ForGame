import os
import uuid
from pathlib import Path
from types import SimpleNamespace

from app import app, create_tables, db
from models import AgentTask, BackgroundTask, Project, Repository
import services.commit_diff_logic as commit_diff_logic
import services.vcs_content_service as vcs_content_service
from utils.path_security import build_repository_local_path


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def test_vcs_get_file_content_from_git_skips_clone_in_platform_mode(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "platform")

    called = {"clone": 0}

    def _clone_should_not_run():
        called["clone"] += 1
        return False, "should not call"

    fake_service = SimpleNamespace(
        local_path="C:/__platform_mode_should_not_clone__/missing_repo",
        clone_or_update_repository=_clone_should_not_run,
    )

    monkeypatch.setattr(vcs_content_service, "get_git_service", lambda _repo: fake_service)

    repository = SimpleNamespace(
        id=1,
        url="https://example.com/repo.git",
        root_directory="",
        username="",
        token="",
        name="demo",
    )
    content = vcs_content_service.get_file_content_from_git(repository, "abcdef12", "src/a.py")
    assert content is None
    assert called["clone"] == 0


def test_commit_diff_retry_skips_platform_repo_update_in_agent_mode(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "platform")

    called = {"update": 0}

    class _FakeService:
        def get_file_diff(self, _commit_id, _path):
            return {"type": "code", "hunks": []}

        def clone_or_update_repository(self):
            called["update"] += 1
            return True, "ok"

    commit = SimpleNamespace(commit_id="abcdef123456", path="src/demo.py")
    diff_data, diagnostics = commit_diff_logic._get_git_code_diff_with_retry(_FakeService(), commit, None)

    assert isinstance(diff_data, dict)
    assert called["update"] == 0
    assert any("跳过平台本地仓库更新重试" in str(item) for item in diagnostics)


def test_platform_mode_repository_test_dispatches_agent_task_without_local_clone(monkeypatch):
    admin_token = _uid("admin-token")
    monkeypatch.setenv("DEPLOYMENT_MODE", "platform")
    monkeypatch.setenv("ADMIN_API_TOKEN", admin_token)

    with app.app_context():
        create_tables()
        project = Project(code=_uid("PT"), name="platform-test-project")
        db.session.add(project)
        db.session.flush()
        repository = Repository(
            project_id=project.id,
            name=_uid("repo"),
            type="git",
            url="https://example.com/repo.git",
            branch="main",
            clone_status="pending",
        )
        db.session.add(repository)
        db.session.commit()

        with app.test_client() as client:
            resp = client.post(
                f"/repositories/{repository.id}/test",
                headers={
                    "X-Admin-Token": admin_token,
                    "Accept": "application/json",
                },
            )
            assert resp.status_code == 200, resp.get_data(as_text=True)
            data = resp.get_json() or {}
            assert data.get("success") is True
            assert "Agent" in str(data.get("message") or "")

        db.session.expire_all()
        source_task = (
            BackgroundTask.query.filter_by(
                repository_id=repository.id,
                task_type="auto_sync",
            )
            .order_by(BackgroundTask.id.desc())
            .first()
        )
        assert source_task is not None
        agent_task = AgentTask.query.filter_by(
            source_task_id=source_task.id,
            task_type="auto_sync",
            project_id=project.id,
        ).first()
        assert agent_task is not None

        local_path = build_repository_local_path(project.code, repository.name, repository.id, strict=False)
        assert os.path.exists(local_path) is False


def test_diff_templates_enable_centered_layout():
    root = Path(__file__).resolve().parents[1]
    commit_diff_tpl = (root / "templates" / "commit_diff.html").read_text(encoding="utf-8")
    commit_diff_new_tpl = (root / "templates" / "commit_diff_new.html").read_text(encoding="utf-8")
    theme_css = (root / "static" / "css" / "diff-unified-theme.css").read_text(encoding="utf-8")

    assert "unified-diff-centered" in commit_diff_tpl
    assert "unified-diff-centered" in commit_diff_new_tpl
    assert ".unified-diff-page.unified-diff-centered" in theme_css
