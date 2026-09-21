"""Weekly task handlers extracted from task_worker_service.py.

这里同时承载周版本同步任务的**载荷解析**与**内存队列账本**（见下面两处注释块）。放在本
模块而不是 `task_worker_service.py`，是因为后者已经贴着 2000 行硬上限
（`scripts/check_file_length.py --strict` 在 2000 行报 ERROR），而这两段恰好是「不依赖
该模块的全局状态、又最值得单独测」的部分。
"""

from __future__ import annotations

import os
import threading
import time

# 优先级常量在本模块**按模块取用**（不是 `worker.X` 现取，也不是 `from … import X as Y`）：
# 本模块不 import `task_worker_service`（那是反向依赖、会成环），而优先级是纯数据、
# 没有补丁需求；按模块取用还避开了 isort 对多别名导入的拆分（I001）。
from services import task_worker_priority as task_priority
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
#  触发来源与请求模式的归一化（**任务身份的落库口径**）
#
#  这两个值原先只活在内存载荷 `TaskWrapper.task_data` 里：去重命中一条更早创建的任务行、
#  或者进程重启，它们就没了。实测那一幕 —— 手工等待意图 477 登记时库里已有 scheduled
#  任务 468，队列复用了 468，于是那次真金白银的分析在库里被记成 `trigger_source=scheduled`
#  （用户明明点过，用量面板上写着「定时」）。
#
#  落库之后（`BackgroundTask.trigger_source` / `requested_mode`），去重命中改的是
#  **同一条行**；归一化也因此只有这一份实现 —— 建任务、唤醒、执行三处必须同一把尺子，
#  各写一份就会出现「入队记成 manual、落库又成了 scheduled」这种自相矛盾的账。
# ---------------------------------------------------------------------------

TRIGGER_SOURCE_MANUAL = "manual"
TRIGGER_SOURCE_SCHEDULED = "scheduled"
TRIGGER_SOURCES = (TRIGGER_SOURCE_MANUAL, TRIGGER_SOURCE_SCHEDULED)

# 「一条 pending 的 AI 分析等了多久就算真卡死」。**必须与同步那条（300 秒）分开**：
# 一次分析本身要跑 6~8 分钟，排队窗口实测到过 13 分 53 秒，拿 300 秒去判会把正常
# 排队的分析全杀掉。定义在这里（判据所在模块）而不是 task_worker_service：那个文件
# 只 import 它，避免两处各写一个 7200。
WEEKLY_AI_TASK_STALE_SECONDS = max(
    600, int(os.environ.get("WEEKLY_AI_TASK_STALE_SECONDS", "7200") or 7200)
)


def normalize_trigger_source(value):
    """认不出来的值返回 `None`（**不回落到 scheduled**）。

    回落到 `scheduled` 会让「载荷里带了个拼错的 manual」静默变成「系统自己跑的」——
    而「用户点过却被记成定时」正是这次要修的那个谎。要默认值的调用方自己写 `or
    "scheduled"`，这样「没传」与「传错了」在日志里还分得开。
    """
    text = str(value or "").strip().lower()
    return text if text in TRIGGER_SOURCES else None


def manual_wins(*values):
    """多个来源说法不一致时取哪个。

    **只要有一处说是 manual，就记 manual。** 理由是不对称的：把用户点出来的那一次记成
    「定时」，是这次实测里真正误导了人的那个谎（账单上写着系统自己跑的）；反过来
    「定时」被记成「手动」只影响用量面板的一个标签，且几乎不会发生（manual 只能由
    用户动作那几条路径写进去）。
    """
    normalized = [normalize_trigger_source(value) for value in values]
    if TRIGGER_SOURCE_MANUAL in normalized:
        return TRIGGER_SOURCE_MANUAL
    for value in normalized:
        if value:
            return value
    return None


def normalize_requested_mode(value):
    """只认 `MODE_INCREMENTAL` / `MODE_FULL`，其余（含空）返回 `None`。

    两个常量定义在 `models/ai_analysis/job.py`（接口冻结文件），**不在本模块重定义**；
    这里按函数内延迟导入取用，避免模块级 import 顺序上的耦合。
    """
    text = str(value or "").strip().lower()
    if not text:
        return None
    from models.ai_analysis import MODE_FULL, MODE_INCREMENTAL

    return text if text in (MODE_INCREMENTAL, MODE_FULL) else None


