from __future__ import annotations

import json
from types import SimpleNamespace

from app import app
import services.agent_management_handlers as agent_handlers


def _extract_response(result):
    if isinstance(result, tuple):
        response, status_code = result
        return status_code, response.get_json()
    return result.status_code, result.get_json()


class _FakeField:
    def __init__(self, name: str):
        self.name = name

    def __eq__(self, other):
        return (self.name, "eq", other)

    def in_(self, items):
        return (self.name, "in", tuple(items))

    def isnot(self, other):
        return (self.name, "isnot", other)

    def __lt__(self, other):
        return (self.name, "lt", other)

    def asc(self):
        return (self.name, "asc")


def test_enqueue_agent_task_injects_schema_version_and_logs_chain(monkeypatch):
    captured_adds = []
    captured_events = []

    class _FakeAgentTask:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    fake_db = SimpleNamespace(session=SimpleNamespace(add=lambda obj: captured_adds.append(obj)))
    monkeypatch.setattr(
        agent_handlers,
        "get_runtime_models",
        lambda *_args: (fake_db, _FakeAgentTask),
    )
    monkeypatch.setattr(
        agent_handlers,
        "log_structured_event",
        lambda event, **fields: captured_events.append((event, fields)),
    )

    row = agent_handlers.enqueue_agent_task(
        task_type="auto_sync",
        project_id=10,
        repository_id=20,
        source_task_id=30,
        priority=5,
        payload={"repository_id": 20},
    )

    assert row in captured_adds
    payload = json.loads(row.payload)
    assert payload.get("schema_version") == 1
    assert payload.get("repository_id") == 20
    assert captured_events
    event, fields = captured_events[-1]
    assert event == "agent_task_enqueued"
    assert fields.get("source_task_id") == 30


def test_enqueue_agent_task_injects_schema_version_for_key_task_types(monkeypatch):
    captured_adds = []

    class _FakeAgentTask:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    fake_db = SimpleNamespace(session=SimpleNamespace(add=lambda obj: captured_adds.append(obj)))
    monkeypatch.setattr(agent_handlers, "log_structured_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        agent_handlers,
        "get_runtime_models",
        lambda *_args: (fake_db, _FakeAgentTask),
    )

    for task_type in ("commit_diff", "auto_sync", "weekly_sync"):
        row = agent_handlers.enqueue_agent_task(
            task_type=task_type,
            project_id=100,
            repository_id=200,
            source_task_id=300,
            payload={"k": "v"},
        )
        payload = json.loads(row.payload)
        assert payload.get("schema_version") == 1


def test_agent_claim_task_backfills_schema_version_for_legacy_payload(monkeypatch):
    task = SimpleNamespace(
        id=101,
        task_type="auto_sync",
        priority=3,
        project_id=1,
        repository_id=2,
        source_task_id=33,
        payload=json.dumps({"repository_id": 2}, ensure_ascii=False),
        status="pending",
        assigned_agent_id=None,
        lease_expires_at=None,
        started_at=None,
        created_at=SimpleNamespace(),
    )
    fake_agent = SimpleNamespace(id=7, agent_code="agent-a", status="offline", last_heartbeat=None)

    class _Query:
        def __init__(self, all_result=None, first_result=None):
            self._all = all_result or []
            self._first = first_result

        def filter(self, *_args, **_kwargs):
            return self

        def filter_by(self, **_kwargs):
            return self

        def order_by(self, *_args, **_kwargs):
            return self

        def all(self):
            return list(self._all)

        def first(self):
            return self._first

    class _FakeAgentProjectBinding:
        query = _Query(all_result=[SimpleNamespace(project_id=1)])

    class _FakeAgentTaskModel:
        status = _FakeField("status")
        project_id = _FakeField("project_id")
        lease_expires_at = _FakeField("lease_expires_at")
        priority = _FakeField("priority")
        created_at = _FakeField("created_at")
        query = _Query(all_result=[], first_result=task)

    fake_db = SimpleNamespace(
        session=SimpleNamespace(
            rollback=lambda: None,
            commit=lambda: None,
        )
    )

    monkeypatch.setattr(agent_handlers, "_validate_agent_shared_secret", lambda: (True, None, None))
    monkeypatch.setattr(agent_handlers, "_get_agent_by_identity", lambda *_args, **_kwargs: fake_agent)
    monkeypatch.setattr(
        agent_handlers,
        "get_runtime_models",
        lambda *_args: (fake_db, _FakeAgentProjectBinding, _FakeAgentTaskModel, lambda *_a, **_k: None),
    )

    with app.test_request_context(
        "/api/agents/tasks/claim",
        method="POST",
        json={"agent_code": "agent-a", "agent_token": "token-a"},
    ):
        result = agent_handlers.agent_claim_task()

    status_code, payload = _extract_response(result)
    assert status_code == 200
    assert payload["success"] is True
    task_payload = (payload.get("task") or {}).get("payload") or {}
    assert task_payload.get("schema_version") == 1
