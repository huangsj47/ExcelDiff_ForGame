#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager

from app import app, db
from auth import routes as auth_routes
from auth.models import AuthUser, PlatformRole


@contextmanager
def _client():
    temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    temp_db.close()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SERVER_NAME"] = "localhost"
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{temp_db.name}"
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "admin123"
    os.environ.setdefault("SECRET_KEY", "test-secret-key-for-debug-register")

    with app.app_context():
        db.engine.dispose()
        db.drop_all()
        db.create_all()
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["_csrf_token"] = "test-csrf-token-debug-register"
            yield client
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
    try:
        os.unlink(temp_db.name)
    except OSError:
        pass


def _post_register(client, username: str, role: str):
    return client.post(
        "/auth/register",
        data={
            "_csrf_token": "test-csrf-token-debug-register",
            "username": username,
            "password": "pass1234",
            "password_confirm": "pass1234",
            "role": role,
        },
        follow_redirects=True,
    )


def test_register_role_forced_to_normal_when_debug_disabled():
    old_mode = auth_routes.AUTH_DEBUG_MODE
    auth_routes.AUTH_DEBUG_MODE = False
    try:
        with _client() as client:
            response = _post_register(client, "debug_off_user", PlatformRole.PLATFORM_ADMIN.value)
            assert response.status_code == 200
            user = AuthUser.query.filter_by(username="debug_off_user").first()
            assert user is not None
            assert user.role == PlatformRole.NORMAL.value
    finally:
        auth_routes.AUTH_DEBUG_MODE = old_mode


def test_register_can_choose_platform_admin_when_debug_enabled():
    old_mode = auth_routes.AUTH_DEBUG_MODE
    auth_routes.AUTH_DEBUG_MODE = True
    try:
        with _client() as client:
            response = _post_register(client, "debug_on_admin", PlatformRole.PLATFORM_ADMIN.value)
            assert response.status_code == 200
            user = AuthUser.query.filter_by(username="debug_on_admin").first()
            assert user is not None
            assert user.role == PlatformRole.PLATFORM_ADMIN.value
    finally:
        auth_routes.AUTH_DEBUG_MODE = old_mode


def test_register_page_shows_role_selector_only_in_debug_mode():
    old_mode = auth_routes.AUTH_DEBUG_MODE
    try:
        with _client() as client:
            auth_routes.AUTH_DEBUG_MODE = False
            normal_page = client.get("/auth/register")
            normal_html = normal_page.data.decode("utf-8")
            assert 'name="role"' not in normal_html

            auth_routes.AUTH_DEBUG_MODE = True
            debug_page = client.get("/auth/register")
            debug_html = debug_page.data.decode("utf-8")
            assert 'name="role"' in debug_html
            assert "platform_admin" in debug_html
    finally:
        auth_routes.AUTH_DEBUG_MODE = old_mode


def test_login_does_not_500_when_auth_tables_missing():
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False

    with app.app_context():
        db.engine.dispose()
        db.drop_all()

    try:
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["_csrf_token"] = "test-csrf-token-login-no-table"
            response = client.post(
                "/auth/login",
                data={
                    "_csrf_token": "test-csrf-token-login-no-table",
                    "username": "111111",
                    "password": "111111",
                },
                follow_redirects=True,
            )
            assert response.status_code == 200
            body = response.data.decode("utf-8", errors="ignore")
            assert "Internal Server Error" not in body
    finally:
        with app.app_context():
            db.create_all()
