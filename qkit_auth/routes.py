#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Qkit auth routes."""

from __future__ import annotations

import os

from flask import (
    Blueprint,
    flash,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)

from models import Project, db
from qkit_auth.config import load_qkit_settings
from qkit_auth.models import (
    QkitAuthProjectJoinRequest,
    QkitAuthUser,
    QkitAuthUserProject,
)
from qkit_auth.services import (
    add_user_to_project,
    check_qkit_jwt_remote,
    decode_qkit_jwt_unsafe,
    ensure_qkit_user,
    extract_identity_from_payload,
    get_project_import_config,
    get_project_members,
    handle_create_project_request,
    handle_join_request,
    import_project_users_from_redmine,
    list_pending_create_requests,
    list_pending_join_requests,
    list_users,
    remove_user_from_project,
    request_create_project,
    request_join_project,
    search_users,
    toggle_user_active,
    update_project_member_role,
    update_user_role,
    upsert_project_import_config,
)
from utils.request_security import (
    _has_admin_access,
    _has_project_admin_access,
    _is_logged_in,
    _is_safe_redirect,
)

auth_bp = Blueprint(
    "auth_bp",
    __name__,
    url_prefix="/auth",
    template_folder="templates",
)

qkit_auth_bp = Blueprint(
    "qkit_auth_bp",
    __name__,
    url_prefix="/qkit_auth",
    template_folder="templates",
)


def _set_user_session(user: QkitAuthUser) -> None:
    session["auth_user_id"] = user.id
    session["auth_username"] = user.username
    session["auth_role"] = user.role
    session["is_admin"] = bool(user.is_platform_admin)
    session["admin_user"] = user.username if user.is_platform_admin else None
    session["auth_backend"] = "qkit"
    session.permanent = True


def _clear_user_session() -> None:
    for key in (
        "auth_user_id",
        "auth_username",
        "auth_role",
        "is_admin",
        "admin_user",
        "qkit_backhost",
        "_csrf_token",
        "auth_backend",
    ):
        session.pop(key, None)


def _qkit_login_redirect(next_url: str):
    if next_url and _is_safe_redirect(next_url):
        session["qkit_backhost"] = next_url
    return redirect(url_for("qkit_auth_bp.login", next=next_url))


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    next_url = request.args.get("next") or request.form.get("next") or url_for("index")
    return _qkit_login_redirect(next_url)


@auth_bp.route("/logout")
def logout():
    return redirect(url_for("qkit_auth_bp.logout"))


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    flash("Qkit 登录模式下不支持平台注册账号。", "error")
    return _qkit_login_redirect(request.args.get("next") or url_for("index"))


@auth_bp.route("/change-password", methods=["GET", "POST"])
def change_password_view():
    flash("Qkit 登录模式下不支持在平台内修改密码。", "error")
    return redirect(url_for("index"))


@qkit_auth_bp.route("/login", methods=["GET"], endpoint="login")
def qkit_login():
    next_url = request.args.get("next") or request.referrer or url_for("index")
    if next_url and _is_safe_redirect(next_url):
        session["qkit_backhost"] = next_url
    settings = load_qkit_settings()
    response = make_response(redirect(settings.login_service))
    if settings.local_jwt_cache:
        response.set_cookie("qkitjwt", "", expires=0)
    return response