def resolve_trigger_source(row_source, payload_source):
    """这次分析该记成谁发起的：**数据库行是权威**，载荷只作兜底。

    原先只有一个来源（内存载荷 `task_data['trigger_source']`），于是「去重命中一条更早
    创建的 scheduled 任务」必然把用户点出来的那次记成 scheduled —— 实测那一幕正是
    意图 477 复用任务 468、run 20 落库 `trigger_source='scheduled'`。

    `manual` 一旦出现在任意一侧就取 `manual`（见 `manual_wins`）：把用户点的那次记成
    「定时」正是这次要修的那个谎。
    """
    return manual_wins(row_source, payload_source) or TRIGGER_SOURCE_SCHEDULED


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
#
#  周版本 AI 分析（`weekly_ai_analysis`）原先**没有对应物**，于是它的「永久 pending」
#  清理只能看年龄（`WEEKLY_AI_TASK_STALE_SECONDS`），与同步那条「先看账本」的判据不一致：
#  同一条任务在同步那边算「只是没排到」、在 AI 这边算「卡死了」。两套判据必须同一把尺子，
#  所以这里给 AI 也开一份账本，形状与同步那份完全一致。
# ---------------------------------------------------------------------------
_enqueued_weekly_sync_task_ids = set()
_enqueued_weekly_sync_task_lock = threading.Lock()

_enqueued_weekly_ai_task_ids = set()
_enqueued_weekly_ai_task_lock = threading.Lock()


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


def register_enqueued_weekly_ai_task(task_id):
    """登记「这条周版本 AI 分析已经在内存队列里」。"""
    if task_id is None:
        return
    with _enqueued_weekly_ai_task_lock:
        _enqueued_weekly_ai_task_ids.add(task_id)


def forget_enqueued_weekly_ai_task(task_id):
    """注销：这条分析已被 worker 取走跑完（或失败），不能再算它还在队列里。"""
    if task_id is None:
        return
    with _enqueued_weekly_ai_task_lock:
        _enqueued_weekly_ai_task_ids.discard(task_id)


def is_weekly_ai_task_enqueued(task_id):
    """这条 AI 分析当前是否还在内存队列里（没被取走）。"""
    if task_id is None:
        return False
    with _enqueued_weekly_ai_task_lock:
        return task_id in _enqueued_weekly_ai_task_ids


def enqueue_weekly_sync_task(queue_obj, task_wrapper_class, task_id, config_id,
                             priority=task_priority.WEEKLY_SYNC):
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


def enqueue_weekly_ai_analysis_task(
    queue_obj,
    task_wrapper_class,
    task_id,
    config_id,
    group_key,
    *,
    trigger_source=None,
    priority=None,
):
    """把 weekly_ai_analysis 任务放进内存队列，并登记进它自己的账本。

    `trigger_source` 随载荷一起走（Agent 模式要把它原样回传给执行侧）；**权威值在
    数据库行上**（`BackgroundTask.trigger_source`），载荷只是让远端少查一次库。
    """
    source = normalize_trigger_source(trigger_source)
    priority = task_priority.WEEKLY_AI_MANUAL if (
        priority is None and source == TRIGGER_SOURCE_MANUAL
    ) else (task_priority.WEEKLY_AI_ANALYSIS if priority is None else priority)
    task_data = {
        'type': 'weekly_ai_analysis',
        'config_id': config_id,
        'group_key': group_key,
        'commit_id': str(config_id),
        'task_id': task_id,
    }
    if source:
        task_data['trigger_source'] = source
    queue_obj.put(task_wrapper_class(priority, int(time.time() * 1000000), task_data))
    register_enqueued_weekly_ai_task(task_id)
    return task_data


def forget_weekly_task_of_payload(task_data):
    """worker 处理完一个任务后调用：按载荷类型注销对应账本。

    只对周版本那两种类型生效（别的类型没有账本）。
    """
    if not isinstance(task_data, dict):
        return
    kind = task_data.get('type')
    if kind == 'weekly_sync':
        forget_enqueued_weekly_sync_task(task_data.get('task_id'))
    elif kind == 'weekly_ai_analysis':
        forget_enqueued_weekly_ai_task(task_data.get('task_id'))


# 旧名保留为别名：worker 主循环与既有用例都按这个名字取用（改名会静默打断注销）。
forget_weekly_sync_task_of_payload = forget_weekly_task_of_payload


# ---------------------------------------------------------------------------
#  卡死 pending 的清理：**同步与 AI 共用同一把尺子**
#
#  判据与阈值是两件事，别混：
#
#  * **判据**（两边完全相同，就是下面这个 `_reset_stale_rows`）：先看内存账本
#    —— 在账本里 = 只是还没轮到，维持现状；不在 = 内存队列已经丢了它，
#    再按年龄判超时。只看年龄会把「排不上队」当成「卡死」（同步那边实测过：
#    30 分钟里重置 12 次、重建 12 次，队列剩余稳定在 31~33 不下降）。
#  * **阈值**（两边不同，各有各的理由）：同步一轮实测 340~420 秒，300 秒就够判
#    「重启后丢了」；而 AI 分析一次要跑 6~8 分钟、排队窗口实测到过 13 分 53 秒，
#    拿 300 秒去判会把**正常排队**的分析全杀掉，所以是 7200 秒。
# ---------------------------------------------------------------------------


