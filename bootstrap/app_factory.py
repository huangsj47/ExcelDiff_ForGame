#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Flask app factory and runtime setting normalization."""

from __future__ import annotations

from dataclasses import dataclass

from flask import Flask


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

