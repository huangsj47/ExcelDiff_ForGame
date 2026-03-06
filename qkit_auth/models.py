#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Qkit账号系统数据模型（与 local auth 数据隔离）。"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from models import db


class QkitPlatformRole(str, enum.Enum):
    PLATFORM_ADMIN = "platform_admin"
    PROJECT_ADMIN = "project_admin"
    NORMAL = "normal"

    @property
    def display_name(self) -> str:
        mapping = {
            self.PLATFORM_ADMIN.value: "平台管理员",
            self.PROJECT_ADMIN.value: "项目管理员",
            self.NORMAL.value: "普通用户",
        }
        return mapping.get(self.value, self.value)


class QkitProjectRole(str, enum.Enum):
    ADMIN = "admin"
    MEMBER = "member"

    @property
    def display_name(self) -> str:
        mapping = {
            self.ADMIN.value: "项目管理员",
            self.MEMBER.value: "成员",
        }
        return mapping.get(self.value, self.value)


class QkitRequestStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


class QkitImportBlockType(str, enum.Enum):
    REMOVED = "removed"


class QkitAuthUser(db.Model):
    __tablename__ = "qkit_auth_users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    display_name = db.Column(db.String(100), nullable=True)
    email = db.Column(db.String(200), nullable=True, index=True)
    role = db.Column(db.String(20), nullable=False, default=QkitPlatformRole.NORMAL.value)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    source = db.Column(db.String(30), nullable=False, default="qkit")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    projects = db.relationship(
        "QkitAuthUserProject",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="dynamic",
        foreign_keys="QkitAuthUserProject.user_id",
    )

    @property
    def platform_role(self) -> QkitPlatformRole:
        try:
            return QkitPlatformRole(self.role)
        except ValueError:
            return QkitPlatformRole.NORMAL

    @property
    def is_platform_admin(self) -> bool:
        return self.platform_role == QkitPlatformRole.PLATFORM_ADMIN

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name or self.username,
            "email": self.email,
            "role": self.role,
            "role_display": self.platform_role.display_name,
            "is_active": self.is_active,
            "source": self.source,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class QkitAuthUserProject(db.Model):
    __tablename__ = "qkit_auth_user_projects"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("qkit_auth_users.id", ondelete="CASCADE"), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id", ondelete="CASCADE"), nullable=False)
    role = db.Column(db.String(20), nullable=False, default=QkitProjectRole.MEMBER.value)
    function_name = db.Column(db.String(255), nullable=True)
    joined_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    approved_by = db.Column(db.Integer, db.ForeignKey("qkit_auth_users.id"), nullable=True)
    imported_from_qkit = db.Column(db.Boolean, default=False, nullable=False)
    import_sync_locked = db.Column(db.Boolean, default=False, nullable=False)
    import_last_synced_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        db.UniqueConstraint("user_id", "project_id", name="uq_qkit_auth_user_project"),
    )

    user = db.relationship("QkitAuthUser", back_populates="projects", foreign_keys=[user_id])
    project = db.relationship("Project", backref=db.backref("qkit_auth_members", lazy="dynamic"))
    approver = db.relationship("QkitAuthUser", foreign_keys=[approved_by])

    @property
    def project_role(self) -> QkitProjectRole:
        try:
            return QkitProjectRole(self.role)
        except ValueError:
            return QkitProjectRole.MEMBER

    @property
    def is_project_admin(self) -> bool:
        return self.project_role == QkitProjectRole.ADMIN

class QkitProjectConfirmPermission(db.Model):
    __tablename__ = "qkit_project_confirm_permissions"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id", ondelete="CASCADE"), nullable=False, index=True)
    function_name = db.Column(db.String(255), nullable=False)
    function_key = db.Column(db.String(255), nullable=False)
    allow_confirm = db.Column(db.Boolean, nullable=False, default=True)
    allow_reject = db.Column(db.Boolean, nullable=False, default=True)
    updated_by = db.Column(db.Integer, db.ForeignKey("qkit_auth_users.id"), nullable=True)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        db.UniqueConstraint("project_id", "function_key", name="uq_qkit_project_confirm_permission"),
    )

    project = db.relationship("Project")
    updater = db.relationship("QkitAuthUser", foreign_keys=[updated_by])


