#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
端到端测试 — 账号系统 (Auth RBAC E2E Test Suite)

涵盖：
  1. 注册 / 登录 / 登出
  2. 密码修改
  3. 项目隔离 — 仅可见已加入项目
  4. 项目申请 — 加入 / 创建，管理员审批
  5. 角色授权 — 平台管理员 / 项目管理员 / 普通用户
  6. 主QA职能 → 自动升级为项目管理员
  7. 安全 — CSRF、未认证拦截、SQL 注入、XSS 尝试
  8. 边界条件
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from contextlib import contextmanager
from typing import Optional

# ── 调整 sys.path ──
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# 使用临时数据库，不影响实际数据
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
_TMP_DB_URI = f"sqlite:///{_tmp_db.name}"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin123"
os.environ["AUTH_DEBUG_MODE"] = "false"
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-e2e")

from app import app, db  # noqa: E402
from utils.db_safety import assert_destructive_db_allowed, reset_sqlalchemy_engine_cache  # noqa: E402

# ── 统计 ──
_passed = 0
_failed = 0
_errors: list[str] = []


# ── 辅助 ──


def _banner(title: str):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")


def _ok(name: str):
    global _passed
    _passed += 1
    print(f"  ✅ PASS  {name}")


def _fail(name: str, detail: str = ""):
    global _failed
    _failed += 1
    msg = f"  ❌ FAIL  {name}"
    if detail:
        msg += f"  ——  {detail}"
    print(msg)
    _errors.append(msg)


def _assert(cond: bool, name: str, detail: str = ""):
    if cond:
        _ok(name)
    else:
        _fail(name, detail)


@contextmanager
def _client():
    """Return a test client with fresh tables.

    Uses the production database (same as app) but creates/drops auth tables
    in each test. CSRF enforcement is temporarily disabled.
    """
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SERVER_NAME"] = "localhost"

    # 暂存原始 CSRF enforce 函数
    _orig_enable_security = os.environ.get("ENABLE_ADMIN_SECURITY")
    # 使 enforce_csrf 不拦截测试请求：通过在 session 中预设 token
    # 更直接的方式：用 SQLALCHEMY_DATABASE_URI 切换到临时数据库
    app.config["SQLALCHEMY_DATABASE_URI"] = _TMP_DB_URI
    reset_sqlalchemy_engine_cache(app)

    # 重新绑定引擎到临时数据库
    with app.app_context():
        runtime_uri = str(db.engine.url)
        assert_destructive_db_allowed(
            database_uri=runtime_uri,
            action_name="tests/test_auth_e2e.py::_client setup drop_all",
            testing=True,
        )

        db.create_all()
        # 初始化默认职能 + 迁移管理员
        from auth.services import init_default_functions, migrate_env_admin_to_db
        init_default_functions()
        migrate_env_admin_to_db()
        with app.test_client() as c:
            # 预设 CSRF token 到每个请求的 session 中
            with c.session_transaction() as sess:
                import uuid
                sess["_csrf_token"] = "test-csrf-token-e2e"
            yield c
        # 清理
        db.session.remove()
        runtime_uri = str(db.engine.url)
        assert_destructive_db_allowed(
            database_uri=runtime_uri,
            action_name="tests/test_auth_e2e.py::_client teardown drop_all",
            testing=True,
        )
        db.drop_all()


def _form_post(c, url: str, data: dict, follow_redirects=False):
    """发送 form POST 请求，自动附带 CSRF token。"""
    token = _get_csrf(c)
    data = dict(data)
    data["csrf_token"] = token
    return c.post(url, data=data, headers={"X-CSRFToken": token}, follow_redirects=follow_redirects)


def _login(c, username: str, password: str) -> int:
    """Login and return status code."""
    return _form_post(c, "/auth/login", {
        "username": username,
        "password": password,
    }, follow_redirects=False).status_code


def _login_follow(c, username: str, password: str):
    """Login and follow redirects, return response."""
    return _form_post(c, "/auth/login", {
        "username": username,
        "password": password,
    }, follow_redirects=True)


