#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Auth backend facade.

统一入口，支持两套账号后端：
1. ``local``: 现有本地账号体系（auth_* 表）
2. ``qkit``:  外部 qkit 登录 + 本地映射账号体系（qkit_auth_* 表）
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from flask import Flask
    from flask_sqlalchemy import SQLAlchemy

_auth_provider = None
_auth_backend = "local"

SUPPORTED_AUTH_BACKENDS = {"local", "qkit"}


def _resolve_auth_backend() -> str:
    backend = (os.environ.get("AUTH_BACKEND") or "local").strip().lower()
    if backend not in SUPPORTED_AUTH_BACKENDS:
        backend = "local"
    return backend


def init_auth(app: Flask, db: SQLAlchemy) -> None:
    """初始化账号后端 Provider。"""
    global _auth_provider, _auth_backend
    _auth_backend = _resolve_auth_backend()

    if _auth_backend == "qkit":
        from qkit_auth.providers import QkitAuthProvider
        # 确保 qkit 模型被导入，以便 db.create_all 创建表
        from qkit_auth import models as _qkit_models  # noqa: F401

        _auth_provider = QkitAuthProvider()
    else:
        from .providers import CompositeAuthProvider, DatabaseAuthProvider, EnvAuthProvider
        # 确保 local auth 模型被导入，以便 db.create_all 创建表
        from . import models as _models  # noqa: F401

        env_provider = EnvAuthProvider()
        db_provider = DatabaseAuthProvider(db)
        _auth_provider = CompositeAuthProvider(primary=db_provider, fallback=env_provider)

    app.extensions["auth_provider"] = _auth_provider
    app.extensions["auth_backend"] = _auth_backend


def register_auth_blueprints(app: Flask) -> None:
    """按后端注册认证相关 Blueprint。"""
    backend = get_auth_backend()
    if backend == "qkit":
        from qkit_auth.routes import auth_bp, qkit_auth_bp

        app.register_blueprint(auth_bp)
        app.register_blueprint(qkit_auth_bp)
        return

    from .routes import auth_bp

    app.register_blueprint(auth_bp)


def init_auth_default_data() -> None:
    """初始化后端默认数据（幂等）。"""
    backend = get_auth_backend()
    if backend == "qkit":
        from qkit_auth.services import bootstrap_qkit_auth_data

        bootstrap_qkit_auth_data()
        return

    from .services import init_default_functions, migrate_env_admin_to_db

    init_default_functions()
    migrate_env_admin_to_db()


def get_auth_backend() -> str:
    return _auth_backend


def get_auth_provider():
    """获取当前已注册的认证提供者实例。"""
    if _auth_provider is None:
        raise RuntimeError("Auth 模块尚未初始化，请先调用 init_auth(app, db)")
    return _auth_provider
