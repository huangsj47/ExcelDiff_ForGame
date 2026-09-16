#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
from contextlib import contextmanager

import auth.services as auth_services
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
from utils.db_safety import assert_destructive_db_allowed


@contextmanager
def _client():
    """每个用例一套干净的表。

    【为什么这里不再切换 SQLALCHEMY_DATABASE_URI】原实现每个用例新建一个临时
    sqlite 文件、改 `app.config["SQLALCHEMY_DATABASE_URI"]` 再调
    `reset_sqlalchemy_engine_cache(app)`，但那次切换**从来没有生效过**：
    `reset_sqlalchemy_engine_cache` 当时是个静默空操作（它判断
    `isinstance(app_engines, dict)`，而 Flask-SQLAlchemy 3.0.x 的 `_app_engines`
    是 `WeakKeyDictionary`，不是 dict 的子类）。于是这些用例实际一直跑在
    tests/conftest.py 隔离出来的临时库上，靠下面的 drop_all / create_all 拿到干净
    的初始状态 —— 这也确实是它们需要的隔离粒度。

    该函数现在被修成真正可用（重建引擎 = 重跑 FSA 的 init_app），但 init_app 会调用
    `app.shell_context_processor(...)` 这类 Flask setup 方法，而 Flask 禁止在首个请求
    之后再调用；`_client()` 是用例里每次都进的，第 2 个用例就会炸。所以这里明确改为
    「用 conftest 的隔离库 + 每用例重置表」这个**本来就生效**的策略，而不是继续写一个
    做不到的切换。
    """
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SERVER_NAME"] = "localhost"
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
    """认证表不存在时登录不应 500。

    本用例要的就是「表被删掉」这个状态：先 drop_all，再打登录接口，最后把表建回来。
    数据库沿用 tests/conftest.py 的隔离临时库（详见文件内 `_client()` 的说明 ——
    原先那套「运行时切换 URI」从来没生效过，而修好之后也不能在首个请求之后调用）。
    """
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False

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