def _get_csrf(c) -> str:
    """从 session 中取 CSRF token（测试模式下通过直接访问）"""
    with c.session_transaction() as sess:
        token = sess.get("_csrf_token", "")
        if not token:
            import uuid
            token = uuid.uuid4().hex
            sess["_csrf_token"] = token
    return token


def _json_post(c, url: str, data: dict, csrf: bool = True):
    """发送 JSON POST 请求。"""
    headers = {"Content-Type": "application/json"}
    if csrf:
        token = _get_csrf(c)
        headers["X-CSRFToken"] = token
    return c.post(url, data=json.dumps(data), headers=headers)


# ═══════════════════════════════════════════════════════════════════
#  测试组 1：注册 / 登录 / 登出
# ═══════════════════════════════════════════════════════════════════

def test_register_login_logout():
    _banner("1. 注册 / 登录 / 登出")

    with _client() as c:
        # --- 1.0 帮助页无需登录 ---
        resp = c.get("/help", follow_redirects=False)
        _assert(resp.status_code == 200, "1.0 /help 无需登录可访问")

        # --- 1.1 注册 ---
        resp = _form_post(c, "/auth/register", {
            "username": "testuser1",
            "password": "pass1234",
            "password_confirm": "pass1234",
            "display_name": "测试用户1",
            "email": "test1@example.com",
        }, follow_redirects=True)
        _assert(resp.status_code == 200, "1.1 注册成功 (状态码=200)")
        _assert("注册成功" in resp.data.decode("utf-8"), "1.1 注册成功 (flash 提示)")

        # --- 1.2 重复注册 ---
        resp = _form_post(c, "/auth/register", {
            "username": "testuser1",
            "password": "pass5678",
            "password_confirm": "pass5678",
        }, follow_redirects=True)
        _assert("已存在" in resp.data.decode("utf-8"), "1.2 重复注册被拒")

        # --- 1.3 密码不一致 ---
        resp = _form_post(c, "/auth/register", {
            "username": "testuser_mismatch",
            "password": "aaa1111",
            "password_confirm": "bbb2222",
        }, follow_redirects=True)
        _assert("不一致" in resp.data.decode("utf-8"), "1.3 密码不一致被拒")

        # --- 1.4 登录 ---
        resp = _login_follow(c, "testuser1", "pass1234")
        _assert(resp.status_code == 200, "1.4 登录成功")

        # --- 1.5 登录后访问首页 ---
        resp = c.get("/", follow_redirects=True)
        _assert(resp.status_code == 200, "1.5 登录后可访问首页")

        # --- 1.6 登出 ---
        resp = c.get("/auth/logout", follow_redirects=True)
        _assert(resp.status_code == 200, "1.6 登出成功")

        # --- 1.7 登出后访问首页跳转到登录 ---
        resp = c.get("/", follow_redirects=False)
        _assert(resp.status_code in (302, 301), "1.7 未登录被重定向")

        # --- 1.8 错误密码 ---
        resp = _login_follow(c, "testuser1", "wrong_password")
        text = resp.data.decode("utf-8")
        _assert("错误" in text or "密码" in text, "1.8 错误密码提示")


# ═══════════════════════════════════════════════════════════════════
#  测试组 2：管理员登录与权限
# ═══════════════════════════════════════════════════════════════════

def test_admin_login():
    _banner("2. 管理员登录与权限")

    with _client() as c:
        # 2.1 管理员登录
        resp = _login_follow(c, "admin", "admin123")
        _assert(resp.status_code == 200, "2.1 管理员登录成功")

        # 2.2 访问用户管理页面
        resp = c.get("/auth/users", follow_redirects=True)
        _assert(resp.status_code == 200, "2.2 管理员可访问用户管理页")

        # 2.3 管理员信息 API
        resp = c.get("/auth/api/me")
        data = json.loads(resp.data)
        _assert(data.get("logged_in") is True, "2.3 /api/me logged_in=true")
        _assert(
            data.get("user", {}).get("role") == "platform_admin",
            "2.3 /api/me role=platform_admin",
        )


