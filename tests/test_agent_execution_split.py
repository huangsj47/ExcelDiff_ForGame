import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from app import app, create_tables, db
from models import AgentTask, AgentTempCache, BackgroundTask, Project, Repository
from agent.config import load_settings
from agent.handlers import auto_sync as auto_sync_handler
from utils.path_security import build_repository_local_path


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _register_agent(client, shared_secret: str, agent_code: str, project_code: str | None = None) -> str:
    project_codes = [project_code] if project_code else []
    resp = client.post(
        "/api/agents/register",
        json={
            "agent_code": agent_code,
            "agent_name": f"{agent_code}-name",
            "project_codes": project_codes,
            "default_admin_username": "admin",
        },
        headers={"X-Agent-Secret": shared_secret},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json() or {}
    assert data.get("success") is True
    return str(data.get("agent_token"))


def test_agent_settings_include_temp_cache_options(monkeypatch):
    monkeypatch.setenv("PLATFORM_BASE_URL", "http://127.0.0.1:8002")
    monkeypatch.setenv("AGENT_SHARED_SECRET", "s1")
    monkeypatch.setenv("AGENT_NAME", "node-a")
    monkeypatch.setenv("AGENT_HOST", "10.2.3.4")
    monkeypatch.setenv("AGENT_TEMP_CACHE_UPLOAD_ENABLED", "true")
    monkeypatch.setenv("AGENT_TEMP_CACHE_THRESHOLD_BYTES", "2*1024_1024")
    monkeypatch.setenv("AGENT_TEMP_CACHE_EXPIRE_DAYS", "30")

    settings = load_settings()
    assert {"auto_sync", "excel_diff", "weekly_sync", "weekly_excel_cache", "temp_cache_fetch"}.issubset(
        set(settings.local_task_types or [])
    )
    assert settings.temp_cache_upload_enabled is True
    assert settings.temp_cache_threshold_bytes == 2 * 1024 * 1024
    assert settings.temp_cache_expire_days == 30


def test_platform_mode_manual_sync_dispatches_agent_task(monkeypatch):
    admin_token = _uid("admin-token")
    monkeypatch.setenv("DEPLOYMENT_MODE", "platform")
    monkeypatch.setattr("app.DEPLOYMENT_MODE", "platform")
    monkeypatch.setenv("ADMIN_API_TOKEN", admin_token)

    with app.app_context():
        create_tables()
        project = Project(code=_uid("P"), name="dispatch-project")
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
                f"/repositories/{repository.id}/sync",
                headers={"X-Admin-Token": admin_token},
            )
            assert resp.status_code == 202, resp.get_data(as_text=True)
            data = resp.get_json() or {}
            assert data.get("status") == "accepted"
            assert data.get("task_id")

        db.session.expire_all()
        src_task = (
            BackgroundTask.query.filter_by(
                repository_id=repository.id,
                task_type="auto_sync",
                status="pending",
            )
            .order_by(BackgroundTask.id.desc())
            .first()
        )
        assert src_task is not None
        agent_task = AgentTask.query.filter_by(
            source_task_id=src_task.id,
            task_type="auto_sync",
            project_id=project.id,
        ).first()
        assert agent_task is not None
        task_payload = json.loads(agent_task.payload or "{}")
        repository_payload = task_payload.get("repository") or {}
        assert repository_payload.get("project_code") == project.code
        assert repository_payload.get("repository_name") == repository.name


def test_agent_auto_sync_uses_repository_name_style_local_path(monkeypatch, tmp_path):
    captured = {}

    def _fake_sync_repo(local_repo_dir, remote_url, branch):
        captured["local_repo_dir"] = local_repo_dir
        captured["remote_url"] = remote_url
        captured["branch"] = branch

    monkeypatch.setattr(auto_sync_handler, "_sync_repo", _fake_sync_repo)
    monkeypatch.setattr(auto_sync_handler, "_collect_commits", lambda **kwargs: [])

    settings = SimpleNamespace(repos_base_dir=str(tmp_path))
    task = {
        "payload": {
            "repository_id": 1,
            "repository": {
                "repository_id": 1,
                "type": "git",
                "url": "https://example.com/repo.git",
                "branch": "main",
                "project_code": "G120",
                "repository_name": "abc",
            },
        }
    }

    status, summary, error, payload = auto_sync_handler.execute_auto_sync(task, settings)
    expected_local_dir = build_repository_local_path(
        "G120", "abc", 1, base_dir=str(tmp_path), strict=False
    )
    assert status == "completed"
    assert error is None
    assert summary.get("repository_id") == 1
    assert payload.get("repository_id") == 1
    assert captured.get("local_repo_dir") == expected_local_dir


