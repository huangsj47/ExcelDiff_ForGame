"""Weekly task handlers extracted from task_worker_service.py.

这里同时承载周版本同步任务的**载荷解析**与**内存队列账本**（见下面两处注释块）。放在本
模块而不是 `task_worker_service.py`，是因为后者已经贴着 2000 行硬上限
（`scripts/check_file_length.py --strict` 在 2000 行报 ERROR），而这两段恰好是「不依赖
该模块的全局状态、又最值得单独测」的部分。
"""

from __future__ import annotations

import threading
import time

from services.weekly_version_sync_status import (
    task_status_and_message as weekly_sync_task_status_and_message,
)


def parse_config_id_from_commit_id(commit_id):
    """把 BackgroundTask.commit_id 解析成 config_id（周版本任务存在这一列）。

    周版本任务没有 repository_id，配置 id 借 `commit_id` 存，写进去时是
    `str(config_id)`（见 `create_weekly_sync_task` / `create_weekly_ai_analysis_task`）。

    **解析失败返回 None，绝不抛异常**：调用方（`load_pending_tasks`）拿到 None 后
    仍会入队，由 `handle_weekly_sync_task` 按「载荷畸形」把那一行标 failed ——
    这比在装载阶段抛出去好，后者会让整次启动的待处理任务装载中断。
    """
    try:
        return int(commit_id)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
#  「已入队」账本
#
#  为什么要它：`create_weekly_sync_task` 按 `status='pending'` 去重，命中就直接返回；
#  而「重新入队」原先只发生在平台模式（`_use_agent_dispatch()`）里。单机模式下内存队列
#  是**唯一**执行路径，进程重启会把它清空、数据库那行却还是 pending —— 于是每次调度都
#  空转：任务永远不跑，也永远不结束（线上「周版本同步任务永久排队中」）。
#
#  但**不能无条件重入队** —— 那会让同一条任务在内存队列里出现两份、被 worker 执行两遍。
#  这个集合就是判据：在里面 = 还在队列里，维持现状；不在 = 内存队列已经丢了它，补一次。
# ---------------------------------------------------------------------------
_enqueued_weekly_sync_task_ids = set()
_enqueued_weekly_sync_task_lock = threading.Lock()


def register_enqueued_weekly_sync_task(task_id):
    """登记「这条任务已经在内存队列里」。"""
    if task_id is None:
        return
    with _enqueued_weekly_sync_task_lock:
        _enqueued_weekly_sync_task_ids.add(task_id)


def forget_enqueued_weekly_sync_task(task_id):
    """注销：这条任务已被 worker 处理完，不能再算它还在队列里。"""
    if task_id is None:
        return
    with _enqueued_weekly_sync_task_lock:
        _enqueued_weekly_sync_task_ids.discard(task_id)


def is_weekly_sync_task_enqueued(task_id):
    """这条任务当前是否还在内存队列里（没被处理完）。"""
    if task_id is None:
        return False
    with _enqueued_weekly_sync_task_lock:
        return task_id in _enqueued_weekly_sync_task_ids


def enqueue_weekly_sync_task(queue_obj, task_wrapper_class, task_id, config_id, priority=3):
    """把 weekly_sync 任务放进内存队列，并登记进「已入队」账本。

    入队与登记必须成对发生，所以合成同一个入口：分开写迟早会出现「入了队没登记」
    （下次去重命中时被当成「队列里没有」而重复补一份、跑两遍）或「登记了没入队」
    （任务永远不跑，正是要修的那个病）。

    `queue_obj` / `task_wrapper_class` 由调用方传进来，而不是在本模块 import
    `task_worker_service` —— 那是反向依赖，会成环。
    """
    task_data = {
        'type': 'weekly_sync',
        'config_id': config_id,
        'task_id': task_id,
    }
    queue_obj.put(task_wrapper_class(priority, int(time.time() * 1000000), task_data))
    register_enqueued_weekly_sync_task(task_id)


def forget_weekly_sync_task_of_payload(task_data):
    """worker 处理完一个任务后调用：只对 weekly_sync 载荷生效，其他类型不动账本。"""
    if isinstance(task_data, dict) and task_data.get('type') == 'weekly_sync':
        forget_enqueued_weekly_sync_task(task_data.get('task_id'))