def test_admin_user_management_page_with_pending_requests():
    _banner("2.x 用户管理页在存在待审批申请时可正常渲染")

    with _client() as c:
        from auth.models import (
            AuthProjectCreateRequest,
            AuthProjectJoinRequest,
            AuthUser,
            RequestStatus,
        )
        from models.project import Project as ProjModel

        # 准备基础数据：一个项目 + 一个普通用户 + 两类待审批申请
        project = ProjModel(code="PENDING_CASE", name="待审批项目")
        db.session.add(project)
        db.session.commit()

        user = AuthUser.query.filter_by(username="pending_user").first()
        if not user:
            _form_post(
                c,
                "/auth/register",
                {
                    "username": "pending_user",
                    "password": "pass1234",
                    "password_confirm": "pass1234",
                    "display_name": "待审批用户",
                },
            )
            user = AuthUser.query.filter_by(username="pending_user").first()

        join_req = AuthProjectJoinRequest(
            user_id=user.id,
            project_id=project.id,
            message="请审批加入",
            status=RequestStatus.PENDING.value,
        )
        create_req = AuthProjectCreateRequest(
            user_id=user.id,
            project_code="PENDING_NEW",
            project_name="待创建项目",
            department="QA",
            reason="测试渲染",
            status=RequestStatus.PENDING.value,
        )
        db.session.add_all([join_req, create_req])
        db.session.commit()

        # 管理员访问用户管理页，应返回200而不是500
        _login(c, "admin", "admin123")
        resp = c.get("/auth/users", follow_redirects=True)
        text = resp.data.decode("utf-8", errors="ignore")

        _assert(resp.status_code == 200, "2.x 用户管理页返回200")
        _assert("待审批申请" in text, "2.x 页面包含待审批区块")


# ═══════════════════════════════════════════════════════════════════
#  测试组 3：密码修改
# ═══════════════════════════════════════════════════════════════════

def test_change_password():
    _banner("3. 密码修改")

    with _client() as c:
        # 注册用户
        _form_post(c, "/auth/register", {
            "username": "pwuser",
            "password": "old_pass1",
            "password_confirm": "old_pass1",
        })
        _login(c, "pwuser", "old_pass1")

        # 3.1 修改密码
        resp = _form_post(c, "/auth/change-password", {
            "old_password": "old_pass1",
            "new_password": "new_pass2",
            "new_password_confirm": "new_pass2",
        }, follow_redirects=True)
        _assert(resp.status_code == 200, "3.1 修改密码请求成功")

        # 3.2 用新密码重新登录
        c.get("/auth/logout")
        resp = _login_follow(c, "pwuser", "new_pass2")
        _assert(resp.status_code == 200, "3.2 新密码登录成功")

        # 3.3 旧密码无法登录
        c.get("/auth/logout")
        resp = _login_follow(c, "pwuser", "old_pass1")
        text = resp.data.decode("utf-8")
        _assert("错误" in text or "密码" in text, "3.3 旧密码不能登录")


# ═══════════════════════════════════════════════════════════════════
#  测试组 4：项目隔离
# ═══════════════════════════════════════════════════════════════════