def _reset_stale_rows(
    rows,
    *,
    now_utc_naive,
    timeout_seconds,
    is_enqueued,
    error_message,
    label,
    db,
    log_print,
):
    """把「既不在队列里、又等超时」的 pending 行置 failed，返回重置条数。"""
    reset = 0
    for stale in rows:
        if is_enqueued(getattr(stale, 'id', None)):
            continue
        stale_created = getattr(stale, 'created_at', None)
        if stale_created is not None and stale_created.tzinfo:
            stale_created = stale_created.replace(tzinfo=None)
        if stale_created and (now_utc_naive - stale_created).total_seconds() > timeout_seconds:
            stale.status = 'failed'
            # **置终态必须一起退租**（与 `update_task_status_with_retry` 同一口径）：
            # 「有租约」= 有人在跑它。留着一个到期的租约，会让续租线程的 CAS
            # （只看 id + 状态 + 租约值，见 `renew_inflight_task_leases`）继续替一条
            # 已经结束的行续命 —— 那样它永远不像「没人管」。
            if hasattr(stale, 'lease_expires_at'):
                stale.lease_expires_at = None
            stale.error_message = error_message
            db.session.commit()
            log_print(f"重置卡死的{label}: task_id={stale.id}", 'WEEKLY', force=True)
            reset += 1
    return reset



def reset_stale_weekly_sync_tasks(
    config,
    now_utc_naive,
    *,
    db,
    background_task_model,
    log_print,
    timeout_seconds=300,
):
    """把**真的不会有人再执行**的 weekly_sync `pending` 行置 failed。

    **这是清理动作，与「这个配置现在活不活跃」无关，对查询到的每个配置都要做。**
    版本窗口一结束，`schedule_weekly_sync_tasks` 会把配置置 `completed` 然后 `continue`，
    此后每次调度都跳过这个配置；而原先重置整段都圈在 `if config.status == 'active':`
    里面 —— 于是窗口结束后残留的 pending 既不会被执行（内存队列早已随进程重启清空），
    也永远没人重置，永久停在 pending（线上「周版本同步任务永久排队中」的成因之一）。

    ## 「还在队列里」不算卡死（只看年龄会把队列排不上的当成卡死）

    队列只有一个 worker，前面排着每 2 分钟一轮的 `auto_sync`（所有仓库）与大仓库的
    周版本同步（800+ 文件、分钟级），所以一个刚创建几分钟的 pending **排不到头是常态**。
    判据若只看「创建至今超过 `timeout_seconds`」，调度器就会每 tick 把它置 failed，
    紧接着 `create_weekly_sync_task` 又建一条新的：实测一轮 30 分钟里重置 12 次、
    重建 12 次，队列剩余稳定在 31~33 不下降。判据因此加上**内存队列账本**
    （`is_weekly_sync_task_enqueued`）：在账本里 = 只是还没轮到，维持现状。

    进程重启丢掉的那种才是真卡死 —— 那时账本里没有它，照旧按年龄重置（这正是本函数
    当初要修的病），所以账本判据不会把它漏掉。

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
    return _reset_stale_rows(
        stale_tasks,
        now_utc_naive=now_utc_naive,
        timeout_seconds=timeout_seconds,
        is_enqueued=is_weekly_sync_task_enqueued,
        error_message='任务超时，已被调度器重置',
        label='周版本同步任务',
        db=db,
        log_print=log_print,
    )


def reset_stale_weekly_analysis_tasks(
    group_key,
    now_utc_naive,
    *,
    db,
    background_task_model,
    log_print,
    timeout_seconds=WEEKLY_AI_TASK_STALE_SECONDS,
):
    """把「真的不会有人再执行」的 `weekly_ai_analysis` pending 行置 failed。

    与 `reset_stale_weekly_sync_tasks` **同一把尺子**（见 `_reset_stale_rows`）：
    先看内存账本，不在账本里才按年龄判超时。原先这里只有年龄判据，于是
    「排在队列里没轮到」与「进程重启把它丢了」在 AI 这边长得一模一样 —— 而同步那边
    早就有账本了，同一件事两套判据必然会分叉。

    阈值比同步那条大得多（7200 秒 vs 300 秒）是有意的：一次分析本身要跑 6~8 分钟，
    排队窗口实测到过 13 分 53 秒，拿 300 秒去判会把正常排队的分析全杀掉。
    """
    stale_tasks = background_task_model.query.filter(
        background_task_model.task_type == 'weekly_ai_analysis',
        background_task_model.file_path == group_key,
        background_task_model.status == 'pending',
    ).all()
    return _reset_stale_rows(
        stale_tasks,
        now_utc_naive=now_utc_naive,
        timeout_seconds=timeout_seconds,
        is_enqueued=is_weekly_ai_task_enqueued,
        error_message='AI分析任务超时，已被调度器重置',
        label='周版本AI分析任务',
        db=db,
        log_print=log_print,
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
