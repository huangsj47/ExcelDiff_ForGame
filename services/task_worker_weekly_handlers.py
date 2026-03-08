"""Weekly task handlers extracted from task_worker_service.py."""

from __future__ import annotations


def handle_weekly_sync_task(
    *,
    task,
    app,
    update_task_status_with_retry,
    process_weekly_version_sync,
    non_critical_task_status_errors,
    non_critical_task_execution_errors,
    log_print,
):
    """Handle weekly sync task lifecycle with status updates."""
    log_print(f"📅 周版本同步: 配置 {task['config_id']}", "WEEKLY")
    with app.app_context():
        if "task_id" in task:
            try:
                update_task_status_with_retry(task["task_id"], "processing")
            except non_critical_task_status_errors as update_error:
                log_print(f"更新任务开始状态失败: {update_error}", "TASK", force=True)
        try:
            process_weekly_version_sync(task["config_id"])
            if "task_id" in task:
                try:
                    update_task_status_with_retry(task["task_id"], "completed")
                except non_critical_task_status_errors as update_error:
                    log_print(f"更新任务完成状态失败: {update_error}", "TASK", force=True)
        except non_critical_task_execution_errors as exc:
            log_print(f"❌ 周版本同步失败: {exc}", "WEEKLY", force=True)
            if "task_id" in task:
                try:
                    update_task_status_with_retry(task["task_id"], "failed", str(exc))
                except non_critical_task_status_errors as update_error:
                    log_print(f"更新任务状态失败: {update_error}", "TASK", force=True)


def handle_weekly_excel_cache_task(
    *,
    task,
    app,
    update_task_status_with_retry,
    process_weekly_excel_cache,
    non_critical_task_status_errors,
    non_critical_task_execution_errors,
    log_print,
):
    """Handle weekly excel-cache task lifecycle with status updates."""
    log_print(
        f"📊 周版本Excel缓存: 配置 {task['data']['config_id']}, 文件 {task['data']['file_path']}",
        "WEEKLY",
    )
    with app.app_context():
        if "id" in task:
            try:
                update_task_status_with_retry(task["id"], "processing")
            except non_critical_task_status_errors as update_error:
                log_print(f"更新任务开始状态失败: {update_error}", "TASK", force=True)
        try:
            process_weekly_excel_cache(task["data"]["config_id"], task["data"]["file_path"])
            if "id" in task:
                try:
                    update_task_status_with_retry(task["id"], "completed")
                except non_critical_task_status_errors as update_error:
                    log_print(f"更新任务完成状态失败: {update_error}", "TASK", force=True)
        except non_critical_task_execution_errors as exc:
            log_print(f"❌ 周版本Excel缓存生成失败: {exc}", "WEEKLY", force=True)
            if "id" in task:
                try:
                    update_task_status_with_retry(task["id"], "failed", str(exc))
                except non_critical_task_status_errors as update_error:
                    log_print(f"更新任务状态失败: {update_error}", "TASK", force=True)
