"""任务租约、回传幂等及过期执行隔离的数据库回归测试。"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, text

from app import app, create_tables, db
from models import AgentNode, AgentTask, BackgroundTask, Commit, Project, Repository, WeeklyVersionConfig


@pytest.fixture
def node(monkeypatch):
    monkeypatch.setenv("AGENT_SHARED_SECRET", "lease-test-secret")
    with app.app_context():
        create_tables()
        client = app.test_client()
        code = "lease_" + uuid.uuid4().hex[:10]
        headers = {"X-Agent-Secret": "lease-test-secret"}
        data = client.post("/api/agents/register", headers=headers, json={
            "agent_code": code, "project_codes": [code],
        }).get_json()
        identity = {"agent_code": code, "agent_token": data["agent_token"]}
        agent = AgentNode.query.filter_by(agent_code=code).one()
        project = Project.query.filter_by(code=code).one()
        yield client, headers, identity, agent.id, project.id


def task_for(node, **kwargs):
    task = AgentTask(task_type="noop", project_id=node[4], payload="{}", **kwargs)
    db.session.add(task)
    db.session.commit()
    return task.id


def report(node, task_id, **kwargs):
    return node[0].post(f"/api/agents/tasks/{task_id}/result", headers=node[1],
                        json={**node[2], "status": "completed", **kwargs})


def claim(node):
    response = node[0].post("/api/agents/tasks/claim", headers=node[1], json=node[2])
    assert response.status_code == 200
    return response.get_json()["task"]


def test_unclaimed_task_cannot_accept_result(node):
    task_id = task_for(node, status="pending")
    assert report(node, task_id).status_code == 403
    db.session.expire_all()
    assert db.session.get(AgentTask, task_id).status == "pending"


def test_duplicate_failed_report_does_not_increment_source_retry(node):
    source = BackgroundTask(task_type="noop", status="processing")
    db.session.add(source)
    db.session.flush()
    task_id = task_for(node, status="processing", assigned_agent_id=node[3], source_task_id=source.id)
    assert report(node, task_id, status="failed", error_message="first").status_code == 200
    assert report(node, task_id, status="failed", error_message="duplicate").status_code == 200
    db.session.expire_all()
    assert db.session.get(BackgroundTask, source.id).retry_count == 1
    assert db.session.get(AgentTask, task_id).error_message == "first"


def test_terminal_result_cannot_be_overwritten(node):
    task_id = task_for(node, status="processing", assigned_agent_id=node[3])
    assert report(node, task_id).status_code == 200
    assert report(node, task_id, status="failed").status_code == 409


def test_reclaim_fences_previous_attempt_even_on_same_node(node):
    task_id = task_for(node, status="pending")
    first = claim(node)
    assert "attempt" in first
    db.session.get(AgentTask, task_id).lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.session.commit()
    second = claim(node)
    assert second["attempt"] > first["attempt"]
    assert report(node, task_id, attempt=first["attempt"]).status_code == 409
    assert report(node, task_id).status_code == 409
    assert report(node, task_id, attempt=second["attempt"]).status_code == 200


def test_heartbeat_renews_only_current_attempt(node):
    task_id = task_for(node, status="pending")
    current = claim(node)
    before = db.session.get(AgentTask, task_id).lease_expires_at
    response = node[0].post("/api/agents/heartbeat", headers=node[1], json={
        **node[2], "task_id": task_id, "attempt": current.get("attempt", 0), "lease_seconds": 600,
    })
    assert response.status_code == 200
    db.session.expire_all()
    assert db.session.get(AgentTask, task_id).lease_expires_at > before
    response = node[0].post("/api/agents/heartbeat", headers=node[1], json={
        **node[2], "task_id": task_id, "attempt": -1,
    })
    assert response.status_code == 409


def test_auto_sync_side_effects_rollback_together(node, monkeypatch):
    from services import agent_management_handlers as handlers

    monkeypatch.setenv("DEPLOYMENT_MODE", "platform")
    repository = Repository(project_id=node[4], name="atomic-repo", type="git",
                            url="https://example.com/test.git", branch="main")
    db.session.add(repository)
    db.session.flush()
    now = datetime.now(timezone.utc)
    config = WeeklyVersionConfig(project_id=node[4], repository_id=repository.id, name="weekly",
                                 branch="main", start_time=now, end_time=now + timedelta(days=7))
    db.session.add(config)
    db.session.commit()
    repository_id = repository.id
    task_id = task_for(node, status="processing", assigned_agent_id=node[3], repository_id=repository_id)
    db.session.get(AgentTask, task_id).task_type = "auto_sync"
    db.session.commit()

    def fail_after_application(*args, **kwargs):
        raise RuntimeError("simulated failure after weekly dispatch")

    monkeypatch.setattr(handlers, "clear_repository_sync_error", fail_after_application)
    response = report(node, task_id, result_payload={"commits": [{
        "commit_id": "abcdef123", "path": "config.txt", "operation": "M",
        "commit_time": now.isoformat(),
    }]})
    assert response.status_code == 500
    db.session.expire_all()
    assert db.session.get(AgentTask, task_id).status == "processing"
    assert Commit.query.filter_by(repository_id=repository_id).count() == 0


def test_reclaim_cannot_overwrite_concurrent_renewal(node):
    expired = datetime.now(timezone.utc) - timedelta(seconds=10)
    task_id = task_for(node, status="processing", assigned_agent_id=node[3], lease_expires_at=expired)
    fired = []

    def renew_before_update(conn, cursor, statement, parameters, context, executemany):
        if fired or not statement.startswith("UPDATE agent_tasks"):
            return
        fired.append(True)
        with db.engine.begin() as other:
            other.execute(text("UPDATE agent_tasks SET lease_expires_at = :lease WHERE id = :id"),
                          {"lease": datetime.now(timezone.utc) + timedelta(minutes=10), "id": task_id})

    event.listen(db.engine, "before_cursor_execute", renew_before_update)
    try:
        assert claim(node) is None
    finally:
        event.remove(db.engine, "before_cursor_execute", renew_before_update)
    assert fired
    db.session.expire_all()
    assert db.session.get(AgentTask, task_id).status == "processing"
    assert db.session.get(AgentTask, task_id).retry_count == 0


def test_concurrent_result_winner_is_not_applied_twice(node):
    task_id = task_for(node, status="processing", assigned_agent_id=node[3])
    fired = []

    def finish_before_update(conn, cursor, statement, parameters, context, executemany):
        if fired or not statement.startswith("UPDATE agent_tasks"):
            return
        fired.append(True)
        with db.engine.begin() as other:
            other.execute(text("UPDATE agent_tasks SET status = 'completed', result_summary = 'winner' WHERE id = :id"),
                          {"id": task_id})

    event.listen(db.engine, "before_cursor_execute", finish_before_update)
    try:
        response = report(node, task_id, result_summary="loser")
    finally:
        event.remove(db.engine, "before_cursor_execute", finish_before_update)
    assert fired
    assert response.status_code == 200
    assert response.get_json()["duplicate"] is True
    db.session.expire_all()
    assert db.session.get(AgentTask, task_id).result_summary == "winner"
