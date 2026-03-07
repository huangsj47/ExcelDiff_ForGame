"""Runtime wiring helpers extracted from app.py."""

from __future__ import annotations


def configure_runtime_wirings(
    *,
    log_print,
    configure_commit_diff_logic,
    configure_weekly_version_logic,
    configure_task_worker,
    excel_cache_service,
    excel_html_cache_service,
    active_git_processes,
    add_excel_diff_task,
    get_unified_diff_data,
    get_git_service,
    get_svn_service,
    weekly_excel_cache_service,
    create_weekly_sync_task,
    get_file_content_from_git,
    get_file_content_from_svn,
    generate_merged_diff_data,
    app,
    db,
    BackgroundTask,
    Commit,
    Repository,
    DiffCache,
    WeeklyVersionConfig,
    process_weekly_version_sync,
    process_weekly_excel_cache,
    db_retry,
):
    """Configure runtime dependencies for split services."""
    configure_commit_diff_logic(
        excel_cache_service=excel_cache_service,
        excel_html_cache_service=excel_html_cache_service,
        active_git_processes=active_git_processes,
        add_excel_diff_task_func=add_excel_diff_task,
        get_unified_diff_data_func=get_unified_diff_data,
        get_git_service_func=get_git_service,
        get_svn_service_func=get_svn_service,
    )

    configure_weekly_version_logic(
        excel_cache_service=excel_cache_service,
        weekly_excel_cache_service=weekly_excel_cache_service,
        excel_html_cache_service=excel_html_cache_service,
        create_weekly_sync_task_func=create_weekly_sync_task,
        get_unified_diff_data_func=get_unified_diff_data,
        get_git_service_func=get_git_service,
        get_svn_service_func=get_svn_service,
        get_file_content_from_git_func=get_file_content_from_git,
        get_file_content_from_svn_func=get_file_content_from_svn,
        generate_merged_diff_data_func=generate_merged_diff_data,
    )
    log_print("[TRACE] weekly_version_logic configured", "APP")

    configure_task_worker(
        app=app,
        db=db,
        excel_cache_service=excel_cache_service,
        BackgroundTask=BackgroundTask,
        Commit=Commit,
        Repository=Repository,
        DiffCache=DiffCache,
        WeeklyVersionConfig=WeeklyVersionConfig,
        active_git_processes=active_git_processes,
        get_git_service=get_git_service,
        get_svn_service=get_svn_service,
        get_unified_diff_data=get_unified_diff_data,
        process_weekly_version_sync=process_weekly_version_sync,
        process_weekly_excel_cache=process_weekly_excel_cache,
        db_retry=db_retry,
    )
    log_print("[TRACE] task_worker configured", "APP")
