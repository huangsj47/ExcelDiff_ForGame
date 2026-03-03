#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager

from app import app, db
from auth import routes as auth_routes
from auth.models import (
    AuthProjectCreateRequest,
    AuthProjectJoinRequest,
    AuthUser,
    AuthUserProject,
    PlatformRole,
    RequestStatus,
)
import auth.services as auth_services
from auth.services import (
    add_user_to_project,
    handle_create_project_request,
    handle_join_request,
    register_user,
    request_create_project,
    request_join_project,
    toggle_user_active,
)
from models.project import Project
from utils.db_safety import assert_destructive_db_allowed, reset_sqlalchemy_engine_cache


@contextmanager
def _client():
    temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    temp_db.close()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SERVER_NAME"] = "localhost"
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{temp_db.name}"
    reset_sqlalchemy_engine_cache(app)
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "admin123"
    os.environ.setdefault("SECRET_KEY", "test-secret-key-for-debug-register")

    with app.app_context():
        runtime_uri = str(db.engine.url)
        assert_destructive_db_allowed(
            database_uri=runtime_uri,
            action_name="tests/test_auth_debug_register_mode.py::_client setup drop_all",
            testing=True,
        )
        db.drop_all()
        db.create_all()
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["_csrf_token"] = "test-csrf-token-debug-register"
            yield client
        db.session.remove()
        runtime_uri = str(db.engine.url)
        assert_destructive_db_allowed(
            database_uri=runtime_uri,
            action_name="tests/test_auth_debug_register_mode.py::_client teardown drop_all",
            testing=True,
        )
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


def _login_admin(client):
    return client.post(
        "/auth/login",
        data={
            "_csrf_token": "test-csrf-token-debug-register",
            "username": "admin",
            "password": "admin123",
        },
        follow_redirects=True,
    )