@qkit_auth_bp.route("/after_login", methods=["GET"], endpoint="after_login")
def after_login():
    token = (request.args.get("qkitjwt") or "").strip()
    if not token:
        flash("Qkit 登录失败：缺少 qkitjwt。", "error")
        return redirect(url_for("qkit_auth_bp.login"))

    valid, message, _payload = check_qkit_jwt_remote(token)
    if not valid:
        flash(message or "Qkit 登录校验失败，请重试。", "error")
        return redirect(url_for("qkit_auth_bp.login"))

    payload = decode_qkit_jwt_unsafe(token)
    if not payload:
        flash("Qkit 登录失败：无法解析用户身份。", "error")
        return redirect(url_for("qkit_auth_bp.login"))

    identity = extract_identity_from_payload(payload)
    user, err = ensure_qkit_user(
        username=identity["username"],
        display_name=identity["display_name"],
        email=identity["email"],
        source="qkit_login",
    )
    if err or user is None:
        flash(err or "Qkit 登录失败，无法同步用户。", "error")
        return redirect(url_for("qkit_auth_bp.login"))
    if not user.is_active:
        flash("账号已被禁用，请联系管理员。", "error")
        return redirect(url_for("qkit_auth_bp.login"))

    _set_user_session(user)
    next_url = session.pop("qkit_backhost", None) or url_for("index")
    if not _is_safe_redirect(next_url):
        next_url = url_for("index")

    settings = load_qkit_settings()
    response = make_response(redirect(next_url))
    if settings.local_jwt_cache:
        response.set_cookie(
            "qkitjwt",
            token,
            httponly=True,
            samesite="Lax",
        )
    return response


@qkit_auth_bp.route("/logout", methods=["GET"], endpoint="logout")
def qkit_logout():
    settings = load_qkit_settings()
    _clear_user_session()
    response = make_response(redirect(settings.logout_service))
    response.set_cookie("qkitjwt", "", expires=0)
    return response


@qkit_auth_bp.route("/assets/project-name-simple-image", methods=["GET"], endpoint="project_name_hint_image")
def project_name_hint_image():
    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "img"))
    return send_from_directory(root_dir, "project_name_simple.png")


@auth_bp.route("/users")
def user_list():
    if not _has_admin_access():
        flash("此页面仅限平台管理员访问。", "error")
        return redirect(url_for("index"))
    users = list_users(include_inactive=True)
    pending_join_requests = list_pending_join_requests()
    pending_create_requests = list_pending_create_requests()
    projects = Project.query.order_by(Project.code.asc()).all()
    return render_template(
        "qkit_user_management.html",
        users=users,
        pending_join_requests=pending_join_requests,
        pending_create_requests=pending_create_requests,
        projects=projects,
    )


@auth_bp.route("/api/users/<int:user_id>/role", methods=["POST"])
def api_update_user_role(user_id):
    if not _has_admin_access():
        return jsonify({"success": False, "message": "权限不足"}), 403
    data = request.get_json(silent=True) or {}
    role = data.get("role")
    if not role:
        return jsonify({"success": False, "message": "缺少 role 参数"}), 400
    ok, err = update_user_role(user_id, role)
    if not ok:
        return jsonify({"success": False, "message": err}), 400
    return jsonify({"success": True, "message": "角色已更新"})


@auth_bp.route("/api/users/<int:user_id>/toggle-active", methods=["POST"])
def api_toggle_user_active(user_id):
    if not _has_admin_access():
        return jsonify({"success": False, "message": "权限不足"}), 403
    current_user_id = session.get("auth_user_id")
    if current_user_id and int(current_user_id) == user_id:
        return jsonify({"success": False, "message": "不能禁用自己的账号"}), 400
    ok, err = toggle_user_active(user_id)
    if not ok:
        return jsonify({"success": False, "message": err}), 400
    return jsonify({"success": True, "message": "用户状态已更新"})


@auth_bp.route("/api/users/<int:user_id>/reset-password", methods=["POST"])
def api_reset_password_disabled(user_id):
    return jsonify({"success": False, "message": "Qkit 模式下不支持重置本地密码"}), 400


@auth_bp.route("/project/<int:project_id>/members")
def project_members(project_id):
    if not _has_project_admin_access(project_id):
        flash("此页面仅限项目管理员访问。", "error")
        return redirect(url_for("index"))
    project = Project.query.get_or_404(project_id)
    members = get_project_members(project_id)
    import_config = get_project_import_config(project_id)
    return render_template(
        "qkit_project_members.html",
        project=project,
        members=members,
        import_config=import_config,
    )