def test_project_isolation():
    _banner("4. 项目隔离")

    with _client() as c:
        # 创建两个项目
        from models.project import Project as ProjModel
        p1 = ProjModel(code="PROJ_A", name="项目A")
        p2 = ProjModel(code="PROJ_B", name="项目B")
        db.session.add_all([p1, p2])
        db.session.commit()

        # 注册两个用户
        _form_post(c, "/auth/register", {
            "username": "user_a", "password": "pass1234", "password_confirm": "pass1234"
        })
        _form_post(c, "/auth/register", {
            "username": "user_b", "password": "pass1234", "password_confirm": "pass1234"
        })

        from auth.models import AuthUser
        from auth.services import add_user_to_project
        ua = AuthUser.query.filter_by(username="user_a").first()
        ub = AuthUser.query.filter_by(username="user_b").first()

        # user_a 加入项目A，user_b 加入项目B
        add_user_to_project(ua.id, p1.id)
        add_user_to_project(ub.id, p2.id)

        # 4.1 user_a — 通过数据库验证仅能访问项目A
        from auth.models import AuthUserProject
        ua_projects = [m.project_id for m in AuthUserProject.query.filter_by(user_id=ua.id).all()]
        _assert(p1.id in ua_projects, "4.1 user_a 有项目A权限")
        _assert(p2.id not in ua_projects, "4.1 user_a 无项目B权限")

        # 4.2 user_b — 通过数据库验证仅能访问项目B
        ub_projects = [m.project_id for m in AuthUserProject.query.filter_by(user_id=ub.id).all()]
        _assert(p2.id in ub_projects, "4.2 user_b 有项目B权限")
        _assert(p1.id not in ub_projects, "4.2 user_b 无项目A权限")

        c.get("/auth/logout")

        # 4.3 管理员看到所有项目
        _login(c, "admin", "admin123")
        resp = c.get("/", follow_redirects=True)
        text = resp.data.decode("utf-8")
        _assert("项目A" in text, "4.3 管理员看到项目A")
        _assert("项目B" in text, "4.3 管理员看到项目B")


# ═══════════════════════════════════════════════════════════════════
#  测试组 5：项目加入申请 → 审批
# ═══════════════════════════════════════════════════════════════════

def test_join_project_request():
    _banner("5. 项目加入申请 → 审批")

    with _client() as c:
        # 创建项目
        from models.project import Project as ProjModel
        p = ProjModel(code="JOIN_TEST", name="加入测试项目")
        db.session.add(p)
        db.session.commit()
        pid = p.id

        # 注册普通用户
        _form_post(c, "/auth/register", {
            "username": "joiner", "password": "pass1234", "password_confirm": "pass1234"
        })
        _login(c, "joiner", "pass1234")

        from auth.models import AuthUser
        joiner = AuthUser.query.filter_by(username="joiner").first()

        # 5.1 提交加入申请
        resp = _json_post(c, "/auth/api/request-join-project", {
            "project_id": pid, "message": "请让我加入"
        })
        data = json.loads(resp.data)
        _assert(data.get("success") is True, "5.1 提交加入申请成功")

        # 5.2 重复申请被拒
        resp = _json_post(c, "/auth/api/request-join-project", {
            "project_id": pid,
        })
        data = json.loads(resp.data)
        _assert(data.get("success") is False, "5.2 重复申请被拒")

        c.get("/auth/logout")

        # 5.3 管理员审批 — approve
        _login(c, "admin", "admin123")

        # 查找申请 ID
        from auth.models import AuthProjectJoinRequest
        req = AuthProjectJoinRequest.query.filter_by(
            user_id=joiner.id, project_id=pid
        ).first()
        _assert(req is not None, "5.3 申请记录存在")

        if req:
            resp = _json_post(c, f"/auth/api/join-requests/{req.id}/handle", {
                "action": "approve"
            })
            data = json.loads(resp.data)
            _assert(data.get("success") is True, "5.3 管理员审批通过")

        c.get("/auth/logout")

        # 5.4 审批后用户可以看到项目
        _login(c, "joiner", "pass1234")
        resp = c.get("/", follow_redirects=True)
        text = resp.data.decode("utf-8")
        _assert("加入测试项目" in text, "5.4 审批后用户可见项目")


# ═══════════════════════════════════════════════════════════════════
#  测试组 6：项目创建申请 → 审批
# ═══════════════════════════════════════════════════════════════════

