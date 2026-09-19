"""Security and template bootstrap helpers extracted from app.py."""

from __future__ import annotations

import hmac
import os

from flask import jsonify, render_template, request, session, url_for
from werkzeug.routing import BuildError
from werkzeug.exceptions import Forbidden, NotFound


# ---------------------------------------------------------------------------
# 平台管理员专属 endpoint 表
#
# 【这些名字必须写**全限定**形式（`<blueprint>.<endpoint>`）】
#
# 原实现写的是裸名（`"delete_repository"` 等 19 条），而 Flask 在注册蓝图路由时
# 会给 endpoint 加上蓝图前缀 —— **即使显式传了 `endpoint=` 也一样**：
#
#     routes/core_management_routes.py
#     @core_management_bp.route("/repositories/<id>/delete", methods=["POST"],
#                               endpoint="delete_repository")
#
#     → app.url_map 里的 endpoint 是 "core_management_routes.delete_repository"
#
# 而判定处写的是 `request.endpoint in SENSITIVE_ENDPOINTS`，裸名永远匹配不上，
# 于是这两张表**整体是死代码**（同文件 `AUTH_EXEMPT_ENDPOINTS` 同时写了
# `"index"` 与 `"core_management_routes.index"` 两种形式，说明作者在那边意识到了
# 前缀问题，这里漏了）。
#
# 【为什么不改成「取裸名再比对」】
# 实测存在短名冲突（`projects` 同时属于 core_management_routes 与当时的
# project_bp —— 后者所在的遗留路由包从未被注册，现已删除；
# `index`/`help_page`/`test`/`project_detail` 等也有多蓝图同名）。取裸名会让
# 同名端点被一并当成平台管理员专属，误伤正常功能。所以这里统一
# 改写成全限定名精确匹配。
#
# 【为什么少了三个原本列在表里的项目级页面】
# `edit_repository` / `add_git_repository` / `add_svn_repository` 原本也在表里。
# 它们是**项目级**页面，各自在函数体内用 `_has_project_access` /
# `_has_project_admin_access` 判权（后者允许项目管理员）。若把它们改成生效的
# 平台管理员门禁，会**把项目管理员挡在门外** —— 那是功能回退，不是加固。
# 因此保留其函数体内的项目级判定，从本表移除。`edit_repository` 原先连函数体
# 判定都没有（表又失效 → 完全无鉴权），已在
# `services/repository_misc_page_service.py::render_edit_repository_page` 补上。
#
# 说明：下表多数 handler 自身已带 `@require_admin` 装饰器，本表是**第二道**防线
# （纵深防御）—— 万一将来有人漏加装饰器，这里仍然拦得住。
# ---------------------------------------------------------------------------
SENSITIVE_ENDPOINTS = {
    "core_management_routes.batch_update_credentials",
    "core_management_routes.clear_all_confirmation_status",
    "core_management_routes.create_git_repository",
    "core_management_routes.create_svn_repository",
    "core_management_routes.delete_project",
    "core_management_routes.delete_repository",
    "core_management_routes.regenerate_cache",
    "core_management_routes.retry_clone_repository",
    "core_management_routes.reuse_repository_and_update",
    "core_management_routes.swap_repository_order",
    "core_management_routes.sync_repository",
    "core_management_routes.test_repository",
    "core_management_routes.update_repository",
    "core_management_routes.update_repository_and_cache",
    "core_management_routes.update_repository_order",
    "commit_diff_routes.batch_update_commits_compat",
    "commit_diff_routes.update_commit_fields_route",
    # **全量重置消耗统计**：删掉全部 `ai_analysis_run` / `_trace` / `_anomaly`
    # （连同 AI 报告正文），是这个平台上唯一一个「一点就删一大片、且没有撤销」的动作。
    # handler 自带 `@require_admin`，这里加的是**第二道**防线 —— 与上面「万一将来有人
    # 漏加装饰器，这里仍然拦得住」同一条理由。
    #
    # 同一个卡片里那个「设置统计起点」**不进这张表**：它只改消耗面板的统计口径，
    # 一条记录都不删（可逆、随时恢复），把它按破坏性动作那样加固会让人觉得它同样危险。
    "ai_analysis_routes.ai_statistics_reset",
}

WRITE_PROTECTED_ENDPOINTS = {
    "core_management_routes.projects",
    "core_management_routes.repository_config",
}

AUTH_EXEMPT_ENDPOINTS = frozenset(
    {
        "static",
        "index",
        "core_management_routes.index",
        "admin_login",
        "admin_logout",
        "auth_bp.login",
        "auth_bp.register",
        "auth_bp.logout",
        "qkit_auth_bp.login",
        "qkit_auth_bp.after_login",
        "qkit_auth_bp.logout",
        "qkit_auth_bp.project_name_hint_image",
        "help_page",
        "core_management_routes.help_page",
        "test",
    }
)

