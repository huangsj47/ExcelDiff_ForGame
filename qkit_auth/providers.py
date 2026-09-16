#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Qkit auth provider."""

from __future__ import annotations

import hashlib
import hmac
import os

from flask import current_app, g, request, session

from models import db
from auth.providers import AuthProvider
from qkit_auth.config import load_qkit_settings
from qkit_auth.services import (
    check_qkit_jwt_remote,
    decode_qkit_jwt_unsafe,
    ensure_qkit_user,
    extract_identity_from_payload,
    get_user_by_id,
)
from utils.logger import log_print

_QKITJWT_COOKIE = "qkitjwt"
_QKITJWT_PARTS_COOKIE = "qkitjwt_parts"
_QKITJWT_PART_PREFIX = "qkitjwt_p"
_QKITJWT_MAX_PARTS = 8


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


_QKIT_AUTH_VERBOSE_LOG = _env_bool("QKIT_AUTH_VERBOSE_LOG", False)


def _qkit_auth_log(message: str, level: str = "INFO", *, verbose: bool = False) -> None:
    """Print qkit auth logs with opt-in verbose mode for high-frequency noise."""
    if verbose and not _QKIT_AUTH_VERBOSE_LOG:
        return
    log_print(message, level)


def _token_fingerprint(token: str) -> str:
    raw = (token or "").strip()
    if not raw:
        return "empty"
    head = raw[:8]
    tail = raw[-6:] if len(raw) > 6 else raw
    return f"len={len(raw)}, head={head}, tail={tail}"


def _derive_csrf_token_from_qkitjwt(token: str) -> str:
    """Derive a stable CSRF token for restored qkit sessions.

    This keeps form token verification stable even when session cookie persistence
    is unstable and the user session is restored from qkitjwt on each request.
    """
    raw = (token or "").strip()
    if not raw:
        return ""

    payload = f"qkit-csrf|{raw}".encode("utf-8")
    secret = str(current_app.config.get("SECRET_KEY") or current_app.secret_key or "")
    if secret:
        return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hashlib.sha256(payload).hexdigest()


def _ensure_session_csrf_token(token: str) -> None:
    if session.get("_csrf_token"):
        return
    csrf_value = _derive_csrf_token_from_qkitjwt(token)
    if csrf_value:
        session["_csrf_token"] = csrf_value


def fix_session_csrf_token_from_qkitjwt(token: str) -> None:
    """登录完成时把会话的 CSRF token **无条件**钉成「由 qkitjwt 派生」的确定值。

    ## 与 `_ensure_session_csrf_token` 的差别，以及为什么必须有这个差别

    两者都写同一个派生值 D（同一个 qkitjwt + 同一个 SECRET_KEY ⇒ 同一个 D），
    区别只有一条：会话里**已经有** token 时，`_ensure_...` 保留原值，
    本函数覆盖它。

    ## 不覆盖会怎样（这是线上真实踩到的形态）

    CSRF token 是**会话级**的，页面里那份（`base.html` 的
    `<meta name="csrf-token">`）取自渲染那一刻的会话：

    * 登录时若**不**把 token 钉成 D，它就会由 `csrf_token()` 随机生成 R；
    * 页面渲染出来的是 R，正常情况下服务端会话里也是 R，提交没问题；
    * 可一旦会话 cookie 被重建（进程重启、`FLASK_SECRET_KEY` 变更、
      浏览器丢 cookie、多进程密钥不一致），服务端会话变成空的 →
      恢复路径派生出的却是 **D ≠ R** →
      用户手上那个页面点任何写操作，服务端都答
      **「CSRF token invalid or missing.」**，而且**刷新一下就好了** ——
      这正是最难排查的那种「偶发、刷新即消失」的报障。
      更隐蔽的是：因为 qkitjwt 还在，用户看起来**一直是登录状态**，
      所以报障听起来像是「权限/网络问题」，而不是「会话丢了」。

    登录那一刻把 token 钉成 D 之后，页面渲染的就是 D；之后无论会话丢几次，
    恢复路径派生的都是同一个 D —— 旧页面依旧能提交，故障不再复现。
    这就是 `_derive_csrf_token_from_qkitjwt` 存在的全部理由（见其 docstring
    「keep form token verification stable … session cookie persistence is unstable」），
    原先只在恢复路径实现了一半。

    ## 为什么在登录时覆盖是安全的

    登录那一刻还没有任何「已发出的页面」依赖旧值（登录流程本身全是 GET：
    `/qkit_auth/login` → 认证中心 → `/qkit_auth/after_login`），
    因此覆盖不会作废任何在途表单。
    """
    csrf_value = _derive_csrf_token_from_qkitjwt(token)
    if csrf_value:
        session["_csrf_token"] = csrf_value