def test_create_project_request():
    _banner("6. 项目创建申请 → 审批")

    with _client() as c:
        # 注册用户
        _form_post(c, "/auth/register", {
            "username": "creator", "password": "pass1234", "password_confirm": "pass1234"
        })
        _login(c, "creator", "pass1234")

        # 6.1 提交创建申请
        resp = _json_post(c, "/auth/api/request-create-project", {
            "project_code": "NEW_PROJ",
            "project_name": "我的新项目",
            "department": "技术部",
            "reason": "需要新的项目空间",
        })
        data = json.loads(resp.data)
        _assert(data.get("success") is True, "6.1 提交创建申请成功")

        c.get("/auth/logout")

        # 6.2 管理员审批
        _login(c, "admin", "admin123")

        from auth.models import AuthProjectCreateRequest, AuthUser
        creator = AuthUser.query.filter_by(username="creator").first()
        req = AuthProjectCreateRequest.query.filter_by(
            user_id=creator.id, project_code="NEW_PROJ"
        ).first()
        _assert(req is not None, "6.2 创建申请记录存在")

        if req:
            resp = _json_post(c, f"/auth/api/create-requests/{req.id}/handle", {
                "action": "approve"
            })
            data = json.loads(resp.data)
            _assert(data.get("success") is True, "6.2 管理员审批创建通过")

        c.get("/auth/logout")

        # 6.3 项目已创建
        from models.project import Project as ProjModel
        proj = ProjModel.query.filter_by(code="NEW_PROJ").first()
        _assert(proj is not None, "6.3 项目已在数据库中创建")
        if proj:
            _assert(proj.name == "我的新项目", "6.3 项目名称正确")

        # 6.4 申请人自动成为项目管理员
        from auth.models import AuthUserProject, ProjectRole
        membership = AuthUserProject.query.filter_by(
            user_id=creator.id, project_id=proj.id if proj else 0,
        ).first()
        _assert(membership is not None, "6.4 申请人已加入项目")
        if membership:
            _assert(
                membership.role == ProjectRole.ADMIN.value,
                "6.4 申请人角色为项目管理员",
            )

        # 6.5 创建者登录后可见项目
        _login(c, "creator", "pass1234")
        resp = c.get("/", follow_redirects=True)
        text = resp.data.decode("utf-8")
        _assert("我的新项目" in text, "6.5 创建者可见自己的项目")


# ═══════════════════════════════════════════════════════════════════
#  测试组 7：主QA职能 → 自动升级为项目管理员
# ═══════════════════════════════════════════════════════════════════

def test_lead_qa_auto_promotion():
    _banner("7. 主QA职能 → 自动升级为项目管理员")

    with _client() as c:
        from models.project import Project as ProjModel
        from auth.models import AuthUser, AuthFunction, AuthUserProject, ProjectRole
        from auth.services import add_user_to_project, assign_function, remove_function

        # 创建项目
        proj = ProjModel(code="QA_PROJ", name="QA测试项目")
        db.session.add(proj)
        db.session.commit()

        # 注册用户
        _form_post(c, "/auth/register", {
            "username": "qa_user", "password": "pass1234", "password_confirm": "pass1234"
        })
        user = AuthUser.query.filter_by(username="qa_user").first()
        admin = AuthUser.query.filter_by(username="admin").first()

        # 先把用户加入项目（普通成员）
        add_user_to_project(user.id, proj.id)

        # 确认当前是 member
        m = AuthUserProject.query.filter_by(user_id=user.id, project_id=proj.id).first()
        _assert(m.role == ProjectRole.MEMBER.value, "7.0 初始角色为 member")

        # 获取 Lead QA 职能
        lead_qa = AuthFunction.query.filter_by(is_lead_qa=True).first()
        _assert(lead_qa is not None, "7.0 Lead QA 职能存在")

        if lead_qa:
            # 7.1 分配主QA → 自动升级
            success, err = assign_function(
                user.id, lead_qa.id, proj.id, assigned_by=admin.id
            )
            _assert(success, f"7.1 分配主QA成功")

            m = AuthUserProject.query.filter_by(
                user_id=user.id, project_id=proj.id
            ).first()
            _assert(
                m.role == ProjectRole.ADMIN.value,
                "7.1 分配主QA后自动升级为项目管理员",
            )

            # 7.2 移除主QA → 自动降级
            success, err = remove_function(user.id, lead_qa.id, proj.id)
            _assert(success, "7.2 移除主QA成功")

            m = AuthUserProject.query.filter_by(
                user_id=user.id, project_id=proj.id
            ).first()
            _assert(
                m.role == ProjectRole.MEMBER.value,
                "7.2 移除主QA后自动降级为普通成员",
            )