AUTH_EXEMPT_PATHS = (
    "/static/",
    "/openid/",
    "/favicon.ico",
    "/auth/login",
    "/auth/register",
    "/auth/logout",
    "/qkit_auth/login",
    "/qkit_auth/after_login",
    "/qkit_auth/logout",
    "/qkit_auth/assets/",
    "/help",
    "/api/agents/",
    # 存活探针：负载均衡 / systemd watchdog / k8s probe 拿不到会话 Cookie，
    # 需要鉴权的探针根本没法用。响应体不含版本号、路径、连接串、表名。
    # 注意它仍受 CSRF 校验约束，但 enforce_csrf 对 GET 直接放行，而探针只会用 GET。
    "/healthz",
)

APP_SECURITY_AUTH_BACKEND_IMPORT_ERRORS = (ImportError, RuntimeError, AttributeError)
APP_SECURITY_PUBLIC_LOGIN_DISCOVERY_ERRORS = (RuntimeError, AttributeError, TypeError)
APP_SECURITY_PUBLIC_LOGIN_BUILD_ERRORS = (BuildError, RuntimeError, AttributeError, TypeError)
APP_SECURITY_PUBLIC_REGISTER_DISCOVERY_ERRORS = (RuntimeError, AttributeError, TypeError)
APP_SECURITY_PUBLIC_REGISTER_BUILD_ERRORS = (BuildError, RuntimeError, AttributeError, TypeError)


def _prefers_json_error_response() -> bool:
    accept = str(request.headers.get("Accept", "") or "").lower()
    if (
        "text/html" not in accept
        and request.path.startswith(("/api/", "/commits/", "/repositories/", "/weekly-version-config/"))
    ):
        return True
    return (
        request.path.startswith("/api/")
        or request.is_json
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in accept
    )


def _infer_resource_label_from_path(path: str) -> str:
    raw = str(path or "").lower()
    if "/repositories/" in raw:
        return "仓库页面"
    if "/weekly-version-config/" in raw or "/weekly-version" in raw:
        return "周版本页面"
    if "/projects/" in raw:
        return "项目页面"
    return "页面"


def public_register_enabled_by_env() -> bool:
    """`AUTH_ENABLE_REGISTER` 的判定。**视图与模板共用这一份。**

    为什么要单独抽出来：这个开关原来只挂进 Jinja globals（`configure_app_security_bootstrap`
    里那一条），作用是「不渲染注册入口按钮」—— 而 `POST /auth/register` 的视图**不查它**，
    于是 `AUTH_ENABLE_REGISTER=false` 时直接 POST 照样注册成功。运维把它当「关掉注册」用，
    实际只是把入口藏起来了（配上 `AUTH_DEBUG_MODE=true` 那条自助当管理员的路，后果更重）。
    判定只此一份，两边都读它。
    """
    auth_backend = str(os.environ.get("AUTH_BACKEND", "local") or "local").strip().lower()
    default_enabled = auth_backend != "qkit"
    raw = str(os.environ.get("AUTH_ENABLE_REGISTER", "") or "").strip().lower()
    if not raw:
        return default_enabled
    return raw in {"1", "true", "yes", "on"}