def _load_qkit_jwt_from_request() -> str:
    token = (request.cookies.get(_QKITJWT_COOKIE) or "").strip()
    if token:
        return token

    raw_parts = (request.cookies.get(_QKITJWT_PARTS_COOKIE) or "").strip()
    if not raw_parts:
        return ""

    try:
        part_count = int(raw_parts)
    except (TypeError, ValueError):
        return ""

    if part_count <= 0 or part_count > _QKITJWT_MAX_PARTS:
        return ""

    chunks = []
    for idx in range(part_count):
        chunk = (request.cookies.get(f"{_QKITJWT_PART_PREFIX}{idx}") or "").strip()
        if not chunk:
            return ""
        chunks.append(chunk)
    return "".join(chunks).strip()


class QkitAuthProvider(AuthProvider):
    """Qkit 登录态提供者。

    设计原则：
    - 每次请求必须调用一次 AUTH_CHECK_JWT_API（通过 request-scope 缓存避免重复调用）
    - 校验失败立即判定未登录
    """

    def _clear_auth_session(self) -> None:
        for key in (
            "auth_user_id",
            "auth_username",
            "auth_role",
            "is_admin",
            "admin_user",
            "auth_backend",
            "qkit_backhost",
            "qkitjwt_session",
        ):
            session.pop(key, None)

    def _restore_session_from_qkit_token(self, token: str) -> bool:
        payload = decode_qkit_jwt_unsafe(token)
        if not payload:
            _qkit_auth_log("[QKIT_AUTH] session restore skipped: cannot decode token payload", verbose=True)
            return False

        try:
            identity = extract_identity_from_payload(payload)
        except Exception as exc:
            _qkit_auth_log(
                f"[QKIT_AUTH] session restore failed: invalid identity payload, error={exc}",
                verbose=True,
            )
            return False

        user, err = ensure_qkit_user(
            username=identity.get("username") or "",
            display_name=identity.get("display_name") or "",
            email=identity.get("email") or "",
            source="qkit_session_restore",
        )
        if err or user is None:
            _qkit_auth_log(
                f"[QKIT_AUTH] session restore failed: ensure user error={err or 'unknown'}",
                verbose=True,
            )
            return False
        if not user.is_active:
            _qkit_auth_log(
                f"[QKIT_AUTH] session restore failed: inactive user username={user.username}",
                verbose=True,
            )
            return False
        try:
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            _qkit_auth_log(f"[QKIT_AUTH] session restore failed: db commit error={exc}", level="ERROR")
            return False

        session["auth_user_id"] = user.id
        session["auth_username"] = user.username
        session["auth_role"] = user.role
        session["is_admin"] = bool(user.is_platform_admin)
        session["admin_user"] = user.username if user.is_platform_admin else None
        session["auth_backend"] = "qkit"

        settings = load_qkit_settings()
        if settings.local_jwt_cache:
            session.pop("qkitjwt_session", None)
        else:
            session["qkitjwt_session"] = token
        _ensure_session_csrf_token(token)
        session.permanent = True
        _qkit_auth_log(
            (
                "[QKIT_AUTH] session restored from token "
                f"user_id={user.id}, username={user.username}, path={request.path}"
            ),
            verbose=True,
        )
        return True

    def _check_current_request_login(self) -> bool:
        cached = getattr(g, "_qkit_login_valid", None)
        if cached is not None:
            return bool(cached)

        user_id = session.get("auth_user_id")
        settings = load_qkit_settings()
        if settings.local_jwt_cache:
            token = (_load_qkit_jwt_from_request() or session.get("qkitjwt_session", "")).strip()
        else:
            token = (session.get("qkitjwt_session", "") or _load_qkit_jwt_from_request()).strip()

        if not user_id and token:
            valid, message, _payload = check_qkit_jwt_remote(token)
            if valid and self._restore_session_from_qkit_token(token):
                g._qkit_login_valid = True
                return True
            _qkit_auth_log(
                (
                    "[QKIT_AUTH] request auth failed: cannot restore session from token, "
                    f"path={request.path}, host={request.host}, verify={message or 'unknown'}, "
                    f"token={_token_fingerprint(token)}"
                ),
                verbose=True,
            )
            self._clear_auth_session()
            g._qkit_login_valid = False
            return False

        if not user_id:
            _qkit_auth_log(
                (
                    "[QKIT_AUTH] request auth failed: missing auth_user_id and token, "
                    f"path={request.path}, host={request.host}, local_cache={settings.local_jwt_cache}"
                ),
                verbose=True,
            )
            g._qkit_login_valid = False
            return False

        if not token:
            missing_hint = "qkitjwt_session" if not settings.local_jwt_cache else "qkitjwt cookie"
            _qkit_auth_log(
                (
                    "[QKIT_AUTH] request auth failed: "
                    f"missing {missing_hint}, path={request.path}, host={request.host}, "
                    f"user_id={user_id}, local_cache={settings.local_jwt_cache}"
                ),
                verbose=True,
            )
            self._clear_auth_session()
            g._qkit_login_valid = False
            return False

        valid, message, _payload = check_qkit_jwt_remote(token)
        if not valid:
            _qkit_auth_log(
                (
                    "[QKIT_AUTH] request auth failed: "
                    f"jwt verify failed={message or 'unknown'}, path={request.path}, "
                    f"host={request.host}, user_id={user_id}, token={_token_fingerprint(token)}"
                ),
                level="WARNING",
            )
            self._clear_auth_session()
            g._qkit_login_valid = False
            return False

        user = get_user_by_id(int(user_id))
        if not user or not user.is_active:
            _qkit_auth_log(
                (
                    "[QKIT_AUTH] request auth failed: "
                    f"user not found or inactive, user_id={user_id}, path={request.path}"
                ),
                level="WARNING",
            )
            self._clear_auth_session()
            g._qkit_login_valid = False
            return False

        # 与数据库角色保持同步
        session["auth_username"] = user.username
        session["auth_role"] = user.role
        session["is_admin"] = bool(user.is_platform_admin)
        session["admin_user"] = user.username if user.is_platform_admin else None
        g._qkit_login_valid = True
        return True

    def _get_active_user(self):
        if not self._check_current_request_login():
            return None
        user_id = session.get("auth_user_id")
        if not user_id:
            return None
        return get_user_by_id(int(user_id))

    # 兼容抽象接口
    def authenticate(self, username: str, password: str):
        return None

    def get_current_user(self):
        return self._get_active_user()

    def is_logged_in(self) -> bool:
        return self._check_current_request_login()

    def has_platform_admin_access(self) -> bool:
        user = self._get_active_user()
        if not user:
            return False
        return bool(user.is_platform_admin)

    def has_project_admin_access(self, project_id: int) -> bool:
        user = self._get_active_user()
        if not user:
            return False
        if user.is_platform_admin:
            return True

        from qkit_auth.models import QkitAuthUserProject

        membership = QkitAuthUserProject.query.filter_by(
            user_id=user.id,
            project_id=project_id,
        ).first()
        if not membership:
            return False
        return membership.is_project_admin

    def has_project_access(self, project_id: int) -> bool:
        user = self._get_active_user()
        if not user:
            return False
        if user.is_platform_admin:
            return True

        from qkit_auth.models import QkitAuthUserProject

        membership = QkitAuthUserProject.query.filter_by(
            user_id=user.id,
            project_id=project_id,
        ).first()
        return membership is not None

    def get_accessible_project_ids(self) -> list[int]:
        user = self._get_active_user()
        if not user:
            return []
        if user.is_platform_admin:
            return []

        from qkit_auth.models import QkitAuthUserProject

        rows = QkitAuthUserProject.query.filter_by(user_id=user.id).all()
        return [row.project_id for row in rows]
