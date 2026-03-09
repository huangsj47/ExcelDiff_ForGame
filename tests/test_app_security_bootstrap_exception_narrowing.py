from __future__ import annotations

import builtins

from flask import Flask
from werkzeug.routing import BuildError

import services.app_security_bootstrap_service as security_bootstrap


def _configure(app):
    security_bootstrap.configure_app_security_bootstrap(
        app=app,
        log_print=lambda *_args, **_kwargs: None,
        csrf_session_key="_csrf_token",
        enable_admin_security=False,
        deployment_mode="single",
        csrf_token=lambda: "csrf-token",
        has_admin_access=lambda: True,
        is_logged_in=lambda: True,
        get_current_user=lambda: {"username": "tester"},
        has_project_access=lambda *_args, **_kwargs: True,
        has_project_admin_access=lambda *_args, **_kwargs: True,
        is_valid_admin_token=lambda: False,
        unauthorized_admin_response=lambda: ("forbidden", 403),
        unauthorized_login_response=lambda: ("login", 401),
        has_project_create_access=lambda: True,
        csrf_token_from_request=lambda: "csrf-token",
        csrf_error_response=lambda msg: (msg, 400),
        is_same_origin_request=lambda: True,
        get_excel_column_letter=lambda _idx: "A",
        format_beijing_time=lambda *_args, **_kwargs: "2026-03-08 12:00:00",
    )


def test_security_bootstrap_exception_tuples_are_declared():
    assert hasattr(security_bootstrap, "APP_SECURITY_AUTH_BACKEND_IMPORT_ERRORS")
    assert hasattr(security_bootstrap, "APP_SECURITY_PUBLIC_LOGIN_DISCOVERY_ERRORS")
    assert hasattr(security_bootstrap, "APP_SECURITY_PUBLIC_LOGIN_BUILD_ERRORS")
    assert hasattr(security_bootstrap, "APP_SECURITY_PUBLIC_REGISTER_DISCOVERY_ERRORS")
    assert hasattr(security_bootstrap, "APP_SECURITY_PUBLIC_REGISTER_BUILD_ERRORS")


def test_auth_backend_falls_back_to_local_when_auth_import_fails(monkeypatch):
    app = Flask(__name__)
    app.secret_key = "test-key"

    original_import = builtins.__import__

    def _fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "auth":
            raise ImportError("mocked import failure")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    _configure(app)

    assert app.jinja_env.globals["auth_backend"]() == "local"


def test_public_login_url_prefers_auth_blueprint_login_route():
    app = Flask(__name__)
    app.secret_key = "test-key"
    app.add_url_rule("/auth/login", endpoint="auth_bp.login", view_func=lambda: "ok")
    app.add_url_rule("/admin/login", endpoint="admin_login", view_func=lambda: "ok")

    _configure(app)
    with app.test_request_context("/"):
        assert app.jinja_env.globals["public_login_url"]() == "/auth/login"


def test_public_login_url_falls_back_to_admin_login():
    app = Flask(__name__)
    app.secret_key = "test-key"
    app.add_url_rule("/admin/login", endpoint="admin_login", view_func=lambda: "ok")

    _configure(app)
    with app.test_request_context("/"):
        assert app.jinja_env.globals["public_login_url"]() == "/admin/login"


def test_public_login_url_uses_hardcoded_path_when_url_build_fails(monkeypatch):
    app = Flask(__name__)
    app.secret_key = "test-key"

    _configure(app)
    monkeypatch.setattr(
        security_bootstrap,
        "url_for",
        lambda _endpoint: (_ for _ in ()).throw(BuildError("admin_login", {}, None)),
    )

    with app.test_request_context("/"):
        assert app.jinja_env.globals["public_login_url"]() == "/auth/login"


def test_public_register_enabled_defaults_to_true_in_local(monkeypatch):
    app = Flask(__name__)
    app.secret_key = "test-key"
    monkeypatch.setenv("AUTH_BACKEND", "local")
    monkeypatch.delenv("AUTH_ENABLE_REGISTER", raising=False)

    _configure(app)

    with app.test_request_context("/"):
        assert app.jinja_env.globals["public_register_enabled"]() is True


def test_public_register_enabled_defaults_to_false_in_qkit(monkeypatch):
    app = Flask(__name__)
    app.secret_key = "test-key"
    monkeypatch.setenv("AUTH_BACKEND", "qkit")
    monkeypatch.delenv("AUTH_ENABLE_REGISTER", raising=False)

    _configure(app)

    with app.test_request_context("/"):
        assert app.jinja_env.globals["public_register_enabled"]() is False


def test_public_register_enabled_allows_env_override(monkeypatch):
    app = Flask(__name__)
    app.secret_key = "test-key"
    monkeypatch.setenv("AUTH_BACKEND", "local")
    monkeypatch.setenv("AUTH_ENABLE_REGISTER", "false")

    _configure(app)

    with app.test_request_context("/"):
        assert app.jinja_env.globals["public_register_enabled"]() is False


def test_public_register_url_prefers_auth_register_route():
    app = Flask(__name__)
    app.secret_key = "test-key"
    app.add_url_rule("/auth/register", endpoint="auth_bp.register", view_func=lambda: "ok")

    _configure(app)
    with app.test_request_context("/"):
        assert app.jinja_env.globals["public_register_url"]() == "/auth/register"


def test_public_register_url_uses_hardcoded_path_when_url_build_fails(monkeypatch):
    app = Flask(__name__)
    app.secret_key = "test-key"

    _configure(app)
    monkeypatch.setattr(
        security_bootstrap,
        "url_for",
        lambda _endpoint: (_ for _ in ()).throw(BuildError("auth_bp.register", {}, None)),
    )

    with app.test_request_context("/"):
        assert app.jinja_env.globals["public_register_url"]() == "/auth/register"