@auth_bp.route("/api/project/<int:project_id>/members", methods=["POST"])
def api_add_project_member(project_id):
    if not _has_project_admin_access(project_id):
        return jsonify({"success": False, "message": "权限不足"}), 403
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    role = data.get("role", "member")
    function_name = data.get("function_name")

    if user_id is None:
        username = (data.get("username") or "").strip()
        if not username:
            return jsonify({"success": False, "message": "缺少 user_id 或 username 参数"}), 400
        user = QkitAuthUser.query.filter_by(username=username).first()
        if user is None:
            user, err = ensure_qkit_user(
                username=username,
                display_name=username,
                email=None,
                source="manual",
            )
            if err or user is None:
                return jsonify({"success": False, "message": err or "创建用户失败"}), 400
        user_id = user.id

    admin_user_id = session.get("auth_user_id")
    ok, err = add_user_to_project(
        int(user_id),
        project_id,
        role,
        function_name=function_name,
        approved_by=admin_user_id,
        imported_from_qkit=False,
        import_sync_locked=False,
    )
    if not ok:
        return jsonify({"success": False, "message": err}), 400
    return jsonify({"success": True, "message": "成员已添加"})


@auth_bp.route("/api/project/<int:project_id>/members/<int:user_id>", methods=["DELETE"])
def api_remove_project_member(project_id, user_id):
    if not _has_project_admin_access(project_id):
        return jsonify({"success": False, "message": "权限不足"}), 403
    actor = session.get("auth_user_id")
    ok, err = remove_user_from_project(user_id, project_id, removed_by=actor)
    if not ok:
        return jsonify({"success": False, "message": err}), 400
    return jsonify({"success": True, "message": "成员已移除，并阻断后续自动导入"})


@auth_bp.route("/api/project/<int:project_id>/members/<int:user_id>/role", methods=["POST"])
def api_update_member_role(project_id, user_id):
    if not _has_project_admin_access(project_id):
        return jsonify({"success": False, "message": "权限不足"}), 403
    data = request.get_json(silent=True) or {}
    role = data.get("role")
    if not role:
        return jsonify({"success": False, "message": "缺少 role 参数"}), 400
    ok, err = update_project_member_role(user_id, project_id, role, lock_import_sync=True)
    if not ok:
        return jsonify({"success": False, "message": err}), 400
    return jsonify({"success": True, "message": "成员角色已更新，后续导入不会覆盖该成员权限"})


@auth_bp.route("/api/users/search")
def api_search_users():
    if not _is_logged_in():
        return jsonify({"success": False, "message": "请先登录"}), 401
    keyword = (request.args.get("q") or "").strip()
    exclude_project_id = request.args.get("exclude_project_id", type=int)
    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(50, max(1, request.args.get("per_page", 20, type=int)))

    exclude_ids = None
    if exclude_project_id:
        existing = QkitAuthUserProject.query.filter_by(project_id=exclude_project_id).all()
        exclude_ids = [row.user_id for row in existing]

    users, total = search_users(
        keyword,
        exclude_user_ids=exclude_ids,
        only_active=True,
        page=page,
        per_page=per_page,
    )
    return jsonify(
        {
            "success": True,
            "users": [
                {
                    "id": u.id,
                    "username": u.username,
                    "display_name": u.display_name or u.username,
                    "email": u.email or "",
                    "role": u.role,
                }
                for u in users
            ],
            "total": total,
            "page": page,
            "per_page": per_page,
            "has_more": page * per_page < total,
        }
    )


@auth_bp.route("/api/project/<int:project_id>/qkit-import-config", methods=["GET"])
def api_get_project_qkit_import_config(project_id):
    if not _has_project_admin_access(project_id):
        return jsonify({"success": False, "message": "权限不足"}), 403
    config = get_project_import_config(project_id)
    if not config:
        return jsonify(
            {
                "success": True,
                "config": {
                    "token": "",
                    "token_masked": "",
                    "host": "",
                    "project_name": "",
                },
            }
        )
    return jsonify(
        {
            "success": True,
            "config": {
                "token": config.token or "",
                "token_masked": config.masked_token,
                "host": config.host or "",
                "project_name": config.project_name or "",
            },
        }
    )


