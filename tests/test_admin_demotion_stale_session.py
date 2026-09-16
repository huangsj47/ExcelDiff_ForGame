#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ACCESS-001 — 数据库管理员被降级后，旧会话不得再拿回管理员权限。

## 缺陷

登录时 `DatabaseAuthProvider.authenticate` 会把当时的角色快照写进 session：

    session["is_admin"] = user.is_platform_admin        # auth/providers.py:181

这份快照**没有随数据库角色变化而失效**。于是被降级的数据库用户，其旧 cookie 仍带着
`is_admin=True`，而它会被两条互相独立的路径当成「平台管理员」：

  1. `EnvAuthProvider.has_platform_admin_access()` 直接读 `session.get("is_admin")`
     （auth/providers.py:113-121）。该 Provider 本意只服务 .env 超级管理员，但它读的是
     **两种会话共用的同一个 key**，于是把数据库用户的快照也算成了环境变量管理员；
     `CompositeAuthProvider.has_platform_admin_access()` 又是
     `primary or fallback`（auth/providers.py:296），primary 判否之后 fallback 判真
     —— 整条链返回 True。
  2. `utils/request_security.py::_has_admin_access()` 的第 3 级兜底
     `return bool(session.get("is_admin"))`（utils/request_security.py:97）。

有了管理员身份，被降级者就能调用 `POST /auth/api/users/<自己>/role`
（auth/routes.py:270）把自己改回 platform_admin —— 提权闭环。

本文件同时覆盖两个方向：
  * 数据库用户：降级 / 禁用后，旧会话必须立即失去管理员权限；
  * 环境变量管理员：AUTH_BACKEND 回退模式是既有合法能力，不得被误伤。
