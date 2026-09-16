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
from sqlalchemy.exc import SQLAlchemyError
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


SESSION_AUTH_USER_ID_KEY = "auth_user_id"


def is_env_admin_session() -> bool:
    """当前会话是否为「环境变量管理员」会话（.env 超级管理员），而非数据库用户会话。

    ## 为什么授权判定必须先回答这个问题

    ``session["is_admin"]`` 是**登录那一刻的角色快照**
    （``DatabaseAuthProvider.authenticate`` 写入）。数据库用户的角色可以在会话存活
    期间被管理员改掉（``auth/services.py::update_user_role``），快照却不会跟着变。
    只要还有人拿这份快照当授权依据，被降级的用户就能用旧 cookie 继续当管理员 ——
    甚至把自己改回 platform_admin（该接口的守卫正是平台管理员判定）。

    环境变量管理员（.env）在数据库里没有记录，没有「当前角色」可查，只能以快照为准；
    所以「要不要信快照」这件事，只能靠**会话有没有绑定数据库用户**来区分：

    - 绑定（``auth_user_id`` 非空）：数据库用户会话 → 授权一律回查数据库，
      快照只当兼容标记，不参与判定；
    - 未绑定（``auth_user_id`` 为空/不存在）：环境变量管理员会话 → 快照即授权。

    这里的判据特意用「有没有 auth_user_id」而不是新加一个来源标记：
    ``DatabaseAuthProvider.authenticate`` 成功时必然写入该字段，
    ``EnvAuthProvider.authenticate`` 与历史 ``admin_login`` 也都会把它显式置空，
    两条链路的写入点都是现成的，不会出现标记与实际身份不一致的中间态。
    """
    return not session.get(SESSION_AUTH_USER_ID_KEY)


class EnvAuthProvider(AuthProvider):
    """环境变量认证提供者 — 兼容现有 .env 超级管理员逻辑。

    当数据库 Provider 找不到用户时，回退到此 Provider。

    注意：本 Provider 的四个 ``has_*`` 判定都只对**环境变量管理员会话**成立
    （见 ``is_env_admin_session``）。数据库用户的 session 里同样有 ``is_admin``，
    但那是过期快照 —— 早期实现直接读它，等于让 ``CompositeAuthProvider`` 的
    ``primary or fallback`` 把被降级的数据库用户又放回管理员，故现在必须过滤。
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
        return is_env_admin_session() and bool(session.get("is_admin"))

    def has_platform_admin_access(self) -> bool:
        return is_env_admin_session() and bool(session.get("is_admin"))

    def has_project_admin_access(self, project_id: int) -> bool:
        # 环境变量管理员拥有所有权限
        return is_env_admin_session() and bool(session.get("is_admin"))

    def has_project_access(self, project_id: int) -> bool:
        return is_env_admin_session() and bool(session.get("is_admin"))

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

    def _clear_auth_session(self) -> None:
        for key in ("auth_user_id", "auth_username", "auth_role", "is_admin", "admin_user"):
            session.pop(key, None)

    def _get_active_user_from_session(self) -> Optional["AuthUser"]:
        user_id = session.get("auth_user_id")
        if not user_id:
            return None

        AuthUser = self._get_user_model()
        try:
            user = AuthUser.query.filter_by(id=user_id, is_active=True).first()
        except SQLAlchemyError:
            return None

        if user is None:
            # Session user may be deactivated/deleted; clear stale auth markers.
            self._clear_auth_session()
            return None

        return user

    def _get_user_project_model(self):
        from .models import AuthUserProject
        return AuthUserProject

    def authenticate(self, username: str, password: str) -> Optional["AuthUser"]:
        AuthUser = self._get_user_model()
        try:
            user = AuthUser.query.filter_by(username=username, is_active=True).first()
        except SQLAlchemyError:
            # Keep login endpoint stable even if auth tables are temporarily unavailable.
            return None
        if user is None:
            return None
        if not check_password_hash(user.password_hash, password):
            return None

        # 写入 Session
        #
        # auth_user_id 是「本会话绑定到数据库用户」的唯一凭据：授权判定靠它区分
        # 数据库用户会话与环境变量管理员会话（见 is_env_admin_session）。写了它，
        # 后续每个请求的权限都回查数据库当前角色；下面的 is_admin 只是给模板/兼容
        # 代码看的登录快照，**不参与授权**，角色被改后它不会自动更新。
        session["auth_user_id"] = user.id
        session["auth_username"] = user.username
        session["auth_role"] = user.role
        # 向后兼容
        session["is_admin"] = user.is_platform_admin
        session["admin_user"] = user.username if user.is_platform_admin else None
        session.permanent = True

        # 应用预分配的项目成员关系
        try:
            from .services import apply_pre_assignments
            applied = apply_pre_assignments(user)
            if applied > 0:
                from utils.logger import log_print
                log_print(f"用户 {user.username} 登录时自动应用了 {applied} 条项目预分配", 'AUTH')
        except Exception:
            pass  # 预分配失败不影响登录

        return user

    def get_current_user(self) -> Optional["AuthUser"]:
        return self._get_active_user_from_session()

    def is_logged_in(self) -> bool:
        return self._get_active_user_from_session() is not None

    def has_platform_admin_access(self) -> bool:
        user = self._get_active_user_from_session()
        if user is None:
            return False
        return bool(user.is_platform_admin)

    def has_project_admin_access(self, project_id: int) -> bool:
        # 平台管理员拥有一切权限
        if self.has_platform_admin_access():
            return True

        user = self._get_active_user_from_session()
        if user is None:
            return False

        AuthUserProject = self._get_user_project_model()
        try:
            membership = AuthUserProject.query.filter_by(
                user_id=user.id, project_id=project_id
            ).first()
        except SQLAlchemyError:
            return False
        if membership is None:
            return False
        return membership.is_project_admin

    def has_project_access(self, project_id: int) -> bool:
        # 平台管理员可访问所有项目
        if self.has_platform_admin_access():
            return True

        user = self._get_active_user_from_session()
        if user is None:
            return False

        AuthUserProject = self._get_user_project_model()
        try:
            return AuthUserProject.query.filter_by(
                user_id=user.id, project_id=project_id
            ).first() is not None
        except SQLAlchemyError:
            return False

    def get_accessible_project_ids(self) -> list[int]:
        if self.has_platform_admin_access():
            return []  # 空列表 = 全部可访问

        user = self._get_active_user_from_session()
        if user is None:
            return []

        AuthUserProject = self._get_user_project_model()
        try:
            memberships = AuthUserProject.query.filter_by(user_id=user.id).all()
        except SQLAlchemyError:
            return []
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
