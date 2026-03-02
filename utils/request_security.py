#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Request security helpers for admin auth and CSRF checks."""

import hmac
import os
import secrets
from functools import wraps
from urllib.parse import urlparse

from flask import flash, jsonify, redirect, request, session, url_for


CSRF_SESSION_KEY = "_csrf_token"
ENABLE_ADMIN_SECURITY = True


def configure_request_security(*, csrf_session_key: str, enable_admin_security: bool) -> None:
    """Configure runtime switches for security helper behavior."""
    global CSRF_SESSION_KEY, ENABLE_ADMIN_SECURITY
    CSRF_SESSION_KEY = csrf_session_key
    ENABLE_ADMIN_SECURITY = enable_admin_security


def csrf_token():
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


# Admin page routes that render HTML templates (not JSON APIs)
_ADMIN_PAGE_ROUTES = frozenset({
    "/admin/excel-cache",
})


def _is_admin_page_route():
    """Check if the current request is for an admin HTML page (not an API endpoint)."""
    return request.path in _ADMIN_PAGE_ROUTES


def _is_api_request():
    # If the browser is requesting an admin HTML page, treat it as a page request
    if _is_admin_page_route():
        accept = request.headers.get("Accept", "")
        # Only treat as API if explicitly requesting JSON
        if request.is_json or "application/json" in accept:
            return True
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return True
        return False

    accept = request.headers.get("Accept", "")
    return (
        request.path.startswith("/api/")
        or request.path.startswith("/admin/")
        or request.is_json
        or "application/json" in accept
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
    )


def _is_valid_admin_token():
    expected = os.environ.get("ADMIN_API_TOKEN", "").strip()
    provided = (request.headers.get("X-Admin-Token") or "").strip()
    return bool(expected and provided and hmac.compare_digest(expected, provided))


def _has_admin_access():
    return bool(session.get("is_admin")) or _is_valid_admin_token()


def _unauthorized_admin_response():
    if _is_api_request():
        return jsonify({"success": False, "message": "Admin authentication required"}), 401
    next_url = request.url if request.url else url_for("index")
    flash("请先使用管理员账号登录。", "error")
    return redirect(url_for("admin_login", next=next_url))


def _csrf_error_response(message):
    if _is_api_request():
        return jsonify({"success": False, "message": message}), 400
    flash(message, "error")
    return redirect(request.referrer or url_for("index"))


def _csrf_token_from_request():
    header_token = request.headers.get("X-CSRF-Token") or request.headers.get("X-CSRFToken")
    if header_token:
        return header_token
    form_token = request.form.get("_csrf_token")
    if form_token:
        return form_token
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        return payload.get("_csrf_token")
    return None


def _is_same_origin_request():
    expected_host = request.host
    origin = request.headers.get("Origin")
    if origin:
        parsed = urlparse(origin)
        return parsed.netloc == expected_host
    referer = request.headers.get("Referer")
    if referer:
        parsed = urlparse(referer)
        return parsed.netloc == expected_host
    return True


def _is_safe_redirect(target):
    if not target:
        return False
    parsed = urlparse(target)
    if parsed.scheme and parsed.scheme not in ("http", "https"):
        return False
    if parsed.netloc and parsed.netloc != request.host:
        return False
    return True


def require_admin(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not ENABLE_ADMIN_SECURITY:
            return func(*args, **kwargs)
        if not _has_admin_access():
            return _unauthorized_admin_response()
        return func(*args, **kwargs)

    return wrapper
