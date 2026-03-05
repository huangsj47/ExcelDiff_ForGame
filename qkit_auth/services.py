#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Qkit auth backend services."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re
from typing import Optional

try:
    import jwt
    _JWT_IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - defensive guard for env/runtime differences
    jwt = None
    _JWT_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

import requests

from models import AgentNode, AgentProjectBinding, Project, db
from qkit_auth.config import load_qkit_settings
from qkit_auth.models import (
    QkitAuthImportBlock,
    QkitAuthProjectCreateRequest,
    QkitAuthProjectImportConfig,
    QkitAuthProjectJoinRequest,
    QkitAuthProjectPreAssignment,
    QkitAuthUserImportToken,
    QkitAuthUser,
    QkitAuthUserProject,
    QkitImportBlockType,
    QkitPlatformRole,
    QkitProjectRole,
    QkitRequestStatus,
)

_USERNAME_ALLOWED_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_FUNCTION_KEYS = (
    "function",
    "function_name",
    "func",
    "role_name",
    "job",
    "job_name",
    "post",
    "post_name",
    "position",
    "duty",
    "title",
    "zhineng",
    "职能",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_username(value: str) -> str:
    username = (value or "").strip()
    if username.endswith("@corp.netease.com"):
        username = username.split("@", 1)[0]
    if "@" in username:
        username = username.split("@", 1)[0]
    return username


def _username_from_email(email: str) -> str:
    raw = (email or "").strip()
    if "@" not in raw:
        return ""
    return raw.split("@", 1)[0].strip()


def _resolve_qkit_admin_username() -> str:
    return _normalize_username((os.environ.get("QKIT_ADMIN_USERNAME") or "").strip())


def _normalize_project_role(role: str) -> str:
    normalized = (role or "").strip()
    if normalized == "project_admin":
        normalized = QkitProjectRole.ADMIN.value
    if normalized not in {QkitProjectRole.ADMIN.value, QkitProjectRole.MEMBER.value}:
        normalized = QkitProjectRole.MEMBER.value
    return normalized


def _normalize_platform_role(role: str) -> str:
    normalized = (role or "").strip()
    allowed = {
        QkitPlatformRole.PLATFORM_ADMIN.value,
        QkitPlatformRole.PROJECT_ADMIN.value,
        QkitPlatformRole.NORMAL.value,
    }
    if normalized not in allowed:
        normalized = QkitPlatformRole.NORMAL.value
    return normalized


def _extract_function_name(item: dict) -> Optional[str]:
    for key in _FUNCTION_KEYS:
        value = item.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text[:255]
    return None


def decode_qkit_jwt_unsafe(token: str) -> Optional[dict]:
    if jwt is None:
        return None
    raw = (token or "").strip()
    if not raw:
        return None
    attempts = (
        {"options": {"verify_signature": False, "verify_exp": False}},
        {
            "options": {"verify_signature": False, "verify_exp": False},
            "algorithms": ["HS256", "HS384", "HS512", "RS256", "RS384", "RS512"],
        },
    )
    for kwargs in attempts:
        try:
            payload = jwt.decode(raw, **kwargs)
            if isinstance(payload, dict):
                return payload
        except Exception:
            continue
    return None


def get_jwt_import_error() -> str:
    return _JWT_IMPORT_ERROR


def extract_identity_from_payload(payload: dict) -> dict:
    uid = (
        payload.get("uid")
        or payload.get("username")
        or payload.get("user")
        or payload.get("mail")
        or payload.get("email")
        or ""
    )
    uid = _normalize_username(str(uid))

    email = str(payload.get("mail") or payload.get("email") or "").strip()
    if not uid and email:
        uid = _username_from_email(email)

    display_name = (
        payload.get("name")
        or payload.get("display_name")
        or payload.get("nickname")
        or uid
        or ""
    )
    display_name = str(display_name).strip()[:100]

    project_payload = payload.get("project")
    token_project_id = None
    token_project_name = None
    if isinstance(project_payload, dict):
        token_project_id = project_payload.get("project_id")
        token_project_name = project_payload.get("project_name")

    return {
        "username": uid[:80],
        "email": email[:200] or None,
        "display_name": display_name[:100] or None,
        "token_project_id": str(token_project_id).strip() if token_project_id is not None else None,
        "token_project_name": str(token_project_name).strip() if token_project_name is not None else None,
    }


def _infer_auth_check_valid(payload: dict) -> bool:
    if not isinstance(payload, dict):
        return False
    if "valid" in payload:
        return bool(payload.get("valid"))
    if "is_valid" in payload:
        return bool(payload.get("is_valid"))
    if "logged_in" in payload:
        return bool(payload.get("logged_in"))
    if "is_login" in payload:
        return bool(payload.get("is_login"))
    # 兼容 qkit-auth2-utils 旧示例：islogin=True 需重新登录
    if "islogin" in payload:
        return not bool(payload.get("islogin"))
    if "success" in payload:
        return bool(payload.get("success"))
    return False


def check_qkit_jwt_remote(qkitjwt: str) -> tuple[bool, str, Optional[dict]]:
    token = (qkitjwt or "").strip()
    if not token:
        return False, "缺少 qkitjwt", None
    settings = load_qkit_settings()
    try:
        response = requests.get(
            settings.auth_check_jwt_api,
            params={"qkitjwt": token},
            timeout=settings.request_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.Timeout:
        return False, "Qkit 会话校验超时（5s）", None
    except Exception as exc:
        return False, f"Qkit 会话校验失败: {exc}", None

    valid = _infer_auth_check_valid(payload)
    if valid:
        return True, "", payload
    message = ""
    if isinstance(payload, dict):
        message = str(payload.get("msg") or payload.get("message") or "").strip()
    return False, message or "Qkit 会话无效，请重新登录", payload


def _get_user_by_username(username: str) -> Optional[QkitAuthUser]:
    return QkitAuthUser.query.filter_by(username=username).first()


def _username_valid(username: str) -> bool:
    return bool(username and _USERNAME_ALLOWED_RE.fullmatch(username))


def ensure_qkit_user(
    *,
    username: str,
    display_name: Optional[str],
    email: Optional[str],
    source: str,
) -> tuple[Optional[QkitAuthUser], Optional[str]]:
    normalized_username = _normalize_username(username)
    if not _username_valid(normalized_username):
        return None, "Qkit 返回的用户名不合法"

    normalized_email = (email or "").strip() or None
    normalized_display_name = (display_name or normalized_username).strip()[:100] or normalized_username

    user = _get_user_by_username(normalized_username)
    if user is None:
        role = QkitPlatformRole.NORMAL.value
        env_admin_user = _resolve_qkit_admin_username()
        if env_admin_user and env_admin_user == normalized_username:
            role = QkitPlatformRole.PLATFORM_ADMIN.value
        user = QkitAuthUser(
            username=normalized_username,
            display_name=normalized_display_name,
            email=normalized_email,
            role=role,
            is_active=True,
            source=source,
        )
        db.session.add(user)
        db.session.flush()
        applied = apply_pre_assignments(user)
        if applied > 0:
            from utils.logger import log_print

            log_print(f"Qkit用户 {normalized_username} 应用了 {applied} 条预分配", "AUTH")
        return user, None

    if normalized_email and user.email and user.email.lower() != normalized_email.lower():
        return None, f"用户名 {normalized_username} 已绑定其他邮箱，导入冲突"

    if normalized_display_name:
        user.display_name = normalized_display_name
    if normalized_email and not user.email:
        user.email = normalized_email
    if source:
        user.source = source[:30]
    return user, None


def _resolve_project_default_admin_usernames(project_id: int) -> set[str]:
    deployment_mode = (os.environ.get("DEPLOYMENT_MODE") or "single").strip().lower()
    result: set[str] = set()

    # single/platform: 全局默认管理员用户名生效
    global_default = (os.environ.get("AGENT_DEFAULT_ADMIN_USERNAME") or "").strip()
    if global_default and deployment_mode in {"single", "platform"}:
        result.add(_normalize_username(global_default).lower())

    # 绑定到该项目的 agent 默认管理员都生效
    bindings = AgentProjectBinding.query.filter_by(project_id=project_id).all()
    if bindings:
        agent_ids = [row.agent_id for row in bindings]
        if agent_ids:
            agents = AgentNode.query.filter(AgentNode.id.in_(agent_ids)).all()
            for agent in agents:
                username = _normalize_username(agent.default_admin_username or "")
                if username:
                    result.add(username.lower())

    # agent 模式：仅依赖项目绑定关系；无绑定时不生效（上面已天然满足）
    return result


def is_default_project_admin(project_id: int, username: str) -> bool:
    normalized = _normalize_username(username).lower()
    if not normalized:
        return False
    return normalized in _resolve_project_default_admin_usernames(project_id)


def bootstrap_qkit_auth_data() -> None:
    env_admin_user = _resolve_qkit_admin_username()
    if env_admin_user:
        user = _get_user_by_username(env_admin_user)
        if user is None:
            user = QkitAuthUser(
                username=env_admin_user,
                display_name="平台管理员",
                email=None,
                role=QkitPlatformRole.PLATFORM_ADMIN.value,
                is_active=True,
                source="bootstrap",
            )
            db.session.add(user)
        elif user.role != QkitPlatformRole.PLATFORM_ADMIN.value:
            user.role = QkitPlatformRole.PLATFORM_ADMIN.value
            user.is_active = True

    # 为当前已有项目创建“默认管理员”预分配（不覆盖已存在成员角色）
    projects = Project.query.order_by(Project.id.asc()).all()
    for project in projects:
        defaults = _resolve_project_default_admin_usernames(project.id)
        for username in defaults:
            if not username:
                continue
            existing_user = _get_user_by_username(username)
            if existing_user:
                membership = QkitAuthUserProject.query.filter_by(
                    user_id=existing_user.id,
                    project_id=project.id,
                ).first()
                if membership is None:
                    db.session.add(
                        QkitAuthUserProject(
                            user_id=existing_user.id,
                            project_id=project.id,
                            role=QkitProjectRole.ADMIN.value,
                            imported_from_qkit=False,
                            import_sync_locked=True,
                        )
                    )
                continue

            existing_pre = QkitAuthProjectPreAssignment.query.filter_by(
                username=username,
                project_id=project.id,
            ).first()
            if existing_pre is None:
                db.session.add(
                    QkitAuthProjectPreAssignment(
                        username=username,
                        project_id=project.id,
                        role=QkitProjectRole.ADMIN.value,
                        applied=False,
                    )
                )
    db.session.commit()


def apply_pre_assignments(user: QkitAuthUser) -> int:
    pending = QkitAuthProjectPreAssignment.query.filter_by(
        username=user.username,
        applied=False,
    ).all()
    if not pending:
        return 0

    applied_count = 0
    for record in pending:
        membership = QkitAuthUserProject.query.filter_by(
            user_id=user.id,
            project_id=record.project_id,
        ).first()
        if membership is None:
            membership = QkitAuthUserProject(
                user_id=user.id,
                project_id=record.project_id,
                role=_normalize_project_role(record.role),
                imported_from_qkit=False,
                import_sync_locked=False,
                approved_by=record.assigned_by,
            )
            db.session.add(membership)
            if membership.role == QkitProjectRole.ADMIN.value and user.role == QkitPlatformRole.NORMAL.value:
                user.role = QkitPlatformRole.PROJECT_ADMIN.value
            applied_count += 1
        record.applied = True
        record.applied_at = _utcnow()
    db.session.commit()
    return applied_count


def list_users(*, include_inactive: bool = False) -> list[QkitAuthUser]:
    query = QkitAuthUser.query
    if not include_inactive:
        query = query.filter_by(is_active=True)
    return query.order_by(QkitAuthUser.created_at.desc()).all()


def get_user_by_id(user_id: int) -> Optional[QkitAuthUser]:
    return db.session.get(QkitAuthUser, user_id)


def update_user_role(user_id: int, new_role: str) -> tuple[bool, Optional[str]]:
    user = db.session.get(QkitAuthUser, user_id)
    if not user:
        return False, "用户不存在"
    user.role = _normalize_platform_role(new_role)
    db.session.commit()
    return True, None


def toggle_user_active(user_id: int) -> tuple[bool, Optional[str]]:
    user = db.session.get(QkitAuthUser, user_id)
    if not user:
        return False, "用户不存在"
    user.is_active = not user.is_active
    db.session.commit()
    return True, None


def search_users(
    keyword: str = "",
    *,
    exclude_user_ids: list[int] | None = None,
    only_active: bool = True,
    page: int = 1,
    per_page: int = 20,
) -> tuple[list[QkitAuthUser], int]:
    query = QkitAuthUser.query
    if only_active:
        query = query.filter_by(is_active=True)
    if keyword:
        kw = f"%{keyword}%"
        query = query.filter(
            db.or_(
                QkitAuthUser.username.ilike(kw),
                QkitAuthUser.display_name.ilike(kw),
                QkitAuthUser.email.ilike(kw),
            )
        )
    if exclude_user_ids:
        query = query.filter(QkitAuthUser.id.notin_(exclude_user_ids))

    total = query.count()
    users = (
        query.order_by(QkitAuthUser.username.asc())
        .offset((max(1, page) - 1) * max(1, per_page))
        .limit(max(1, per_page))
        .all()
    )
    return users, total


def add_user_to_project(
    user_id: int,
    project_id: int,
    role: str = QkitProjectRole.MEMBER.value,
    *,
    function_name: Optional[str] = None,
    approved_by: Optional[int] = None,
    imported_from_qkit: bool = False,
    import_sync_locked: bool = False,
) -> tuple[bool, Optional[str]]:
    user = db.session.get(QkitAuthUser, user_id)
    if not user:
        return False, "用户不存在"
    project = db.session.get(Project, project_id)
    if not project:
        return False, "项目不存在"

    existing = QkitAuthUserProject.query.filter_by(user_id=user_id, project_id=project_id).first()
    if existing:
        return False, "用户已是该项目成员"

    membership = QkitAuthUserProject(
        user_id=user_id,
        project_id=project_id,
        role=_normalize_project_role(role),
        function_name=(function_name or "").strip()[:255] or None,
        approved_by=approved_by,
        imported_from_qkit=imported_from_qkit,
        import_sync_locked=import_sync_locked,
        import_last_synced_at=_utcnow() if imported_from_qkit else None,
    )
    db.session.add(membership)

    # 手工添加时，如果此前在该项目被标记“删除阻断”，此时应解除阻断
    QkitAuthImportBlock.query.filter_by(
        project_id=project_id,
        username=user.username,
        block_type=QkitImportBlockType.REMOVED.value,
    ).delete()

    db.session.commit()
    return True, None


def remove_user_from_project(
    user_id: int,
    project_id: int,
    *,
    removed_by: Optional[int] = None,
) -> tuple[bool, Optional[str]]:
    membership = QkitAuthUserProject.query.filter_by(user_id=user_id, project_id=project_id).first()
    if membership is None:
        return False, "用户不是该项目成员"
    username = membership.user.username if membership.user else ""
    db.session.delete(membership)
    if username:
        existing_block = QkitAuthImportBlock.query.filter_by(
            project_id=project_id,
            username=username,
            block_type=QkitImportBlockType.REMOVED.value,
        ).first()
        if existing_block is None:
            db.session.add(
                QkitAuthImportBlock(
                    project_id=project_id,
                    username=username,
                    block_type=QkitImportBlockType.REMOVED.value,
                    reason="manual_remove",
                    created_by=removed_by,
                )
            )
    db.session.commit()
    return True, None


def update_project_member_role(
    user_id: int,
    project_id: int,
    new_role: str,
    *,
    lock_import_sync: bool = True,
) -> tuple[bool, Optional[str]]:
    membership = QkitAuthUserProject.query.filter_by(user_id=user_id, project_id=project_id).first()
    if membership is None:
        return False, "用户不是该项目成员"
    membership.role = _normalize_project_role(new_role)
    if lock_import_sync and membership.imported_from_qkit:
        membership.import_sync_locked = True
    db.session.commit()
    return True, None


def request_join_project(user_id: int, project_id: int, message: Optional[str] = None) -> tuple[bool, Optional[str]]:
    user = db.session.get(QkitAuthUser, user_id)
    if not user:
        return False, "用户不存在"
    project = db.session.get(Project, project_id)
    if not project:
        return False, "项目不存在"

    existing = QkitAuthUserProject.query.filter_by(user_id=user_id, project_id=project_id).first()
    if existing:
        return False, "你已经是该项目成员"
    pending = QkitAuthProjectJoinRequest.query.filter_by(
        user_id=user_id,
        project_id=project_id,
        status=QkitRequestStatus.PENDING.value,
    ).first()
    if pending:
        return False, "你已提交过加入申请，请等待审批"

    db.session.add(
        QkitAuthProjectJoinRequest(
            user_id=user_id,
            project_id=project_id,
            message=message,
            status=QkitRequestStatus.PENDING.value,
        )
    )
    db.session.commit()
    return True, None


def handle_join_request(request_id: int, action: str, handled_by: int) -> tuple[bool, Optional[str]]:
    req = db.session.get(QkitAuthProjectJoinRequest, request_id)
    if not req:
        return False, "申请不存在"
    if req.status != QkitRequestStatus.PENDING.value:
        return False, f"申请已处理 (状态: {req.status})"

    now = _utcnow()
    if action == "approve":
        existing = QkitAuthUserProject.query.filter_by(user_id=req.user_id, project_id=req.project_id).first()
        if existing is None:
            db.session.add(
                QkitAuthUserProject(
                    user_id=req.user_id,
                    project_id=req.project_id,
                    role=QkitProjectRole.MEMBER.value,
                    approved_by=handled_by,
                    imported_from_qkit=False,
                )
            )
        req.status = QkitRequestStatus.APPROVED.value
    elif action == "deny":
        req.status = QkitRequestStatus.DENIED.value
    else:
        return False, "无效操作"
    req.handled_by = handled_by
    req.handled_at = now
    db.session.commit()
    return True, None


def list_pending_join_requests(project_id: Optional[int] = None) -> list[dict]:
    query = QkitAuthProjectJoinRequest.query.filter_by(status=QkitRequestStatus.PENDING.value)
    if project_id:
        query = query.filter_by(project_id=project_id)
    rows = query.order_by(QkitAuthProjectJoinRequest.created_at.desc()).all()
    return [row.to_dict() for row in rows]


def request_create_project(
    user_id: int,
    project_code: str,
    project_name: str,
    department: Optional[str] = None,
    reason: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    user = db.session.get(QkitAuthUser, user_id)
    if not user:
        return False, "用户不存在"

    code = (project_code or "").strip()
    name = (project_name or "").strip()
    if not code or not name:
        return False, "项目编码和名称不能为空"

    existing_project = Project.query.filter_by(code=code).first()
    if existing_project:
        return False, f"项目编码 '{code}' 已存在"

    pending = QkitAuthProjectCreateRequest.query.filter_by(
        project_code=code,
        status=QkitRequestStatus.PENDING.value,
    ).first()
    if pending:
        return False, f"已有编码为 '{code}' 的待审批申请"

    db.session.add(
        QkitAuthProjectCreateRequest(
            user_id=user_id,
            project_code=code,
            project_name=name,
            department=(department or "").strip() or None,
            reason=(reason or "").strip() or None,
            status=QkitRequestStatus.PENDING.value,
        )
    )
    db.session.commit()
    return True, None


def handle_create_project_request(
    request_id: int,
    action: str,
    handled_by: int,
) -> tuple[bool, Optional[str]]:
    req = db.session.get(QkitAuthProjectCreateRequest, request_id)
    if not req:
        return False, "申请不存在"
    if req.status != QkitRequestStatus.PENDING.value:
        return False, f"申请已处理 (状态: {req.status})"

    now = _utcnow()
    if action == "approve":
        applicant = db.session.get(QkitAuthUser, req.user_id)
        if not applicant:
            return False, "申请用户不存在，无法创建项目"
        existing = Project.query.filter_by(code=req.project_code).first()
        if existing:
            return False, f"项目编码 '{req.project_code}' 已存在，无法创建"

        project = Project(
            code=req.project_code,
            name=req.project_name,
            department=req.department,
        )
        db.session.add(project)
        db.session.flush()

        db.session.add(
            QkitAuthUserProject(
                user_id=req.user_id,
                project_id=project.id,
                role=QkitProjectRole.ADMIN.value,
                approved_by=handled_by,
                imported_from_qkit=False,
            )
        )
        req.status = QkitRequestStatus.APPROVED.value
        req.created_project_id = project.id
    elif action == "deny":
        req.status = QkitRequestStatus.DENIED.value
    else:
        return False, "无效操作"

    req.handled_by = handled_by
    req.handled_at = now
    db.session.commit()
    return True, None


def list_pending_create_requests() -> list[dict]:
    rows = (
        QkitAuthProjectCreateRequest.query.filter_by(status=QkitRequestStatus.PENDING.value)
        .order_by(QkitAuthProjectCreateRequest.created_at.desc())
        .all()
    )
    return [row.to_dict() for row in rows]


def get_project_members(project_id: int) -> list[QkitAuthUserProject]:
    return (
        QkitAuthUserProject.query.filter_by(project_id=project_id)
        .join(QkitAuthUser, QkitAuthUserProject.user_id == QkitAuthUser.id)
        .order_by(QkitAuthUser.username.asc())
        .all()
    )


def _mask_token(token: str) -> str:
    raw = (token or "").strip()
    if not raw:
        return ""
    if len(raw) <= 8:
        return "*" * len(raw)
    return f"{raw[:4]}{'*' * (len(raw) - 8)}{raw[-4:]}"


def _get_project_import_config_row(project_id: int) -> Optional[QkitAuthProjectImportConfig]:
    return QkitAuthProjectImportConfig.query.filter_by(project_id=project_id).first()


def _get_user_import_token_row(user_id: int | None) -> Optional[QkitAuthUserImportToken]:
    if not user_id:
        return None
    return QkitAuthUserImportToken.query.filter_by(user_id=int(user_id)).first()


def get_project_import_config(project_id: int, user_id: int | None = None) -> dict:
    project_cfg = _get_project_import_config_row(project_id)
    token_cfg = _get_user_import_token_row(user_id)

    token_value = (token_cfg.token or "").strip() if token_cfg else ""
    # Backward compatibility: keep old token visible only to the same updater.
    if not token_value and project_cfg and user_id and int(project_cfg.updated_by or 0) == int(user_id):
        token_value = (project_cfg.token or "").strip()

    host_value = (project_cfg.host or "").strip() if project_cfg else ""
    project_name_value = (project_cfg.project_name or "").strip() if project_cfg else ""
    return {
        "token": token_value,
        "token_masked": _mask_token(token_value),
        "host": host_value,
        "project_name": project_name_value,
    }


def upsert_project_import_config(
    project_id: int,
    *,
    token: str,
    host: str,
    project_name: Optional[str],
    updated_by: Optional[int],
) -> tuple[Optional[dict], Optional[str]]:
    project = db.session.get(Project, project_id)
    if not project:
        return None, "项目不存在"
    try:
        updater_user_id = int(updated_by or 0)
    except (TypeError, ValueError):
        updater_user_id = 0
    if updater_user_id <= 0:
        return None, "未获取当前登录用户，无法保存导入配置"

    token_value = (token or "").strip()
    host_value = (host or "").strip()
    project_name_value = (project_name or "").strip() or None

    token_cfg = _get_user_import_token_row(updater_user_id)
    if token_cfg is None:
        token_cfg = QkitAuthUserImportToken(user_id=updater_user_id)
        db.session.add(token_cfg)
    token_cfg.token = token_value
    token_cfg.updated_by = updater_user_id
    token_cfg.updated_at = _utcnow()

    cfg = _get_project_import_config_row(project_id)
    if cfg is None:
        cfg = QkitAuthProjectImportConfig(project_id=project_id)
        db.session.add(cfg)
    # Project-level shared settings.
    cfg.token = None
    cfg.host = host_value or None
    cfg.project_name = project_name_value
    cfg.updated_by = updater_user_id
    cfg.updated_at = _utcnow()
    db.session.commit()
    return get_project_import_config(project_id, user_id=updater_user_id), None


def _import_identity_from_item(item: dict) -> tuple[str, Optional[str], Optional[str], Optional[str]]:
    mail = str(item.get("mail") or item.get("email") or "").strip()
    name = str(item.get("name") or "").strip() or None
    username = _username_from_email(mail)
    function_name = _extract_function_name(item)
    return username, mail or None, name, function_name


def import_project_users_from_redmine(
    *,
    project_id: int,
    operator_user_id: Optional[int],
) -> dict:
    config = get_project_import_config(project_id, user_id=operator_user_id)
    token = str(config.get("token") or "").strip()
    host = str(config.get("host") or "").strip()
    if not token or not host:
        return {"success": False, "message": "token 或 host 为空，请先保存配置（token 跟随个人，host 跟随项目）。"}

    settings = load_qkit_settings()
    params = {"token": token, "host": host}
    project_name = str(config.get("project_name") or "").strip()
    if project_name:
        params["project"] = project_name

    try:
        resp = requests.get(
            settings.redmine_api_url,
            params=params,
            timeout=settings.request_timeout_seconds,
        )
        resp.raise_for_status()
        payload = resp.json()
    except requests.Timeout:
        return {"success": False, "message": "导入失败：调用用户接口超时（5s）。"}
    except Exception as exc:
        return {"success": False, "message": f"导入失败：{exc}"}

    if not isinstance(payload, dict):
        return {"success": False, "message": "导入失败：返回格式非法。"}
    if not payload.get("success"):
        return {"success": False, "message": str(payload.get("msg") or "导入失败")}

    data = payload.get("data")
    if not isinstance(data, list):
        return {"success": False, "message": "导入失败：返回 data 非数组。"}

    now = _utcnow()
    added = 0
    updated = 0
    skipped_removed: list[str] = []
    skipped_locked: list[str] = []
    skipped_invalid: list[str] = []
    conflicts: list[dict] = []

    removed_blocks = {
        row.username.lower()
        for row in QkitAuthImportBlock.query.filter_by(
            project_id=project_id,
            block_type=QkitImportBlockType.REMOVED.value,
        ).all()
    }

    for item in data:
        if not isinstance(item, dict):
            continue
        username, email, display_name, function_name = _import_identity_from_item(item)
        if not _username_valid(username):
            skipped_invalid.append(email or json.dumps(item, ensure_ascii=False))
            continue
        if username.lower() in removed_blocks:
            skipped_removed.append(username)
            continue

        user = _get_user_by_username(username)
        if user and user.email and email and user.email.lower() != email.lower():
            conflicts.append(
                {
                    "username": username,
                    "existing_email": user.email,
                    "incoming_email": email,
                }
            )
            continue

        if user is None:
            user, err = ensure_qkit_user(
                username=username,
                display_name=display_name,
                email=email,
                source="qkit_import",
            )
            if err or user is None:
                conflicts.append(
                    {
                        "username": username,
                        "existing_email": "",
                        "incoming_email": email or "",
                    }
                )
                continue
        else:
            if display_name:
                user.display_name = display_name[:100]
            if email and not user.email:
                user.email = email[:200]

        membership = QkitAuthUserProject.query.filter_by(user_id=user.id, project_id=project_id).first()
        if membership:
            if membership.import_sync_locked:
                skipped_locked.append(username)
                continue
            membership.imported_from_qkit = True
            membership.import_last_synced_at = now
            membership.function_name = function_name
            updated += 1
            continue

        role = QkitProjectRole.ADMIN.value if is_default_project_admin(project_id, username) else QkitProjectRole.MEMBER.value
        db.session.add(
            QkitAuthUserProject(
                user_id=user.id,
                project_id=project_id,
                role=role,
                function_name=function_name,
                approved_by=operator_user_id,
                imported_from_qkit=True,
                import_sync_locked=False,
                import_last_synced_at=now,
            )
        )
        added += 1

    db.session.commit()
    return {
        "success": True,
        "message": f"导入完成：新增 {added}，更新 {updated}，冲突 {len(conflicts)}，跳过删除 {len(skipped_removed)}，跳过锁定 {len(skipped_locked)}。",
        "added": added,
        "updated": updated,
        "conflicts": conflicts,
        "skipped_removed": sorted(set(skipped_removed)),
        "skipped_locked": sorted(set(skipped_locked)),
        "skipped_invalid": sorted(set(skipped_invalid)),
        "remote_count": len(data),
    }
