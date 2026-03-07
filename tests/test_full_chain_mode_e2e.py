import json
import uuid
from datetime import datetime, timedelta, timezone

import app as app_module
from app import app, create_tables, db
from models import AgentTask, BackgroundTask, Commit, Project, Repository


TEST_REPO_URL = (
    "ssh://git@git-blsm.nie.netease.com:32200/"
    "beiluoshimen/qz_project/qz_chengxu/qz_client_lua.git"
)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _register_agent(client, shared_secret: str, agent_code: str) -> str:
    resp = client.post(
        "/api/agents/register",
        json={
            "agent_code": agent_code,
            "agent_name": f"{agent_code}-name",
            "project_codes": [],
            "default_admin_username": "admin",
        },
        headers={"X-Agent-Secret": shared_secret},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json() or {}
    assert data.get("success") is True
    return str(data.get("agent_token"))


def _create_project_via_api(client, admin_token: str, *, code: str, name: str, agent_code: str | None = None):
    payload = {"code": code, "name": name, "department": "QA"}
    if agent_code:
        payload["agent_code"] = agent_code
    resp = client.post(
        "/projects",
        data=payload,
        headers={"X-Admin-Token": admin_token},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303), resp.get_data(as_text=True)
    project = Project.query.filter_by(code=code).first()
    assert project is not None
    return project


def _create_git_repository_via_api(client, admin_token: str, *, project_id: int, name: str):
    resp = client.post(
        "/repositories/git",
        data={
            "project_id": str(project_id),
            "name": name,
            "category": "config",
            "url": TEST_REPO_URL,
            "server_url": "git-blsm.nie.netease.com",
            "token": "test-token",
            "branch": "main",
            "resource_type": "code",
        },
        headers={"X-Admin-Token": admin_token},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303), resp.get_data(as_text=True)
    repository = (
        Repository.query.filter_by(project_id=project_id, name=name, type="git")
        .order_by(Repository.id.desc())
        .first()
    )
    assert repository is not None
    return repository


def _seed_commits(repository_id: int):
    now = datetime.now(timezone.utc)
    excel_commit = Commit(
        repository_id=repository_id,
        commit_id=_uid("excel"),
        path="config/demo/table_a.xlsx",
        operation="M",
        author="qa-bot",
        commit_time=now - timedelta(minutes=2),
        message="excel update",
    )
    code_commit = Commit(
        repository_id=repository_id,
        commit_id=_uid("code"),
        path="src/demo/script.lua",
        operation="M",
        author="qa-bot",
        commit_time=now - timedelta(minutes=1),
        message="lua update",
    )
    db.session.add(excel_commit)
    db.session.add(code_commit)
    db.session.commit()
    return excel_commit, code_commit


def _regenerate_cache(client, admin_token: str, repository_id: int):
    resp = client.post(
        f"/repositories/{repository_id}/regenerate-cache",
        headers={"X-Admin-Token": admin_token},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json() or {}
    assert data.get("success") is True
    return data


def _confirm_commit(client, admin_token: str, commit_id: int):
    resp = client.post(
        f"/commits/{commit_id}/status",
        json={"action": "confirm"},
        headers={"X-Admin-Token": admin_token, "Accept": "application/json"},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json() or {}
    assert data.get("success") is True


def test_single_mode_full_chain_project_repo_cache_diff_confirm(monkeypatch):
    admin_token = _uid("admin-token")
    monkeypatch.setenv("DEPLOYMENT_MODE", "single")
    monkeypatch.setenv("ADMIN_API_TOKEN", admin_token)

    class _NoStartThread:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def start(self):
            # single 模式测试仅验证业务链路，不执行真实 clone。
            return None

    monkeypatch.setattr("services.repository_creation_handlers.threading.Thread", _NoStartThread)

    with app.app_context():
        create_tables()
        with app.test_client() as client:
            project = _create_project_via_api(
                client,
                admin_token,
                code=_uid("SINGLE"),
                name="single-e2e-project",
            )
            repository = _create_git_repository_via_api(
                client,
                admin_token,
                project_id=project.id,
                name=_uid("repo"),
            )
            excel_commit, code_commit = _seed_commits(repository.id)

            monkeypatch.setattr(
                app_module.excel_cache_service,
                "get_recent_excel_commits",
                lambda _repository, limit=1000: [excel_commit],
            )
            cache_data = _regenerate_cache(client, admin_token, repository.id)
            assert int(cache_data.get("task_count") or 0) == 1

            excel_task = BackgroundTask.query.filter_by(
                repository_id=repository.id,
                task_type="excel_diff",
                commit_id=excel_commit.commit_id,
                file_path=excel_commit.path,
            ).first()
            assert excel_task is not None

            monkeypatch.setattr(
                app_module,
                "get_unified_diff_data",
                lambda commit, previous_commit=None: {
                    "type": "code",
                    "file_path": commit.path,
                    "hunks": [],
                },
            )
            diff_resp = client.get(
                f"/commits/{code_commit.id}/diff-data",
                headers={"X-Admin-Token": admin_token, "Accept": "application/json"},
            )
            assert diff_resp.status_code == 200, diff_resp.get_data(as_text=True)
            diff_payload = diff_resp.get_json() or {}
            assert diff_payload.get("success") is True
            assert diff_payload.get("status") == "ready"
            assert (diff_payload.get("diff_data") or {}).get("type") == "code"

            _confirm_commit(client, admin_token, code_commit.id)
            db.session.expire_all()
            saved_commit = db.session.get(Commit, code_commit.id)
            assert saved_commit is not None
            assert saved_commit.status == "confirmed"


def test_platform_agent_mode_full_chain_project_repo_cache_diff_confirm(monkeypatch):
    admin_token = _uid("admin-token")
    shared_secret = _uid("shared-secret")
    agent_code = _uid("agent")
    monkeypatch.setenv("DEPLOYMENT_MODE", "platform")
    monkeypatch.setenv("ADMIN_API_TOKEN", admin_token)
    monkeypatch.setenv("AGENT_SHARED_SECRET", shared_secret)

    with app.app_context():
        create_tables()
        with app.test_client() as client:
            _register_agent(client, shared_secret, agent_code)
            project = _create_project_via_api(
                client,
                admin_token,
                code=_uid("PLAT"),
                name="platform-agent-e2e-project",
                agent_code=agent_code,
            )
            repository = _create_git_repository_via_api(
                client,
                admin_token,
                project_id=project.id,
                name=_uid("repo"),
            )
            excel_commit, code_commit = _seed_commits(repository.id)

            # 创建仓库后应派发 auto_sync 到 agent。
            created_auto_sync = (
                BackgroundTask.query.filter_by(
                    repository_id=repository.id,
                    task_type="auto_sync",
                )
                .order_by(BackgroundTask.id.desc())
                .first()
            )
            assert created_auto_sync is not None
            linked_auto_sync = AgentTask.query.filter_by(
                source_task_id=created_auto_sync.id,
                task_type="auto_sync",
                project_id=project.id,
            ).first()
            assert linked_auto_sync is not None

            monkeypatch.setattr(
                app_module.excel_cache_service,
                "get_recent_excel_commits",
                lambda _repository, limit=1000: [excel_commit],
            )
            cache_data = _regenerate_cache(client, admin_token, repository.id)
            assert int(cache_data.get("task_count") or 0) == 1

            excel_source_task = (
                BackgroundTask.query.filter_by(
                    repository_id=repository.id,
                    task_type="excel_diff",
                    commit_id=excel_commit.commit_id,
                    file_path=excel_commit.path,
                )
                .order_by(BackgroundTask.id.desc())
                .first()
            )
            assert excel_source_task is not None
            excel_agent_task = AgentTask.query.filter_by(
                source_task_id=excel_source_task.id,
                task_type="excel_diff",
                project_id=project.id,
            ).first()
            assert excel_agent_task is not None

            diff_resp = client.get(
                f"/commits/{code_commit.id}/diff-data",
                headers={"X-Admin-Token": admin_token, "Accept": "application/json"},
            )
            assert diff_resp.status_code == 202, diff_resp.get_data(as_text=True)
            diff_payload = diff_resp.get_json() or {}
            assert diff_payload.get("success") is True
            assert diff_payload.get("pending") is True
            assert diff_payload.get("status") in {"pending", "pending_offline"}

            commit_diff_task = (
                AgentTask.query.filter_by(
                    task_type="commit_diff",
                    project_id=project.id,
                    repository_id=repository.id,
                )
                .order_by(AgentTask.id.desc())
                .first()
            )
            assert commit_diff_task is not None
            payload = json.loads(commit_diff_task.payload or "{}")
            assert int(payload.get("commit_record_id") or 0) == int(code_commit.id)

            _confirm_commit(client, admin_token, code_commit.id)
            db.session.expire_all()
            saved_commit = db.session.get(Commit, code_commit.id)
            assert saved_commit is not None
            assert saved_commit.status == "confirmed"