class QkitAuthProjectJoinRequest(db.Model):
    __tablename__ = "qkit_auth_project_join_requests"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("qkit_auth_users.id", ondelete="CASCADE"), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id", ondelete="CASCADE"), nullable=False)
    message = db.Column(db.String(500), nullable=True)
    status = db.Column(db.String(20), nullable=False, default=QkitRequestStatus.PENDING.value)
    handled_by = db.Column(db.Integer, db.ForeignKey("qkit_auth_users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    handled_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("QkitAuthUser", foreign_keys=[user_id])
    project = db.relationship("Project")
    handler = db.relationship("QkitAuthUser", foreign_keys=[handled_by])

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


class QkitAuthProjectCreateRequest(db.Model):
    __tablename__ = "qkit_auth_project_create_requests"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("qkit_auth_users.id", ondelete="CASCADE"), nullable=False)
    project_code = db.Column(db.String(50), nullable=False)
    project_name = db.Column(db.String(100), nullable=False)
    department = db.Column(db.String(100), nullable=True)
    reason = db.Column(db.String(500), nullable=True)
    status = db.Column(db.String(20), nullable=False, default=QkitRequestStatus.PENDING.value)
    handled_by = db.Column(db.Integer, db.ForeignKey("qkit_auth_users.id"), nullable=True)
    created_project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    handled_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("QkitAuthUser", foreign_keys=[user_id])
    handler = db.relationship("QkitAuthUser", foreign_keys=[handled_by])
    created_project = db.relationship("Project", foreign_keys=[created_project_id])

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


class QkitAuthProjectPreAssignment(db.Model):
    __tablename__ = "qkit_auth_project_pre_assignments"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), nullable=False, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id", ondelete="CASCADE"), nullable=False)
    role = db.Column(db.String(20), nullable=False, default=QkitProjectRole.MEMBER.value)
    assigned_by = db.Column(db.Integer, db.ForeignKey("qkit_auth_users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    applied = db.Column(db.Boolean, default=False, nullable=False)
    applied_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        db.UniqueConstraint("username", "project_id", name="uq_qkit_pre_assign_user_project"),
    )

    project = db.relationship("Project")
    assigner = db.relationship("QkitAuthUser", foreign_keys=[assigned_by])


class QkitAuthImportBlock(db.Model):
    __tablename__ = "qkit_auth_import_blocks"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id", ondelete="CASCADE"), nullable=False, index=True)
    username = db.Column(db.String(80), nullable=False, index=True)
    block_type = db.Column(db.String(20), nullable=False, default=QkitImportBlockType.REMOVED.value)
    reason = db.Column(db.String(255), nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("qkit_auth_users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.UniqueConstraint("project_id", "username", "block_type", name="uq_qkit_import_block"),
    )

    project = db.relationship("Project")
    creator = db.relationship("QkitAuthUser", foreign_keys=[created_by])


class QkitAuthProjectImportConfig(db.Model):
    __tablename__ = "qkit_auth_project_import_configs"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id", ondelete="CASCADE"), nullable=False, unique=True)
    token = db.Column(db.String(255), nullable=True)
    host = db.Column(db.String(255), nullable=True)
    project_name = db.Column(db.String(255), nullable=True)
    updated_by = db.Column(db.Integer, db.ForeignKey("qkit_auth_users.id"), nullable=True)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    project = db.relationship("Project")
    updater = db.relationship("QkitAuthUser", foreign_keys=[updated_by])

    @property
    def masked_token(self) -> str:
        token = (self.token or "").strip()
        if not token:
            return ""
        if len(token) <= 8:
            return "*" * len(token)
        return f"{token[:4]}{'*' * (len(token) - 8)}{token[-4:]}"


class QkitAuthUserImportToken(db.Model):
    __tablename__ = "qkit_auth_user_import_tokens"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("qkit_auth_users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    token = db.Column(db.String(255), nullable=True)
    updated_by = db.Column(db.Integer, db.ForeignKey("qkit_auth_users.id"), nullable=True)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    user = db.relationship("QkitAuthUser", foreign_keys=[user_id])
    updater = db.relationship("QkitAuthUser", foreign_keys=[updated_by])

    @property
    def masked_token(self) -> str:
        token = (self.token or "").strip()
        if not token:
            return ""
        if len(token) <= 8:
            return "*" * len(token)
        return f"{token[:4]}{'*' * (len(token) - 8)}{token[-4:]}"


