#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Qkit auth provider."""

from __future__ import annotations

from flask import g, request, session

from auth.providers import AuthProvider
from qkit_auth.services import check_qkit_jwt_remote, get_user_by_id
from utils.logger import log_print


class QkitAuthProvider(AuthProvider):
    """Qkit 登录态提供者。

    设计原则：
    - 每次请求必须调用一次 AUTH_CHECK_JWT_API（通过 request-scope 缓存避免重复调用）
    - 校验失败立即判定未登录
    """

    def _clear_auth_session(self) -> None:
        for key in (
            "auth_user_id",
            "auth_username",
            "auth_role",
            "is_admin",
            "admin_user",
            "auth_backend",
            "qkit_backhost",
            "qkitjwt_session",
        ):
            session.pop(key, None)

    def _check_current_request_login(self) -> bool:
        cached = getattr(g, "_qkit_login_valid", None)
        if cached is not None:
            return bool(cached)

        user_id = session.get("auth_user_id")
        if not user_id:
            g._qkit_login_valid = False
            return False

        token = (request.cookies.get("qkitjwt", "") or session.get("qkitjwt_session", "")).strip()
        if not token:
            self._clear_auth_session()
            g._qkit_login_valid = False
            return False

        valid, message, _payload = check_qkit_jwt_remote(token)
        if not valid:
            log_print(f"Qkit 会话校验失败: {message or 'unknown'}", "AUTH", force=True)
            self._clear_auth_session()
            g._qkit_login_valid = False
            return False

        user = get_user_by_id(int(user_id))
        if not user or not user.is_active:
            self._clear_auth_session()
            g._qkit_login_valid = False
            return False

        # 与数据库角色保持同步
        session["auth_username"] = user.username
        session["auth_role"] = user.role
        session["is_admin"] = bool(user.is_platform_admin)
        session["admin_user"] = user.username if user.is_platform_admin else None
        g._qkit_login_valid = True
        return True

    def _get_active_user(self):
        if not self._check_current_request_login():
            return None
        user_id = session.get("auth_user_id")
        if not user_id:
            return None
        return get_user_by_id(int(user_id))

    # 兼容抽象接口
    def authenticate(self, username: str, password: str):
        return None

    def get_current_user(self):
        return self._get_active_user()

    def is_logged_in(self) -> bool:
        return self._check_current_request_login()

    def has_platform_admin_access(self) -> bool:
        user = self._get_active_user()
        if not user:
            return False
        return bool(user.is_platform_admin)

    def has_project_admin_access(self, project_id: int) -> bool:
        user = self._get_active_user()
        if not user:
            return False
        if user.is_platform_admin:
            return True

        from qkit_auth.models import QkitAuthUserProject

        membership = QkitAuthUserProject.query.filter_by(
            user_id=user.id,
            project_id=project_id,
        ).first()
        if not membership:
            return False
        return membership.is_project_admin

    def has_project_access(self, project_id: int) -> bool:
        user = self._get_active_user()
        if not user:
            return False
        if user.is_platform_admin:
            return True

        from qkit_auth.models import QkitAuthUserProject

        membership = QkitAuthUserProject.query.filter_by(
            user_id=user.id,
            project_id=project_id,
        ).first()
        return membership is not None

    def get_accessible_project_ids(self) -> list[int]:
        user = self._get_active_user()
        if not user:
            return []
        if user.is_platform_admin:
            return []

        from qkit_auth.models import QkitAuthUserProject

        rows = QkitAuthUserProject.query.filter_by(user_id=user.id).all()
        return [row.project_id for row in rows]
