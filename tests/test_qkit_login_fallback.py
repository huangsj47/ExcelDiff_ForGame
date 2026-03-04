#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from flask import Flask

from services import core_navigation_handlers as cnh
from qkit_auth import routes as qroutes


def _build_min_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "test-secret"

    @app.route("/", endpoint="index")
    def index():
        return "ok"

    return app


def test_admin_login_qkit_missing_blueprint_returns_503(monkeypatch):
    app = _build_min_app()
    monkeypatch.setenv("AUTH_BACKEND", "qkit")
    app.config["AUTH_INIT_ERROR"] = "ImportError: No module named 'jwt'"

    monkeypatch.setattr(
        cnh,
        "render_template",
        lambda template_name, **kwargs: f"{template_name}|{kwargs.get('next_url', '')}",
    )

    with app.test_request_context("/auth/login?next=/projects"):
        resp = cnh.admin_login()

    assert isinstance(resp, tuple)
    assert resp[1] == 503
    assert "admin_login.html" in resp[0]


def test_admin_login_qkit_with_blueprint_redirects(monkeypatch):
    app = _build_min_app()
    monkeypatch.setenv("AUTH_BACKEND", "qkit")

    app.add_url_rule(
        "/qkit_auth/login",
        endpoint="qkit_auth_bp.login",
        view_func=lambda: "qkit-login",
    )

    with app.test_request_context("/auth/login?next=/projects"):
        resp = cnh.admin_login()

    assert resp.status_code == 302
    assert resp.location.endswith("/qkit_auth/login?next=/projects")


def test_admin_login_qkit_only_view_function_without_rule_returns_503(monkeypatch):
    app = _build_min_app()
    monkeypatch.setenv("AUTH_BACKEND", "qkit")
    app.config["AUTH_INIT_ERROR"] = "partial blueprint registration"

    # Simulate half-registered endpoint: exists in view_functions but missing url_map rule.
    app.view_functions["qkit_auth_bp.login"] = lambda: "stub"
    monkeypatch.setattr(
        cnh,
        "render_template",
        lambda template_name, **kwargs: f"{template_name}|{kwargs.get('next_url', '')}",
    )

    with app.test_request_context("/auth/login?next=/projects"):
        resp = cnh.admin_login()

    assert isinstance(resp, tuple)
    assert resp[1] == 503
    assert "admin_login.html" in resp[0]


def test_admin_logout_qkit_missing_blueprint_clears_session(monkeypatch):
    app = _build_min_app()
    monkeypatch.setenv("AUTH_BACKEND", "qkit")

    with app.test_request_context("/auth/logout"):
        from flask import session

        session["auth_user_id"] = 1
        session["auth_username"] = "u"
        session["auth_role"] = "platform_admin"
        session["is_admin"] = True
        resp = cnh.admin_logout()
        assert "auth_user_id" not in session
        assert "auth_username" not in session
        assert "auth_role" not in session
        assert "is_admin" not in session

    assert resp.status_code == 302
    assert resp.location.endswith("/")


def test_qkit_auth_login_redirect_missing_qkit_bp_returns_503(monkeypatch):
    app = _build_min_app()
    app.config["AUTH_INIT_ERROR"] = "Exception: qkit blueprint register failed"

    monkeypatch.setattr(
        qroutes,
        "render_template",
        lambda template_name, **kwargs: f"{template_name}|{kwargs.get('next_url', '')}",
    )

    with app.test_request_context("/auth/login?next=/projects"):
        resp = qroutes._qkit_login_redirect("/projects")

    assert isinstance(resp, tuple)
    assert resp[1] == 503
    assert "admin_login.html" in resp[0]


def test_qkit_auth_login_redirect_with_qkit_bp_redirects():
    app = _build_min_app()
    app.add_url_rule(
        "/qkit_auth/login",
        endpoint="qkit_auth_bp.login",
        view_func=lambda: "qkit-login",
    )

    with app.test_request_context("/auth/login?next=/projects"):
        resp = qroutes._qkit_login_redirect("/projects")

    assert resp.status_code == 302
    assert resp.location.endswith("/qkit_auth/login?next=/projects")
