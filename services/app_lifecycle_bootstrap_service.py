"""App lifecycle/bootstrap helpers extracted from app.py."""

from __future__ import annotations

from bootstrap.bootstrap import AppBootstrapManager


def _init_auth_default_data_with_context(*, app) -> None:
    from auth import init_auth_default_data

    with app.app_context():
        init_auth_default_data()


def create_bootstrap_manager(
    *,
    app,
    log_print,
    enable_local_worker,
    create_tables_func,
    start_background_task_worker_func,
    stop_background_task_worker_func,
    start_scheduler_func,
    stop_scheduler_func,
    clear_version_mismatch_cache_func,
    cleanup_pending_deletions_func,
    cleanup_git_processes_func,
):
    """Build the runtime lifecycle manager with extracted startup hooks."""
    return AppBootstrapManager(
        app=app,
        log_print=log_print,
        enable_local_worker=enable_local_worker,
        create_tables_func=create_tables_func,
        init_auth_default_data_func=lambda: _init_auth_default_data_with_context(app=app),
        start_background_task_worker_func=start_background_task_worker_func,
        stop_background_task_worker_func=stop_background_task_worker_func,
        start_scheduler_func=start_scheduler_func,
        stop_scheduler_func=stop_scheduler_func,
        clear_version_mismatch_cache_func=clear_version_mismatch_cache_func,
        cleanup_pending_deletions_func=cleanup_pending_deletions_func,
        cleanup_git_processes_func=cleanup_git_processes_func,
    )


def register_cleanup_hook(*, atexit_module, cleanup_app, log_print, module_name: str) -> None:
    """Register app cleanup and keep startup trace logs consistent."""
    log_print("[TRACE] about to register atexit", "APP")
    atexit_module.register(cleanup_app)
    log_print(f"[TRACE] reached if __name__ check, __name__={module_name!r}", "APP")
