#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
权限装饰器 (Auth Decorators)

提供路由级别的权限控制装饰器，与 Provider 解耦。

使用方式:
    from auth.decorators import require_login, require_role, require_project_access

    @app.route('/some-page')
    @require_login
    def some_page():
        ...

    @app.route('/admin-only')
    @require_role('platform_admin')
    def admin_page():
        ...

    @app.route('/project/<int:project_id>/settings')
    @require_project_admin
    def project_settings(project_id):
        ...
"""

from __future__ import annotations

from functools import wraps

from flask import flash, jsonify, redirect, request, session, url_for

from .providers import AuthProvider


def _get_provider() -> AuthProvider:
    """获取当前 Auth Provider 实例。"""
    from . import get_auth_provider
    return get_auth_provider()


def _is_api_request() -> bool:
    """判断当前请求是否为 API 请求。"""
    from utils.request_security import _is_api_request as _is_api
    return _is_api()


def _is_document_navigation() -> bool:
    """当前请求是不是「打开一个页面」（判据与说明见 utils/request_security 里的同名函数）。"""
    from utils.request_security import _is_document_navigation as _is_navigation
    return _is_navigation()


def _build_next_url() -> str:
    """构造登录后的跳转目标 URL。"""
    if request.method == "GET":
        return request.url
    return request.referrer or url_for("index")


def _unauthorized_response(message: str = "请先登录"):
    """构造未认证响应（API 返回 JSON，页面重定向到登录页）。

    「flash + 跳登录页」只对**页面导航**成立：页面脚本发的请求（轮询、异步加载）
    走那条路会把 flash 写进 session cookie，而晚到的 cookie 会把用户在登录页刚
    消费掉的提示又写回去 —— 登录成功后第一个页面顶上冒出「请先登录。」。
    判据见 `utils.request_security._is_document_navigation`（没有 Fetch Metadata
    头的请求按导航处理，与改前一致）。
    """
    if _is_api_request() or not _is_document_navigation():
        return jsonify({"success": False, "message": message}), 401
    next_url = _build_next_url()
    flash(message, "error")
    return redirect(url_for("auth_bp.login", next=next_url))


def _forbidden_response(message: str = "权限不足"):
    """构造无权限响应。与 `_unauthorized_response` 同一个道理：脚本发的请求不走 flash。"""
    if _is_api_request() or not _is_document_navigation():
        return jsonify({"success": False, "message": message}), 403
    flash(message, "error")
    return redirect(request.referrer or url_for("index"))


# ──────────────────────────── 装饰器 ────────────────────────────


def require_login(func):
    """要求用户已登录。未登录则跳转到登录页面。"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        provider = _get_provider()
        if not provider.is_logged_in():
            return _unauthorized_response("请先登录。")
        return func(*args, **kwargs)
    return wrapper


def _current_platform_role():
    """当前用户的平台角色，以**数据库当前角色**为准；取不到时返回 None。

    ## 为什么不能直接读 `session["auth_role"]`

    那是登录那一刻的角色快照，用户被降级后不会跟着变 —— 直接读它等于让旧会话
    保留降级前的权限（与 `session["is_admin"]` 是同一类问题，见
    `auth/providers.py::is_env_admin_session` 的说明）。环境变量管理员在数据库里
    没有记录，仍然只能以会话标记为准，故保留一条显式兜底。
    """
    user = _get_provider().get_current_user()
    if user is not None:
        return user.role

    from .providers import is_env_admin_session
    if is_env_admin_session() and session.get("is_admin"):
        from .models import PlatformRole
        return PlatformRole.PLATFORM_ADMIN.value
    return None


def require_role(*roles: str):
    """要求用户拥有指定的平台角色之一。

    Args:
        roles: 一个或多个 PlatformRole 值，如 'platform_admin', 'project_admin'

    Usage:
        @require_role('platform_admin')
        def admin_only_view():
            ...

        @require_role('platform_admin', 'project_admin')
        def admin_or_project_admin_view():
            ...
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            provider = _get_provider()
            if not provider.is_logged_in():
                return _unauthorized_response("请先登录。")

            if _current_platform_role() not in roles:
                return _forbidden_response("您没有权限执行此操作。")

            return func(*args, **kwargs)
        return wrapper
    return decorator


def require_platform_admin(func):
    """要求用户为平台管理员。语法糖 = require_role('platform_admin')。"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        provider = _get_provider()
        if not provider.is_logged_in():
            return _unauthorized_response("请先登录。")
        if not provider.has_platform_admin_access():
            return _forbidden_response("此操作仅限平台管理员。")
        return func(*args, **kwargs)
    return wrapper


def require_project_access(func):
    """要求用户拥有指定项目的访问权限（成员或管理员）。

    路由函数必须包含 ``project_id`` 参数（URL 参数或关键字参数）。
    平台管理员自动通过。
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        provider = _get_provider()
        if not provider.is_logged_in():
            return _unauthorized_response("请先登录。")

        # 从 kwargs 或 view_args 中获取 project_id
        project_id = kwargs.get("project_id") or request.view_args.get("project_id")
        if project_id is None:
            # 尝试从请求参数中获取
            project_id = request.args.get("project_id", type=int)
        if project_id is None:
            return _forbidden_response("缺少项目 ID。")

        project_id = int(project_id)
        if not provider.has_project_access(project_id):
            return _forbidden_response("您没有该项目的访问权限。")

        return func(*args, **kwargs)
    return wrapper


def require_project_admin(func):
    """要求用户为指定项目的管理员。

    路由函数必须包含 ``project_id`` 参数。
    平台管理员自动通过。
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        provider = _get_provider()
        if not provider.is_logged_in():
            return _unauthorized_response("请先登录。")

        project_id = kwargs.get("project_id") or request.view_args.get("project_id")
        if project_id is None:
            project_id = request.args.get("project_id", type=int)
        if project_id is None:
            return _forbidden_response("缺少项目 ID。")

        project_id = int(project_id)
        if not provider.has_project_admin_access(project_id):
            return _forbidden_response("此操作仅限项目管理员。")

        return func(*args, **kwargs)
    return wrapper


def require_admin_or_project_admin(func):
    """要求用户为平台管理员 或 指定项目的管理员。

    路由函数需要包含 ``project_id``（可选），
    如果没有 project_id 则要求平台管理员。
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        provider = _get_provider()
        if not provider.is_logged_in():
            return _unauthorized_response("请先登录。")

        # 平台管理员直接放行
        if provider.has_platform_admin_access():
            return func(*args, **kwargs)

        # 尝试获取 project_id
        project_id = kwargs.get("project_id") or request.view_args.get("project_id")
        if project_id is None:
            project_id = request.args.get("project_id", type=int)

        if project_id is not None:
            project_id = int(project_id)
            if provider.has_project_admin_access(project_id):
                return func(*args, **kwargs)

        return _forbidden_response("此操作需要管理员权限。")
    return wrapper