def _login_user(client, username: str, password: str):
    return client.post(
        "/auth/login",
        data={
            "_csrf_token": "test-csrf-token-debug-register",
            "username": username,
            "password": password,
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


def test_register_page_hides_one_char_username_hint_copy():
    with _client() as client:
        page = client.get("/auth/register")
        html = page.data.decode("utf-8")
        assert "可使用 1 位用户名（例如：2）。" not in html


def test_add_user_to_project_rejects_invalid_role():
    with _client():
        user, err = register_user("invalid_role_member", "pass1234")
        assert err is None
        assert user is not None

        project = Project(code="ROLE_INVALID_1", name="Role Invalid Project 1")
        db.session.add(project)
        db.session.commit()

        success, error = add_user_to_project(user.id, project.id, role="owner")
        assert success is False
        assert error is not None
        assert "无效的项目角色" in error
        assert AuthUserProject.query.filter_by(
            user_id=user.id,
            project_id=project.id,
        ).first() is None


def test_api_add_project_member_rejects_invalid_role():
    with _client() as client:
        login_resp = _login_admin(client)
        assert login_resp.status_code == 200

        user, err = register_user("invalid_role_member_api", "pass1234")
        assert err is None
        assert user is not None

        project = Project(code="ROLE_INVALID_2", name="Role Invalid Project 2")
        db.session.add(project)
        db.session.commit()

        response = client.post(
            f"/auth/api/project/{project.id}/members",
            json={"user_id": user.id, "role": "owner"},
            headers={"X-CSRFToken": "test-csrf-token-debug-register"},
        )
        data = response.get_json(silent=True) or {}
        assert response.status_code == 400
        assert data.get("success") is False
        assert "无效的项目角色" in data.get("message", "")
        assert AuthUserProject.query.filter_by(
            user_id=user.id,
            project_id=project.id,
        ).first() is None


def test_add_user_to_project_rejects_nonexistent_user_or_project():
    with _client():
        user, err = register_user("exist_member_1", "pass1234")
        assert err is None
        assert user is not None

        project = Project(code="ROLE_EXIST_1", name="Role Exist Project 1")
        db.session.add(project)
        db.session.commit()

        success_1, error_1 = add_user_to_project(999999, project.id, role="member")
        assert success_1 is False
        assert error_1 == "用户不存在"

        success_2, error_2 = add_user_to_project(user.id, 999999, role="member")
        assert success_2 is False
        assert error_2 == "项目不存在"

        assert AuthUserProject.query.filter_by(project_id=project.id).count() == 0


def test_api_add_project_member_rejects_nonexistent_user():
    with _client() as client:
        login_resp = _login_admin(client)
        assert login_resp.status_code == 200

        project = Project(code="ROLE_EXIST_2", name="Role Exist Project 2")
        db.session.add(project)
        db.session.commit()

        response = client.post(
            f"/auth/api/project/{project.id}/members",
            json={"user_id": 999999, "role": "member"},
            headers={"X-CSRFToken": "test-csrf-token-debug-register"},
        )
        data = response.get_json(silent=True) or {}
        assert response.status_code == 400
        assert data.get("success") is False
        assert data.get("message") == "用户不存在"
        assert AuthUserProject.query.filter_by(project_id=project.id).count() == 0


def test_request_join_project_rejects_nonexistent_project():
    with _client():
        user, err = register_user("join_missing_project_user", "pass1234")
        assert err is None
        assert user is not None

        success, error = request_join_project(user.id, 999999, "join test")
        assert success is False
        assert error == "项目不存在"
        assert AuthProjectJoinRequest.query.count() == 0


def test_request_join_project_rejects_nonexistent_user():
    with _client():
        project = Project(code="JOIN_USER_MISSING", name="Join User Missing")
        db.session.add(project)
        db.session.commit()

        success, error = request_join_project(999999, project.id, "join test")
        assert success is False
        assert error == "用户不存在"
        assert AuthProjectJoinRequest.query.count() == 0


def test_request_create_project_rejects_nonexistent_user():
    with _client():
        success, error = request_create_project(
            999999,
            "CREATE_USER_MISSING",
            "Create User Missing",
            "QA",
            "need project",
        )
        assert success is False
        assert error == "用户不存在"
        assert AuthProjectCreateRequest.query.count() == 0


def test_handle_create_project_request_keeps_pending_when_applicant_missing(monkeypatch):
    with _client():
        user, err = register_user("create_handle_user", "pass1234")
        assert err is None
        assert user is not None

        ok_req, req_err = request_create_project(
            user.id,
            "CREATE_HANDLE_MISSING",
            "Create Handle Missing",
            "QA",
            "need project",
        )
        assert ok_req is True
        assert req_err is None

        req = AuthProjectCreateRequest.query.filter_by(
            user_id=user.id,
            project_code="CREATE_HANDLE_MISSING",
            status=RequestStatus.PENDING.value,
        ).first()
        assert req is not None

        original_get = auth_services.db.session.get

        def _fake_get(model, identity, *args, **kwargs):
            if model is AuthUser and identity == user.id:
                return None
            return original_get(model, identity, *args, **kwargs)

        monkeypatch.setattr(auth_services.db.session, "get", _fake_get)

        ok, handle_err = handle_create_project_request(req.id, "approve", handled_by=0)
        assert ok is False
        assert handle_err == "申请用户不存在，无法创建项目"

        refreshed = db.session.get(AuthProjectCreateRequest, req.id)
        assert refreshed is not None
        assert refreshed.status == RequestStatus.PENDING.value


def test_api_request_join_project_rejects_nonexistent_project():
    with _client() as client:
        reg_resp = _post_register(client, "join_api_missing_project_user", PlatformRole.NORMAL.value)
        assert reg_resp.status_code == 200

        login_resp = _login_user(client, "join_api_missing_project_user", "pass1234")
        assert login_resp.status_code == 200

        response = client.post(
            "/auth/api/request-join-project",
            json={"project_id": 999999, "message": "join please"},
            headers={"X-CSRFToken": "test-csrf-token-debug-register"},
        )
        data = response.get_json(silent=True) or {}
        assert response.status_code == 400
        assert data.get("success") is False
        assert data.get("message") == "项目不存在"
        assert AuthProjectJoinRequest.query.count() == 0


def test_handle_join_request_keeps_pending_when_add_member_fails(monkeypatch):
    with _client():
        user, err = register_user("join_handle_fail_user", "pass1234")
        assert err is None
        assert user is not None

        project = Project(code="JOIN_HANDLE_FAIL", name="Join Handle Fail")
        db.session.add(project)
        db.session.commit()

        success_req, error_req = request_join_project(user.id, project.id, "please allow")
        assert success_req is True
        assert error_req is None

        req = AuthProjectJoinRequest.query.filter_by(
            user_id=user.id,
            project_id=project.id,
            status=RequestStatus.PENDING.value,
        ).first()
        assert req is not None

        def _fake_add_user_to_project(*_args, **_kwargs):
            return False, "项目不存在"

        monkeypatch.setattr("auth.services.add_user_to_project", _fake_add_user_to_project)
        ok, err = handle_join_request(req.id, "approve", handled_by=0)
        assert ok is False
        assert err is not None
        assert "项目不存在" in err

        refreshed = db.session.get(AuthProjectJoinRequest, req.id)
        assert refreshed is not None
        assert refreshed.status == RequestStatus.PENDING.value


def test_deactivated_user_session_is_not_treated_as_logged_in():
    with _client() as client:
        reg_resp = _post_register(client, "deactivated_user_case", PlatformRole.NORMAL.value)
        assert reg_resp.status_code == 200

        login_resp = _login_user(client, "deactivated_user_case", "pass1234")
        assert login_resp.status_code == 200

        user = AuthUser.query.filter_by(username="deactivated_user_case").first()
        assert user is not None
        success, error = toggle_user_active(user.id)
        assert success is True
        assert error is None

        me_resp = client.get("/auth/api/me", headers={"Accept": "application/json"})
        me_data = me_resp.get_json(silent=True) or {}
        assert me_resp.status_code in (200, 401)
        if me_resp.status_code == 200:
            assert me_data.get("logged_in") is False

        protected_resp = client.get("/auth/change-password", follow_redirects=False)
        assert protected_resp.status_code in (301, 302)
        assert "/auth/login" in protected_resp.headers.get("Location", "")


def test_login_does_not_500_when_auth_tables_missing():
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_db.close()
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{tmp_db.name}"
    reset_sqlalchemy_engine_cache(app)

    with app.app_context():
        runtime_uri = str(db.engine.url)
        assert_destructive_db_allowed(
            database_uri=runtime_uri,
            action_name="tests/test_auth_debug_register_mode.py::test_login_does_not_500_when_auth_tables_missing",
            testing=True,
        )
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
        try:
            os.unlink(tmp_db.name)
        except OSError:
            pass
