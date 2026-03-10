"""Blueprint registration bootstrap helpers extracted from app.py."""

from __future__ import annotations

import traceback

APP_BLUEPRINT_BOOTSTRAP_ERRORS = (
    RuntimeError,
    TypeError,
    ValueError,
    AttributeError,
    LookupError,
)


def _register_blueprint_with_trace(*, app, blueprint, label: str, log_print) -> None:
    try:
        app.register_blueprint(blueprint)
        log_print(f"[TRACE] {label} registered", "APP")
    except APP_BLUEPRINT_BOOTSTRAP_ERRORS as exc:
        log_print(f"[TRACE] {label} FAILED: {exc}", "APP", force=True)
        traceback.print_exc()


def configure_app_blueprints(
    *,
    app,
    log_print,
    cache_management_bp,
    commit_diff_bp,
    core_management_bp,
    weekly_version_bp,
    agent_management_bp,
    ai_analysis_bp,
) -> None:
    """Register all split blueprints with startup trace logging."""
    app.register_blueprint(cache_management_bp)
    log_print("[TRACE] cache_management_bp registered", "APP")

    _register_blueprint_with_trace(
        app=app,
        blueprint=commit_diff_bp,
        label="commit_diff_bp",
        log_print=log_print,
    )
    _register_blueprint_with_trace(
        app=app,
        blueprint=core_management_bp,
        label="core_management_bp",
        log_print=log_print,
    )
    _register_blueprint_with_trace(
        app=app,
        blueprint=weekly_version_bp,
        label="weekly_version_bp",
        log_print=log_print,
    )
    _register_blueprint_with_trace(
        app=app,
        blueprint=agent_management_bp,
        label="agent_management_bp",
        log_print=log_print,
    )
    _register_blueprint_with_trace(
        app=app,
        blueprint=ai_analysis_bp,
        label="ai_analysis_bp",
        log_print=log_print,
    )
