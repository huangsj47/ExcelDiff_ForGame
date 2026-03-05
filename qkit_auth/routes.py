#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Qkit auth routes."""

from __future__ import annotations

import os
from urllib.parse import urlencode, urlparse

from flask import (
    Blueprint,
    current_app,
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
from werkzeug.routing import BuildError

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
    get_jwt_import_error,
)
from utils.request_security import (
    _has_admin_access,
    _has_project_admin_access,
    _is_logged_in,
    _is_safe_redirect,
)
from utils.logger import log_print

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

_QKITJWT_COOKIE = "qkitjwt"
_QKITJWT_PARTS_COOKIE = "qkitjwt_parts"
_QKITJWT_PART_PREFIX = "qkitjwt_p"
_QKITJWT_CHUNK_SIZE = 3500
_QKITJWT_MAX_PARTS = 8


def _token_fingerprint(token: str) -> str:
    raw = (token or "").strip()
    if not raw:
        return "empty"
    head = raw[:8]
    tail = raw[-6:] if len(raw) > 6 else raw
    return f"len={len(raw)}, head={head}, tail={tail}"


def _auth_center_base(login_host: str) -> str:
    host = str(login_host or "").strip().rstrip("/")
    if host.startswith("http://") or host.startswith("https://"):
        return host
    return f"http://{host}"


def _request_public_base_url(settings) -> str:
    configured = str(getattr(settings, "public_base_url", "") or "").strip().rstrip("/")
    if configured:
        return configured

    forwarded_proto = (request.headers.get("X-Forwarded-Proto") or "").split(",", 1)[0].strip()
    forwarded_host = (request.headers.get("X-Forwarded-Host") or "").split(",", 1)[0].strip()
    scheme = forwarded_proto or request.scheme or "http"
    host = forwarded_host or request.host
    return f"{scheme}://{host}".rstrip("/")


def _resolve_login_service(settings) -> str:
    if getattr(settings, "login_service_explicit", False):
        return settings.login_service
    callback_url = f"{_request_public_base_url(settings)}/qkit_auth/after_login"
    auth_center = _auth_center_base(settings.login_host)
    return f"{auth_center}/openid/login?{urlencode({'next': callback_url})}"


def _resolve_logout_service(settings) -> str:
    if getattr(settings, "logout_service_explicit", False):
        return settings.logout_service
    next_url = _request_public_base_url(settings)
    auth_center = _auth_center_base(settings.login_host)
    return f"{auth_center}/openid/logout?{urlencode({'next': next_url})}"


def _has_routable_endpoint(endpoint: str) -> bool:
    try:
        return any(rule.endpoint == endpoint for rule in current_app.url_map.iter_rules())
    except Exception:
        return False


def _has_local_path(path: str) -> bool:
    target = str(path or "").strip()
    if not target:
        return False
    try:
        return any(rule.rule == target for rule in current_app.url_map.iter_rules())
    except Exception:
        return False


def _render_qkit_unavailable(next_url: str, default_message: str):
    init_error = str(current_app.config.get("AUTH_INIT_ERROR") or "").strip()
    flash(f"Qkit 登录模块初始化失败：{init_error}" if init_error else default_message, "error")
    try:
        return render_template("admin_login.html", next_url=next_url), 503
    except Exception:
        fallback_html = (
            "<h3>Qkit 登录模块不可用</h3>"
            f"<p>{init_error or default_message}</p>"
        )
        return fallback_html, 503


def _clear_qkit_jwt_cookies(response) -> None:
    response.set_cookie(_QKITJWT_COOKIE, "", expires=0)
    response.set_cookie(_QKITJWT_PARTS_COOKIE, "", expires=0)
    for idx in range(_QKITJWT_MAX_PARTS):
        response.set_cookie(f"{_QKITJWT_PART_PREFIX}{idx}", "", expires=0)


