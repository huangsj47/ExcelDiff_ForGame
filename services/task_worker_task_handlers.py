"""通用任务 handler：搬出 `task_worker_service.py` 的执行末端。

## 为什么单独一个文件

`services/task_worker_service.py` 原先 1999 行，**顶在 `scripts/check_file_length.py
--strict` 的 2000 行 ERROR 门槛上** —— 再加一行 CI 就红。这里放的是 worker 主循环
「拿到任务之后真正干活」的那几个末端函数：配表差异、同步失败后的仓库善后、周版本
AI 分析。它们的共同点是**只被 worker 主循环调用**、彼此之间不互相调用，搬走之后
`task_worker_service.py` 里剩下的就只有「调度 + 队列 + 主循环」这条主线了。

## 与 `task_worker_service.py` 的耦合方式（本文件唯一需要理解的东西）

函数体里凡是引用 worker 模块级名字的地方都写成 `worker.X`，而不是在本文件顶部
`from services.task_worker_service import X`。这不是风格偏好，是**语义要求**：

- 那些槽位（`_app` / `_db` / `_excel_cache_service`）由 `configure_task_worker()`
  **事后重绑**，走 `from ... import` 会在 import 那一刻把它们绑成 `None`；
- 几十处测试靠 `monkeypatch.setattr(worker, "_db", fake)` 换掉它们，只有**调用时**
  去取模块属性才看得见补丁。

环形 import（本模块在 worker 加载到一半时被 import）是安全的：本文件只在函数被
调用时读 `worker.X`，那时 worker 早已加载完。`os` / `time` / `traceback` 这类
没人重绑的标准库名字不绕 worker，直接 import。
"""

from __future__ import annotations

import os

import services.task_worker_service as worker


def _handle_excel_diff_task(task, priority):
    """处理Excel差异任务"""
    worker.log_print(f"📊 处理Excel差异: repo={task['repository_id']}, commit={task['commit_id'][:8]}, file={task['file_path']}", 'EXCEL')
    with worker._app.app_context():
        if 'task_id' in task:
            try:
                worker.update_task_status_with_retry(task['task_id'], 'processing')
            except worker.NON_CRITICAL_TASK_STATUS_ERRORS as update_error:
                worker.log_print(f"更新任务开始状态失败: {update_error}", 'TASK', force=True)
        try:
            # 必须看返回值决定任务终态。原实现忽略返回值、无条件标 completed，
            # 于是「仓库已删 / 提交查不到」这类什么都没做的情况也被记为成功，
            # 与真的生成成功在任务表里完全同形（管理员无法区分「已完成」和「根本没做」）。
            status = worker._excel_cache_service.process_excel_diff_background(
                task['repository_id'], task['commit_id'], task['file_path']
            )
            if 'task_id' in task:
                try:
                    if status == worker._BG_STATUS_COMPLETED:
                        worker.update_task_status_with_retry(task['task_id'], 'completed')
                    elif status == worker._BG_STATUS_SKIPPED_IN_PROGRESS:
                        # 同一 (repo, commit, file) 正被另一线程处理 —— 工作确实在推进，
                        # 本任务无需重试，标 completed 是合理的。
                        worker.log_print(f"任务与在途处理重复，跳过: {task['task_id']}", 'TASK')
                        worker.update_task_status_with_retry(task['task_id'], 'completed')
                    else:
                        # repository_missing / commit_missing / error → 显式失败，
                        # 让管理员在任务列表里看得见，而不是被 ✅ 盖住。
                        worker.update_task_status_with_retry(
                            task['task_id'], 'failed',
                            f'Excel差异未生成（{status}）: repo={task["repository_id"]}, '
                            f'commit={task["commit_id"]}, file={task["file_path"]}'
                        )
                except worker.NON_CRITICAL_TASK_STATUS_ERRORS as update_error:
                    worker.log_print(f"更新任务完成状态失败: {update_error}", 'TASK', force=True)
        except worker.NON_CRITICAL_TASK_EXECUTION_ERRORS as e:
            worker.log_print(f"❌ Excel差异处理失败: {e}", 'EXCEL', force=True)
            try:
                worker._db.session.rollback()
            except worker.SQLAlchemyError as rollback_error:
                worker.log_print(f"会话回滚失败: {rollback_error}", 'DB', force=True)
            if 'task_id' in task:
                try:
                    worker.update_task_status_with_retry(task['task_id'], 'failed', str(e))
                except worker.NON_CRITICAL_TASK_STATUS_ERRORS as update_error:
                    worker.log_print(f"更新任务状态失败: {update_error}", 'TASK', force=True)