@auth_bp.route("/api/project/<int:project_id>/qkit-import-config", methods=["POST"])
def api_save_project_qkit_import_config(project_id):
    if not _has_project_admin_access(project_id):
        return jsonify({"success": False, "message": "权限不足"}), 403
    data = request.get_json(silent=True) or {}
    cfg, err = upsert_project_import_config(
        project_id,
        token=data.get("token", ""),
        host=data.get("host", ""),
        project_name=data.get("project_name", ""),
        updated_by=session.get("auth_user_id"),
    )
    if err or cfg is None:
        return jsonify({"success": False, "message": err or "保存失败"}), 400
    return jsonify({"success": True, "message": "导入配置已保存"})


@auth_bp.route("/api/project/<int:project_id>/qkit-import", methods=["POST"])
def api_import_project_users(project_id):
    if not _has_project_admin_access(project_id):
        return jsonify({"success": False, "message": "权限不足"}), 403
    result = import_project_users_from_redmine(
        project_id=project_id,
        operator_user_id=session.get("auth_user_id"),
    )
    status_code = 200 if result.get("success") else 400
    return jsonify(result), status_code


@auth_bp.route("/api/request-join-project", methods=["POST"])
def api_request_join_project():
    if not _is_logged_in():
        return jsonify({"success": False, "message": "请先登录"}), 401
    data = request.get_json(silent=True) or {}
    project_id = data.get("project_id")
    message = data.get("message", "")
    user_id = session.get("auth_user_id")
    if not project_id or not user_id:
        return jsonify({"success": False, "message": "参数不完整"}), 400
    ok, err = request_join_project(int(user_id), int(project_id), message)
    if not ok:
        return jsonify({"success": False, "message": err}), 400
    return jsonify({"success": True, "message": "申请已提交"})


@auth_bp.route("/api/join-requests/<int:request_id>/handle", methods=["POST"])
def api_handle_join_request(request_id):
    if not _has_admin_access():
        return jsonify({"success": False, "message": "权限不足"}), 403
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    if action not in {"approve", "deny"}:
        return jsonify({"success": False, "message": "无效操作"}), 400
    handler_id = session.get("auth_user_id") or 0
    ok, err = handle_join_request(request_id, action, int(handler_id))
    if not ok:
        return jsonify({"success": False, "message": err}), 400
    return jsonify({"success": True, "message": f"申请已{'通过' if action == 'approve' else '拒绝'}"})


@auth_bp.route("/api/request-create-project", methods=["POST"])
def api_request_create_project():
    if not _is_logged_in():
        return jsonify({"success": False, "message": "请先登录"}), 401
    data = request.get_json(silent=True) or {}
    user_id = session.get("auth_user_id")
    if not user_id:
        return jsonify({"success": False, "message": "请先登录"}), 401
    ok, err = request_create_project(
        int(user_id),
        data.get("project_code", ""),
        data.get("project_name", ""),
        data.get("department", ""),
        data.get("reason", ""),
    )
    if not ok:
        return jsonify({"success": False, "message": err}), 400
    return jsonify({"success": True, "message": "项目创建申请已提交，请等待管理员审批"})


@auth_bp.route("/api/create-requests/<int:request_id>/handle", methods=["POST"])
def api_handle_create_request(request_id):
    if not _has_admin_access():
        return jsonify({"success": False, "message": "权限不足"}), 403
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    if action not in {"approve", "deny"}:
        return jsonify({"success": False, "message": "无效操作"}), 400
    handler_id = session.get("auth_user_id") or 0
    ok, err = handle_create_project_request(request_id, action, int(handler_id))
    if not ok:
        return jsonify({"success": False, "message": err}), 400
    return jsonify({"success": True, "message": f"申请已{'通过并创建项目' if action == 'approve' else '拒绝'}"})


@auth_bp.route("/api/me")
def api_current_user():
    if not _is_logged_in():
        return jsonify({"logged_in": False}), 200
    user_id = session.get("auth_user_id")
    if not user_id:
        return jsonify({"logged_in": False}), 200
    user = db.session.get(QkitAuthUser, int(user_id))
    if not user or not user.is_active:
        return jsonify({"logged_in": False}), 200
    return jsonify({"logged_in": True, "user": user.to_dict()})