def _set_qkit_jwt_cookies(response, token: str) -> None:
    raw = (token or "").strip()
    _clear_qkit_jwt_cookies(response)
    if not raw:
        log_print("[QKIT_COOKIE] skip set qkitjwt: token empty", "INFO")
        return

    cookie_kwargs = {"httponly": True, "samesite": "Lax"}
    if len(raw) <= _QKITJWT_CHUNK_SIZE:
        response.set_cookie(_QKITJWT_COOKIE, raw, **cookie_kwargs)
        log_print(
            f"[QKIT_COOKIE] set single cookie, token={_token_fingerprint(raw)}",
            "INFO",
        )
        return

    chunks = [raw[i:i + _QKITJWT_CHUNK_SIZE] for i in range(0, len(raw), _QKITJWT_CHUNK_SIZE)]
    if len(chunks) > _QKITJWT_MAX_PARTS:
        current_app.logger.warning(
            "Qkit jwt size=%s exceeds supported multipart cookies, fallback to single cookie",
            len(raw),
        )
        response.set_cookie(_QKITJWT_COOKIE, raw, **cookie_kwargs)
        log_print(
            f"[QKIT_COOKIE] oversize fallback single cookie, parts={len(chunks)}, token={_token_fingerprint(raw)}",
            "INFO",
        )
        return

    response.set_cookie(_QKITJWT_PARTS_COOKIE, str(len(chunks)), **cookie_kwargs)
    for idx, chunk in enumerate(chunks):
        response.set_cookie(f"{_QKITJWT_PART_PREFIX}{idx}", chunk, **cookie_kwargs)
    log_print(
        f"[QKIT_COOKIE] set multipart cookies, part_count={len(chunks)}, token={_token_fingerprint(raw)}",
        "INFO",
    )


def _set_user_session(user: QkitAuthUser, token: str | None = None) -> None:
    settings = load_qkit_settings()
    normalized_token = (token or "").strip()
    session["auth_user_id"] = user.id
    session["auth_username"] = user.username
    session["auth_role"] = user.role
    session["is_admin"] = bool(user.is_platform_admin)
    session["admin_user"] = user.username if user.is_platform_admin else None
    session["auth_backend"] = "qkit"
    if settings.local_jwt_cache:
        # Cookie mode: avoid writing raw qkitjwt into Flask session cookie.
        session.pop("qkitjwt_session", None)
    else:
        # Session mode: fallback for environments where browser cookie caching is restricted.
        session["qkitjwt_session"] = normalized_token
    session.permanent = True


def _clear_user_session() -> None:
    for key in (
        "auth_user_id",
        "auth_username",
        "auth_role",
        "is_admin",
        "admin_user",
        "qkit_backhost",
        "qkitjwt_session",
        "_csrf_token",
        "auth_backend",
    ):
        session.pop(key, None)


def _qkit_login_redirect(next_url: str):
    if next_url and _is_safe_redirect(next_url):
        session["qkit_backhost"] = next_url
    if not _has_routable_endpoint("qkit_auth_bp.login"):
        return _render_qkit_unavailable(next_url, "Qkit 登录模块未完整注册，请检查启动日志。")
    try:
        return redirect(url_for("qkit_auth_bp.login", next=next_url))
    except BuildError:
        return _render_qkit_unavailable(next_url, "Qkit 登录模块路由不可用，请检查启动日志。")


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


@qkit_auth_bp.route("/login", methods=["GET"], endpoint="login", strict_slashes=False)
def qkit_login():
    next_url = request.args.get("next") or request.referrer or url_for("index")
    if next_url and _is_safe_redirect(next_url):
        session["qkit_backhost"] = next_url
    settings = load_qkit_settings()
    login_service = _resolve_login_service(settings)
    log_print(
        (
            "[QKIT_LOGIN] enter "
            f"host={request.host}, path={request.path}, next={next_url}, "
            f"local_cache={settings.local_jwt_cache}, login_service={login_service}"
        ),
        "INFO",
    )

    parsed = urlparse(login_service)
    # 防止配置误指向当前服务但未提供 /openid/login 时的 302 死循环。
    if (not parsed.netloc or parsed.netloc == request.host) and parsed.path.startswith("/openid/login"):
        if not _has_local_path(parsed.path):
            return _render_qkit_unavailable(
                next_url,
                (
                    "QKIT_LOGIN_SERVICE 指向当前服务的 /openid/login，"
                    "但当前服务未提供该路由。请将 QKIT_LOGIN_HOST/QKIT_LOGIN_SERVICE "
                    "配置为统一认证中心地址。"
                ),
            )

    response = make_response(redirect(login_service))
    _clear_qkit_jwt_cookies(response)
    # Always clear session token before login round-trip.
    session.pop("qkitjwt_session", None)
    log_print("[QKIT_LOGIN] redirecting to qkit auth center", "INFO")
    return response


