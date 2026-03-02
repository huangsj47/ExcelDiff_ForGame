#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
账号系统业务逻辑层 (Auth Services)

提供用户注册、密码修改、职能分配、项目归属管理等业务操作。
所有写操作通过此模块进行，保证业务规则一致性。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from werkzeug.security import check_password_hash, generate_password_hash

from models import db
from models.project import Project

from .models import (
    DEFAULT_FUNCTIONS,
    AuthFunction,
    AuthProjectCreateRequest,
    AuthProjectJoinRequest,
    AuthUser,
    AuthUserFunction,
    AuthUserProject,
    PlatformRole,
    ProjectRole,
    RequestStatus,
)


# ──────────────────────────── 用户管理 ────────────────────────────


def register_user(
    username: str,
    password: str,
    *,
    display_name: Optional[str] = None,
    email: Optional[str] = None,
    role: str = PlatformRole.NORMAL.value,
) -> tuple[Optional[AuthUser], Optional[str]]:
    """注册新用户。

    Returns:
        (user, None) 成功时
        (None, error_message) 失败时
    """
    username = username.strip()
    if not username:
        return None, "用户名不能为空"
    if len(username) < 2:
        return None, "用户名至少2个字符"
    if len(password) < 4:
        return None, "密码至少4个字符"

    existing = AuthUser.query.filter_by(username=username).first()
    if existing:
        return None, f"用户名 '{username}' 已存在"

    # 验证角色值
    try:
        PlatformRole(role)
    except ValueError:
        role = PlatformRole.NORMAL.value

    user = AuthUser(
        username=username,
        password_hash=generate_password_hash(password, method="pbkdf2:sha256"),
        display_name=display_name or username,
        email=email,
        role=role,
        is_active=True,
    )
    db.session.add(user)
    db.session.commit()
    return user, None


def change_password(
    user_id: int,
    old_password: str,
    new_password: str,
) -> tuple[bool, Optional[str]]:
    """修改用户密码（需验证旧密码）。

    Returns:
        (True, None) 成功时
        (False, error_message) 失败时
    """
    user = AuthUser.query.get(user_id)
    if not user:
        return False, "用户不存在"

    if not check_password_hash(user.password_hash, old_password):
        return False, "旧密码不正确"

    if len(new_password) < 4:
        return False, "新密码至少4个字符"

    user.password_hash = generate_password_hash(new_password, method="pbkdf2:sha256")
    db.session.commit()
    return True, None


def admin_reset_password(user_id: int, new_password: str) -> tuple[bool, Optional[str]]:
    """管理员重置用户密码（无需旧密码）。"""
    user = AuthUser.query.get(user_id)
    if not user:
        return False, "用户不存在"
    if len(new_password) < 4:
        return False, "新密码至少4个字符"

    user.password_hash = generate_password_hash(new_password, method="pbkdf2:sha256")
    db.session.commit()
    return True, None


def update_user_role(user_id: int, new_role: str) -> tuple[bool, Optional[str]]:
    """修改用户平台角色。"""
    user = AuthUser.query.get(user_id)
    if not user:
        return False, "用户不存在"

    try:
        PlatformRole(new_role)
    except ValueError:
        return False, f"无效的角色值: {new_role}"

    user.role = new_role
    db.session.commit()
    return True, None


def toggle_user_active(user_id: int) -> tuple[bool, Optional[str]]:
    """切换用户启用/禁用状态。"""
    user = AuthUser.query.get(user_id)
    if not user:
        return False, "用户不存在"

    user.is_active = not user.is_active
    db.session.commit()
    return True, None


def get_user_by_id(user_id: int) -> Optional[AuthUser]:
    return AuthUser.query.get(user_id)


def get_user_by_username(username: str) -> Optional[AuthUser]:
    return AuthUser.query.filter_by(username=username).first()


def list_users(*, include_inactive: bool = False) -> list[AuthUser]:
    query = AuthUser.query
    if not include_inactive:
        query = query.filter_by(is_active=True)
    return query.order_by(AuthUser.created_at.desc()).all()


# ──────────────────────────── 职能管理 ────────────────────────────


