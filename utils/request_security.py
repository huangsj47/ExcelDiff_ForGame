#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Request security helpers for admin auth and CSRF checks.

已扩展支持多级角色权限系统（平台管理员 / 项目管理员 / 普通用户）。
通过 AuthProvider 接口实现解耦，同时保持向后兼容。
"""

import hmac
import os
import secrets
from functools import wraps
from urllib.parse import urlparse

from flask import flash, jsonify, redirect, request, session, url_for


CSRF_SESSION_KEY = "_csrf_token"
ENABLE_ADMIN_SECURITY = True


def configure_request_security(*, csrf_session_key: str, enable_admin_security: bool) -> None:
    """Configure runtime switches for security helper behavior."""
    global CSRF_SESSION_KEY, ENABLE_ADMIN_SECURITY
    CSRF_SESSION_KEY = csrf_session_key
    ENABLE_ADMIN_SECURITY = enable_admin_security


def csrf_token():
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


# Admin page routes that render HTML templates (not JSON APIs)
_ADMIN_PAGE_ROUTES = frozenset({
    "/admin/excel-cache",
    "/admin/agents",
    "/admin/performance",
})


def _is_admin_page_route():
    """Check if the current request is for an admin HTML page (not an API endpoint)."""
    return request.path in _ADMIN_PAGE_ROUTES


def _is_api_request():
    # If the browser is requesting an admin HTML page, treat it as a page request
    if _is_admin_page_route():
        accept = request.headers.get("Accept", "")
        # Only treat as API if explicitly requesting JSON
        if request.is_json or "application/json" in accept:
            return True
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return True
        return False

    accept = request.headers.get("Accept", "")
    return (
        request.path.startswith("/api/")
        or request.path.startswith("/admin/")
        or request.is_json
        or "application/json" in accept
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
    )


def _is_valid_admin_token():
    expected = os.environ.get("ADMIN_API_TOKEN", "").strip()
    provided = (request.headers.get("X-Admin-Token") or "").strip()
    return bool(expected and provided and hmac.compare_digest(expected, provided))


def _has_admin_access():
    """判断当前请求是否拥有平台管理员权限。

    优先使用 AuthProvider（数据库用户），回退到 Session / API Token 兼容逻辑。
    """
    # 1. API Token 兼容（无需 Provider）
    if _is_valid_admin_token():
        return True

    # 2. 尝试通过 AuthProvider 判断
    try:
        from auth import get_auth_provider
        provider = get_auth_provider()
        if provider.has_platform_admin_access():
            return True
    except (RuntimeError, ImportError):
        pass

    # 3. 回退到原始 Session 兼容
    return bool(session.get("is_admin"))


def _is_logged_in():
    """判断当前请求是否已登录（任意角色）。"""
    try:
        from auth import get_auth_provider
        provider = get_auth_provider()
        return provider.is_logged_in()
    except (RuntimeError, ImportError):
        return bool(session.get("auth_user_id") or session.get("is_admin"))


def _get_current_user():
    """获取当前已登录的用户对象，未登录返回 None。"""
    try:
        from auth import get_auth_provider
        provider = get_auth_provider()
        return provider.get_current_user()
    except (RuntimeError, ImportError):
        return None


def _has_project_admin_access(project_id):
    """判断当前用户是否为指定项目的管理员。

    平台管理员自动拥有所有项目的管理员权限。
    """
    if _has_admin_access():
        return True

    try:
        from auth import get_auth_provider
        provider = get_auth_provider()
        return provider.has_project_admin_access(project_id)
    except (RuntimeError, ImportError):
        return False


def _has_project_access(project_id):
    """判断当前用户是否拥有指定项目的访问权限（成员或管理员）。

    平台管理员自动拥有所有项目的访问权限。
    """
    if _has_admin_access():
        return True

    try:
        from auth import get_auth_provider
        provider = get_auth_provider()
        return provider.has_project_access(project_id)
    except (RuntimeError, ImportError):
        return False


def _normalize_identity_username(value):
    username = str(value or "").strip()
    if username.endswith("@corp.netease.com"):
        username = username.split("@", 1)[0]
    if "@" in username:
        username = username.split("@", 1)[0]
    return username.strip()


def _resolve_current_username():
    username = _normalize_identity_username(session.get("auth_username") or session.get("admin_user") or "")
    if username:
        return username

    user = _get_current_user()
    if user and getattr(user, "username", None):
        return _normalize_identity_username(user.username)
    return ""


def _normalize_confirm_action(action: str) -> str:
    value = str(action or "").strip().lower()
    if value in {"confirm", "confirmed", "approve"}:
        return "confirm"
    if value in {"reject", "rejected", "deny"}:
        return "reject"
    return ""


def _normalize_function_key(value: str) -> str:
    text = str(value or "").strip().lower()
    return " ".join(text.split())


def can_current_user_operate_project_confirmation(project_id, action: str):
    """判断当前用户是否有项目级确认/拒绝权限。

    返回 (allowed, message)。
    """
    normalized_action = _normalize_confirm_action(action)
    if not normalized_action:
        return False, "无效的操作类型"

    try:
        project_id = int(project_id)
    except (TypeError, ValueError):
        return False, "缺少项目信息，无法校验权限"

    # 平台管理员（含 ADMIN_API_TOKEN）始终放行，避免锁死管理能力。
    if _has_admin_access():
        return True, ""

    # 非平台管理员必须先具备项目可访问权限，避免跨项目越权确认/拒绝。
    if not _has_project_access(project_id):
        return False, "当前账号无权访问该项目"

    user = _get_current_user()
    if not user:
        return False, "请先登录后再执行该操作"

    try:
        from auth import get_auth_backend
        from models import db
    except Exception:
        return True, ""

    backend = "local"
    try:
        backend = get_auth_backend()
    except Exception:
        backend = "local"

    if backend == "qkit":
        try:
            from qkit_auth.models import QkitAuthUserProject, QkitProjectConfirmPermission
        except Exception:
            return True, ""

        rules = QkitProjectConfirmPermission.query.filter_by(project_id=project_id).all()
        if not rules:
            return True, ""

        memberships = QkitAuthUserProject.query.filter_by(user_id=user.id, project_id=project_id).all()
        user_function_keys = {
            _normalize_function_key(row.function_name)
            for row in memberships
            if getattr(row, "function_name", None)
        }
        user_function_keys.discard("")
        if not user_function_keys:
            return False, "当前账号在该项目没有职能信息，无法执行该操作"

        for row in rules:
            function_key = _normalize_function_key(getattr(row, "function_key", "") or getattr(row, "function_name", ""))
            if function_key not in user_function_keys:
                continue
            if normalized_action == "confirm" and bool(getattr(row, "allow_confirm", False)):
                return True, ""
            if normalized_action == "reject" and bool(getattr(row, "allow_reject", False)):
                return True, ""
        return False, "当前职能未被授权执行该操作"

    try:
        from auth.models import AuthProjectConfirmPermission, AuthUserFunction
    except Exception:
        return True, ""

    rules = AuthProjectConfirmPermission.query.filter_by(project_id=project_id).all()
    if not rules:
        return True, ""

    memberships = (
        AuthUserFunction.query
        .filter(
            AuthUserFunction.user_id == user.id,
            db.or_(
                AuthUserFunction.project_id == project_id,
                AuthUserFunction.project_id.is_(None),
            ),
        )
        .all()
    )
    function_ids = {row.function_id for row in memberships if row.function_id}
    if not function_ids:
        return False, "当前账号在该项目没有职能信息，无法执行该操作"

    for row in rules:
        if row.function_id not in function_ids:
            continue
        if normalized_action == "confirm" and bool(row.allow_confirm):
            return True, ""
        if normalized_action == "reject" and bool(row.allow_reject):
            return True, ""
    return False, "当前职能未被授权执行该操作"


def _get_project_create_agent_codes():
    """获取当前用户可直接创建项目并绑定的 Agent 节点代号列表。"""
    username = _resolve_current_username()
    if not username:
        return []

    normalized = username.lower()
    codes = set()
    try:
        from models import AgentNode

        # 兼容旧结构：agent_nodes.default_admin_username
        for row in AgentNode.query.all():
            if _normalize_identity_username(row.default_admin_username or "").lower() == normalized:
                if row.agent_code:
                    codes.add(row.agent_code)

        # 新结构：agent_default_admins（历史累计，不覆盖）
        try:
            from models import AgentDefaultAdmin

            rows = AgentDefaultAdmin.query.all()
            agent_ids = {
                row.agent_id
                for row in rows
                if _normalize_identity_username(row.username or "").lower() == normalized and row.agent_id
            }
            if agent_ids:
                agents = AgentNode.query.filter(AgentNode.id.in_(list(agent_ids))).all()
                for agent in agents:
                    if agent.agent_code:
                        codes.add(agent.agent_code)
        except Exception:
            pass
    except Exception:
        return []

    return sorted(codes)


def _has_project_create_access():
    """判断当前用户是否可直接创建项目（平台管理员或 Agent 默认管理员）。"""
    if _has_admin_access():
        return True
    return bool(_get_project_create_agent_codes())


def _get_accessible_project_ids():
    """获取当前用户可访问的所有项目 ID 列表。

    平台管理员返回 None（表示可访问所有项目）。
    未登录返回空列表。
    """
    if _has_admin_access():
        return None  # None = 全部可访问

    try:
        from auth import get_auth_provider
        provider = get_auth_provider()
        ids = provider.get_accessible_project_ids()
        # Provider 返回空列表时可能表示平台管理员或未登录
        if not ids and provider.has_platform_admin_access():
            return None
        return ids
    except (RuntimeError, ImportError):
        return []


def _unauthorized_admin_response():
    if _is_api_request():
        return jsonify({"success": False, "message": "Admin authentication required"}), 401
    # POST/PUT/DELETE 等非 GET 请求的 URL 不能作为登录后跳转目标（会导致 405）
    if request.method == "GET":
        next_url = request.url
    else:
        next_url = request.referrer or url_for("index")
    flash("请先使用管理员账号登录。", "error")
    # 优先跳转到新的登录页，回退到旧的 admin_login
    try:
        login_url = url_for("auth_bp.login", next=next_url)
    except Exception:
        login_url = url_for("admin_login", next=next_url)
    return redirect(login_url)


def _unauthorized_login_response():
    """未登录时的通用响应（跳转到登录页面）。"""
    if _is_api_request():
        return jsonify({"success": False, "message": "Authentication required"}), 401
    if request.method == "GET":
        next_url = request.url
    else:
        next_url = request.referrer or url_for("index")
    flash("请先登录。", "error")
    try:
        login_url = url_for("auth_bp.login", next=next_url)
    except Exception:
        login_url = url_for("admin_login", next=next_url)
    return redirect(login_url)


def _csrf_error_response(message):
    if _is_api_request():
        return jsonify({"success": False, "message": message}), 400
    flash(message, "error")
    return redirect(request.referrer or url_for("index"))


def _csrf_token_from_request():
    header_token = request.headers.get("X-CSRF-Token") or request.headers.get("X-CSRFToken")
    if header_token:
        return header_token
    form_token = request.form.get("_csrf_token")
    if form_token:
        return form_token
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        return payload.get("_csrf_token")
    return None


def _is_same_origin_request():
    expected_host = request.host
    origin = request.headers.get("Origin")
    if origin:
        parsed = urlparse(origin)
        return parsed.netloc == expected_host
    referer = request.headers.get("Referer")
    if referer:
        parsed = urlparse(referer)
        return parsed.netloc == expected_host
    return True


def _is_safe_redirect(target):
    if not target:
        return False
    parsed = urlparse(target)
    if parsed.scheme and parsed.scheme not in ("http", "https"):
        return False
    if parsed.netloc and parsed.netloc != request.host:
        return False
    return True


def require_admin(func):
    """要求平台管理员权限（向后兼容装饰器）。"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not ENABLE_ADMIN_SECURITY:
            return func(*args, **kwargs)
        if not _has_admin_access():
            return _unauthorized_admin_response()
        return func(*args, **kwargs)

    return wrapper


def require_login(func):
    """要求用户已登录（任意角色即可）。"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not ENABLE_ADMIN_SECURITY:
            return func(*args, **kwargs)
        if not _is_logged_in():
            return _unauthorized_login_response()
        return func(*args, **kwargs)

    return wrapper