@qkit_auth_bp.route("/after_login", methods=["GET"], endpoint="after_login", strict_slashes=False)
def after_login():
    token = (request.args.get("qkitjwt") or "").strip()
    log_print(
        f"[QKIT_AFTER_LOGIN] callback host={request.host}, token={_token_fingerprint(token)}",
        "INFO",
    )
    if not token:
        flash("Qkit 登录失败：缺少 qkitjwt。", "error")
        log_print("[QKIT_AFTER_LOGIN] failed: missing qkitjwt in callback query", "INFO")
        return redirect(url_for("qkit_auth_bp.login"))

    valid, message, _payload = check_qkit_jwt_remote(token)
    if not valid:
        flash(message or "Qkit 登录校验失败，请重试。", "error")
        log_print(f"[QKIT_AFTER_LOGIN] jwt remote verify failed: {message or 'unknown'}", "INFO")
        return redirect(url_for("qkit_auth_bp.login"))

    payload = decode_qkit_jwt_unsafe(token)
    if not payload:
        jwt_dep_error = str(get_jwt_import_error() or "").strip()
        if jwt_dep_error:
            return _render_qkit_unavailable(
                request.args.get("next") or request.referrer or url_for("index"),
                f"Qkit 登录依赖缺失（PyJWT）：{jwt_dep_error}",
            )
        flash("Qkit 登录失败：无法解析用户身份。", "error")
        log_print("[QKIT_AFTER_LOGIN] failed: cannot decode jwt payload", "INFO")
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
        log_print(f"[QKIT_AFTER_LOGIN] failed: ensure user error={err or 'unknown'}", "INFO")
        return redirect(url_for("qkit_auth_bp.login"))
    if not user.is_active:
        flash("账号已被禁用，请联系管理员。", "error")
        log_print(f"[QKIT_AFTER_LOGIN] failed: inactive user username={user.username}", "INFO")
        return redirect(url_for("qkit_auth_bp.login"))

    # ensure_qkit_user() only flushes newly created users.
    # We must commit here; otherwise the request teardown rolls back and
    # next request cannot load auth_user_id from DB.
    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("Qkit login user persist failed: %s", exc)
        flash("Qkit 登录失败：用户数据保存异常，请重试或联系管理员。", "error")
        log_print(f"[QKIT_AFTER_LOGIN] failed: db commit error={exc}", "INFO")
        return redirect(url_for("qkit_auth_bp.login"))

    settings = load_qkit_settings()
    _set_user_session(user, token=token)
    next_url = session.pop("qkit_backhost", None) or url_for("index")
    if not _is_safe_redirect(next_url):
        next_url = url_for("index")

    response = make_response(redirect(next_url))
    if settings.local_jwt_cache:
        # Keep qkitjwt in dedicated cookie to avoid oversized Flask session payload.
        _set_qkit_jwt_cookies(response, token)
    else:
        _clear_qkit_jwt_cookies(response)
    log_print(
        (
            "[QKIT_AFTER_LOGIN] success "
            f"user_id={session.get('auth_user_id')}, username={session.get('auth_username')}, "
            f"next={next_url}, local_cache={settings.local_jwt_cache}"
        ),
        "INFO",
    )
    return response


@qkit_auth_bp.route("/logout", methods=["GET"], endpoint="logout", strict_slashes=False)
def qkit_logout():
    settings = load_qkit_settings()
    _clear_user_session()
    logout_service = _resolve_logout_service(settings)
    response = make_response(redirect(logout_service))
    _clear_qkit_jwt_cookies(response)
    log_print(
        f"[QKIT_LOGOUT] clear session/cookies and redirect host={request.host} -> {logout_service}",
        "INFO",
    )
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