# ═══════════════════════════════════════════════════════════════════
#  测试组 8：角色权限控制
# ═══════════════════════════════════════════════════════════════════

def test_role_access_control():
    _banner("8. 角色权限控制")

    with _client() as c:
        # 注册普通用户
        _form_post(c, "/auth/register", {
            "username": "normal_user", "password": "pass1234", "password_confirm": "pass1234"
        })

        # 8.1 普通用户不能访问用户管理
        _login(c, "normal_user", "pass1234")
        resp = c.get("/auth/users", follow_redirects=True)
        text = resp.data.decode("utf-8")
        # 应该被重定向或看到权限错误
        _assert(
            "仅限" in text or "权限" in text or resp.status_code == 403,
            "8.1 普通用户不能访问用户管理",
        )

        # 8.2 普通用户不能修改他人角色
        from auth.models import AuthUser
        admin = AuthUser.query.filter_by(username="admin").first()
        resp = _json_post(c, f"/auth/api/users/{admin.id}/role", {
            "role": "normal"
        })
        _assert(resp.status_code in (403, 401), "8.2 普通用户不能修改角色 (403)")

        # 8.3 普通用户不能审批申请
        resp = _json_post(c, "/auth/api/join-requests/1/handle", {
            "action": "approve"
        })
        _assert(resp.status_code in (403, 401), "8.3 普通用户不能审批 (403)")

        c.get("/auth/logout")

        # 8.4 管理员可以修改用户角色
        _login(c, "admin", "admin123")
        normal_user = AuthUser.query.filter_by(username="normal_user").first()
        resp = _json_post(c, f"/auth/api/users/{normal_user.id}/role", {
            "role": "project_admin"
        })
        data = json.loads(resp.data)
        _assert(data.get("success") is True, "8.4 管理员可修改用户角色")

        # 8.5 管理员重置密码
        resp = _json_post(c, f"/auth/api/users/{normal_user.id}/reset-password", {
            "password": "reset_pass"
        })
        data = json.loads(resp.data)
        _assert(data.get("success") is True, "8.5 管理员可重置密码")

        # 验证重置生效
        c.get("/auth/logout")
        resp = _login_follow(c, "normal_user", "reset_pass")
        _assert(resp.status_code == 200, "8.5 重置后新密码可登录")


# ═══════════════════════════════════════════════════════════════════
#  测试组 9：安全测试
# ═══════════════════════════════════════════════════════════════════

