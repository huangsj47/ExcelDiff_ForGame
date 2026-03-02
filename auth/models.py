#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
账号系统数据模型

所有表名使用 ``auth_`` 前缀，与 Diff 平台业务表隔离。
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from models import db


# ──────────────────────────────── 枚举类型 ────────────────────────────────


class PlatformRole(str, enum.Enum):
    """平台级角色"""
    PLATFORM_ADMIN = "platform_admin"   # 平台管理员（超级管理员）
    PROJECT_ADMIN = "project_admin"     # 项目管理员（可管理其所属项目）
    NORMAL = "normal"                   # 普通用户

    def __str__(self) -> str:
        return self.value

    @property
    def display_name(self) -> str:
        _names = {
            "platform_admin": "平台管理员",
            "project_admin": "项目管理员",
            "normal": "普通用户",
        }
        return _names.get(self.value, self.value)


class ProjectRole(str, enum.Enum):
    """项目级角色（用户在某个项目中的角色）"""
    ADMIN = "admin"     # 项目管理员
    MEMBER = "member"   # 普通成员

    def __str__(self) -> str:
        return self.value

    @property
    def display_name(self) -> str:
        _names = {"admin": "项目管理员", "member": "普通成员"}
        return _names.get(self.value, self.value)


class RequestStatus(str, enum.Enum):
    """审批请求状态"""
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"

    def __str__(self) -> str:
        return self.value


# ──────────────────────────────── 用户表 ────────────────────────────────


class AuthUser(db.Model):
    """用户表"""
    __tablename__ = "auth_users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    display_name = db.Column(db.String(100), nullable=True)
    email = db.Column(db.String(200), nullable=True)
    role = db.Column(
        db.String(20),
        nullable=False,
        default=PlatformRole.NORMAL.value,
    )
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # 关系
    functions = db.relationship(
        "AuthUserFunction", back_populates="user", cascade="all, delete-orphan", lazy="dynamic"
    )
    projects = db.relationship(
        "AuthUserProject", back_populates="user", cascade="all, delete-orphan", lazy="dynamic"
    )

    def __repr__(self) -> str:
        return f"<AuthUser {self.username} [{self.role}]>"

    @property
    def platform_role(self) -> PlatformRole:
        try:
            return PlatformRole(self.role)
        except ValueError:
            return PlatformRole.NORMAL

    @property
    def is_platform_admin(self) -> bool:
        return self.platform_role == PlatformRole.PLATFORM_ADMIN

    def get_function_names(self) -> list[str]:
        """获取用户所有职能名称列表"""
        return [uf.function.name for uf in self.functions.all() if uf.function]

    def has_function(self, function_name: str) -> bool:
        """检查用户是否拥有某个职能"""
        return any(
            uf.function.name == function_name
            for uf in self.functions.all()
            if uf.function
        )

    def to_dict(self, include_functions: bool = False) -> dict:
        result = {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name or self.username,
            "email": self.email,
            "role": self.role,
            "role_display": self.platform_role.display_name,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if include_functions:
            result["functions"] = self.get_function_names()
        return result


# ──────────────────────────────── 职能表 ────────────────────────────────


class AuthFunction(db.Model):
    """职能定义表

    预设职能：主QA✦、QA、主策划、策划、主程序、程序、主美、美术、PM、其他、管理员
    其中 ``is_lead_qa == True`` 的职能会自动赋予项目管理员身份。
    """
    __tablename__ = "auth_functions"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    description = db.Column(db.String(200), nullable=True)
    is_system = db.Column(db.Boolean, default=False, nullable=False)
    is_lead_qa = db.Column(db.Boolean, default=False, nullable=False)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # 关系
    users = db.relationship(
        "AuthUserFunction", back_populates="function", cascade="all, delete-orphan", lazy="dynamic"
    )

    def __repr__(self) -> str:
        return f"<AuthFunction {self.name}>"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "is_system": self.is_system,
            "is_lead_qa": self.is_lead_qa,
            "sort_order": self.sort_order,
        }


# ────────────────────────── 用户-职能关联表 ──────────────────────────