def assign_function(
    user_id: int,
    function_id: int,
    project_id: Optional[int] = None,
    assigned_by: Optional[int] = None,
) -> tuple[bool, Optional[str]]:
    """为用户分配职能。

    如果分配的是 Lead QA 职能，自动将用户在该项目中升级为项目管理员。
    """
    user = AuthUser.query.get(user_id)
    if not user:
        return False, "用户不存在"

    func = AuthFunction.query.get(function_id)
    if not func:
        return False, "职能不存在"

    # 检查是否已分配
    existing = AuthUserFunction.query.filter_by(
        user_id=user_id, function_id=function_id, project_id=project_id
    ).first()
    if existing:
        return False, f"用户已拥有职能 '{func.name}'"

    uf = AuthUserFunction(
        user_id=user_id,
        function_id=function_id,
        project_id=project_id,
        assigned_by=assigned_by,
    )
    db.session.add(uf)

    # 主QA联动：自动升级为项目管理员
    if func.is_lead_qa and project_id:
        _ensure_project_admin(user_id, project_id)

    db.session.commit()
    return True, None


def remove_function(
    user_id: int,
    function_id: int,
    project_id: Optional[int] = None,
) -> tuple[bool, Optional[str]]:
    """移除用户的某个职能。

    如果移除的是 Lead QA 职能，自动将用户在该项目中降级为普通成员。
    """
    uf = AuthUserFunction.query.filter_by(
        user_id=user_id, function_id=function_id, project_id=project_id
    ).first()
    if not uf:
        return False, "用户未拥有该职能"

    func = uf.function

    db.session.delete(uf)

    # 主QA联动：自动降级为普通成员
    if func and func.is_lead_qa and project_id:
        _demote_to_member(user_id, project_id)

    db.session.commit()
    return True, None


def _ensure_project_admin(user_id: int, project_id: int) -> None:
    """确保用户在指定项目中是管理员（升级或创建关联）。"""
    membership = AuthUserProject.query.filter_by(
        user_id=user_id, project_id=project_id
    ).first()

    if membership:
        membership.role = ProjectRole.ADMIN.value
    else:
        membership = AuthUserProject(
            user_id=user_id,
            project_id=project_id,
            role=ProjectRole.ADMIN.value,
        )
        db.session.add(membership)


def _demote_to_member(user_id: int, project_id: int) -> None:
    """将用户在指定项目中降级为普通成员。

    注意：只有当用户在该项目中不再拥有任何 Lead QA 职能时才降级。
    """
    # 检查用户是否仍然有其他 Lead QA 职能绑定到该项目
    remaining_lead_qa = (
        AuthUserFunction.query
        .join(AuthFunction)
        .filter(
            AuthUserFunction.user_id == user_id,
            AuthUserFunction.project_id == project_id,
            AuthFunction.is_lead_qa.is_(True),
        )
        .count()
    )

    if remaining_lead_qa > 0:
        return  # 仍有其他 Lead QA 职能，不降级

    membership = AuthUserProject.query.filter_by(
        user_id=user_id, project_id=project_id
    ).first()

    if membership:
        membership.role = ProjectRole.MEMBER.value


def get_user_functions(user_id: int, project_id: Optional[int] = None) -> list[dict]:
    """获取用户的职能列表。"""
    query = AuthUserFunction.query.filter_by(user_id=user_id)
    if project_id is not None:
        query = query.filter_by(project_id=project_id)
    ufs = query.all()
    return [
        {
            "id": uf.id,
            "function_id": uf.function_id,
            "function_name": uf.function.name if uf.function else None,
            "project_id": uf.project_id,
            "is_lead_qa": uf.function.is_lead_qa if uf.function else False,
        }
        for uf in ufs
    ]


def list_functions() -> list[AuthFunction]:
    """获取所有职能定义。"""
    return AuthFunction.query.order_by(AuthFunction.sort_order).all()


# ──────────────────────────── 项目归属管理 ────────────────────────────


def add_user_to_project(
    user_id: int,
    project_id: int,
    role: str = ProjectRole.MEMBER.value,
    approved_by: Optional[int] = None,
) -> tuple[bool, Optional[str]]:
    """将用户添加到项目。"""
    existing = AuthUserProject.query.filter_by(
        user_id=user_id, project_id=project_id
    ).first()
    if existing:
        return False, "用户已是该项目成员"

    membership = AuthUserProject(
        user_id=user_id,
        project_id=project_id,
        role=role,
        approved_by=approved_by,
    )
    db.session.add(membership)
    db.session.commit()
    return True, None


