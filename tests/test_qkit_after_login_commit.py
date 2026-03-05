#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from flask import Flask, session

from qkit_auth import providers as qproviders
from qkit_auth import routes as qroutes


def _build_app_with_qkit_bp() -> Flask:
    app = Flask(__name__)
    app.secret_key = "test-secret"

    @app.route("/", endpoint="index")
    def index():
        return "ok"

    app.register_blueprint(qroutes.qkit_auth_bp)
    return app


class _FakeUser:
    id = 101
    username = "demo_user"
    role = "normal"
    is_platform_admin = False
    is_active = True


def test_qkit_after_login_commits_user_before_session_redirect(monkeypatch):
    app = _build_app_with_qkit_bp()

    monkeypatch.setattr(qroutes, "check_qkit_jwt_remote", lambda token: (True, "", {"ok": True}))
    monkeypatch.setattr(qroutes, "decode_qkit_jwt_unsafe", lambda token: {"uid": "demo_user@corp.netease.com"})
    monkeypatch.setattr(
        qroutes,
        "extract_identity_from_payload",
        lambda payload: {
            "username": "demo_user",
            "display_name": "Demo User",
            "email": "demo_user@corp.netease.com",
        },
    )
    monkeypatch.setattr(qroutes, "ensure_qkit_user", lambda **kwargs: (_FakeUser(), None))

    commit_called = {"value": False}

    def _commit():
        commit_called["value"] = True

    monkeypatch.setattr(qroutes.db.session, "commit", _commit)

    with app.test_request_context("/qkit_auth/after_login?qkitjwt=test-token"):
        session["qkit_backhost"] = "/projects"
        resp = qroutes.after_login()

        assert commit_called["value"] is True
        assert session.get("auth_user_id") == 101
        assert session.get("auth_backend") == "qkit"
        assert resp.status_code == 302
        assert resp.location.endswith("/projects")
        assert any("qkitjwt=test-token" in value for value in resp.headers.getlist("Set-Cookie"))


def test_qkit_after_login_commit_failure_redirects_to_login(monkeypatch):
    app = _build_app_with_qkit_bp()

    monkeypatch.setattr(qroutes, "check_qkit_jwt_remote", lambda token: (True, "", {"ok": True}))
    monkeypatch.setattr(qroutes, "decode_qkit_jwt_unsafe", lambda token: {"uid": "demo_user@corp.netease.com"})
    monkeypatch.setattr(
        qroutes,
        "extract_identity_from_payload",
        lambda payload: {
            "username": "demo_user",
            "display_name": "Demo User",
            "email": "demo_user@corp.netease.com",
        },
    )
    monkeypatch.setattr(qroutes, "ensure_qkit_user", lambda **kwargs: (_FakeUser(), None))

    rollback_called = {"value": False}

    def _commit():
        raise RuntimeError("commit failed")

    def _rollback():
        rollback_called["value"] = True

    monkeypatch.setattr(qroutes.db.session, "commit", _commit)
    monkeypatch.setattr(qroutes.db.session, "rollback", _rollback)

    with app.test_request_context("/qkit_auth/after_login?qkitjwt=test-token"):
        resp = qroutes.after_login()
        assert rollback_called["value"] is True
        assert session.get("auth_user_id") is None
        assert resp.status_code == 302
        assert resp.location.endswith("/qkit_auth/login")


def test_qkit_after_login_large_jwt_is_split_to_multi_cookies(monkeypatch):
    app = _build_app_with_qkit_bp()

    monkeypatch.setattr(qroutes, "check_qkit_jwt_remote", lambda token: (True, "", {"ok": True}))
    monkeypatch.setattr(qroutes, "decode_qkit_jwt_unsafe", lambda token: {"uid": "demo_user@corp.netease.com"})
    monkeypatch.setattr(
        qroutes,
        "extract_identity_from_payload",
        lambda payload: {
            "username": "demo_user",
            "display_name": "Demo User",
            "email": "demo_user@corp.netease.com",
        },
    )
    monkeypatch.setattr(qroutes, "ensure_qkit_user", lambda **kwargs: (_FakeUser(), None))

    long_token = "x" * 8200
    with app.test_request_context(f"/qkit_auth/after_login?qkitjwt={long_token}"):
        resp = qroutes.after_login()
        cookies = resp.headers.getlist("Set-Cookie")
        assert any("qkitjwt_parts=" in value for value in cookies)
        assert any("qkitjwt_p0=" in value for value in cookies)
        assert not any("qkitjwt=xxxxxxxx" in value for value in cookies)


def test_provider_can_reassemble_multi_part_qkit_cookie():
    app = Flask(__name__)
    app.secret_key = "test-secret"
    part_a = "abc123"
    part_b = "xyz456"
    cookie_header = f"qkitjwt_parts=2; qkitjwt_p0={part_a}; qkitjwt_p1={part_b}"
    with app.test_request_context("/", headers={"Cookie": cookie_header}):
        token = qproviders._load_qkit_jwt_from_request()
        assert token == f"{part_a}{part_b}"


def test_qkit_after_login_uses_session_token_when_local_cache_disabled(monkeypatch):
    app = _build_app_with_qkit_bp()
    monkeypatch.setenv("QKIT_LOCAL_JWT_CACHE", "false")

    monkeypatch.setattr(qroutes, "check_qkit_jwt_remote", lambda token: (True, "", {"ok": True}))
    monkeypatch.setattr(qroutes, "decode_qkit_jwt_unsafe", lambda token: {"uid": "demo_user@corp.netease.com"})
    monkeypatch.setattr(
        qroutes,
        "extract_identity_from_payload",
        lambda payload: {
            "username": "demo_user",
            "display_name": "Demo User",
            "email": "demo_user@corp.netease.com",
        },
    )
    monkeypatch.setattr(qroutes, "ensure_qkit_user", lambda **kwargs: (_FakeUser(), None))

    with app.test_request_context("/qkit_auth/after_login?qkitjwt=test-token"):
        resp = qroutes.after_login()
        assert session.get("qkitjwt_session") == "test-token"
        assert resp.status_code == 302


def test_provider_can_validate_with_session_token_when_local_cache_disabled(monkeypatch):
    app = Flask(__name__)
    app.secret_key = "test-secret"
    monkeypatch.setenv("QKIT_LOCAL_JWT_CACHE", "false")
    monkeypatch.setattr(qproviders, "check_qkit_jwt_remote", lambda token: (token == "session-token", "", {"ok": True}))
    monkeypatch.setattr(qproviders, "get_user_by_id", lambda user_id: _FakeUser())

    provider = qproviders.QkitAuthProvider()
    with app.test_request_context("/"):
        session["auth_user_id"] = 101
        session["qkitjwt_session"] = "session-token"
        assert provider.is_logged_in() is True
