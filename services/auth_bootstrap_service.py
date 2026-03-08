"""Auth bootstrap routines extracted from app.py."""

from __future__ import annotations

import os
import traceback

from flask import redirect, session, url_for
from sqlalchemy.exc import SQLAlchemyError


AUTH_ROUTE_DISCOVERY_ERRORS = (RuntimeError, AttributeError, TypeError)
AUTH_QKIT_ROUTE_REGISTER_ERRORS = (AssertionError, RuntimeError, TypeError, ValueError)
AUTH_DEFAULT_DATA_INIT_ERRORS = (
    SQLAlchemyError,
    RuntimeError,
    ValueError,
    TypeError,
    AttributeError,
    KeyError,
)
AUTH_MODULE_INIT_ERRORS = (
    SQLAlchemyError,
    RuntimeError,
    ValueError,
    TypeError,
    AttributeError,
    KeyError,
)


def register_qkit_fallback_endpoints(app_instance, log_print):
    """Register minimal qkit endpoints when qkit blueprint is unavailable."""
    backend = (os.environ.get("AUTH_BACKEND") or "local").strip().lower()
    if backend != "qkit":
        return

    def _endpoint_exists(endpoint: str) -> bool:
        try:
            return any(rule.endpoint == endpoint for rule in app_instance.url_map.iter_rules())
        except AUTH_ROUTE_DISCOVERY_ERRORS:
            return False

    def _qkit_unavailable_page():
        init_error = str(app_instance.config.get("AUTH_INIT_ERROR") or "").strip()
        hint = "Qkit 登录模块未初始化，请检查 AUTH_BACKEND 与依赖安装。"
        if init_error:
            hint = f"Qkit 登录模块初始化失败：{init_error}"
        return f"<h3>Qkit 登录不可用</h3><p>{hint}</p>", 503

    def _qkit_logout_fallback():
        session.pop("auth_user_id", None)
        session.pop("auth_username", None)
        session.pop("auth_role", None)
        session.pop("is_admin", None)
        session.pop("admin_user", None)
        session.pop("auth_backend", None)
        session.pop("qkit_backhost", None)
        return redirect(url_for("index"))

    fallback_routes = [
        ("/qkit_auth/login", "qkit_auth_bp.login", _qkit_unavailable_page, ["GET"]),
        ("/qkit_auth/after_login", "qkit_auth_bp.after_login", _qkit_unavailable_page, ["GET"]),
        ("/qkit_auth/logout", "qkit_auth_bp.logout", _qkit_logout_fallback, ["GET"]),
    ]

    for rule, endpoint, view_func, methods in fallback_routes:
        if _endpoint_exists(endpoint):
            continue
        try:
            app_instance.add_url_rule(
                rule,
                endpoint=endpoint,
                view_func=view_func,
                methods=methods,
                strict_slashes=False,
            )
            log_print(f"⚠️ 已注册 qkit 兜底路由: {endpoint} -> {rule}", "AUTH", force=True)
        except AUTH_QKIT_ROUTE_REGISTER_ERRORS as exc:
            log_print(f"❌ 注册 qkit 兜底路由失败 {endpoint}: {exc}", "AUTH", force=True)


def log_auth_route_diagnostics(app_instance, log_print):
    backend = (os.environ.get("AUTH_BACKEND") or "local").strip().lower()
    try:
        endpoints = {rule.endpoint for rule in app_instance.url_map.iter_rules()}
    except AUTH_ROUTE_DISCOVERY_ERRORS:
        endpoints = set()
    log_print(
        "AUTH 路由诊断: "
        f"backend={backend}, "
        f"auth_bp.login={'Y' if 'auth_bp.login' in endpoints else 'N'}, "
        f"qkit_auth_bp.login={'Y' if 'qkit_auth_bp.login' in endpoints else 'N'}, "
        f"AUTH_INIT_FAILED={bool(app_instance.config.get('AUTH_INIT_FAILED'))}",
        "AUTH",
        force=True,
    )
    if backend == "qkit":
        login_service = (
            os.environ.get("QKIT_LOGIN_SERVICE")
            or os.environ.get("LOGIN_SERVICE")
            or ""
        ).strip()
        if login_service:
            log_print(f"AUTH 路由诊断: QKIT_LOGIN_SERVICE={login_service}", "AUTH", force=True)


def initialize_auth_subsystem(*, app, db, log_print):
    """Initialize auth subsystem and register fallback diagnostics."""
    app.config["AUTH_INIT_FAILED"] = False
    app.config["AUTH_INIT_ERROR"] = ""
    try:
        from auth import init_auth, init_auth_default_data, register_auth_blueprints

        init_auth(app, db)
        log_print("[TRACE] auth module initialized", "APP")

        register_auth_blueprints(app)
        log_print("[TRACE] auth blueprints registered", "APP")

        with app.app_context():
            try:
                init_auth_default_data()
                log_print("[TRACE] auth default data initialized", "APP")
            except AUTH_DEFAULT_DATA_INIT_ERRORS as exc:
                log_print(f"[TRACE] auth: default data init skipped: {exc}", "APP")
    except ImportError as exc:
        app.config["AUTH_INIT_FAILED"] = True
        app.config["AUTH_INIT_ERROR"] = f"ImportError: {exc}"
        log_print(f"❌ 账号系统初始化失败（ImportError）: {exc}", "AUTH", force=True)
        log_print(f"[TRACE] auth module not available: {exc}", "APP")
    except AUTH_MODULE_INIT_ERRORS as exc:
        app.config["AUTH_INIT_FAILED"] = True
        app.config["AUTH_INIT_ERROR"] = f"{type(exc).__name__}: {exc}"
        log_print(f"❌ 账号系统初始化失败: {type(exc).__name__}: {exc}", "AUTH", force=True)
        log_print(f"[TRACE] auth module init failed: {exc}", "APP", force=True)
        traceback.print_exc()

    register_qkit_fallback_endpoints(app, log_print)
    log_auth_route_diagnostics(app, log_print)
