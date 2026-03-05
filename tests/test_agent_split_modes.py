import json
import uuid
from types import SimpleNamespace

from auth.models import AuthProjectPreAssignment, AuthUser, AuthUserProject
from auth.services import register_user
import services.task_worker_service as task_worker_service
from agent.config import load_settings
from agent import executor as agent_executor
from agent.system_metrics import collect_agent_metrics
from app import app, create_tables, db
from models import AgentDefaultAdmin, AgentNode, AgentProjectBinding, AgentTask, BackgroundTask, Commit, Project, Repository


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _register_agent(
    client,
    shared_secret: str,
    agent_code: str,
    project_code: str | None,
    default_admin_username: str = "admin",
) -> str:
    project_codes = [project_code] if project_code else []
    response = client.post(
        "/api/agents/register",
        json={
            "agent_code": agent_code,
            "agent_name": f"{agent_code}-name",
            "project_codes": project_codes,
            "default_admin_username": default_admin_username,
        },
        headers={"X-Agent-Secret": shared_secret},
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    data = response.get_json() or {}
    assert data.get("success") is True
    return str(data.get("agent_token"))


def test_task_worker_dispatch_mode_switch(monkeypatch):
    monkeypatch.delenv("DEPLOYMENT_MODE", raising=False)
    assert task_worker_service._deployment_mode() == "single"
    assert task_worker_service._use_agent_dispatch() is False

    monkeypatch.setenv("DEPLOYMENT_MODE", "platform")
    assert task_worker_service._deployment_mode() == "platform"
    assert task_worker_service._use_agent_dispatch() is True

    monkeypatch.setenv("DEPLOYMENT_MODE", "agent")
    assert task_worker_service._deployment_mode() == "agent"
    assert task_worker_service._use_agent_dispatch() is True

    monkeypatch.setenv("DEPLOYMENT_MODE", "single")
    assert task_worker_service._deployment_mode() == "single"
    assert task_worker_service._use_agent_dispatch() is False


def test_agent_executor_local_auto_sync_and_proxy_fallback(monkeypatch):
    called = {"count": 0}

    def _fake_auto_sync(task, settings):
        called["count"] += 1
        return "completed", {"message": "ok"}, None, {"commits": []}

    monkeypatch.setattr(agent_executor, "execute_auto_sync", _fake_auto_sync)

    local_settings = SimpleNamespace(local_task_types=["auto_sync"])
    status, summary, error, payload = agent_executor.execute_task(
        {"task_type": "auto_sync", "payload": {"repository_id": 1}},
        local_settings,
    )
    assert status == "completed"
    assert summary == {"message": "ok"}
    assert error is None
    assert payload == {"commits": []}
    assert called["count"] == 1

    proxy_settings = SimpleNamespace(local_task_types=[])
    status, summary, error, payload = agent_executor.execute_task(
        {"task_type": "auto_sync", "payload": {"repository_id": 1}},
        proxy_settings,
    )
    assert status == "failed"
    assert summary is None
    assert payload is None
    assert "unsupported task_type=auto_sync" in str(error)


def test_agent_settings_support_name_only_without_agent_code(monkeypatch):
    monkeypatch.setenv("PLATFORM_BASE_URL", "http://127.0.0.1:8002")
    monkeypatch.setenv("AGENT_SHARED_SECRET", "s1")
    monkeypatch.delenv("AGENT_CODE", raising=False)
    monkeypatch.setenv("AGENT_NAME", "OnlyNameNode")
    monkeypatch.setenv("AGENT_HOST", "10.20.30.40")
    monkeypatch.setenv("AGENT_PROJECT_CODES", "")

    settings = load_settings()
    assert settings.agent_name == "OnlyNameNode"
    assert settings.agent_host == "10.20.30.40"
    assert settings.agent_code.startswith("onlynamenode-10-20-30-40")
    assert settings.project_codes == []


def test_agent_register_allows_empty_project_codes(monkeypatch):
    shared_secret = _uid("secret")
    agent_code = _uid("agent")
    monkeypatch.setenv("AGENT_SHARED_SECRET", shared_secret)

    with app.app_context():
        create_tables()
        with app.test_client() as client:
            response = client.post(
                "/api/agents/register",
                json={
                    "agent_code": agent_code,
                    "agent_name": "empty-project-agent",
                    "project_codes": [],
                },
                headers={"X-Agent-Secret": shared_secret},
            )
            assert response.status_code == 200, response.get_data(as_text=True)
            data = response.get_json() or {}
            assert data.get("success") is True
            assert data.get("created_project_codes") == []

            saved_agent = AgentNode.query.filter_by(agent_code=agent_code).first()
            assert saved_agent is not None
            bindings = AgentProjectBinding.query.filter_by(agent_id=saved_agent.id).all()
            assert bindings == []


def test_agent_api_register_claim_report_roundtrip(monkeypatch):
    shared_secret = _uid("secret")
    agent_code = _uid("agent")
    project_code = _uid("P")
    monkeypatch.setenv("AGENT_SHARED_SECRET", shared_secret)

    with app.app_context():
        create_tables()
        with app.test_client() as client:
            agent_token = _register_agent(client, shared_secret, agent_code, project_code)

            project = Project.query.filter_by(code=project_code).first()
            assert project is not None

            task = AgentTask(
                task_type="weekly_sync",
                project_id=project.id,
                repository_id=None,
                source_task_id=None,
                priority=3,
                payload=json.dumps({"config_id": 12345}),
                status="pending",
            )
            db.session.add(task)
            db.session.commit()

            claim_response = client.post(
                "/api/agents/tasks/claim",
                json={"agent_code": agent_code, "agent_token": agent_token, "lease_seconds": 120},
                headers={"X-Agent-Secret": shared_secret},
            )
            assert claim_response.status_code == 200, claim_response.get_data(as_text=True)
            claim_data = claim_response.get_json() or {}
            assert claim_data.get("success") is True
            assert claim_data.get("task", {}).get("id") == task.id
            assert claim_data.get("task", {}).get("task_type") == "weekly_sync"

            report_response = client.post(
                f"/api/agents/tasks/{task.id}/result",
                json={
                    "agent_code": agent_code,
                    "agent_token": agent_token,
                    "status": "completed",
                    "result_summary": {"message": "weekly_sync ok"},
                },
                headers={"X-Agent-Secret": shared_secret},
            )
            assert report_response.status_code == 200, report_response.get_data(as_text=True)
            assert (report_response.get_json() or {}).get("success") is True

            db.session.expire_all()
            saved_task = AgentTask.query.get(task.id)
            assert saved_task is not None
            assert saved_task.status == "completed"
            summary = json.loads(saved_task.result_summary or "{}")
            assert summary.get("message") == "weekly_sync ok"
            assert saved_task.completed_at is not None


def test_auto_sync_result_payload_creates_commits_and_excel_task(monkeypatch):
    shared_secret = _uid("secret")
    agent_code = _uid("agent")
    project_code = _uid("PX")
    monkeypatch.setenv("AGENT_SHARED_SECRET", shared_secret)

    with app.app_context():
        create_tables()
        with app.test_client() as client:
            agent_token = _register_agent(client, shared_secret, agent_code, project_code)

            project = Project.query.filter_by(code=project_code).first()
            agent = AgentNode.query.filter_by(agent_code=agent_code).first()
            assert project is not None
            assert agent is not None

            repository = Repository(
                project_id=project.id,
                name=_uid("repo"),
                type="git",
                url="https://example.com/demo/repo.git",
                branch="main",
                clone_status="completed",
            )
            db.session.add(repository)
            db.session.flush()

            src_task = BackgroundTask(
                task_type="auto_sync",
                repository_id=repository.id,
                status="processing",
                priority=5,
            )
            db.session.add(src_task)
            db.session.flush()

            auto_task = AgentTask(
                task_type="auto_sync",
                project_id=project.id,
                repository_id=repository.id,
                source_task_id=src_task.id,
                priority=5,
                payload=json.dumps({"repository_id": repository.id}),
                status="processing",
                assigned_agent_id=agent.id,
            )
            db.session.add(auto_task)
            db.session.commit()

            commit_excel = f"{uuid.uuid4().hex}a"
            commit_text = f"{uuid.uuid4().hex}b"
            result_payload = {
                "repository_id": repository.id,
                "commits": [
                    {
                        "commit_id": commit_excel,
                        "version": commit_excel[:8],
                        "path": "config/demo/table_a.xlsx",
                        "operation": "M",
                        "author": "alice",
                        "commit_time": "2026-03-01T10:00:00+00:00",
                        "message": "modify excel",
                    },
                    {
                        "commit_id": commit_text,
                        "version": commit_text[:8],
                        "path": "config/demo/readme.txt",
                        "operation": "A",
                        "author": "bob",
                        "commit_time": "2026-03-01T11:00:00+00:00",
                        "message": "add txt",
                    },
                ],
            }

            response = client.post(
                f"/api/agents/tasks/{auto_task.id}/result",
                json={
                    "agent_code": agent_code,
                    "agent_token": agent_token,
                    "status": "completed",
                    "result_payload": result_payload,
                },
                headers={"X-Agent-Secret": shared_secret},
            )
            assert response.status_code == 200, response.get_data(as_text=True)
            assert (response.get_json() or {}).get("success") is True

            db.session.expire_all()
            saved_auto_task = AgentTask.query.get(auto_task.id)
            assert saved_auto_task is not None
            assert saved_auto_task.status == "completed"
            summary = json.loads(saved_auto_task.result_summary or "{}")
            assert summary.get("commits_added") == 2
            assert summary.get("excel_tasks_added") == 1
            assert summary.get("latest_commit_id") == commit_text

            inserted_commits = Commit.query.filter_by(repository_id=repository.id).all()
            assert len(inserted_commits) == 2

            excel_followup = BackgroundTask.query.filter_by(
                task_type="excel_diff",
                repository_id=repository.id,
                commit_id=commit_excel,
                file_path="config/demo/table_a.xlsx",
            ).first()
            assert excel_followup is not None

            synced_source_task = BackgroundTask.query.get(src_task.id)
            assert synced_source_task is not None
            assert synced_source_task.status == "completed"
            assert synced_source_task.error_message is None

            refreshed_repo = Repository.query.get(repository.id)
            assert refreshed_repo is not None
            assert refreshed_repo.last_sync_commit_id == commit_text
            assert refreshed_repo.last_sync_time is not None


def test_platform_mode_project_create_can_bind_selected_agent(monkeypatch):
    admin_token = _uid("admin-token")
    shared_secret = _uid("secret")
    agent_code = _uid("agent")
    monkeypatch.setenv("DEPLOYMENT_MODE", "platform")
    monkeypatch.setenv("ADMIN_API_TOKEN", admin_token)
    monkeypatch.setenv("AGENT_SHARED_SECRET", shared_secret)

    with app.app_context():
        create_tables()
        with app.test_client() as client:
            _register_agent(client, shared_secret, agent_code, None)

            response = client.post(
                "/projects",
                data={
                    "code": _uid("P"),
                    "name": "平台绑定项目",
                    "department": "QA",
                    "agent_code": agent_code,
                },
                headers={"X-Admin-Token": admin_token},
                follow_redirects=False,
            )
            assert response.status_code in (302, 303)

            project = Project.query.filter_by(name="平台绑定项目").first()
            assert project is not None
            binding = AgentProjectBinding.query.filter_by(project_id=project.id).first()
            assert binding is not None
            agent = AgentNode.query.get(binding.agent_id)
            assert agent is not None
            assert agent.agent_code == agent_code


def test_platform_mode_project_create_without_agent_nodes_is_rejected(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "platform")

    with app.app_context():
        create_tables()
        AgentTask.query.delete()
        AgentProjectBinding.query.delete()
        AgentDefaultAdmin.query.delete()
        AgentNode.query.delete()
        db.session.commit()
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["is_admin"] = True
                sess["admin_user"] = "admin"
                sess["_csrf_token"] = "csrf-no-agent"

            response = client.post(
                "/projects",
                data={
                    "_csrf_token": "csrf-no-agent",
                    "code": _uid("NOAGENT"),
                    "name": "无节点项目",
                },
                follow_redirects=False,
            )
            assert response.status_code in (302, 303)
            assert Project.query.filter_by(name="无节点项目").first() is None


def test_platform_mode_index_shows_no_agent_nodes_hint(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "platform")

    with app.app_context():
        create_tables()
        AgentTask.query.delete()
        AgentProjectBinding.query.delete()
        AgentDefaultAdmin.query.delete()
        AgentNode.query.delete()
        db.session.commit()
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["is_admin"] = True
                sess["admin_user"] = "admin"

            response = client.get("/", follow_redirects=True)
            assert response.status_code == 200
            html = response.get_data(as_text=True)
            assert "暂未启动任何节点" in html


def test_list_agents_uses_name_plus_ip_for_duplicate_names(monkeypatch):
    admin_token = _uid("admin-token")
    shared_secret = _uid("secret")
    monkeypatch.setenv("ADMIN_API_TOKEN", admin_token)
    monkeypatch.setenv("AGENT_SHARED_SECRET", shared_secret)

    with app.app_context():
        create_tables()
        with app.test_client() as client:
            client.post(
                "/api/agents/register",
                json={
                    "agent_name": "same-name",
                    "agent_code": _uid("agent-a"),
                    "host": "10.0.0.11",
                    "project_codes": [],
                },
                headers={"X-Agent-Secret": shared_secret},
            )
            client.post(
                "/api/agents/register",
                json={
                    "agent_name": "same-name",
                    "agent_code": _uid("agent-b"),
                    "host": "10.0.0.12",
                    "project_codes": [],
                },
                headers={"X-Agent-Secret": shared_secret},
            )

            list_resp = client.get("/api/agents", headers={"X-Admin-Token": admin_token})
            assert list_resp.status_code == 200
            payload = list_resp.get_json() or {}
            items = payload.get("items") or []
            display_names = {item.get("display_name") for item in items}
            assert "same-name_10.0.0.11" in display_names
            assert "same-name_10.0.0.12" in display_names


def test_agent_heartbeat_updates_runtime_metrics(monkeypatch):
    shared_secret = _uid("secret")
    agent_code = _uid("agent")
    monkeypatch.setenv("AGENT_SHARED_SECRET", shared_secret)

    with app.app_context():
        create_tables()
        with app.test_client() as client:
            agent_token = _register_agent(client, shared_secret, agent_code, None)

            hb_resp = client.post(
                "/api/agents/heartbeat",
                json={
                    "agent_code": agent_code,
                    "agent_token": agent_token,
                    "status": "online",
                    "ip": "10.66.1.20",
                    "cpu_cores": 16,
                    "cpu_usage_percent": 22.7,
                    "memory_total_bytes": 64 * 1024 * 1024 * 1024,
                    "memory_available_bytes": 31 * 1024 * 1024 * 1024,
                    "disk_free_bytes": 500 * 1024 * 1024 * 1024,
                    "os_name": "Linux",
                    "os_version": "6.8",
                    "os_platform": "Linux-6.8-x86_64",
                },
                headers={"X-Agent-Secret": shared_secret},
            )
            assert hb_resp.status_code == 200, hb_resp.get_data(as_text=True)
            assert (hb_resp.get_json() or {}).get("success") is True

            saved_agent = AgentNode.query.filter_by(agent_code=agent_code).first()
            assert saved_agent is not None
            assert saved_agent.host == "10.66.1.20"
            assert saved_agent.cpu_cores == 16
            assert float(saved_agent.cpu_usage_percent) == 22.7
            assert int(saved_agent.memory_total_bytes) == 64 * 1024 * 1024 * 1024
            assert int(saved_agent.memory_available_bytes) == 31 * 1024 * 1024 * 1024
            assert int(saved_agent.disk_free_bytes) == 500 * 1024 * 1024 * 1024
            assert saved_agent.os_name == "Linux"
            assert saved_agent.metrics_updated_at is not None


def test_admin_agents_page_accessible_with_admin_token(monkeypatch):
    admin_token = _uid("admin-token")
    monkeypatch.setenv("ADMIN_API_TOKEN", admin_token)

    with app.app_context():
        create_tables()
        with app.test_client() as client:
            resp = client.get("/admin/agents", headers={"X-Admin-Token": admin_token})
            assert resp.status_code == 200
            assert "Agent 节点监控" in resp.get_data(as_text=True)


def test_collect_agent_metrics_has_expected_fields():
    metrics = collect_agent_metrics(".")
    required_keys = {
        "cpu_cores",
        "cpu_usage_percent",
        "memory_total_bytes",
        "memory_available_bytes",
        "disk_free_bytes",
        "os_name",
        "os_version",
        "os_platform",
    }
    assert required_keys.issubset(metrics.keys())
    assert int(metrics["cpu_cores"]) >= 1


def test_default_admin_username_existing_user_becomes_project_admin(monkeypatch):
    shared_secret = _uid("secret")
    agent_code = _uid("agent")
    project_code = _uid("PADMIN")
    default_admin_username = _uid("owner")
    monkeypatch.setenv("AGENT_SHARED_SECRET", shared_secret)

    with app.app_context():
        create_tables()
        user, err = register_user(default_admin_username, "pass1234")
        assert err is None
        assert user is not None

        with app.test_client() as client:
            _register_agent(
                client,
                shared_secret,
                agent_code,
                project_code,
                default_admin_username=default_admin_username,
            )

            project = Project.query.filter_by(code=project_code).first()
            assert project is not None
            db.session.expire_all()
            membership = AuthUserProject.query.filter_by(
                user_id=user.id,
                project_id=project.id,
            ).first()
            assert membership is not None
            assert membership.role == "admin"


def test_default_admin_username_missing_user_creates_pre_assignment(monkeypatch):
    shared_secret = _uid("secret")
    agent_code = _uid("agent")
    project_code = _uid("PPRE")
    default_admin_username = _uid("future")
    monkeypatch.setenv("AGENT_SHARED_SECRET", shared_secret)

    with app.app_context():
        create_tables()
        with app.test_client() as client:
            _register_agent(
                client,
                shared_secret,
                agent_code,
                project_code,
                default_admin_username=default_admin_username,
            )

            project = Project.query.filter_by(code=project_code).first()
            assert project is not None
            pre = AuthProjectPreAssignment.query.filter_by(
                username=default_admin_username,
                project_id=project.id,
            ).first()
            assert pre is not None
            assert pre.role == "admin"
            assert pre.applied is False
            assert AuthUser.query.filter_by(username=default_admin_username).first() is None


def test_agent_reregister_adds_new_project_without_removing_old_binding(monkeypatch):
    shared_secret = _uid("secret")
    agent_code = _uid("agent")
    first_code = "G119"
    second_code = "G120"
    monkeypatch.setenv("AGENT_SHARED_SECRET", shared_secret)

    with app.app_context():
        create_tables()
        with app.test_client() as client:
            _register_agent(client, shared_secret, agent_code, first_code, default_admin_username=_uid("owner"))

            second_resp = client.post(
                "/api/agents/register",
                json={
                    "agent_code": agent_code,
                    "agent_name": f"{agent_code}-name",
                    "project_codes": [second_code],
                    "default_admin_username": _uid("owner2"),
                },
                headers={"X-Agent-Secret": shared_secret},
            )
            assert second_resp.status_code == 200, second_resp.get_data(as_text=True)
            assert (second_resp.get_json() or {}).get("success") is True

            agent = AgentNode.query.filter_by(agent_code=agent_code).first()
            assert agent is not None

            bindings = AgentProjectBinding.query.filter_by(agent_id=agent.id).all()
            binding_codes = sorted({row.project_code for row in bindings})
            assert binding_codes == sorted([first_code, second_code])

            assert Project.query.filter_by(code=first_code).first() is not None
            assert Project.query.filter_by(code=second_code).first() is not None


def test_default_admin_username_change_is_accumulative(monkeypatch):
    shared_secret = _uid("secret")
    agent_code = _uid("agent")
    project_code = _uid("P")
    old_admin = _uid("owner_old")
    new_admin = _uid("owner_new")
    monkeypatch.setenv("AGENT_SHARED_SECRET", shared_secret)

    with app.app_context():
        create_tables()
        with app.test_client() as client:
            _register_agent(client, shared_secret, agent_code, project_code, default_admin_username=old_admin)
            _register_agent(client, shared_secret, agent_code, None, default_admin_username=new_admin)

            agent = AgentNode.query.filter_by(agent_code=agent_code).first()
            assert agent is not None

            records = AgentDefaultAdmin.query.filter_by(agent_id=agent.id).all()
            usernames = sorted({row.username for row in records})
            assert usernames == sorted([old_admin, new_admin])

            project = Project.query.filter_by(code=project_code).first()
            assert project is not None
            pre_old = AuthProjectPreAssignment.query.filter_by(username=old_admin, project_id=project.id).first()
            pre_new = AuthProjectPreAssignment.query.filter_by(username=new_admin, project_id=project.id).first()
            assert pre_old is not None
            assert pre_new is not None


def test_default_admin_user_can_create_project_only_on_owned_agents(monkeypatch):
    admin_token = _uid("admin-token")
    shared_secret = _uid("secret")
    user_name = _uid("owner")
    agent_a_code = _uid("agent-a")
    agent_b_code = _uid("agent-b")
    monkeypatch.setenv("DEPLOYMENT_MODE", "platform")
    monkeypatch.setenv("ADMIN_API_TOKEN", admin_token)
    monkeypatch.setenv("AGENT_SHARED_SECRET", shared_secret)

    with app.app_context():
        create_tables()
        user, err = register_user(user_name, "pass1234")
        assert err is None
        assert user is not None

        with app.test_client() as client:
            _register_agent(client, shared_secret, agent_a_code, None, default_admin_username=user_name)
            _register_agent(client, shared_secret, agent_b_code, None, default_admin_username=_uid("other-owner"))

            with client.session_transaction() as sess:
                sess["auth_user_id"] = user.id
                sess["auth_username"] = user.username
                sess["auth_role"] = user.role
                sess["is_admin"] = False
                sess["_csrf_token"] = "csrf-owner"

            owned_project_code = _uid("OWNP")
            create_owned = client.post(
                "/projects",
                data={
                    "_csrf_token": "csrf-owner",
                    "code": owned_project_code,
                    "name": "owner-created-project",
                    # 不传 agent_code，应自动落到唯一授权的 agent_a
                },
                follow_redirects=False,
            )
            assert create_owned.status_code in (302, 303)
            owned_project = Project.query.filter_by(code=owned_project_code).first()
            assert owned_project is not None
            owned_binding = AgentProjectBinding.query.filter_by(project_id=owned_project.id).first()
            assert owned_binding is not None
            owned_agent = AgentNode.query.get(owned_binding.agent_id)
            assert owned_agent is not None
            assert owned_agent.agent_code == agent_a_code
            owned_membership = AuthUserProject.query.filter_by(
                user_id=user.id,
                project_id=owned_project.id,
            ).first()
            assert owned_membership is not None
            assert owned_membership.role == "admin"
            refreshed_user = db.session.get(AuthUser, user.id)
            assert refreshed_user is not None
            assert refreshed_user.role == "project_admin"

            denied_project_code = _uid("DENY")
            create_denied = client.post(
                "/projects",
                data={
                    "_csrf_token": "csrf-owner",
                    "code": denied_project_code,
                    "name": "should-deny",
                    "agent_code": agent_b_code,
                },
                follow_redirects=False,
            )
            assert create_denied.status_code in (302, 303)
            assert Project.query.filter_by(code=denied_project_code).first() is None


def test_default_admin_user_with_two_owned_agents_can_select_either(monkeypatch):
    admin_token = _uid("admin-token")
    shared_secret = _uid("secret")
    user_name = _uid("owner")
    agent_a_code = _uid("agent-a")
    agent_b_code = _uid("agent-b")
    monkeypatch.setenv("DEPLOYMENT_MODE", "platform")
    monkeypatch.setenv("ADMIN_API_TOKEN", admin_token)
    monkeypatch.setenv("AGENT_SHARED_SECRET", shared_secret)

    with app.app_context():
        create_tables()
        user, err = register_user(user_name, "pass1234")
        assert err is None
        assert user is not None

        with app.test_client() as client:
            _register_agent(client, shared_secret, agent_a_code, None, default_admin_username=user_name)
            _register_agent(client, shared_secret, agent_b_code, None, default_admin_username=user_name)

            with client.session_transaction() as sess:
                sess["auth_user_id"] = user.id
                sess["auth_username"] = user.username
                sess["auth_role"] = user.role
                sess["is_admin"] = False
                sess["_csrf_token"] = "csrf-owner-2"

            code_a = _uid("PA")
            code_b = _uid("PB")
            resp_a = client.post(
                "/projects",
                data={
                    "_csrf_token": "csrf-owner-2",
                    "code": code_a,
                    "name": "project-on-a",
                    "agent_code": agent_a_code,
                },
                follow_redirects=False,
            )
            resp_b = client.post(
                "/projects",
                data={
                    "_csrf_token": "csrf-owner-2",
                    "code": code_b,
                    "name": "project-on-b",
                    "agent_code": agent_b_code,
                },
                follow_redirects=False,
            )
            assert resp_a.status_code in (302, 303)
            assert resp_b.status_code in (302, 303)

            p_a = Project.query.filter_by(code=code_a).first()
            p_b = Project.query.filter_by(code=code_b).first()
            assert p_a is not None
            assert p_b is not None

            b_a = AgentProjectBinding.query.filter_by(project_id=p_a.id).first()
            b_b = AgentProjectBinding.query.filter_by(project_id=p_b.id).first()
            assert b_a is not None
            assert b_b is not None

            a1 = AgentNode.query.get(b_a.agent_id)
            a2 = AgentNode.query.get(b_b.agent_id)
            assert a1 is not None and a1.agent_code == agent_a_code
            assert a2 is not None and a2.agent_code == agent_b_code


def test_default_admin_email_prefix_user_can_create_project(monkeypatch):
    admin_token = _uid("admin-token")
    shared_secret = _uid("secret")
    username_prefix = _uid("prefix_owner")
    agent_code = _uid("agent-prefix")
    monkeypatch.setenv("DEPLOYMENT_MODE", "platform")
    monkeypatch.setenv("ADMIN_API_TOKEN", admin_token)
    monkeypatch.setenv("AGENT_SHARED_SECRET", shared_secret)
    monkeypatch.setenv("AUTH_BACKEND", "local")

    with app.app_context():
        create_tables()
        user, err = register_user(username_prefix, "pass1234")
        assert err is None
        assert user is not None

        with app.test_client() as client:
            _register_agent(
                client,
                shared_secret,
                agent_code,
                None,
                default_admin_username=f"{username_prefix}@corp.netease.com",
            )

            with client.session_transaction() as sess:
                sess["auth_user_id"] = user.id
                # 使用邮箱登录态，验证权限判断按邮箱前缀归一化
                sess["auth_username"] = f"{username_prefix}@corp.netease.com"
                sess["auth_role"] = user.role
                sess["is_admin"] = False
                sess["_csrf_token"] = "csrf-prefix-owner"

            code = _uid("PREFIXP")
            resp = client.post(
                "/projects",
                data={
                    "_csrf_token": "csrf-prefix-owner",
                    "code": code,
                    "name": "prefix-owner-project",
                },
                follow_redirects=False,
            )
            assert resp.status_code in (302, 303)

            project = Project.query.filter_by(code=code).first()
            assert project is not None
            binding = AgentProjectBinding.query.filter_by(project_id=project.id).first()
            assert binding is not None
            bound_agent = AgentNode.query.get(binding.agent_id)
            assert bound_agent is not None
            assert bound_agent.agent_code == agent_code

            membership = AuthUserProject.query.filter_by(user_id=user.id, project_id=project.id).first()
            assert membership is not None
            assert membership.role == "admin"
            refreshed_user = db.session.get(AuthUser, user.id)
            assert refreshed_user is not None
            assert refreshed_user.role == "project_admin"


def test_qkit_default_admin_user_can_create_project_and_auto_admin(monkeypatch):
    admin_token = _uid("admin-token")
    shared_secret = _uid("secret")
    username_prefix = _uid("qkit_owner")
    agent_code = _uid("qkit-agent")
    monkeypatch.setenv("DEPLOYMENT_MODE", "platform")
    monkeypatch.setenv("ADMIN_API_TOKEN", admin_token)
    monkeypatch.setenv("AGENT_SHARED_SECRET", shared_secret)
    monkeypatch.setenv("AUTH_BACKEND", "qkit")

    with app.app_context():
        create_tables()
        from qkit_auth.models import QkitAuthUser, QkitAuthUserProject
        from qkit_auth.services import ensure_qkit_user

        user, err = ensure_qkit_user(
            username=username_prefix,
            display_name="Qkit Owner",
            email=f"{username_prefix}@corp.netease.com",
            source="test",
        )
        assert err is None
        assert user is not None
        db.session.commit()

        with app.test_client() as client:
            _register_agent(
                client,
                shared_secret,
                agent_code,
                None,
                default_admin_username=f"{username_prefix}@corp.netease.com",
            )

            with client.session_transaction() as sess:
                sess["auth_user_id"] = user.id
                sess["auth_username"] = f"{username_prefix}@corp.netease.com"
                sess["auth_role"] = user.role
                sess["is_admin"] = False
                sess["auth_backend"] = "qkit"
                sess["_csrf_token"] = "csrf-qkit-owner"

            code = _uid("QKITP")
            resp = client.post(
                "/projects",
                data={
                    "_csrf_token": "csrf-qkit-owner",
                    "code": code,
                    "name": "qkit-owner-project",
                },
                follow_redirects=False,
            )
            assert resp.status_code in (302, 303)

            project = Project.query.filter_by(code=code).first()
            assert project is not None
            binding = AgentProjectBinding.query.filter_by(project_id=project.id).first()
            assert binding is not None
            bound_agent = AgentNode.query.get(binding.agent_id)
            assert bound_agent is not None
            assert bound_agent.agent_code == agent_code

            membership = QkitAuthUserProject.query.filter_by(user_id=user.id, project_id=project.id).first()
            assert membership is not None
            assert membership.role == "admin"
            refreshed_user = db.session.get(QkitAuthUser, user.id)
            assert refreshed_user is not None
            assert refreshed_user.role == "project_admin"