def _reset_repository_to_head(git_service, repository):
    """超时或失败后重置仓库到 HEAD 状态"""
    local_path = getattr(git_service, "local_path", None)
    if local_path and not os.path.isdir(local_path):
        # 工作目录根本不存在（clone 从来没成功过、或被人删掉了）：下面三条 git 命令
        # 都只会以「[WinError 267] 目录名称无效」失败，每条刷两行日志 —— 而这里
        # 本来就没有任何东西可重置。线上报障就是这一串（`repos/<xxx>_4` 反复刷屏）。
        worker.log_print(f"ℹ️ [RESET] 跳过重置：工作目录不存在 {local_path}", 'SYNC')
        return
    try:
        worker.log_print(f"🔄 [RESET] 正在重置仓库 {repository.name} 到 HEAD 状态...", 'SYNC', force=True)
        cleanup_locks = getattr(git_service, "_cleanup_git_lock_files", None)
        if callable(cleanup_locks):
            removed_locks = cleanup_locks()
            if removed_locks:
                worker.log_print(f"🧹 [RESET] 已清理Git锁文件: {', '.join(removed_locks)}", 'SYNC')

        reset_result = git_service._run_git_command(['git', 'reset', '--hard', 'HEAD'], timeout=60)
        if reset_result and reset_result.returncode == 0:
            worker.log_print("✅ [RESET] git reset --hard HEAD 成功", 'SYNC')
        else:
            worker.log_print("⚠️ [RESET] git reset --hard HEAD 失败", 'SYNC', force=True)
        clean_result = git_service._run_git_command(['git', 'clean', '-fd'], timeout=60)
        if clean_result and clean_result.returncode == 0:
            worker.log_print("✅ [RESET] git clean -fd 成功", 'SYNC')
        else:
            worker.log_print("⚠️ [RESET] git clean -fd 失败", 'SYNC', force=True)

        gc_result = git_service._run_git_command(['git', 'gc', '--prune=now'], timeout=120)
        if gc_result and gc_result.returncode == 0:
            worker.log_print("✅ [RESET] git gc --prune=now 成功", 'SYNC')
        else:
            worker.log_print("⚠️ [RESET] git gc --prune=now 失败", 'SYNC', force=True)
    except worker.NON_CRITICAL_VCS_PREHEAL_ERRORS as reset_err:
        worker.log_print(f"❌ [RESET] 重置仓库异常: {reset_err}", 'SYNC', force=True)


def _record_sync_error(repository, error_message):
    """将同步错误信息记录到仓库模型"""
    worker.record_repository_sync_error(
        worker._db.session,
        repository,
        error_message,
        log_func=worker.log_print,
        log_type="SYNC",
        commit=True,
    )


def _clear_sync_error(repository):
    """同步成功后清除仓库的错误信息"""
    worker.clear_repository_sync_error(
        worker._db.session,
        repository,
        log_func=worker.log_print,
        log_type="SYNC",
        commit=True,
    )


def _abandon_timed_out_auto_sync_task(task, message):
    """并发许可等不到时，把任务**真正了结掉**，而不是留下一行 pending。

    原先这里只打一行日志就 `return`：任务已经从内存队列里弹掉（单机模式下内存队列是
    唯一执行路径），而库里的行仍是 `pending` —— 它既不会被执行、也不会被重新入队，
    却仍然占着 `create_auto_sync_task` 的「同仓库已有 pending」去重位。于是这个仓库
    此后每一次「更新仓库 / 重试同步」都会被去重成一个返回 `existing_task.id` 的空操作，
    调用方却以为任务已经派下去了 —— 一次并发等待超时 = 该仓库静默停止同步，直到进程
    重启（重启时 `load_pending_tasks` 才会把它捞回队列）。

    标成 failed 的作用就是**把去重位释放掉**：下一次触发会创建一个新任务。
    `update_task_status_with_retry` 会顺手累加 `retry_count`，但本仓库没有任何代码
    读它做自动重试（`BackgroundTask.retry_count` 只在界面上展示），所以不会因此
    变成「重试次数用尽」。
    """
    task_id = task.get('task_id')
    if task_id is None:
        # Agent 派发路径不传 task_id（见 _dispatch_agent_task 的 auto_sync 分支）：
        # 那条路径的任务状态由 agent 侧管理，这里不能替它改。
        worker.log_print(f"⚠️ 任务无 task_id（Agent 派发路径），跳过状态标记：{message}", 'SYNC', force=True)
        return
    try:
        # 必须自带 app context：本函数由 Thread 启动的 worker 调用，不继承主线程的
        # context，而 update_task_status_with_retry 用的是模块级 _db.session.get()。
        with worker._app.app_context():
            worker.update_task_status_with_retry(task_id, worker.TASK_STATUS_FAILED, message)
    except worker.NON_CRITICAL_TASK_STATUS_ERRORS as update_error:
        worker.log_print(f"标记超时任务为失败时出错: {update_error}", 'TASK', force=True)


def _handle_weekly_ai_analysis_task(task):
    """处理周版本AI分析任务"""
    with worker._app.app_context():
        task_id = task.get("task_id")
        if task_id is not None:
            try:
                worker.update_task_status_with_retry(task_id, "processing")
            except worker.NON_CRITICAL_TASK_STATUS_ERRORS as update_error:
                worker.log_print(f"更新AI分析任务开始状态失败: {update_error}", "AI", force=True)
        try:
            config_id = task.get("config_id") or task.get("commit_id") or task.get("repository_id")
            try:
                config_id = int(config_id)
            except (TypeError, ValueError):
                config_id = None
            if not config_id:
                raise ValueError("weekly_ai_analysis 缺少有效 config_id")

            result = worker.run_weekly_analysis_background(config_id, task_id=task_id)
            status = result.get("status")
            if task_id is not None:
                if status == "succeeded":
                    worker.update_task_status_with_retry(task_id, "completed")
                elif status == "skipped":
                    reason = result.get("reason") or "skipped"
                    worker.update_task_status_with_retry(task_id, "completed", f"skipped:{reason}")
                else:
                    worker.update_task_status_with_retry(task_id, "failed", str(result.get("reason") or "analysis_failed"))
        except worker.NON_CRITICAL_TASK_EXECUTION_ERRORS as exc:
            worker.log_print(f"❌ 周版本AI分析失败: {exc}", "AI", force=True)
            try:
                worker._db.session.rollback()
            except worker.SQLAlchemyError:
                pass
            if task_id is not None:
                try:
                    worker.update_task_status_with_retry(task_id, "failed", str(exc))
                except worker.NON_CRITICAL_TASK_STATUS_ERRORS as update_error:
                    worker.log_print(f"更新AI分析任务失败状态异常: {update_error}", "AI", force=True)