def test_repository_sync_js_treats_accepted_as_dispatched_success():
    template_path = Path(__file__).resolve().parents[1] / "templates" / "repository_config.html"
    content = template_path.read_text(encoding="utf-8")
    assert "data.status === 'accepted'" in content
    assert "!!data.task_id" in content
    assert "showToast('已派发同步'" in content


def test_agent_temp_cache_upsert_and_admin_fetch(monkeypatch):
    shared_secret = _uid("secret")
    admin_token = _uid("admin-token")
    agent_code = _uid("agent")
    monkeypatch.setenv("AGENT_SHARED_SECRET", shared_secret)
    monkeypatch.setenv("ADMIN_API_TOKEN", admin_token)

    with app.app_context():
        create_tables()
        with app.test_client() as client:
            agent_token = _register_agent(client, shared_secret, agent_code)
            payload_json = json.dumps({"hello": "world"}, ensure_ascii=False)
            upsert_resp = client.post(
                "/api/agents/cache/upsert",
                json={
                    "agent_code": agent_code,
                    "agent_token": agent_token,
                    "cache_key": f"cache-{uuid.uuid4().hex[:8]}",
                    "task_type": "excel_diff",
                    "cache_kind": "task_result_payload",
                    "payload_json": payload_json,
                    "payload_size": len(payload_json.encode("utf-8")),
                    "expire_seconds": 3600,
                },
                headers={"X-Agent-Secret": shared_secret},
            )
            assert upsert_resp.status_code == 200, upsert_resp.get_data(as_text=True)
            upsert_data = upsert_resp.get_json() or {}
            assert upsert_data.get("success") is True
            cache_key = upsert_data.get("cache_key")
            assert cache_key

            get_resp = client.get(
                f"/api/agents/cache/{cache_key}",
                headers={"X-Admin-Token": admin_token},
            )
            assert get_resp.status_code == 200, get_resp.get_data(as_text=True)
            get_data = get_resp.get_json() or {}
            assert get_data.get("success") is True
            parsed = json.loads(get_data["payload_json"])
            assert parsed.get("hello") == "world"