"""

from __future__ import annotations

import os
import sys
import uuid
from contextlib import contextmanager

import pytest
from flask import session as flask_session

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import utils.request_security as security  # noqa: E402
from app import app, create_tables, db  # noqa: E402
from auth import get_auth_provider  # noqa: E402
from auth.models import AuthUser  # noqa: E402
from auth.providers import EnvAuthProvider  # noqa: E402
from auth.services import register_user  # noqa: E402

PASSWORD = "pw-123456"
ENV_ADMIN_USER = "envadmin-access-001"
ENV_ADMIN_PASSWORD = "env-pw-123456"


def _uid(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:10]}"


def _csrf(client) -> str:
    with client.session_transaction() as sess:
        token = sess.get("_csrf_token", "")
        if not token:
            token = uuid.uuid4().hex
            sess["_csrf_token"] = token
    return token


def _login(client, username: str, password: str):
    token = _csrf(client)
    return client.post(
        "/auth/login",
        data={"username": username, "password": password, "csrf_token": token},
        headers={"X-CSRFToken": token},
        follow_redirects=False,
    )


def _set_role_via_api(client, user_id: int, role: str):
    token = _csrf(client)
    return client.post(
        f"/auth/api/users/{user_id}/role",
        json={"role": role},
        headers={"X-CSRFToken": token},
    )


def _toggle_active_via_api(client, user_id: int):
    token = _csrf(client)
    return client.post(
        f"/auth/api/users/{user_id}/toggle-active",
        json={},
        headers={"X-CSRFToken": token},
    )


def _role_in_db(user_id: int) -> str:
    with app.app_context():
        db.session.expire_all()
        return db.session.get(AuthUser, user_id).role


def _is_active_in_db(user_id: int) -> bool:
    with app.app_context():
        db.session.expire_all()
        return db.session.get(AuthUser, user_id).is_active


@contextmanager
def _act_as_db_session(user_id: int, username: str, is_admin_snapshot: bool):
    """构造一个请求上下文，session 内容等同于该数据库用户登录时留下的旧会话。"""
    # 带上 Accept: application/json，让权限失败走 API 分支返回 403 而不是重定向
    with app.test_request_context(headers={"Accept": "application/json"}):
        flask_session["auth_user_id"] = user_id
        flask_session["auth_username"] = username
        flask_session["auth_role"] = "platform_admin" if is_admin_snapshot else "normal"
        flask_session["is_admin"] = is_admin_snapshot
        flask_session["admin_user"] = username if is_admin_snapshot else None
        yield


@contextmanager
def _act_as_env_admin_session(username: str = ENV_ADMIN_USER):
    """构造一个请求上下文，session 内容等同于环境变量管理员登录后留下的会话。"""
    with app.test_request_context(headers={"Accept": "application/json"}):
        flask_session["auth_user_id"] = None
        flask_session["auth_username"] = username
        flask_session["auth_role"] = "platform_admin"
        flask_session["is_admin"] = True
        flask_session["admin_user"] = username
        yield


@pytest.fixture
def accounts():
    """建两个平台管理员：victim（将被降级）与 other（执行降级的管理员）。"""
    victim_name, other_name = _uid("victim"), _uid("otheradm")
    with app.app_context():
        create_tables()
        victim, err = register_user(victim_name, PASSWORD, role="platform_admin")
        assert victim is not None, err
        other, err = register_user(other_name, PASSWORD, role="platform_admin")
        assert other is not None, err
        return {
            "victim": {"name": victim_name, "password": PASSWORD, "id": victim.id},
            "other": {"name": other_name, "password": PASSWORD, "id": other.id},
        }


@pytest.fixture
def demoted_old_session(accounts):
    """victim 以管理员登录 → 被 other 降级为普通成员。

    返回 (victim_client, other_client, accounts)。victim_client 保留的是**旧会话**，
    从未重新登录。
    """
    victim, other = accounts["victim"], accounts["other"]
    victim_client = app.test_client()
    other_client = app.test_client()

    assert _login(victim_client, victim["name"], victim["password"]).status_code == 302
    assert _login(other_client, other["name"], other["password"]).status_code == 302
    assert _set_role_via_api(other_client, victim["id"], "normal").status_code == 200
    assert _role_in_db(victim["id"]) == "normal"

    # 前提确认：旧会话 cookie 里确实残留着登录时的管理员快照
    with victim_client.session_transaction() as sess:
        assert sess.get("is_admin") is True
        assert sess.get("auth_user_id") == victim["id"]

    yield victim_client, other_client, accounts


# ──────────────────────────── 主缺陷：旧会话自升权 ────────────────────────────


def test_demoted_admin_old_session_cannot_self_promote(demoted_old_session):
    """核心复现：被降级管理员的**旧会话**调用改角色接口改自己，必须 403 且角色不变。"""
    victim_client, _, accounts = demoted_old_session
    victim = accounts["victim"]

    resp = _set_role_via_api(victim_client, victim["id"], "platform_admin")

    assert resp.status_code == 403, (
        "被降级的数据库管理员用旧会话自升权成功（期望 403，实际 "
        f"{resp.status_code}）: {resp.get_data(as_text=True)}"
    )
    assert _role_in_db(victim["id"]) == "normal", "旧会话仍改动了数据库角色"


def test_demoted_admin_new_session_cannot_self_promote(accounts):
    """对照组：降级后**重新登录**的新会话本来就该 403。

    没有这条，「旧会话被正确拒绝」与「新会话本来就 403」无法区分。
    """
    victim, other = accounts["victim"], accounts["other"]
    other_client = app.test_client()

    assert _login(other_client, other["name"], other["password"]).status_code == 302
    assert _set_role_via_api(other_client, victim["id"], "normal").status_code == 200

    fresh_client = app.test_client()
    assert _login(fresh_client, victim["name"], victim["password"]).status_code == 302
    with fresh_client.session_transaction() as sess:
        # 新会话的快照本身就是 False —— 说明对照组走的是另一条判定路径
        assert sess.get("is_admin") is False

    resp = _set_role_via_api(fresh_client, victim["id"], "platform_admin")
    assert resp.status_code == 403, resp.get_data(as_text=True)
    assert _role_in_db(victim["id"]) == "normal"


def test_demoted_admin_old_session_loses_admin_page(demoted_old_session):
    """旧会话不能只在这一条接口上被拦：管理员页面同样必须拒绝。"""
    victim_client, _, _ = demoted_old_session

    resp = victim_client.get("/admin/excel-cache", follow_redirects=False)
    assert resp.status_code == 403, (
        f"被降级的旧会话仍被放行到管理员页面（实际 {resp.status_code}）"
    )


# ────────────────── 提权路径 1：组合 Provider 的 fallback 误判 ──────────────────


def test_composite_provider_ignores_db_user_session_marker(demoted_old_session):
    """`CompositeAuthProvider` 不得把数据库用户的 is_admin 快照当成环境变量管理员。

    单独钉住 auth/providers.py:296 + auth/providers.py:113-121 这一跳。
    """
    _, _, accounts = demoted_old_session
    victim = accounts["victim"]

    with _act_as_db_session(victim["id"], victim["name"], is_admin_snapshot=True):
        assert EnvAuthProvider().has_platform_admin_access() is False, (
            "EnvAuthProvider 把数据库用户的会话快照当成了环境变量管理员"
        )
        assert get_auth_provider().has_platform_admin_access() is False, (
            "组合 Provider 的 or 短路让旧会话仍被判为平台管理员"
        )


def test_env_admin_session_still_platform_admin(monkeypatch):
    """环境变量管理员（AUTH_BACKEND 回退模式）必须保持可用 —— 这是既有合法能力。"""
    monkeypatch.setenv("ADMIN_USERNAME", ENV_ADMIN_USER)
    monkeypatch.setenv("ADMIN_PASSWORD", ENV_ADMIN_PASSWORD)

    client = app.test_client()
    resp = _login(client, ENV_ADMIN_USER, ENV_ADMIN_PASSWORD)
    assert resp.status_code == 302, resp.get_data(as_text=True)

    with client.session_transaction() as sess:
        assert sess.get("is_admin") is True
        assert sess.get("auth_user_id") is None

    with _act_as_env_admin_session():
        assert EnvAuthProvider().has_platform_admin_access() is True
        assert get_auth_provider().has_platform_admin_access() is True

    resp = client.get("/admin/excel-cache", follow_redirects=False)
    assert resp.status_code == 200, f"环境变量管理员被误伤，管理员页面返回 {resp.status_code}"


# ────────────── 提权路径 2：request_security 的 session 兜底 ──────────────


def test_session_fallback_does_not_grant_db_user_admin(demoted_old_session, monkeypatch):
    """AuthProvider 不可用时，session 兜底也不得把数据库用户当成管理员。

    把 `get_auth_provider` 换成抛 RuntimeError（`_has_admin_access` 会吞掉并落到第 3 级
    兜底），单独验证 `utils/request_security.py:97` 这一跳。
    """
    _, _, accounts = demoted_old_session
    victim = accounts["victim"]

    def _broken_provider():
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr("auth.get_auth_provider", _broken_provider)

    with _act_as_db_session(victim["id"], victim["name"], is_admin_snapshot=True):
        assert security._has_admin_access() is False, (
            "session 兜底仍把被降级数据库用户的旧会话判为管理员"
        )


def test_session_fallback_keeps_env_admin(monkeypatch):
    """同一个兜底分支必须继续放行环境变量管理员会话。"""
    monkeypatch.setenv("ADMIN_USERNAME", ENV_ADMIN_USER)
    monkeypatch.setenv("ADMIN_PASSWORD", ENV_ADMIN_PASSWORD)

    def _broken_provider():
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr("auth.get_auth_provider", _broken_provider)

    with _act_as_env_admin_session():
        assert security._has_admin_access() is True


# ───────── 提权路径 3：auth/decorators.py 的 require_role 角色快照 ─────────


def _run_require_role(*roles):
    """在请求上下文里跑一次 @require_role 包裹的视图，返回 (响应, 状态码)。"""
    from auth.decorators import require_role

    @require_role(*roles)
    def _view():
        return "view-ran"

    result = _view()
    if isinstance(result, tuple):
        return result[0], result[1]
    return result, 200


def test_require_role_rejects_demoted_old_session(demoted_old_session):
    """`require_role` 原先读 session 里的 auth_role 快照，同样会放过被降级者。"""
    _, _, accounts = demoted_old_session
    victim = accounts["victim"]

    with _act_as_db_session(victim["id"], victim["name"], is_admin_snapshot=True):
        # 旧会话里 auth_role 仍是降级前的 platform_admin
        assert flask_session.get("auth_role") == "platform_admin"
        body, status = _run_require_role("platform_admin")
        assert status == 403, f"被降级者仍通过 require_role: {body!r}"


def test_require_role_allows_current_db_admin(accounts):
    """当前仍是平台管理员的数据库用户必须照常通过。"""
    admin = accounts["other"]
    with _act_as_db_session(admin["id"], admin["name"], is_admin_snapshot=True):
        assert _run_require_role("platform_admin") == ("view-ran", 200)


def test_require_role_allows_env_admin(monkeypatch):
    """环境变量管理员会话必须照常通过（既有合法能力）。"""
    monkeypatch.setenv("ADMIN_USERNAME", ENV_ADMIN_USER)
    monkeypatch.setenv("ADMIN_PASSWORD", ENV_ADMIN_PASSWORD)
    with _act_as_env_admin_session():
        assert _run_require_role("platform_admin") == ("view-ran", 200)


# ──────────────────────── 顺带核实：登出 / 禁用 ────────────────────────


def test_logout_clears_admin_marker(accounts):
    """登出必须清掉 is_admin 快照，不能留给下一个使用者。"""
    victim = accounts["victim"]
    client = app.test_client()

    assert _login(client, victim["name"], victim["password"]).status_code == 302
    with client.session_transaction() as sess:
        assert sess.get("is_admin") is True

    client.get("/auth/logout", follow_redirects=False)

    with client.session_transaction() as sess:
        assert not sess.get("is_admin")
        assert not sess.get("auth_user_id")

    resp = client.get("/admin/excel-cache", follow_redirects=False)
    assert resp.status_code in (302, 401), resp.status_code


def test_deactivated_admin_old_session_loses_admin(accounts):
    """被禁用的管理员，旧会话不得再访问管理员接口/页面。"""
    victim, other = accounts["victim"], accounts["other"]
    victim_client = app.test_client()
    other_client = app.test_client()

    assert _login(victim_client, victim["name"], victim["password"]).status_code == 302
    assert _login(other_client, other["name"], other["password"]).status_code == 302

    assert _toggle_active_via_api(other_client, victim["id"]).status_code == 200
    assert _is_active_in_db(victim["id"]) is False

    # 旧会话不得再能改任何人的角色（包括自己）。
    # 401 与 403 都可接受：被禁用后会话标记会被清掉，此时按「未登录」处理更贴切；
    # 关键是绝不能是 200，且数据库角色不得被改动。
    assert _set_role_via_api(victim_client, victim["id"], "platform_admin").status_code in (401, 403)
    assert _set_role_via_api(victim_client, other["id"], "normal").status_code in (401, 403)
    assert victim_client.get("/admin/excel-cache", follow_redirects=False).status_code in (302, 401, 403)
    assert _role_in_db(victim["id"]) == "platform_admin"  # 未被旧会话改动


def test_env_admin_after_db_admin_demotion_still_works(accounts, monkeypatch):
    """降级一个数据库用户，不得影响环境变量管理员会话的判定。"""
    monkeypatch.setenv("ADMIN_USERNAME", ENV_ADMIN_USER)
    monkeypatch.setenv("ADMIN_PASSWORD", ENV_ADMIN_PASSWORD)

    victim, other = accounts["victim"], accounts["other"]
    env_client = app.test_client()
    other_client = app.test_client()

    assert _login(env_client, ENV_ADMIN_USER, ENV_ADMIN_PASSWORD).status_code == 302
    assert _login(other_client, other["name"], other["password"]).status_code == 302
    assert _set_role_via_api(other_client, victim["id"], "normal").status_code == 200

    assert env_client.get("/admin/excel-cache", follow_redirects=False).status_code == 200
    resp = _set_role_via_api(env_client, victim["id"], "platform_admin")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert _role_in_db(victim["id"]) == "platform_admin"
