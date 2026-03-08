from datetime import timezone
from types import ModuleType, SimpleNamespace
import sys

import services.agent_management_handlers as agent_handlers


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