def remove_user_from_project(user_id: int, project_id: int) -> tuple[bool, Optional[str]]:
    """从项目中移除用户。"""
    membership = AuthUserProject.query.filter_by(
        user_id=user_id, project_id=project_id
    ).first()
    if not membership:
        return False, "用户不是该项目成员"

    # 同时移除该用户在该项目上的所有职能关联
    AuthUserFunction.query.filter_by(user_id=user_id, project_id=project_id).delete()

    db.session.delete(membership)
    db.session.commit()
    return True, None


def update_project_member_role(
    user_id: int, project_id: int, new_role: str
) -> tuple[bool, Optional[str]]:
    """修改用户在项目中的角色。"""
    membership = AuthUserProject.query.filter_by(
        user_id=user_id, project_id=project_id
    ).first()
    if not membership:
        return False, "用户不是该项目成员"

    try:
        ProjectRole(new_role)
    except ValueError:
        return False, f"无效的项目角色: {new_role}"

    membership.role = new_role
    db.session.commit()
    return True, None


def get_project_members(project_id: int) -> list[dict]:
    """获取项目的所有成员信息。"""
    memberships = AuthUserProject.query.filter_by(project_id=project_id).all()
    result = []
    for m in memberships:
        user = m.user
        if not user:
            continue
        functions = get_user_functions(user.id, project_id)
        result.append({
            "user_id": user.id,
            "username": user.username,
            "display_name": user.display_name or user.username,
            "role": m.role,
            "role_display": m.project_role.display_name,
            "functions": functions,
            "joined_at": m.joined_at.isoformat() if m.joined_at else None,
        })
    return result


# ──────────────────────────── 项目加入申请 ────────────────────────────


def request_join_project(
    user_id: int, project_id: int, message: Optional[str] = None
) -> tuple[bool, Optional[str]]:
    """提交项目加入申请。"""
    # 检查是否已经是成员
    existing_member = AuthUserProject.query.filter_by(
        user_id=user_id, project_id=project_id
    ).first()
    if existing_member:
        return False, "你已经是该项目的成员"

    # 检查是否有待处理的申请
    pending = AuthProjectJoinRequest.query.filter_by(
        user_id=user_id,
        project_id=project_id,
        status=RequestStatus.PENDING.value,
    ).first()
    if pending:
        return False, "你已提交过加入申请，请等待审批"

    req = AuthProjectJoinRequest(
        user_id=user_id,
        project_id=project_id,
        message=message,
        status=RequestStatus.PENDING.value,
    )
    db.session.add(req)
    db.session.commit()
    return True, None


def handle_join_request(
    request_id: int,
    action: str,
    handled_by: int,
) -> tuple[bool, Optional[str]]:
    """处理项目加入申请 (approve / deny)。"""
    req = AuthProjectJoinRequest.query.get(request_id)
    if not req:
        return False, "申请不存在"

    if req.status != RequestStatus.PENDING.value:
        return False, f"申请已处理 (状态: {req.status})"

    if action == "approve":
        req.status = RequestStatus.APPROVED.value
        req.handled_by = handled_by
        req.handled_at = datetime.now(timezone.utc)

        # 自动将用户添加到项目
        success, err = add_user_to_project(
            req.user_id, req.project_id, approved_by=handled_by
        )
        if not success:
            # 如果已经是成员，也标记为已通过
            pass

    elif action == "deny":
        req.status = RequestStatus.DENIED.value
        req.handled_by = handled_by
        req.handled_at = datetime.now(timezone.utc)
    else:
        return False, f"无效的操作: {action}"

    db.session.commit()
    return True, None


def list_pending_join_requests(project_id: Optional[int] = None) -> list[dict]:
    """获取待处理的项目加入申请列表。"""
    query = AuthProjectJoinRequest.query.filter_by(
        status=RequestStatus.PENDING.value
    )
    if project_id:
        query = query.filter_by(project_id=project_id)
    requests = query.order_by(AuthProjectJoinRequest.created_at.desc()).all()
    return [r.to_dict() for r in requests]


# ──────────────────────────── 项目创建申请 ────────────────────────────


