from datetime import timezone
from types import ModuleType, SimpleNamespace
import sys

import services.agent_management_handlers as agent_handlers
from app import app


def _extract_response(result):
    if isinstance(result, tuple):
        response, status_code = result
        return status_code, response.get_json()
    return result.status_code, result.get_json()


def test_int_env_falls_back_to_default_when_env_not_numeric(monkeypatch):
    monkeypatch.setenv("AGENT_SAMPLE_INT", "not-a-number")
    value = agent_handlers._int_env("AGENT_SAMPLE_INT", default=12, min_value=1, max_value=100)
    assert value == 12


def test_to_int_or_none_and_to_float_or_none_return_none_for_invalid_values():
    assert agent_handlers._to_int_or_none("oops") is None
    assert agent_handlers._to_int_or_none(9, min_value=10) is None
    assert agent_handlers._to_float_or_none("oops") is None
    assert agent_handlers._to_float_or_none(2.5, max_value=2.0) is None


def test_parse_commit_time_handles_invalid_and_utc_z():
    assert agent_handlers._parse_commit_time("invalid-time") is None

    parsed = agent_handlers._parse_commit_time("2026-03-08T10:20:30Z")
    assert parsed is not None
    assert parsed.tzinfo == timezone.utc
    assert parsed.hour == 10


def test_ensure_default_admin_returns_auth_unavailable_when_auth_import_fails(monkeypatch):
    fake_auth = ModuleType("auth")

    def _raise_backend_error():
        raise RuntimeError("backend unavailable")

    fake_auth.get_auth_backend = _raise_backend_error
    monkeypatch.setitem(sys.modules, "auth", fake_auth)
    monkeypatch.delitem(sys.modules, "auth.models", raising=False)
    monkeypatch.delitem(sys.modules, "qkit_auth.models", raising=False)

    result = agent_handlers._ensure_default_admin_for_projects(
        db=SimpleNamespace(),
        default_admin_username="admin",
        project_ids=[1, 2],
    )

    assert result["mode"] == "auth_unavailable"
    assert result["project_count"] == 2


def test_agent_get_latest_release_returns_500_for_release_runtime_errors(monkeypatch):
    monkeypatch.setattr(agent_handlers, "get_runtime_models", lambda *_args: (lambda *_a, **_k: None,))
    monkeypatch.setattr(agent_handlers, "_validate_agent_shared_secret", lambda: (True, None, None))
    monkeypatch.setattr(agent_handlers, "_get_agent_by_identity", lambda *_args, **_kwargs: SimpleNamespace(id=1))
    monkeypatch.setattr(
        agent_handlers,
        "load_latest_release_manifest",
        lambda: (_ for _ in ()).throw(RuntimeError("manifest-broken")),
    )

    with app.test_request_context(
        "/api/agents/releases/latest",
        method="POST",
        json={"agent_code": "a1", "agent_token": "t1"},
    ):
        result = agent_handlers.agent_get_latest_release()

    status_code, payload = _extract_response(result)
    assert status_code == 500
    assert payload["success"] is False


def test_agent_download_release_package_returns_500_for_package_oserror(monkeypatch):
    monkeypatch.setattr(agent_handlers, "get_runtime_models", lambda *_args: (lambda *_a, **_k: None,))
    monkeypatch.setattr(agent_handlers, "_validate_agent_shared_secret", lambda: (True, None, None))
    monkeypatch.setattr(agent_handlers, "_get_agent_by_identity", lambda *_args, **_kwargs: SimpleNamespace(id=1))
    monkeypatch.setattr(agent_handlers, "load_release_manifest", lambda _version: {"package_file": "agent.zip"})
    monkeypatch.setattr(
        agent_handlers,
        "get_release_package_path",
        lambda _version: (_ for _ in ()).throw(OSError("package missing")),
    )

    with app.test_request_context("/api/agents/releases/v1.0.0/package?agent_code=a1&agent_token=t1"):
        result = agent_handlers.agent_download_release_package("v1.0.0")

    status_code, payload = _extract_response(result)
    assert status_code == 500
    assert payload["success"] is False


def test_list_agent_releases_returns_500_when_manifest_loader_raises(monkeypatch):
    monkeypatch.setattr(agent_handlers, "get_runtime_models", lambda *_args: (lambda *_a, **_k: None,))
    monkeypatch.setattr(
        agent_handlers,
        "load_latest_release_manifest",
        lambda: (_ for _ in ()).throw(ValueError("bad-manifest")),
    )

    with app.test_request_context("/api/agents/releases"):
        result = agent_handlers.list_agent_releases.__wrapped__()

    status_code, payload = _extract_response(result)
    assert status_code == 500
    assert payload["success"] is False


def test_rollback_agent_release_returns_400_for_invalid_steps(monkeypatch):
    monkeypatch.setattr(agent_handlers, "get_runtime_models", lambda *_args: (lambda *_a, **_k: None,))

    with app.test_request_context("/api/agents/releases/rollback", method="POST", json={"steps": "oops"}):
        result = agent_handlers.rollback_agent_release.__wrapped__()

    status_code, payload = _extract_response(result)
    assert status_code == 400
    assert payload["success"] is False