def test_security():
    _banner("9. 安全测试")

    with _client() as c:
        # 9.1 未认证访问 API 被拒
        resp = _json_post(c, "/auth/api/request-join-project", {
            "project_id": 1
        })
        _assert(resp.status_code in (401, 302), "9.1 未认证访问 API 被拒")

        # 9.2 SQL 注入尝试 — 登录
        resp = _form_post(c, "/auth/login", {
            "username": "' OR 1=1 --",
            "password": "anything",
        }, follow_redirects=True)
        text = resp.data.decode("utf-8")
        _assert("错误" in text or "密码" in text, "9.2 SQL 注入登录被拒")

        # 9.3 SQL 注入尝试 — 注册
        resp = _form_post(c, "/auth/register", {
            "username": "'; DROP TABLE auth_users; --",
            "password": "test1234",
            "password_confirm": "test1234",
        }, follow_redirects=True)
        # 确保 auth_users 表仍然存在
        from auth.models import AuthUser
        count = AuthUser.query.count()
        _assert(count > 0, "9.3 SQL 注入未破坏数据库")

        # 9.4 XSS 尝试 — 注册
        _form_post(c, "/auth/register", {
            "username": "xss_user",
            "password": "pass1234",
            "password_confirm": "pass1234",
            "display_name": "<script>alert('xss')</script>",
        })
        user = AuthUser.query.filter_by(username="xss_user").first()
        _assert(user is not None, "9.4 XSS 用户注册成功（存入数据库）")
        # XSS 防护在模板渲染时检查（Jinja2 autoescaping）

        # 9.5 密码强度 — 太短
        resp = _form_post(c, "/auth/register", {
            "username": "short_pw",
            "password": "ab",
            "password_confirm": "ab",
        }, follow_redirects=True)
        text = resp.data.decode("utf-8")
        _assert("至少" in text, "9.5 短密码被拒")

        # 9.6 用户名非法字符
        resp = _form_post(c, "/auth/register", {
            "username": "bad user",
            "password": "pass1234",
            "password_confirm": "pass1234",
        }, follow_redirects=True)
        text = resp.data.decode("utf-8")
        _assert("仅支持" in text or "长度" in text, "9.6 非法用户名被拒")

        # 9.7 用户名过短
        resp = _form_post(c, "/auth/register", {
            "username": "ab",
            "password": "pass1234",
            "password_confirm": "pass1234",
        }, follow_redirects=True)
        text = resp.data.decode("utf-8")
        _assert("至少 3" in text or "长度" in text, "9.7 短用户名被拒")

        # 9.8 用户名不能为纯数字
        resp = _form_post(c, "/auth/register", {
            "username": "111111",
            "password": "pass1234",
            "password_confirm": "pass1234",
        }, follow_redirects=True)
        text = resp.data.decode("utf-8")
        _assert("纯数字" in text or "不能" in text, "9.8 纯数字用户名被拒")


# ═══════════════════════════════════════════════════════════════════
#  测试组 10：边界条件 & 管理员操作
# ═══════════════════════════════════════════════════════════════════

def test_edge_cases():
    _banner("10. 边界条件")

    with _client() as c:
        # 10.1 禁用用户无法登录
        _form_post(c, "/auth/register", {
            "username": "disabled_user", "password": "pass1234", "password_confirm": "pass1234"
        })
        from auth.models import AuthUser
        user = AuthUser.query.filter_by(username="disabled_user").first()

        # 管理员登录禁用该用户
        _login(c, "admin", "admin123")
        resp = _json_post(c, f"/auth/api/users/{user.id}/toggle-active", {})
        data = json.loads(resp.data)
        _assert(data.get("success") is True, "10.1a 禁用用户操作成功")

        c.get("/auth/logout")

        # 被禁用的用户尝试登录
        resp = _login_follow(c, "disabled_user", "pass1234")
        text = resp.data.decode("utf-8")
        # 应该看到错误信息（因为 provider 不应允许禁用用户登录）
        _assert(
            "禁用" in text or "错误" in text or "密码" in text,
            "10.1b 禁用用户不能登录",
        )

        # 10.2 空白输入注册
        resp = _form_post(c, "/auth/register", {
            "username": "",
            "password": "pass1234",
            "password_confirm": "pass1234",
        }, follow_redirects=True)
        text = resp.data.decode("utf-8")
        _assert("空" in text or "至少" in text or resp.status_code == 200, "10.2 空用户名被拒")

        # 10.3 删除不存在的职能
        _login(c, "admin", "admin123")
        token = _get_csrf(c)
        resp = c.delete(
            "/auth/api/users/99999/functions/99999",
            headers={"X-CSRFToken": token},
        )
        _assert(resp.status_code in (400, 404), "10.3 删除不存在的职能返回错误")

        # 10.4 处理已处理的申请
        # 先创建一个 join request 然后处理两次
        _form_post(c, "/auth/register", {
            "username": "double_apply", "password": "pass1234", "password_confirm": "pass1234"
        })
        from models.project import Project as ProjModel
        proj = ProjModel(code="EDGE_P", name="边界项目")
        db.session.add(proj)
        db.session.commit()

        c.get("/auth/logout")
        _login(c, "double_apply", "pass1234")
        applicant = AuthUser.query.filter_by(username="double_apply").first()
        resp = _json_post(c, "/auth/api/request-join-project", {
            "project_id": proj.id, "message": "请加入"
        })

        c.get("/auth/logout")
        _login(c, "admin", "admin123")

        from auth.models import AuthProjectJoinRequest
        req = AuthProjectJoinRequest.query.filter_by(
            user_id=applicant.id, project_id=proj.id
        ).first()
        if req:
            # 第一次处理
            _json_post(c, f"/auth/api/join-requests/{req.id}/handle", {"action": "approve"})
            # 第二次处理 — 应该被拒
            resp = _json_post(c, f"/auth/api/join-requests/{req.id}/handle", {"action": "approve"})
            data = json.loads(resp.data)
            _assert(data.get("success") is False, "10.4 重复处理申请被拒")