def request_create_project(
    user_id: int,
    project_code: str,
    project_name: str,
    department: Optional[str] = None,
    reason: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """提交项目创建申请。"""
    project_code = project_code.strip()
    project_name = project_name.strip()

    if not project_code or not project_name:
        return False, "项目编码和名称不能为空"

    # 检查编码是否已存在
    existing_project = Project.query.filter_by(code=project_code).first()
    if existing_project:
        return False, f"项目编码 '{project_code}' 已存在"

    # 检查是否有待处理的同编码申请
    pending = AuthProjectCreateRequest.query.filter_by(
        project_code=project_code,
        status=RequestStatus.PENDING.value,
    ).first()
    if pending:
        return False, f"已有编码为 '{project_code}' 的待审批申请"

    req = AuthProjectCreateRequest(
        user_id=user_id,
        project_code=project_code,
        project_name=project_name,
        department=department,
        reason=reason,
        status=RequestStatus.PENDING.value,
    )
    db.session.add(req)
    db.session.commit()
    return True, None


def handle_create_project_request(
    request_id: int,
    action: str,
    handled_by: int,
) -> tuple[bool, Optional[str]]:
    """处理项目创建申请 (approve / deny)。

    审批通过时：
      1. 创建项目
      2. 将申请人添加为项目管理员
    """
    req = AuthProjectCreateRequest.query.get(request_id)
    if not req:
        return False, "申请不存在"

    if req.status != RequestStatus.PENDING.value:
        return False, f"申请已处理 (状态: {req.status})"

    if action == "approve":
        # 再次检查编码唯一性
        existing = Project.query.filter_by(code=req.project_code).first()
        if existing:
            return False, f"项目编码 '{req.project_code}' 已存在，无法创建"

        # 创建项目
        project = Project(
            code=req.project_code,
            name=req.project_name,
            department=req.department,
        )
        db.session.add(project)
        db.session.flush()  # 获取 project.id

        # 将申请人添加为项目管理员
        membership = AuthUserProject(
            user_id=req.user_id,
            project_id=project.id,
            role=ProjectRole.ADMIN.value,
            approved_by=handled_by,
        )
        db.session.add(membership)

        req.status = RequestStatus.APPROVED.value
        req.handled_by = handled_by
        req.handled_at = datetime.now(timezone.utc)
        req.created_project_id = project.id

    elif action == "deny":
        req.status = RequestStatus.DENIED.value
        req.handled_by = handled_by
        req.handled_at = datetime.now(timezone.utc)
    else:
        return False, f"无效的操作: {action}"

    db.session.commit()
    return True, None


def list_pending_create_requests() -> list[dict]:
    """获取待处理的项目创建申请列表。"""
    requests = (
        AuthProjectCreateRequest.query
        .filter_by(status=RequestStatus.PENDING.value)
        .order_by(AuthProjectCreateRequest.created_at.desc())
        .all()
    )
    return [r.to_dict() for r in requests]


# ──────────────────────────── 初始化 ────────────────────────────


def init_default_functions() -> int:
    """初始化默认职能数据。返回新增数量。"""
    count = 0
    for func_data in DEFAULT_FUNCTIONS:
        existing = AuthFunction.query.filter_by(name=func_data["name"]).first()
        if not existing:
            func = AuthFunction(**func_data)
            db.session.add(func)
            count += 1
    if count > 0:
        db.session.commit()
    return count


def migrate_env_admin_to_db() -> Optional[AuthUser]:
    """将 .env 中配置的管理员迁移到数据库（如果数据库中不存在）。

    只在数据库中没有任何平台管理员时执行。
    """
    import os

    env_username = os.environ.get("ADMIN_USERNAME", "admin").strip()
    env_password = os.environ.get("ADMIN_PASSWORD", "").strip()

    if not env_password:
        return None

    # 如果数据库中已有平台管理员，跳过迁移
    existing_admin = AuthUser.query.filter_by(
        role=PlatformRole.PLATFORM_ADMIN.value
    ).first()
    if existing_admin:
        return None

    # 检查用户名是否已存在
    existing_user = AuthUser.query.filter_by(username=env_username).first()
    if existing_user:
        # 如果用户已存在但不是管理员，升级为管理员
        existing_user.role = PlatformRole.PLATFORM_ADMIN.value
        db.session.commit()
        return existing_user

    # 创建新的管理员用户
    user, err = register_user(
        username=env_username,
        password=env_password,
        display_name="超级管理员",
        role=PlatformRole.PLATFORM_ADMIN.value,
    )
    return user
