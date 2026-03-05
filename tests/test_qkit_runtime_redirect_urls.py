#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from flask import Flask

from qkit_auth.config import QkitSettings
from qkit_auth import routes as qroutes


def _build_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "test-secret"

    @app.route("/", endpoint="index")
    def index():
        return "ok"

    app.register_blueprint(qroutes.qkit_auth_bp)
    return app


def _settings(**kwargs) -> QkitSettings:
    data = dict(
        local_host="127.0.0.1:8002",
        login_host="auth2.qkit.nie.netease.com",
        public_base_url="",
        jwt_secret="",
        local_jwt_cache=True,
        lock_project_id="",
        my_project_api="http://auth2.qkit.nie.netease.com/api/v1/projects/myprojects/",
        change_project_api="http://auth2.qkit.nie.netease.com/api/v1/projects/change_project_custom/",
        auth_check_jwt_api="http://auth2.qkit.nie.netease.com/api/v1/users/jwt_ver/",
        login_service="http://auth2.qkit.nie.netease.com/openid/login?next=http://127.0.0.1:8002/qkit_auth/after_login",
        login_service_explicit=False,
        logout_service="http://auth2.qkit.nie.netease.com/openid/logout?next=http://127.0.0.1:8002",
        logout_service_explicit=False,
        request_timeout_seconds=5,
        redmine_api_url="http://redmineapi.nie.netease.com/api/user",
    )
    data.update(kwargs)
    return QkitSettings(**data)


def test_qkit_login_uses_runtime_host_when_service_not_explicit(monkeypatch):
    app = _build_app()
    monkeypatch.setattr(qroutes, "load_qkit_settings", lambda: _settings())

    headers = {
        "Host": "internal.platform.local:8002",
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "diff.example.com",
    }
    with app.test_request_context("/qkit_auth/login?next=/projects", headers=headers):
        resp = qroutes.qkit_login()

    assert resp.status_code == 302
    assert resp.location == (
        "http://auth2.qkit.nie.netease.com/openid/login"
        "?next=https%3A%2F%2Fdiff.example.com%2Fqkit_auth%2Fafter_login"
    )


def test_qkit_login_prefers_explicit_login_service(monkeypatch):
    app = _build_app()
    explicit_url = "https://auth.custom/openid/login?next=https://fixed.example.com/qkit_auth/after_login"
    monkeypatch.setattr(
        qroutes,
        "load_qkit_settings",
        lambda: _settings(login_service=explicit_url, login_service_explicit=True),
    )

    with app.test_request_context("/qkit_auth/login?next=/projects", headers={"Host": "diff.example.com"}):
        resp = qroutes.qkit_login()

    assert resp.status_code == 302
    assert resp.location == explicit_url


def test_qkit_logout_uses_runtime_host_when_service_not_explicit(monkeypatch):
    app = _build_app()
    monkeypatch.setattr(qroutes, "load_qkit_settings", lambda: _settings())

    headers = {
        "Host": "internal.platform.local:8002",
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "diff.example.com",
    }
    with app.test_request_context("/qkit_auth/logout", headers=headers):
        resp = qroutes.qkit_logout()

    assert resp.status_code == 302
    assert resp.location == (
        "http://auth2.qkit.nie.netease.com/openid/logout"
        "?next=https%3A%2F%2Fdiff.example.com"
    )