class AuthUserFunction(db.Model):
    """用户-职能 多对多关联表"""
    __tablename__ = "auth_user_functions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=False)
    function_id = db.Column(db.Integer, db.ForeignKey("auth_functions.id", ondelete="CASCADE"), nullable=False)
    # 职能在哪个项目中生效（None 表示全局）
    project_id = db.Column(db.Integer, db.ForeignKey("project.id", ondelete="CASCADE"), nullable=True)
    assigned_by = db.Column(db.Integer, db.ForeignKey("auth_users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.UniqueConstraint("user_id", "function_id", "project_id", name="uq_user_function_project"),
    )

    # 关系
    user = db.relationship("AuthUser", back_populates="functions", foreign_keys=[user_id])
    function = db.relationship("AuthFunction", back_populates="users")
    assigner = db.relationship("AuthUser", foreign_keys=[assigned_by])

    def __repr__(self) -> str:
        return f"<AuthUserFunction user={self.user_id} func={self.function_id} project={self.project_id}>"


# ────────────────────────── 用户-项目关联表 ──────────────────────────


class AuthUserProject(db.Model):
    """用户-项目 归属关联表

    ``role`` 字段标识用户在该项目中的角色（admin / member）。
    """
    __tablename__ = "auth_user_projects"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id", ondelete="CASCADE"), nullable=False)
    role = db.Column(
        db.String(20),
        nullable=False,
        default=ProjectRole.MEMBER.value,
    )
    joined_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    approved_by = db.Column(db.Integer, db.ForeignKey("auth_users.id"), nullable=True)

    __table_args__ = (
        db.UniqueConstraint("user_id", "project_id", name="uq_user_project"),
    )

    # 关系
    user = db.relationship("AuthUser", back_populates="projects", foreign_keys=[user_id])
    project = db.relationship("Project", backref=db.backref("auth_members", lazy="dynamic"))
    approver = db.relationship("AuthUser", foreign_keys=[approved_by])

    @property
    def project_role(self) -> ProjectRole:
        try:
            return ProjectRole(self.role)
        except ValueError:
            return ProjectRole.MEMBER

    @property
    def is_project_admin(self) -> bool:
        return self.project_role == ProjectRole.ADMIN

    def __repr__(self) -> str:
        return f"<AuthUserProject user={self.user_id} project={self.project_id} role={self.role}>"


# ────────────────────────── 项目加入申请表 ──────────────────────────


class AuthProjectJoinRequest(db.Model):
    """项目加入申请表"""
    __tablename__ = "auth_project_join_requests"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id", ondelete="CASCADE"), nullable=False)
    message = db.Column(db.String(500), nullable=True)
    status = db.Column(
        db.String(20),
        nullable=False,
        default=RequestStatus.PENDING.value,
    )
    handled_by = db.Column(db.Integer, db.ForeignKey("auth_users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    handled_at = db.Column(db.DateTime, nullable=True)

    # 关系
    user = db.relationship("AuthUser", foreign_keys=[user_id])
    project = db.relationship("Project")
    handler = db.relationship("AuthUser", foreign_keys=[handled_by])

    def __repr__(self) -> str:
        return f"<AuthProjectJoinRequest user={self.user_id} project={self.project_id} status={self.status}>"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "user_name": self.user.display_name or self.user.username if self.user else None,
            "project_id": self.project_id,
            "project_name": self.project.name if self.project else None,
            "message": self.message,
            "status": self.status,
            "handled_by": self.handled_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "handled_at": self.handled_at.isoformat() if self.handled_at else None,
        }


# ────────────────────────── 项目创建申请表 ──────────────────────────


class AuthProjectCreateRequest(db.Model):
    """项目创建申请表

    非管理员用户可以通过此表「申请新项目」，
    平台管理员审批通过后自动创建项目，申请人成为项目管理员。
    """
    __tablename__ = "auth_project_create_requests"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=False)
    project_code = db.Column(db.String(50), nullable=False)
    project_name = db.Column(db.String(100), nullable=False)
    department = db.Column(db.String(100), nullable=True)
    reason = db.Column(db.String(500), nullable=True)
    status = db.Column(
        db.String(20),
        nullable=False,
        default=RequestStatus.PENDING.value,
    )
    handled_by = db.Column(db.Integer, db.ForeignKey("auth_users.id"), nullable=True)
    # 审批通过后创建的项目 ID
    created_project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    handled_at = db.Column(db.DateTime, nullable=True)

    # 关系
    user = db.relationship("AuthUser", foreign_keys=[user_id])
    handler = db.relationship("AuthUser", foreign_keys=[handled_by])
    created_project = db.relationship("Project", foreign_keys=[created_project_id])

    def __repr__(self) -> str:
        return (
            f"<AuthProjectCreateRequest user={self.user_id} "
            f"code={self.project_code} status={self.status}>"
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "user_name": self.user.display_name or self.user.username if self.user else None,
            "project_code": self.project_code,
            "project_name": self.project_name,
            "department": self.department,
            "reason": self.reason,
            "status": self.status,
            "handled_by": self.handled_by,
            "created_project_id": self.created_project_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "handled_at": self.handled_at.isoformat() if self.handled_at else None,
        }


# ──────────────────────────── 默认职能数据 ────────────────────────────

DEFAULT_FUNCTIONS: list[dict] = [
    {"name": "主QA✦", "description": "主测试（自动获得项目管理员权限）", "is_system": True, "is_lead_qa": True, "sort_order": 1},
    {"name": "QA", "description": "测试", "is_system": True, "is_lead_qa": False, "sort_order": 2},
    {"name": "主策划", "description": "主策划", "is_system": True, "is_lead_qa": False, "sort_order": 3},
    {"name": "策划", "description": "策划", "is_system": True, "is_lead_qa": False, "sort_order": 4},
    {"name": "主程序", "description": "主程序", "is_system": True, "is_lead_qa": False, "sort_order": 5},
    {"name": "程序", "description": "程序", "is_system": True, "is_lead_qa": False, "sort_order": 6},
    {"name": "主美", "description": "主美术", "is_system": True, "is_lead_qa": False, "sort_order": 7},
    {"name": "美术", "description": "美术", "is_system": True, "is_lead_qa": False, "sort_order": 8},
    {"name": "PM", "description": "项目经理", "is_system": True, "is_lead_qa": False, "sort_order": 9},
    {"name": "其他", "description": "其他职能", "is_system": True, "is_lead_qa": False, "sort_order": 10},
    {"name": "管理员", "description": "管理员", "is_system": True, "is_lead_qa": False, "sort_order": 11},
]
