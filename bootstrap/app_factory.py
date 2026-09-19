#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Flask app factory and runtime setting normalization."""

from __future__ import annotations

from dataclasses import dataclass

from flask import Flask

from services.app_routing_bootstrap_service import bound_integer_url_converter

_DEPLOYMENT_MODES = {"single", "platform", "agent"}


@dataclass(frozen=True)
class RuntimeSettings:
    secret_key: str
    cors_allowed_origins: list[str]
    enable_admin_security: bool
    deployment_mode: str
    deployment_mode_invalid: bool
    enable_local_worker: bool


def create_app(import_name: str) -> Flask:
    app = Flask(import_name)
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    # **必须在这里**给 `<int:…>` 加上界：`Rule.get_converter` 是在 `Rule.compile()`
    # （由 `Map.add` 触发，也就是注册每一条路由的那一刻）里读 `self.map.converters` 的，
    # 换晚了已经注册的规则不会跟着变。放在工厂里是唯一早于**所有**蓝图（含 `auth` 那批
    # 注册得更早的）的位置 —— 实测只放在蓝图注册前，`auth` 的
    # `/auth/api/users/<int:user_id>/…` 仍是旧的 `IntegerConverter`。
    # 起因见 `services/app_routing_bootstrap_service.BoundedIntegerConverter`。
    bound_integer_url_converter(app)
    return app


def _normalize_deployment_mode(raw_value: str) -> tuple[str, bool]:
    mode = str(raw_value or "").strip().lower() or "single"
    if mode in _DEPLOYMENT_MODES:
        return mode, False
    return "single", True


def build_runtime_settings(environ: dict) -> RuntimeSettings:
    secret_key = (environ.get("FLASK_SECRET_KEY") or environ.get("SECRET_KEY") or "").strip()
    cors_allowed_origins = [
        origin.strip()
        for origin in str(environ.get("CORS_ALLOWED_ORIGINS") or "").split(",")
        if origin.strip()
    ]
    enable_admin_security = str(environ.get("ENABLE_ADMIN_SECURITY") or "true").lower() != "false"
    deployment_mode, invalid = _normalize_deployment_mode(environ.get("DEPLOYMENT_MODE") or "single")
    return RuntimeSettings(
        secret_key=secret_key,
        cors_allowed_origins=cors_allowed_origins,
        enable_admin_security=enable_admin_security,
        deployment_mode=deployment_mode,
        deployment_mode_invalid=invalid,
        enable_local_worker=(deployment_mode == "single"),
    )