def test_agent_temp_cache_expired_entry_returns_404(monkeypatch):
    admin_token = _uid("admin-token")
    monkeypatch.setenv("ADMIN_API_TOKEN", admin_token)

    with app.app_context():
        create_tables()
        project = Project(code=_uid("PC"), name="expired-cache-project")
        db.session.add(project)
        db.session.flush()
        row = AgentTempCache(
            cache_key=f"expired-{uuid.uuid4().hex[:8]}",
            project_id=project.id,
            payload_json=json.dumps({"stale": True}),
            payload_hash="x",
            payload_size=16,
            expire_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        db.session.add(row)
        db.session.commit()
        cache_key = row.cache_key

        with app.test_client() as client:
            resp = client.get(
                f"/api/agents/cache/{cache_key}",
                headers={"X-Admin-Token": admin_token},
            )
            assert resp.status_code == 404


def test_platform_mode_create_git_repository_does_not_start_local_clone_thread(monkeypatch):
    admin_token = _uid("admin-token")
    monkeypatch.setenv("DEPLOYMENT_MODE", "platform")
    monkeypatch.setenv("ADMIN_API_TOKEN", admin_token)

    def _fail_thread(*args, **kwargs):
        raise AssertionError("platform模式不应创建本地克隆线程")

    monkeypatch.setattr("services.repository_creation_handlers.threading.Thread", _fail_thread)

    with app.app_context():
        create_tables()
        project = Project(code=_uid("G"), name="create-git-project")
        db.session.add(project)
        db.session.commit()

        with app.test_client() as client:
            resp = client.post(
                "/repositories/git",
                data={
                    "project_id": str(project.id),
                    "name": _uid("repo"),
                    "category": "config",
                    "url": "https://example.com/git/repo.git",
                    "server_url": "https://example.com",
                    "token": "token-demo",
                    "branch": "main",
                    "resource_type": "code",
                },
                headers={"X-Admin-Token": admin_token},
                follow_redirects=False,
            )
            assert resp.status_code in (302, 303), resp.get_data(as_text=True)

        db.session.expire_all()
        repository = Repository.query.filter_by(project_id=project.id, type="git").order_by(Repository.id.desc()).first()
        assert repository is not None
        local_path = build_repository_local_path(project.code, repository.name, repository.id, strict=False)
        assert os.path.exists(local_path) is False
        task = BackgroundTask.query.filter_by(repository_id=repository.id, task_type="auto_sync").first()
        assert task is not None


def test_platform_mode_create_svn_repository_does_not_start_local_clone_thread(monkeypatch):
    admin_token = _uid("admin-token")
    monkeypatch.setenv("DEPLOYMENT_MODE", "platform")
    monkeypatch.setenv("ADMIN_API_TOKEN", admin_token)

    def _fail_thread(*args, **kwargs):
        raise AssertionError("platform模式不应创建本地SVN克隆线程")

    monkeypatch.setattr("services.repository_creation_handlers.threading.Thread", _fail_thread)

    with app.app_context():
        create_tables()
        project = Project(code=_uid("S"), name="create-svn-project")
        db.session.add(project)
        db.session.commit()

        with app.test_client() as client:
            resp = client.post(
                "/repositories/svn",
                data={
                    "project_id": str(project.id),
                    "name": _uid("repo"),
                    "category": "config",
                    "url": "https://example.com/svn/repo",
                    "root_directory": "/trunk",
                    "username": "u1",
                    "password": "p1",
                    "current_version": "1",
                    "resource_type": "code",
                },
                headers={"X-Admin-Token": admin_token},
                follow_redirects=False,
            )
            assert resp.status_code in (302, 303), resp.get_data(as_text=True)

        db.session.expire_all()
        repository = Repository.query.filter_by(project_id=project.id, type="svn").order_by(Repository.id.desc()).first()
        assert repository is not None
        local_path = build_repository_local_path(project.code, repository.name, repository.id, strict=False)
        assert os.path.exists(local_path) is False
        task = BackgroundTask.query.filter_by(repository_id=repository.id, task_type="auto_sync").first()
        assert task is not None


def test_resolve_agent_temp_cache_prefers_platform_cache(monkeypatch):
    admin_token = _uid("admin-token")
    monkeypatch.setenv("ADMIN_API_TOKEN", admin_token)

    with app.app_context():
        create_tables()
        project = Project(code=_uid("PC"), name="resolve-hit-project")
        db.session.add(project)
        db.session.flush()
        row = AgentTempCache(
            cache_key=f"resolve-{uuid.uuid4().hex[:8]}",
            project_id=project.id,
            payload_json=json.dumps({"value": 1}),
            payload_hash="hash-abc",
            payload_size=16,
            expire_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        )
        db.session.add(row)
        db.session.commit()

        with app.test_client() as client:
            resp = client.get(
                f"/api/agents/cache/{row.cache_key}/resolve?expected_hash=hash-abc",
                headers={"X-Admin-Token": admin_token},
            )
            assert resp.status_code == 200, resp.get_data(as_text=True)
            data = resp.get_json() or {}
            assert data.get("success") is True
            assert data.get("status") == "hit"
            assert data.get("source") == "platform_temp_cache"


def test_resolve_agent_temp_cache_hash_mismatch_dispatches_recompute(monkeypatch):
    admin_token = _uid("admin-token")
    monkeypatch.setenv("ADMIN_API_TOKEN", admin_token)
    monkeypatch.setenv("DEPLOYMENT_MODE", "single")

    with app.app_context():
        create_tables()
        project = Project(code=_uid("PR"), name="resolve-miss-project")
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
        row = AgentTempCache(
            cache_key=f"resolve-{uuid.uuid4().hex[:8]}",
            project_id=project.id,
            repository_id=repository.id,
            payload_json=json.dumps({"value": 2}),
            payload_hash="hash-origin",
            payload_size=16,
            expire_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        )
        db.session.add(row)
        db.session.commit()

        with app.test_client() as client:
            resp = client.get(
                f"/api/agents/cache/{row.cache_key}/resolve?expected_hash=hash-other&try_agent_fetch=0&trigger_recompute=1&repository_id={repository.id}",
                headers={"X-Admin-Token": admin_token},
            )
            assert resp.status_code == 202, resp.get_data(as_text=True)
            data = resp.get_json() or {}
            assert data.get("success") is True
            assert data.get("status") == "pending_recompute"
            assert data.get("task_id")

        db.session.expire_all()
        task = (
            BackgroundTask.query.filter_by(
                repository_id=repository.id,
                task_type="auto_sync",
            )
            .order_by(BackgroundTask.id.desc())
            .first()
        )
        assert task is not None


def test_execute_proxy_api_removed(monkeypatch):
    shared_secret = _uid("secret")
    monkeypatch.setenv("AGENT_SHARED_SECRET", shared_secret)

    with app.app_context():
        create_tables()
        with app.test_client() as client:
            resp = client.post(
                "/api/agents/tasks/123/execute-proxy",
                json={},
                headers={"X-Agent-Secret": shared_secret},
            )
            assert resp.status_code == 404
