#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
认证提供者 (Auth Providers)

通过抽象接口 ``AuthProvider`` 实现解耦，后续可替换为 LDAP / OAuth 等后端。

层级：
    CompositeAuthProvider
        ├── DatabaseAuthProvider  (主)
        └── EnvAuthProvider       (兜底 — 兼容 .env 超级管理员)
"""

from __future__ import annotations

import hmac
import os
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional

from flask import session
from werkzeug.security import check_password_hash

if TYPE_CHECKING:
    from flask_sqlalchemy import SQLAlchemy

    from .models import AuthUser


class AuthProvider(ABC):
    """认证提供者抽象接口。

    所有认证与授权操作都通过此接口调用，
    更换认证后端只需实现新的 Provider 类即可。
    """

    # ── 认证 ──

    @abstractmethod
    def authenticate(self, username: str, password: str) -> Optional["AuthUser"]:
        """验证用户名与密码，返回用户对象或 None。"""

    @abstractmethod
    def get_current_user(self) -> Optional["AuthUser"]:
        """从当前 Session 中获取已登录用户，未登录返回 None。"""

    @abstractmethod
    def is_logged_in(self) -> bool:
        """判断当前请求是否已登录。"""

    # ── 授权 ──

    @abstractmethod
    def has_platform_admin_access(self) -> bool:
        """判断当前用户是否为平台管理员。"""

    @abstractmethod
    def has_project_admin_access(self, project_id: int) -> bool:
        """判断当前用户是否为指定项目的管理员。"""

    @abstractmethod
    def has_project_access(self, project_id: int) -> bool:
        """判断当前用户是否拥有指定项目的访问权限（成员或管理员）。"""

    @abstractmethod
    def get_accessible_project_ids(self) -> list[int]:
        """获取当前用户可访问的所有项目 ID 列表。

        平台管理员返回空列表（表示全部可访问，由调用方处理）。
        """


class EnvAuthProvider(AuthProvider):
    """环境变量认证提供者 — 兼容现有 .env 超级管理员逻辑。

    当数据库 Provider 找不到用户时，回退到此 Provider。
    """

    def authenticate(self, username: str, password: str) -> Optional["AuthUser"]:
        """使用 .env 中的 ADMIN_USERNAME / ADMIN_PASSWORD 认证。

        返回 None（因为环境变量管理员不存在于数据库中），
        但会在 Session 中设置 ``is_admin`` 标志。
        """
        configured_user = os.environ.get("ADMIN_USERNAME", "admin").strip()
        configured_password = os.environ.get("ADMIN_PASSWORD", "").strip()

        if not configured_password:
            return None

        if hmac.compare_digest(username, configured_user) and hmac.compare_digest(
            password, configured_password
        ):
            # 设置 Session 标志（向后兼容）
            session["is_admin"] = True
            session["admin_user"] = username
            session["auth_user_id"] = None  # 标记为环境变量管理员
            session["auth_username"] = username
            session["auth_role"] = "platform_admin"
            session.permanent = True
            return None  # 环境变量管理员没有数据库 User 对象

        return None

    def get_current_user(self) -> Optional["AuthUser"]:
        # 环境变量管理员不对应数据库用户
        return None

    def is_logged_in(self) -> bool:
        return bool(session.get("is_admin"))

    def has_platform_admin_access(self) -> bool:
        return bool(session.get("is_admin"))

    def has_project_admin_access(self, project_id: int) -> bool:
        # 环境变量管理员拥有所有权限
        return bool(session.get("is_admin"))

    def has_project_access(self, project_id: int) -> bool:
        return bool(session.get("is_admin"))

    def get_accessible_project_ids(self) -> list[int]:
        # 环境变量管理员可访问所有项目
        return []


class DatabaseAuthProvider(AuthProvider):
    """数据库认证提供者 — 本次主要实现。"""

    def __init__(self, db: "SQLAlchemy") -> None:
        self._db = db

    def _get_user_model(self):
        from .models import AuthUser
        return AuthUser

    def _get_user_project_model(self):
        from .models import AuthUserProject
        return AuthUserProject

    def authenticate(self, username: str, password: str) -> Optional["AuthUser"]:
        AuthUser = self._get_user_model()
        user = AuthUser.query.filter_by(username=username, is_active=True).first()
        if user is None:
            return None
        if not check_password_hash(user.password_hash, password):
            return None

        # 写入 Session
        session["auth_user_id"] = user.id
        session["auth_username"] = user.username
        session["auth_role"] = user.role
        # 向后兼容
        session["is_admin"] = user.is_platform_admin
        session["admin_user"] = user.username if user.is_platform_admin else None
        session.permanent = True

        return user

    def get_current_user(self) -> Optional["AuthUser"]:
        user_id = session.get("auth_user_id")
        if not user_id:
            return None
        AuthUser = self._get_user_model()
        return AuthUser.query.filter_by(id=user_id, is_active=True).first()

    def is_logged_in(self) -> bool:
        return session.get("auth_user_id") is not None

    def has_platform_admin_access(self) -> bool:
        return session.get("auth_role") == "platform_admin"

    def has_project_admin_access(self, project_id: int) -> bool:
        # 平台管理员拥有一切权限
        if self.has_platform_admin_access():
            return True

        user_id = session.get("auth_user_id")
        if not user_id:
            return False

        AuthUserProject = self._get_user_project_model()
        membership = AuthUserProject.query.filter_by(
            user_id=user_id, project_id=project_id
        ).first()
        if membership is None:
            return False
        return membership.is_project_admin

    def has_project_access(self, project_id: int) -> bool:
        # 平台管理员可访问所有项目
        if self.has_platform_admin_access():
            return True

        user_id = session.get("auth_user_id")
        if not user_id:
            return False

        AuthUserProject = self._get_user_project_model()
        return AuthUserProject.query.filter_by(
            user_id=user_id, project_id=project_id
        ).first() is not None

    def get_accessible_project_ids(self) -> list[int]:
        if self.has_platform_admin_access():
            return []  # 空列表 = 全部可访问

        user_id = session.get("auth_user_id")
        if not user_id:
            return []

        AuthUserProject = self._get_user_project_model()
        memberships = AuthUserProject.query.filter_by(user_id=user_id).all()
        return [m.project_id for m in memberships]


class CompositeAuthProvider(AuthProvider):
    """组合认证提供者 — 先走 primary，失败再走 fallback。

    默认 primary = DatabaseAuthProvider, fallback = EnvAuthProvider.
    """

    def __init__(self, primary: AuthProvider, fallback: AuthProvider) -> None:
        self.primary = primary
        self.fallback = fallback

    def authenticate(self, username: str, password: str) -> Optional["AuthUser"]:
        # 先尝试数据库认证
        user = self.primary.authenticate(username, password)
        if user is not None:
            return user

        # 检查是否数据库认证成功但返回 None（不应该发生）
        if self.primary.is_logged_in():
            return None

        # 回退到环境变量认证
        self.fallback.authenticate(username, password)
        return None  # EnvAuthProvider.authenticate 总是返回 None

    def get_current_user(self) -> Optional["AuthUser"]:
        user = self.primary.get_current_user()
        if user is not None:
            return user
        return self.fallback.get_current_user()

    def is_logged_in(self) -> bool:
        return self.primary.is_logged_in() or self.fallback.is_logged_in()

    def has_platform_admin_access(self) -> bool:
        return self.primary.has_platform_admin_access() or self.fallback.has_platform_admin_access()

    def has_project_admin_access(self, project_id: int) -> bool:
        return (
            self.primary.has_project_admin_access(project_id)
            or self.fallback.has_project_admin_access(project_id)
        )

    def has_project_access(self, project_id: int) -> bool:
        return (
            self.primary.has_project_access(project_id)
            or self.fallback.has_project_access(project_id)
        )

    def get_accessible_project_ids(self) -> list[int]:
        # 如果任一 Provider 是平台管理员，返回空列表（全部可访问）
        if self.has_platform_admin_access():
            return []
        return self.primary.get_accessible_project_ids()
