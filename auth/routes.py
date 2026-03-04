#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
认证路由 (Auth Routes)

提供登录、注册、登出、密码修改、用户管理等路由。
所有路由挂载在 ``/auth/`` 前缀下。
"""

from __future__ import annotations

import os

from flask import (
    Blueprint,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from utils.request_security import (
    _has_admin_access,
    _is_logged_in,
    _is_safe_redirect,
    csrf_token,
)

from . import get_auth_provider
from .models import (
    AuthProjectCreateRequest,
    AuthProjectJoinRequest,
    PlatformRole,
    RequestStatus,
)
from .services import (
    admin_reset_password,
    assign_function,
    change_password,
    get_project_members,
    get_project_pre_assignments,
    get_user_by_id,
    handle_create_project_request,
    handle_join_request,
    list_functions,
    list_pending_create_requests,
    list_pending_join_requests,
    list_users,
    pre_assign_user_to_project,
    register_user,
    remove_function,
    remove_pre_assignment,
    remove_user_from_project,
    request_create_project,
    request_join_project,
    search_users,
    toggle_user_active,
    update_project_member_role,
    update_user_role,
    add_user_to_project,
)

auth_bp = Blueprint(
    "auth_bp",
    __name__,
    url_prefix="/auth",
    template_folder="templates",
)

# Debug 模式：允许注册时选择角色
AUTH_DEBUG_MODE = os.environ.get("AUTH_DEBUG_MODE", "false").lower() in ("1", "true", "yes")
DEBUG_REGISTER_ALLOWED_ROLES = {
    PlatformRole.NORMAL.value,
    PlatformRole.PLATFORM_ADMIN.value,
}


# ──────────────────────────── 登录 / 注册 / 登出 ────────────────────────────


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    """登录页面"""
    next_url = request.args.get("next") or request.form.get("next") or url_for("index")

    # 已登录则直接跳转
    if _is_logged_in():
        if _is_safe_redirect(next_url):
            return redirect(next_url)
        return redirect(url_for("index"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()

        if not username or not password:
            flash("请输入用户名和密码。", "error")
            return render_template(
                "auth_login.html",
                next_url=next_url,
                form_username=username,
            )

        provider = get_auth_provider()
        user = provider.authenticate(username, password)

        # 检查是否认证成功（数据库用户返回 user，环境变量管理员也设置了 session）
        if provider.is_logged_in():
            flash("登录成功。", "success")
            if not _is_safe_redirect(next_url):
                next_url = url_for("index")
            return redirect(next_url)

        flash("用户名或密码错误。", "error")
        return render_template(
            "auth_login.html",
            next_url=next_url,
            form_username=username,
        )

    return render_template("auth_login.html", next_url=next_url, form_username="")


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    """注册页面"""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()
        confirm_password = (request.form.get("password_confirm") or request.form.get("confirm_password") or "").strip()
        display_name = (request.form.get("display_name") or "").strip()
        email = (request.form.get("email") or "").strip()

        # Debug 模式允许选择角色（仅 normal/platform_admin）
        role = PlatformRole.NORMAL.value
        if AUTH_DEBUG_MODE:
            requested_role = (request.form.get("role") or PlatformRole.NORMAL.value).strip()
            if requested_role in DEBUG_REGISTER_ALLOWED_ROLES:
                role = requested_role

        if password != confirm_password:
            flash("两次输入的密码不一致。", "error")
            return render_template(
                "auth_register.html",
                debug_mode=AUTH_DEBUG_MODE,
                form_data={
                    "username": username,
                    "display_name": display_name,
                    "email": email,
                    "role": role,
                },
            )

        user, error = register_user(
            username=username,
            password=password,
            display_name=display_name or None,
            email=email or None,
            role=role,
        )

        if error:
            flash(error, "error")
            return render_template(
                "auth_register.html",
                debug_mode=AUTH_DEBUG_MODE,
                form_data={
                    "username": username,
                    "display_name": display_name,
                    "email": email,
                    "role": role,
                },
            )

        flash(f"注册成功！用户 '{username}' 已创建，请登录。", "success")
        return redirect(url_for("auth_bp.login"))

    return render_template(
        "auth_register.html",
        debug_mode=AUTH_DEBUG_MODE,
        form_data={"role": PlatformRole.NORMAL.value},
    )


@auth_bp.route("/logout")
def logout():
    """登出"""
    session.pop("auth_user_id", None)
    session.pop("auth_username", None)
    session.pop("auth_role", None)
    session.pop("is_admin", None)
    session.pop("admin_user", None)
    session.pop("_csrf_token", None)
    flash("已退出登录。", "success")
    return redirect(url_for("auth_bp.login"))


# ──────────────────────────── 密码修改 ────────────────────────────


@auth_bp.route("/change-password", methods=["GET", "POST"])
def change_password_view():
    """修改密码页面"""
    if not _is_logged_in():
        return redirect(url_for("auth_bp.login"))

    if request.method == "POST":
        user_id = session.get("auth_user_id")
        if not user_id:
            flash("环境变量管理员无法在此修改密码。", "error")
            return render_template("auth_change_password.html")

        old_password = (request.form.get("old_password") or "").strip()
        new_password = (request.form.get("new_password") or "").strip()
        confirm_password = (request.form.get("new_password_confirm") or request.form.get("confirm_password") or "").strip()

        if new_password != confirm_password:
            flash("两次输入的新密码不一致。", "error")
            return render_template("auth_change_password.html")

        success, error = change_password(user_id, old_password, new_password)
        if not success:
            flash(error, "error")
        else:
            flash("密码修改成功。", "success")
            return redirect(url_for("index"))

    return render_template("auth_change_password.html")


# ──────────────────────────── 用户管理 (平台管理员) ────────────────────────────


@auth_bp.route("/users")
def user_list():
    """用户管理页面（仅平台管理员）"""
    if not _has_admin_access():
        flash("此页面仅限平台管理员访问。", "error")
        return redirect(url_for("index"))

    users = list_users(include_inactive=True)
    functions = list_functions()
    # 页面模板依赖 ORM 关系字段（req.user / req.project）和 datetime 类型。
    # 因此这里直接查询模型对象，避免 dict 结构导致模板渲染异常（500）。
    pending_join_requests = (
        AuthProjectJoinRequest.query
        .filter_by(status=RequestStatus.PENDING.value)
        .order_by(AuthProjectJoinRequest.created_at.desc())
        .all()
    )
    pending_create_requests = (
        AuthProjectCreateRequest.query
        .filter_by(status=RequestStatus.PENDING.value)
        .order_by(AuthProjectCreateRequest.created_at.desc())
        .all()
    )
    return render_template(
        "user_management.html",
        users=users,
        functions=functions,
        roles=PlatformRole,
        pending_join_requests=pending_join_requests,
        pending_create_requests=pending_create_requests,
    )


@auth_bp.route("/api/users/<int:user_id>/role", methods=["POST"])
def api_update_user_role(user_id):
    """修改用户角色（API）"""
    if not _has_admin_access():
        return jsonify({"success": False, "message": "权限不足"}), 403

    data = request.get_json(silent=True) or {}
    new_role = data.get("role")
    if not new_role:
        return jsonify({"success": False, "message": "缺少 role 参数"}), 400

    success, error = update_user_role(user_id, new_role)
    if not success:
        return jsonify({"success": False, "message": error}), 400
    return jsonify({"success": True, "message": "角色已更新"})


@auth_bp.route("/api/users/<int:user_id>/toggle-active", methods=["POST"])
def api_toggle_user_active(user_id):
    """启用/禁用用户（API）"""
    if not _has_admin_access():
        return jsonify({"success": False, "message": "权限不足"}), 403

    # 禁止管理员禁用自身账号
    current_user_id = session.get("auth_user_id")
    if current_user_id and current_user_id == user_id:
        return jsonify({"success": False, "message": "不能禁用自己的账号"}), 400

    success, error = toggle_user_active(user_id)
    if not success:
        return jsonify({"success": False, "message": error}), 400
    return jsonify({"success": True, "message": "用户状态已更新"})


@auth_bp.route("/api/users/<int:user_id>/reset-password", methods=["POST"])
def api_reset_password(user_id):
    """管理员重置用户密码（API）"""
    if not _has_admin_access():
        return jsonify({"success": False, "message": "权限不足"}), 403

    data = request.get_json(silent=True) or {}
    new_password = data.get("password", "")
    if not new_password:
        return jsonify({"success": False, "message": "缺少 password 参数"}), 400

    success, error = admin_reset_password(user_id, new_password)
    if not success:
        return jsonify({"success": False, "message": error}), 400
    return jsonify({"success": True, "message": "密码已重置"})


# ──────────────────────────── 职能管理 (API) ────────────────────────────


@auth_bp.route("/api/users/<int:user_id>/functions", methods=["POST"])
def api_assign_function(user_id):
    """为用户分配职能（API）"""
    if not _has_admin_access():
        return jsonify({"success": False, "message": "权限不足"}), 403

    data = request.get_json(silent=True) or {}
    function_id = data.get("function_id")
    project_id = data.get("project_id")  # 可选
    if not function_id:
        return jsonify({"success": False, "message": "缺少 function_id 参数"}), 400

    admin_user_id = session.get("auth_user_id")
    success, error = assign_function(user_id, function_id, project_id, assigned_by=admin_user_id)
    if not success:
        return jsonify({"success": False, "message": error}), 400
    return jsonify({"success": True, "message": "职能已分配"})


@auth_bp.route("/api/users/<int:user_id>/functions/<int:function_id>", methods=["DELETE"])
def api_remove_function(user_id, function_id):
    """移除用户职能（API）"""
    if not _has_admin_access():
        return jsonify({"success": False, "message": "权限不足"}), 403

    project_id = request.args.get("project_id", type=int)
    success, error = remove_function(user_id, function_id, project_id)
    if not success:
        return jsonify({"success": False, "message": error}), 400
    return jsonify({"success": True, "message": "职能已移除"})


# ──────────────────────────── 项目成员管理 ────────────────────────────


@auth_bp.route("/project/<int:project_id>/members")
def project_members(project_id):
    """项目成员管理页面"""
    from utils.request_security import _has_project_admin_access
    if not _has_project_admin_access(project_id):
        flash("此页面仅限项目管理员访问。", "error")
        return redirect(url_for("index"))

    from models.project import Project
    from .models import AuthUserProject
    project = Project.query.get_or_404(project_id)
    members = AuthUserProject.query.filter_by(project_id=project_id).all()
    functions = list_functions()
    return render_template(
        "project_members.html",
        project=project,
        members=members,
        functions=functions,
    )


@auth_bp.route("/api/project/<int:project_id>/members", methods=["POST"])
def api_add_project_member(project_id):
    """添加项目成员（API）"""
    from utils.request_security import _has_project_admin_access
    if not _has_project_admin_access(project_id):
        return jsonify({"success": False, "message": "权限不足"}), 403

    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    role = data.get("role", "member")
    if not user_id:
        return jsonify({"success": False, "message": "缺少 user_id 参数"}), 400

    admin_user_id = session.get("auth_user_id")
    success, error = add_user_to_project(user_id, project_id, role, approved_by=admin_user_id)
    if not success:
        return jsonify({"success": False, "message": error}), 400
    return jsonify({"success": True, "message": "成员已添加"})


@auth_bp.route("/api/project/<int:project_id>/members/<int:user_id>", methods=["DELETE"])
def api_remove_project_member(project_id, user_id):
    """移除项目成员（API）"""
    from utils.request_security import _has_project_admin_access
    if not _has_project_admin_access(project_id):
        return jsonify({"success": False, "message": "权限不足"}), 403

    success, error = remove_user_from_project(user_id, project_id)
    if not success:
        return jsonify({"success": False, "message": error}), 400
    return jsonify({"success": True, "message": "成员已移除"})


@auth_bp.route("/api/project/<int:project_id>/members/<int:user_id>/role", methods=["POST"])
def api_update_member_role(project_id, user_id):
    """修改项目成员角色（API）"""
    from utils.request_security import _has_project_admin_access
    if not _has_project_admin_access(project_id):
        return jsonify({"success": False, "message": "权限不足"}), 403

    data = request.get_json(silent=True) or {}
    new_role = data.get("role")
    if not new_role:
        return jsonify({"success": False, "message": "缺少 role 参数"}), 400

    success, error = update_project_member_role(user_id, project_id, new_role)
    if not success:
        return jsonify({"success": False, "message": error}), 400
    return jsonify({"success": True, "message": "角色已更新"})


# ──────────────────────────── 项目申请 ────────────────────────────


@auth_bp.route("/api/request-join-project", methods=["POST"])
def api_request_join_project():
    """提交项目加入申请（API）"""
    if not _is_logged_in():
        return jsonify({"success": False, "message": "请先登录"}), 401

    data = request.get_json(silent=True) or {}
    project_id = data.get("project_id")
    message = data.get("message", "")
    user_id = session.get("auth_user_id")

    if not project_id or not user_id:
        return jsonify({"success": False, "message": "参数不完整"}), 400

    success, error = request_join_project(user_id, project_id, message)
    if not success:
        return jsonify({"success": False, "message": error}), 400
    return jsonify({"success": True, "message": "申请已提交"})


@auth_bp.route("/api/join-requests/<int:request_id>/handle", methods=["POST"])
def api_handle_join_request(request_id):
    """处理项目加入申请（API）"""
    if not _has_admin_access():
        return jsonify({"success": False, "message": "权限不足"}), 403

    data = request.get_json(silent=True) or {}
    action = data.get("action")  # approve / deny
    if action not in ("approve", "deny"):
        return jsonify({"success": False, "message": "无效的操作"}), 400

    handler_id = session.get("auth_user_id") or 0
    success, error = handle_join_request(request_id, action, handler_id)
    if not success:
        return jsonify({"success": False, "message": error}), 400
    return jsonify({"success": True, "message": f"申请已{'通过' if action == 'approve' else '拒绝'}"})


@auth_bp.route("/api/request-create-project", methods=["POST"])
def api_request_create_project():
    """提交项目创建申请（API）"""
    if not _is_logged_in():
        return jsonify({"success": False, "message": "请先登录"}), 401

    data = request.get_json(silent=True) or {}
    user_id = session.get("auth_user_id")
    if not user_id:
        return jsonify({"success": False, "message": "环境变量管理员请直接创建项目"}), 400

    project_code = data.get("project_code", "").strip()
    project_name = data.get("project_name", "").strip()
    department = data.get("department", "").strip()
    reason = data.get("reason", "").strip()

    success, error = request_create_project(user_id, project_code, project_name, department, reason)
    if not success:
        return jsonify({"success": False, "message": error}), 400
    return jsonify({"success": True, "message": "项目创建申请已提交，请等待管理员审批"})


@auth_bp.route("/api/create-requests/<int:request_id>/handle", methods=["POST"])
def api_handle_create_request(request_id):
    """处理项目创建申请（API）"""
    if not _has_admin_access():
        return jsonify({"success": False, "message": "权限不足"}), 403

    data = request.get_json(silent=True) or {}
    action = data.get("action")  # approve / deny
    if action not in ("approve", "deny"):
        return jsonify({"success": False, "message": "无效的操作"}), 400

    handler_id = session.get("auth_user_id") or 0
    success, error = handle_create_project_request(request_id, action, handler_id)
    if not success:
        return jsonify({"success": False, "message": error}), 400
    return jsonify({"success": True, "message": f"申请已{'通过并创建项目' if action == 'approve' else '拒绝'}"})


# ──────────────────────────── 当前用户信息 (API) ────────────────────────────


@auth_bp.route("/api/me")
def api_current_user():
    """获取当前登录用户信息（API）"""
    if not _is_logged_in():
        return jsonify({"logged_in": False}), 200

    provider = get_auth_provider()
    user = provider.get_current_user()
    if user:
        return jsonify({"logged_in": True, "user": user.to_dict(include_functions=True)})

    # 环境变量管理员
    return jsonify({
        "logged_in": True,
        "user": {
            "id": None,
            "username": session.get("auth_username", session.get("admin_user", "admin")),
            "display_name": "超级管理员",
            "role": "platform_admin",
            "role_display": "平台管理员",
            "is_active": True,
            "functions": [],
        },
    })


# ──────────────────────────── 用户搜索 (API) ────────────────────────────


@auth_bp.route("/api/users/search")
def api_search_users():
    """搜索用户（支持关键词、排除已有成员、分页）

    Query params:
      - q: 关键词（模糊匹配用户名/显示名/邮箱）
      - exclude_project_id: 排除已在该项目中的用户
      - page: 页码（默认1）
      - per_page: 每页数量（默认20，最大50）
    """
    if not _is_logged_in():
        return jsonify({"success": False, "message": "请先登录"}), 401

    keyword = (request.args.get("q") or "").strip()
    exclude_project_id = request.args.get("exclude_project_id", type=int)
    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(50, max(1, request.args.get("per_page", 20, type=int)))

    # 获取需要排除的用户 ID 列表（已是项目成员）
    exclude_ids = None
    if exclude_project_id:
        from .models import AuthUserProject
        existing_memberships = AuthUserProject.query.filter_by(
            project_id=exclude_project_id
        ).all()
        exclude_ids = [m.user_id for m in existing_memberships]

    users, total = search_users(
        keyword,
        exclude_user_ids=exclude_ids,
        only_active=True,
        page=page,
        per_page=per_page,
    )

    return jsonify({
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
        "has_more": (page * per_page) < total,
    })


# ──────────────────────────── 项目成员预分配 (API) ────────────────────────────


@auth_bp.route("/api/project/<int:project_id>/pre-assign", methods=["POST"])
def api_pre_assign_member(project_id):
    """通过用户名预分配项目成员（支持未注册用户）"""
    from utils.request_security import _has_project_admin_access
    if not _has_project_admin_access(project_id):
        return jsonify({"success": False, "message": "权限不足"}), 403

    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    role = data.get("role", "member")
    if not username:
        return jsonify({"success": False, "message": "请输入用户名"}), 400

    admin_user_id = session.get("auth_user_id")
    success, error = pre_assign_user_to_project(username, project_id, role, assigned_by=admin_user_id)
    if not success:
        return jsonify({"success": False, "message": error}), 400

    # 判断是直接添加还是预分配
    from .models import AuthUser
    user_exists = AuthUser.query.filter_by(username=username).first() is not None
    if user_exists:
        return jsonify({"success": True, "message": f"用户 '{username}' 已直接添加到项目"})
    else:
        return jsonify({"success": True, "message": f"用户 '{username}' 尚未注册，已创建预分配记录，该用户注册后将自动加入项目"})


@auth_bp.route("/api/project/<int:project_id>/pre-assignments")
def api_list_pre_assignments(project_id):
    """获取项目的预分配记录列表"""
    from utils.request_security import _has_project_admin_access
    if not _has_project_admin_access(project_id):
        return jsonify({"success": False, "message": "权限不足"}), 403

    include_applied = request.args.get("include_applied", "false").lower() in ("1", "true", "yes")
    records = get_project_pre_assignments(project_id, include_applied=include_applied)
    return jsonify({"success": True, "pre_assignments": records})


@auth_bp.route("/api/pre-assignments/<int:pre_id>", methods=["DELETE"])
def api_remove_pre_assignment(pre_id):
    """删除预分配记录"""
    if not _is_logged_in():
        return jsonify({"success": False, "message": "请先登录"}), 401

    # 需要检查该预分配记录所属项目的管理员权限
    from .models import AuthProjectPreAssignment
    record = AuthProjectPreAssignment.query.get(pre_id)
    if not record:
        return jsonify({"success": False, "message": "预分配记录不存在"}), 404

    from utils.request_security import _has_project_admin_access
    if not _has_project_admin_access(record.project_id):
        return jsonify({"success": False, "message": "权限不足"}), 403

    success, error = remove_pre_assignment(pre_id)
    if not success:
        return jsonify({"success": False, "message": error}), 400
    return jsonify({"success": True, "message": "预分配记录已删除"})