def reset_stale_weekly_sync_tasks(
    config,
    now_utc_naive,
    *,
    db,
    background_task_model,
    log_print,
    timeout_seconds=300,
):
    """把卡死的 weekly_sync `pending` 行置 failed。

    **这是清理动作，与「这个配置现在活不活跃」无关，对查询到的每个配置都要做。**
    版本窗口一结束，`schedule_weekly_sync_tasks` 会把配置置 `completed` 然后 `continue`，
    此后每次调度都跳过这个配置；而原先重置整段都圈在 `if config.status == 'active':`
    里面 —— 于是窗口结束后残留的 pending 既不会被执行（内存队列早已随进程重启清空），
    也永远没人重置，永久停在 pending（线上「周版本同步任务永久排队中」的成因之一）。

    时刻口径：`created_at` 是 **naive-UTC**（ORM 默认 `datetime.now(timezone.utc)` 写入、
    SQLite 丢掉 tzinfo），所以 `now_utc_naive` 必须是同口径的 naive-UTC。传
    `datetime.now()`（宿主机本地时间）在 UTC+8 机器上会让任务年龄恒多算 28800 秒，
    刚创建几秒的任务也会被判超时。
    """
    stale_tasks = background_task_model.query.filter_by(
        task_type='weekly_sync',
        commit_id=str(config.id),
        status='pending',
    ).all()
    for stale in stale_tasks:
        stale_created = stale.created_at
        if stale_created and stale_created.tzinfo:
            stale_created = stale_created.replace(tzinfo=None)
        if stale_created and (now_utc_naive - stale_created).total_seconds() > timeout_seconds:
            stale.status = 'failed'
            stale.error_message = '任务超时，已被调度器重置'
            db.session.commit()
            log_print(
                f"重置卡死的周版本同步任务: task_id={stale.id}, config_id={config.id}",
                'WEEKLY',
                force=True,
            )


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
    # **判「载荷畸形」与判「同步结局」是两回事，别把现有语义改掉。**
    # 配置不存在 / 已禁用 / 窗口内无提交 / 单文件失败都是 process_weekly_version_sync
    # 的正常结局，由 weekly_version_sync_status 映射成 failed/skipped/partial_failed，
    # 那条路径完全不动。这里说的是**任务载荷里根本没有 config_id** —— 连「要同步哪个
    # 配置」都无从谈起，谈不上什么结局。
    #
    # 为什么必须在这里收口：原实现在 try 之外直接 `task['config_id']`，抛 KeyError；
    # KeyError 在 worker 循环的 NON_CRITICAL_WORKER_LOOP_ERRORS 里被吞成一行
    # 「后台任务处理异常」，而写终态的那句在 try 之内、永远执行不到 —— 库里那行从头到尾
    # 没被碰过，永久停在 pending（线上「周版本同步任务永久排队中」的直接成因）。
    # 所以畸形载荷必须自己把那一行标 failed：将来任何漏进队列的坏载荷都不该再留下
    # 一行无人认领的 pending。
    config_id = task.get("config_id")
    if config_id is None:
        reason = "任务缺少 config_id，已标记失败：无法确定要同步哪个配置"
        log_print(f"❌ 周版本同步: {reason}", "WEEKLY", force=True)
        task_id = task.get("task_id")
        if task_id is not None:
            with app.app_context():
                try:
                    update_task_status_with_retry(task_id, "failed", reason)
                except non_critical_task_status_errors as update_error:
                    log_print(f"更新任务状态失败: {update_error}", "TASK", force=True)
        return
    log_print(f"📅 周版本同步: 配置 {config_id}", "WEEKLY")
    with app.app_context():
        if "task_id" in task:
            try:
                update_task_status_with_retry(task["task_id"], "processing")
            except non_critical_task_status_errors as update_error:
                log_print(f"更新任务开始状态失败: {update_error}", "TASK", force=True)
        try:
            # process_weekly_version_sync 返回显式结局（见 weekly_version_sync_status）：
            # 无提交 → skipped（正常跳过），配置缺失/禁用 → failed，有文件失败 →
            # partial_failed。过去这里无条件标 "completed"，把失败和「本来就没数据」
            # 抹成同一个样子。
            sync_status, sync_message = weekly_sync_task_status_and_message(
                process_weekly_version_sync(config_id)
            )
            if sync_status != "completed":
                log_print(f"📅 周版本同步结局: {sync_status} - {sync_message}", "WEEKLY")
            if "task_id" in task:
                try:
                    update_task_status_with_retry(task["task_id"], sync_status, sync_message)
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
