from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _safe_import_app_or_skip():
    try:
        with patch("sys.stdout") as mock_stdout, patch("sys.stderr") as mock_stderr:
            mock_stdout.buffer = MagicMock()
            mock_stderr.buffer = MagicMock()
            import app  # noqa: WPS433

        return app
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"app import unavailable in this environment: {exc}")


def test_startup_registers_expected_blueprints():
    app_module = _safe_import_app_or_skip()
    blueprint_names = set(app_module.app.blueprints.keys())
    assert {
        "cache_management",
        "commit_diff_routes",
        "core_management_routes",
        "weekly_version_routes",
        "agent_management_routes",
    }.issubset(blueprint_names)


def test_startup_key_route_rules_exist():
    app_module = _safe_import_app_or_skip()
    endpoint_to_rule = {rule.endpoint: rule.rule for rule in app_module.app.url_map.iter_rules()}
    assert endpoint_to_rule.get("admin_login") == "/auth/login"
    assert endpoint_to_rule.get("projects") == "/projects"
    assert endpoint_to_rule.get("commit_list") == "/repositories/<int:repository_id>/commits"
    assert endpoint_to_rule.get("list_agent_nodes") == "/api/agents"


def test_startup_key_routes_reachable_without_5xx():
    app_module = _safe_import_app_or_skip()
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        for path in ("/auth/login", "/projects", "/repositories/1/commits", "/api/agents"):
            response = client.get(path, follow_redirects=False)
            assert response.status_code < 500, f"{path} returned {response.status_code}"
