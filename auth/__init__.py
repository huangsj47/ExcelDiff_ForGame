#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
账号认证模块 (Auth Module)

独立的认证与权限管理模块，通过 Provider Pattern 实现解耦。
可替换为 LDAP / OAuth 等其他认证后端。

使用方式:
    from auth import init_auth, get_auth_provider
    init_auth(app, db)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from flask import Flask
    from flask_sqlalchemy import SQLAlchemy

# 延迟导入，避免循环依赖
_auth_provider = None


def init_auth(app: Flask, db: SQLAlchemy) -> None:
    """初始化认证模块，注册 Provider 和默认数据。

    应在 ``db.init_app(app)`` 之后调用。
    """
    global _auth_provider

    from .providers import CompositeAuthProvider, DatabaseAuthProvider, EnvAuthProvider

    # 确保模型被导入，以便 db.create_all() 能够创建对应的表
    from . import models as _models  # noqa: F841

    env_provider = EnvAuthProvider()
    db_provider = DatabaseAuthProvider(db)
    _auth_provider = CompositeAuthProvider(primary=db_provider, fallback=env_provider)

    # 将 provider 挂载到 app 上，方便全局访问
    app.extensions["auth_provider"] = _auth_provider


def get_auth_provider():
    """获取当前已注册的认证提供者实例。"""
    if _auth_provider is None:
        raise RuntimeError("Auth 模块尚未初始化，请先调用 init_auth(app, db)")
    return _auth_provider