def configure_app_security_bootstrap(
    *,
    app,
    log_print,
    csrf_session_key,
    enable_admin_security,
    deployment_mode,
    csrf_token,
    has_admin_access,
    is_logged_in,
    get_current_user,
    has_project_access,
    has_project_admin_access,
    is_valid_admin_token,
    unauthorized_admin_response,
    unauthorized_login_response,
    has_project_create_access,
    csrf_token_from_request,
    csrf_error_response,
    is_same_origin_request,
    get_excel_column_letter,
    format_beijing_time,
):
    """Register security hooks, error handlers and template globals."""

    @app.before_request
    def enforce_admin_access():
        if not enable_admin_security:
            return None
        if request.endpoint in AUTH_EXEMPT_ENDPOINTS:
            return None
        if any(request.path.startswith(path) for path in AUTH_EXEMPT_PATHS):
            return None
        if is_valid_admin_token():
            return None
        if not is_logged_in():
            return unauthorized_login_response()
        if request.path.startswith("/admin/") or request.endpoint in SENSITIVE_ENDPOINTS:
            if not has_admin_access():
                return unauthorized_admin_response()
        if request.endpoint in WRITE_PROTECTED_ENDPOINTS:
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                if request.endpoint == "core_management_routes.projects":
                    if not has_project_create_access():
                        return unauthorized_admin_response()
                elif not has_admin_access():
                    return unauthorized_admin_response()
        return None

    @app.before_request
    def enforce_csrf():
        if request.method in {"GET", "HEAD", "OPTIONS", "TRACE"}:
            return None
        if request.endpoint in {"static"}:
            return None
        if request.path.startswith("/api/agents/"):
            return None
        if is_valid_admin_token():
            return None

        expected = session.get(csrf_session_key)
        provided = csrf_token_from_request()
        if not (expected and provided and hmac.compare_digest(str(expected), str(provided))):
            # 只在「会话里确实有 token」时记一条：那说明请求来自一个真实登录态，
            # 却带着对不上的 token（会话被重建 / 页面是旧的），是真正要排查的异常。
            # 未登录来源必然也没有 expected，不记录 —— 否则任何人裸 POST 一下
            # 就能把这个分支刷成日志洪水。token 本身**不入日志**，只记有无。
            if expected:
                log_print(
                    f"CSRF 校验失败: path={request.path} method={request.method} "
                    f"请求token={'有' if provided else '无'} "
                    f"auth_backend={session.get('auth_backend') or '-'} "
                    f"→ 会话里有 token 但请求对不上：通常是会话被重建"
                    f"（进程重启 / FLASK_SECRET_KEY 变更 / 浏览器丢 cookie），"
                    f"而用户手上的页面还是旧的；刷新页面即可恢复",
                    "ERROR",
                )
            return csrf_error_response("CSRF token invalid or missing.")
        if not is_same_origin_request():
            return csrf_error_response("Cross-site request blocked.")
        return None

    @app.errorhandler(NotFound)
    def handle_not_found(_error):
        if _prefers_json_error_response():
            return jsonify({"success": False, "message": "资源不存在或已被删除"}), 404
        resource_label = _infer_resource_label_from_path(request.path)
        return (
            render_template(
                "resource_access_error.html",
                error_code=404,
                page_title="页面不存在",
                resource_label=resource_label,
                message=f"当前{resource_label}不存在，可能已被删除或访问链接已失效。",
            ),
            404,
        )

    @app.errorhandler(Forbidden)
    def handle_forbidden(_error):
        if _prefers_json_error_response():
            return jsonify({"success": False, "message": "权限不足"}), 403
        resource_label = _infer_resource_label_from_path(request.path)
        return (
            render_template(
                "resource_access_error.html",
                error_code=403,
                page_title="权限不足",
                resource_label=resource_label,
                message=f"当前账号对该{resource_label}权限不足，请联系项目管理员或平台管理员授权。",
            ),
            403,
        )

    app.jinja_env.globals["get_excel_column_letter"] = get_excel_column_letter
    app.jinja_env.globals["csrf_token"] = csrf_token
    app.jinja_env.globals["is_admin"] = has_admin_access
    app.jinja_env.globals["is_logged_in"] = is_logged_in
    app.jinja_env.globals["get_current_user"] = get_current_user
    app.jinja_env.globals["has_project_access"] = has_project_access
    app.jinja_env.globals["has_project_admin_access"] = has_project_admin_access
    app.jinja_env.globals["deployment_mode"] = deployment_mode
    try:
        from auth import get_auth_backend as _get_auth_backend

        app.jinja_env.globals["auth_backend"] = _get_auth_backend
    except APP_SECURITY_AUTH_BACKEND_IMPORT_ERRORS:
        app.jinja_env.globals["auth_backend"] = lambda: "local"

    def public_login_url():
        try:
            if any(rule.endpoint == "auth_bp.login" for rule in app.url_map.iter_rules()):
                return url_for("auth_bp.login")
        except APP_SECURITY_PUBLIC_LOGIN_DISCOVERY_ERRORS:
            pass
        try:
            return url_for("admin_login")
        except APP_SECURITY_PUBLIC_LOGIN_BUILD_ERRORS:
            return "/auth/login"

    def _read_bool_env(var_name: str, default: bool) -> bool:
        raw = str(os.environ.get(var_name, "") or "").strip().lower()
        if not raw:
            return default
        return raw in {"1", "true", "yes", "on"}

    def public_register_enabled() -> bool:
        return public_register_enabled_by_env()

    def public_register_url():
        try:
            if any(rule.endpoint == "auth_bp.register" for rule in app.url_map.iter_rules()):
                return url_for("auth_bp.register")
        except APP_SECURITY_PUBLIC_REGISTER_DISCOVERY_ERRORS:
            pass
        try:
            return url_for("auth_bp.register")
        except APP_SECURITY_PUBLIC_REGISTER_BUILD_ERRORS:
            return "/auth/register"

    app.jinja_env.globals["public_login_url"] = public_login_url
    app.jinja_env.globals["public_register_enabled"] = public_register_enabled
    app.jinja_env.globals["public_register_url"] = public_register_url
    app.jinja_env.globals["format_beijing_time"] = format_beijing_time

    log_print("[TRACE] app security bootstrap configured", "APP")