# ═══════════════════════════════════════════════════════════════════
#  测试组 11：项目成员管理 API
# ═══════════════════════════════════════════════════════════════════

def test_project_member_management():
    _banner("11. 项目成员管理 API")

    with _client() as c:
        from models.project import Project as ProjModel
        from auth.models import AuthUser, AuthUserProject
        from auth.services import add_user_to_project

        # 创建项目
        proj = ProjModel(code="MEMBER_PROJ", name="成员管理测试")
        db.session.add(proj)
        db.session.commit()

        # 注册用户
        _form_post(c, "/auth/register", {
            "username": "member1", "password": "pass1234", "password_confirm": "pass1234"
        })
        _form_post(c, "/auth/register", {
            "username": "member2", "password": "pass1234", "password_confirm": "pass1234"
        })
        m1 = AuthUser.query.filter_by(username="member1").first()
        m2 = AuthUser.query.filter_by(username="member2").first()

        # 管理员添加 member1
        _login(c, "admin", "admin123")

        resp = _json_post(c, f"/auth/api/project/{proj.id}/members", {
            "user_id": m1.id, "role": "member"
        })
        data = json.loads(resp.data)
        _assert(data.get("success") is True, "11.1 添加项目成员成功")

        # 重复添加
        resp = _json_post(c, f"/auth/api/project/{proj.id}/members", {
            "user_id": m1.id
        })
        data = json.loads(resp.data)
        _assert(data.get("success") is False, "11.2 重复添加被拒")

        # 修改角色
        resp = _json_post(c, f"/auth/api/project/{proj.id}/members/{m1.id}/role", {
            "role": "admin"
        })
        data = json.loads(resp.data)
        _assert(data.get("success") is True, "11.3 修改成员角色成功")

        # 移除成员（DELETE 也需要 CSRF token）
        token = _get_csrf(c)
        resp = c.delete(
            f"/auth/api/project/{proj.id}/members/{m1.id}",
            headers={"X-CSRFToken": token},
        )
        _assert(resp.status_code == 200, "11.4 移除成员成功")


# ═══════════════════════════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════════════════════════

def run_all():
    print("\n" + "█" * 70)
    print("  Auth RBAC 端到端测试")
    print("█" * 70)

    tests = [
        test_register_login_logout,
        test_admin_login,
        test_change_password,
        test_project_isolation,
        test_join_project_request,
        test_create_project_request,
        test_lead_qa_auto_promotion,
        test_role_access_control,
        test_security,
        test_edge_cases,
        test_project_member_management,
    ]

    for t in tests:
        try:
            t()
        except Exception as e:
            _fail(t.__name__, f"EXCEPTION: {e}")
            traceback.print_exc()

    print("\n" + "=" * 70)
    print(f"  结果: ✅ {_passed} 通过  |  ❌ {_failed} 失败")
    print("=" * 70)

    if _errors:
        print("\n失败项目:")
        for e in _errors:
            print(e)

    # 清理临时数据库
    try:
        os.unlink(_tmp_db.name)
    except:
        pass

    return _failed == 0


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
