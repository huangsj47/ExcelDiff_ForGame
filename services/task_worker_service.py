#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
后台任务工作服务 - 从 app.py 拆分
包含 TaskWrapper、后台任务队列管理、定时调度等
"""

import atexit
import os
import queue
import signal
import shutil
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import SQLAlchemyError

from utils.logger import log_print, log_structured_event
from utils.db_retry import db_retry
from utils.timezone_utils import now_beijing
from services.deployment_mode import get_deployment_mode, is_agent_dispatch_mode
from services.background_task_service import is_tasks_paused
from services.repo_worktree_cleanup import force_remove_repo_worktree
from services.branch_refresh_service import (
    NON_CRITICAL_BRANCH_REFRESH_ERRORS,
    queue_missing_git_branch_refresh as queue_missing_git_branch_refresh_service,
)
from services.task_worker_agent_tasks import (
    _enqueue_agent_task_from_background_task,
    _ensure_agent_dispatch_for_background_task,
    execute_task_inline_for_agent,  # noqa: F401 —— 外部调用方与测试仍按 worker.execute_task_inline_for_agent 取
)
from services.task_worker_queue_service import (
    TASK_LEASE_SECONDS,
    WAITING_INTENT_TASK_TYPE,  # 「等同步」的意图行不是可执行任务，见 _starvation_yield_note
    check_and_create_auto_sync_tasks,  # noqa: F401 —— 外部调用方与测试仍按 worker.check_and_create_auto_sync_tasks 取
    claim_task_row_for_execution,
    clear_task_lease,
    create_auto_sync_task,
    create_weekly_ai_analysis_task,
    dispatch_auto_sync_task_when_agent_mode,  # noqa: F401 —— 外部调用方与测试仍按 worker.dispatch_auto_sync_task_when_agent_mode 取
    forget_inflight_task,
    lease_is_expired,
    load_pending_tasks,
    reclaim_expired_task_leases_safely,
    regenerate_repository_cache,
    register_inflight_task,
    schedule_cleanup_task,
    stamp_task_lease,
    start_lease_renewer,
    stop_lease_renewer,
    wake_waiting_analysis_intents,
)
# 优先级的**单一来源**。本文件里凡是需要写优先级的字面量都从这里取；唯一的例外见
# `create_weekly_sync_task` 的注释（有一处非本文件的用例按源码文本断言了字面量）。
from services.task_worker_priority import (
    EXCEL_DIFF_DEFAULT as _PRIORITY_EXCEL_DIFF_DEFAULT,
    reweigh_queue,
    retune_queued_task,  # noqa: F401 —— 队列服务按 worker.retune_queued_task 现取
)
from services.task_worker_task_handlers import (
    _abandon_timed_out_auto_sync_task,
    _clear_sync_error,
    _handle_excel_diff_task,
    _handle_weekly_ai_analysis_task,
    _record_sync_error,
    _reset_repository_to_head,
)
from services.task_worker_weekly_handlers import (
    WEEKLY_AI_TASK_STALE_SECONDS as weekly_ai_task_stale_seconds,
    enqueue_weekly_sync_task,
    forget_weekly_task_of_payload,
    handle_weekly_excel_cache_task as handle_weekly_excel_cache_task_service,
    handle_weekly_sync_task as handle_weekly_sync_task_service,
    is_weekly_sync_task_enqueued,
    parse_config_id_from_commit_id,
    reset_stale_weekly_analysis_tasks,
    reset_stale_weekly_sync_tasks,
)
from services.repository_sync_status import clear_sync_error as clear_repository_sync_error
from services.repository_sync_status import record_sync_error as record_repository_sync_error
# 同步连续失败的退避（判据 + 那句汇总日志都在那个模块里，见其 docstring）。
from services.repository_sync_backoff import (
    backoff_skip_message,
    sync_failure_backoff_active,
)
# process_excel_diff_background 的返回状态：用它判断 excel_diff 任务该标 completed 还是 failed。
# 原先忽略返回值、无条件标 completed，导致「仓库已删/提交查不到」也被记为成功。
from services.excel_diff_cache_service import (
    BG_STATUS_COMPLETED as _BG_STATUS_COMPLETED,
    BG_STATUS_SKIPPED_IN_PROGRESS as _BG_STATUS_SKIPPED_IN_PROGRESS,
)
from services.ai_analysis_service import (
    build_weekly_group_key,
    run_weekly_analysis_background,
    select_primary_weekly_config,
    cleanup_expired_analysis_runs,
    fail_orphaned_analysis_runs,
    get_project_analysis_config,
    has_weekly_changes,
)
from models.ai_analysis import AiWeeklyAnalysisState
from models.ai_analysis.project_config import (
    DEFAULT_AUTO_WEEKLY_ENABLED,
    DEFAULT_WEEKLY_INTERVAL_MINUTES,
)
from services.ai.analysis_budget import budget_gate_reason
# 启动恢复扫描：把卡在非终态、且确定不会再有下文的 job 结清（`start_background_task_worker`
# 里紧跟 `fail_orphaned_analysis_runs()` 的那一步）。写侧只有一份，实现在 `job_service`。
from services.ai.job_service import recover_stale_jobs
from services.ai.scope_sampling import snapshot_already_analyzed
from services.repository_sync_window import apply_start_date, resolve_sync_window
from services.ai.weekly_state import get_or_create_weekly_state
# 「周版本同步还在跑就先别分析」的闸门（同步逐文件写缓存，跑到一半的清单会静默变小）
# `group_config_ids` 在这里的用处是**排同步时的批次判据**（同一批只跑一轮，见
# `_group_is_writing_cache`）—— 与闸门用的是同一个分组口径，不另立一份。
from services.ai.weekly_sync_gate import (
    group_config_ids,
    weekly_sync_in_flight,
    weekly_sync_stuck_note,
)
# 周版本同步的显式结局 → 任务状态映射：process_weekly_version_sync 过去用 None
# 同时表示「配置不存在/被禁用/窗口内无提交/正常跑完」四种结局，调用方只能无条件
# 标 completed，于是真正的失败与「这周本来就没数据」在任务列表里长得一模一样。
from services.weekly_version_sync_status import (
    FAILURE_TASK_STATUSES,
    STATUSES_WITH_REASON,
    TASK_STATUS_FAILED,
    TERMINAL_TASK_STATUSES,
    task_status_and_message as weekly_sync_task_status_and_message,
)

# ---------------------------------------------------------------------------
#  全局状态（由 app.py 通过 configure_task_worker 注入）
# ---------------------------------------------------------------------------
_app = None
_db = None
_excel_cache_service = None
_weekly_excel_cache_service = None
_BackgroundTask = None
_Commit = None
_Repository = None
_DiffCache = None
_WeeklyVersionConfig = None
_get_git_service = None
_get_svn_service = None
_get_unified_diff_data = None
_process_weekly_version_sync = None
_process_weekly_excel_cache = None
_db_retry = None

# 后台任务队列和状态
background_task_queue = queue.PriorityQueue()
ai_task_queue = queue.PriorityQueue()
background_task_running = False
background_task_thread = None
ai_task_thread = None
scheduler_running = False
scheduler_thread = None
_schedule_initialized = False

# 同步并发控制：同时最多5个仓库更新
_sync_semaphore = threading.Semaphore(5)
# 等不到并发许可就不再占着任务：等太久会把单线程的 worker 卡死（队列里还有
# excel_diff / cleanup 等任务），所以给一个有界的等待窗口。
# 单机模式下这条路径**不可达** —— worker 只有一个线程、且信号量只在这一处
# acquire，自己不会和自己抢；它是为 Agent/平台模式的并发派发留的。
SYNC_SEMAPHORE_TIMEOUT_SECONDS = 120

# Git 进程集合（由 configure_task_worker 注入）
_active_git_processes = None

# 分支刷新锁与冷却
branch_refresh_lock = threading.Lock()
branch_refresh_cooldown_until = {}
import os
BRANCH_REFRESH_COOLDOWN_SECONDS = max(10, int(os.environ.get("BRANCH_REFRESH_COOLDOWN_SECONDS", "120") or 120))

# Excel diff 任务入队冷却（用于抑制短时间重复入队）
excel_task_enqueue_lock = threading.Lock()
excel_task_enqueue_cooldown_until = {}
EXCEL_TASK_ENQUEUE_COOLDOWN_SECONDS = max(
    5, int(os.environ.get("EXCEL_TASK_ENQUEUE_COOLDOWN_SECONDS", "45") or 45)
)
EXCEL_TASK_ENQUEUE_COOLDOWN_MAX_KEYS = 5000
# 「一条 pending 的 AI 分析等了多久算真卡死」。判据与阈值都在
# `task_worker_weekly_handlers`（清理逻辑所在模块），这里只做名字转发 ——
# 原先两边各写一个 7200，改一处另一处不会跟着动。
WEEKLY_AI_TASK_STALE_SECONDS = weekly_ai_task_stale_seconds

NON_CRITICAL_TASK_STATUS_ERRORS = (
    SQLAlchemyError,
    RuntimeError,
    AttributeError,
    TypeError,
    ValueError,
)
NON_CRITICAL_QUEUE_ENQUEUE_ERRORS = (
    queue.Full,
    RuntimeError,
    AttributeError,
    TypeError,
    ValueError,
)
NON_CRITICAL_VCS_PREHEAL_ERRORS = (
    OSError,
    RuntimeError,
    AttributeError,
    TypeError,
    ValueError,
    subprocess.SubprocessError,
)
NON_CRITICAL_TASK_EXECUTION_ERRORS = (
    SQLAlchemyError,
    OSError,
    RuntimeError,
    AttributeError,
    TypeError,
    ValueError,
    subprocess.SubprocessError,
)
NON_CRITICAL_WORKER_LOOP_ERRORS = (
    SQLAlchemyError,
    RuntimeError,
    AttributeError,
    TypeError,
    ValueError,
    KeyError,
)
NON_CRITICAL_SYNC_THREAD_ERRORS = (
    OSError,
    RuntimeError,
    AttributeError,
    TypeError,
    ValueError,
    subprocess.SubprocessError,
)


def _deployment_mode():
    return get_deployment_mode()


def _use_agent_dispatch():
    return is_agent_dispatch_mode()


def _make_excel_task_key(repository_id, commit_id, file_path):
    return f"{repository_id}:{commit_id}:{file_path}"


def _is_excel_task_cooling_down(task_key):
    now_ts = time.time()
    with excel_task_enqueue_lock:
        cooldown_until = excel_task_enqueue_cooldown_until.get(task_key, 0.0)
    if cooldown_until > now_ts:
        return True, cooldown_until - now_ts
    return False, 0.0


def _mark_excel_task_cooldown(task_key):
    now_ts = time.time()
    with excel_task_enqueue_lock:
        excel_task_enqueue_cooldown_until[task_key] = now_ts + EXCEL_TASK_ENQUEUE_COOLDOWN_SECONDS
        if len(excel_task_enqueue_cooldown_until) > EXCEL_TASK_ENQUEUE_COOLDOWN_MAX_KEYS:
            expired_keys = [
                key for key, until in excel_task_enqueue_cooldown_until.items()
                if until <= now_ts
            ]
            for key in expired_keys:
                excel_task_enqueue_cooldown_until.pop(key, None)


def configure_task_worker(*, app, db, excel_cache_service,
                          weekly_excel_cache_service,
                          BackgroundTask, Commit, Repository, DiffCache,
                          WeeklyVersionConfig,
                          active_git_processes,
                          get_git_service, get_svn_service,
                          get_unified_diff_data,
                          process_weekly_version_sync,
                          process_weekly_excel_cache,
                          db_retry):
    """注入 Flask 应用和数据库等依赖"""
    global _app, _db, _excel_cache_service, _weekly_excel_cache_service
    global _BackgroundTask, _Commit, _Repository, _DiffCache
    global _WeeklyVersionConfig
    global _active_git_processes
    global _get_git_service, _get_svn_service, _get_unified_diff_data
    global _process_weekly_version_sync, _process_weekly_excel_cache
    global _db_retry
    _app = app
    _db = db
    _excel_cache_service = excel_cache_service
    _weekly_excel_cache_service = weekly_excel_cache_service
    _BackgroundTask = BackgroundTask
    _Commit = Commit
    _Repository = Repository
    _DiffCache = DiffCache
    _WeeklyVersionConfig = WeeklyVersionConfig
    _active_git_processes = active_git_processes
    _get_git_service = get_git_service
    _get_svn_service = get_svn_service
    _get_unified_diff_data = get_unified_diff_data
    _process_weekly_version_sync = process_weekly_version_sync
    _process_weekly_excel_cache = process_weekly_excel_cache
    _db_retry = db_retry


# ---------------------------------------------------------------------------
#  TaskWrapper
# ---------------------------------------------------------------------------
class TaskWrapper:
    """队列里的一项任务。

    `priority` 是**现在**用来排队的那一个数（会被 aging 改写）；
    `base_priority` 是它入队时的原始优先级（aging 每次都从它算起，避免反复叠加）；
    `enqueued_at` 是入队时刻（从库里恢复的老任务会被 `backdate_wrapper` 往前拨，
    否则它按「刚入队」参与 aging，等了几十分钟等于白等）。
    """

    def __init__(self, priority, counter, task_data):
        self.base_priority = priority
        self.priority = priority
        self.counter = counter
        self.task_data = task_data
        self.enqueued_at = time.time()

    def __lt__(self, other):
        if self.priority != other.priority:
            return self.priority < other.priority
        return self.counter < other.counter

    def __eq__(self, other):
        return (self.priority == other.priority
                and self.counter == other.counter)


# ---------------------------------------------------------------------------
#  Git 进程清理
# ---------------------------------------------------------------------------
def cleanup_git_processes():
    """清理所有活跃的Git进程"""
    if _active_git_processes is None:
        return
    for proc in list(_active_git_processes):
        try:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
            _active_git_processes.discard(proc)
        except (OSError, RuntimeError, ValueError, AttributeError, subprocess.SubprocessError) as e:
            log_print(f"清理Git进程时出错: {e}", 'GIT', force=True)
            try:
                proc.kill()
                _active_git_processes.discard(proc)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as kill_error:
                log_print(f"强制终止Git进程失败: {kill_error}", 'GIT', force=True)


def signal_handler(signum, frame):
    cleanup_git_processes()
    sys.exit(0)


def register_cleanup():
    """注册清理函数和信号处理"""
    atexit.register(cleanup_git_processes)
    # 仅在非测试环境下注册信号处理器（pytest 有自己的信号管理）
    if threading.current_thread() is threading.main_thread() and 'pytest' not in sys.modules:
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)


# ---------------------------------------------------------------------------
#  任务状态更新
# ---------------------------------------------------------------------------
@db_retry(max_retries=5, delay=0.1)
def update_task_status_with_retry(task_id, status, error_message=None):
    """使用重试机制更新任务状态"""
    if task_id is None:
        log_print(f"⚠️ 跳过任务状态更新，task_id为None", 'TASK')
        return
    try:
        db_task = _db.session.get(_BackgroundTask, task_id)
        if db_task:
            db_task.status = status
            if status == 'processing':
                db_task.started_at = datetime.now(timezone.utc)
                # **被取走 = 起租。** 平台侧原先没有租约：`started_at` 只证明「被取走过」，
                # 判不出「它的执行者还在不在」，于是定时清理只能靠各管一摊的年龄阈值，
                # 启动恢复更是一刀切（把所有 processing 无条件收回 pending）。
                # 租约给这两处一个共同判据（见 task_worker_queue_service 的
                # `reclaim_expired_task_leases` 与 `load_pending_tasks`）。
                lease_value = stamp_task_lease(db_task)
                # **谁开始执行它，谁就负责续租。** 登记与起租放在同一个地方，是因为
                # 「被执行」这件事有两个入口：单机模式的 worker 主循环（`claim_task_row_for_execution`
                # 里也登记一次，用的就是这个值）与 **agent 侧**（另一个进程调到这里
                # `execute_task_inline_for_agent` → 状态写 processing）。只挂主循环那一条，
                # agent 侧的长分析就没有人续租 —— 平台侧的扫描会在 3600 秒后把它的行判死、
                # 重新派发一遍，同一份输入付两次费。续租线程是幂等的（`start_lease_renewer`），
                # agent 进程没有 worker 线程时也要有它。
                register_inflight_task(task_id, lease_value)
                start_lease_renewer()
            elif status in TERMINAL_TASK_STATUSES:
                db_task.completed_at = datetime.now(timezone.utc)
                # 跑完（不论成败）都要**退租**：留着一个到期的租约，会让下一次清理
                # 把这条已经结束的行再捞一次（状态已经不是 processing 了，但
                # 「谁在跑它」这个事实必须跟着终态一起消失）。
                clear_task_lease(db_task)
                # 退租与撤出续租名单必须同时发生（两处都写，是因为终态还有别的入口，
                # 而名字挂在名单里而租约已经没了 = 一条永远被续的空行）。
                forget_inflight_task(task_id)
                # failed / skipped / partial_failed 都要把原因落库：error_message 是
                # 任务上唯一的文本字段，周版本模板靠它回答「为什么这个周版本没数据」。
                # 只有 failed 才累加 retry_count —— 跳过是正常结局，不该被算成一次重试。
                if status in STATUSES_WITH_REASON:
                    db_task.error_message = error_message
                    if status == TASK_STATUS_FAILED:
                        db_task.retry_count += 1
                else:
                    # `completed` 没有原因可写，所以要把上一轮留下的清掉（调度器把卡住的
                    # pending 标成 failed 时写过「任务超时」，那句话会被当成失败原因显示）。
                    db_task.error_message = None
            _db.session.commit()
            log_print(f"✅ 任务状态更新成功: {task_id} -> {status}", 'TASK')
            log_structured_event(
                "background_task_status_updated",
                log_type="TASK",
                background_task_id=task_id,
                status=status,
                retry_count=getattr(db_task, "retry_count", None),
            )
        else:
            log_print(f"⚠️ 未找到任务: {task_id}", 'TASK')
    except SQLAlchemyError as e:
        log_print(f"❌ 更新任务状态失败: {task_id} -> {status}, 错误: {e}", 'TASK', force=True)
        _db.session.rollback()
        raise e


# ---------------------------------------------------------------------------
#  后台任务工作线程
# ---------------------------------------------------------------------------
def background_task_worker(task_queue=None, worker_label="通用"):
    """后台任务工作线程"""
    global background_task_running
    active_queue = task_queue or background_task_queue
    log_print(f"{worker_label}后台任务工作线程启动", 'APP')
    log_print(f"{worker_label}初始队列大小: {active_queue.qsize()}", 'APP')
    while background_task_running:
        # 「临时暂停」要真的停 —— 这个标志由 `refresh_merge_diff` 在删缓存前后设/清，
        # 目的是别让后台线程把它刚删掉的那批缓存在同一瞬间算完写回去
        # （那正是「点了重新计算、刷新后没变化」的成因）。见
        # `services/background_task_service.py` 的模块 docstring。
        #
        # 这里是**跳过这一轮**而不是阻塞等待：领到任务之后阻塞会把心跳与租约续期
        # 一起停掉（超时会让节点被判离线、任务被别的节点领走）。没领任务时等，
        # 才是安全的等法。
        if is_tasks_paused():
            time.sleep(0.2)
            continue
        task_processed = False
        try:
            # **取任务之前先按等待时长重算优先级。** 只在入队那一刻算 aging 等于没算
            # （那时等待时长为 0），而饿死的形态恰恰是「任务在队列里躺着不动」：
            # 同步每 15 分钟补一条、一条要跑 4~5 分钟，低优先级任务于是无限「排队中」。
            # 在**外面**做：队里那条本该被提上来的任务还在堆底，不重算就永远取不到它。
            # 读不动（假队列）时它自己返回 0，不影响主循环。
            if task_queue is None:
                reweigh_queue(background_task_queue)
                task_wrapper = background_task_queue.get(timeout=1)
            else:
                reweigh_queue(active_queue)
                task_wrapper = active_queue.get(timeout=1)
            task_processed = True
            priority = task_wrapper.priority
            task = task_wrapper.task_data
            # **先把库里那一行占下来**（条件 UPDATE + rowcount，见
            # `claim_task_row_for_execution`）：双 worker 竞争时只有一个能抢到，
            # 否则同一条同步会被两个进程同时写缓存、同一份输入付两次费。
            # 抢不到就跳过这一条（`task_processed` 已经是 True，finally 里照常注销账本）。
            if not claim_task_row_for_execution(task.get('task_id') if isinstance(task, dict) else None):
                log_print(
                    f"⏭️ 任务已被别的 worker 取走，本进程跳过: {task.get('type')} "
                    f"(task_id={task.get('task_id')})",
                    'TASK',
                    force=True,
                )
                continue
            log_print(f"🔧 后台任务开始处理: {task['type']} (优先级: {priority}) | 队列剩余: {active_queue.qsize()}", 'EXCEL')

            if task['type'] == 'excel_diff':
                _handle_excel_diff_task(task, priority)
            elif task['type'] == 'cleanup_cache':
                # ⚠️ 必须自带 app context。本函数由 threading.Thread 启动，**不继承**
                # 主线程的 app context（bootstrap 里那个 `with app.app_context()`
                # 只包住了「启动线程」这一瞬间）。而 cleanup_old_cache /
                # cleanup_expired_analysis_runs 内部用的是模块级 Model.query，
                # 没有 context 就抛 RuntimeError: Working outside of application context。
                #
                # 历史后果（已实测复现）：cleanup_old_cache 把 RuntimeError 吞成 return 0
                # → 每天 04:00 的缓存清理**从未真正清理过任何数据**；
                # cleanup_expired_analysis_runs 则把 RuntimeError 抛到外层，
                # 被 NON_CRITICAL_WORKER_LOOP_ERRORS 接住只打一行日志。
                with _app.app_context():
                    log_print(f"🧹 清理缓存: {task.get('days', 30)} 天前的数据", 'CACHE')
                    cleaned = _excel_cache_service.cleanup_old_cache(task.get('days', 30))
                    if cleaned is None:
                        log_print("❌ 清理缓存失败（返回 None，与「清理 0 条」区分开）", 'CACHE', force=True)
                    else:
                        log_print(f"🧹 清理缓存完成: {cleaned} 条", 'CACHE')
                    # 周版本 Excel 缓存：每行是一整份渲染好的 HTML/CSS/JS，是本库最占
                    # 空间的表。它的**过期清理此前只挂在 `tasks/cache_cleanup.py` 上**，
                    # 而那个模块没有任何调用者（已整包删除），于是这张表从上线起就没被
                    # 自动清过，只能靠管理页上的手动按钮。
                    # 分工：版本对不上的行由启动清理负责（cleanup_version_mismatch_cache），
                    # 这里负责「超过 expire_days 没用过」的行。
                    weekly_cleaned = _weekly_excel_cache_service.cleanup_expired_cache()
                    if weekly_cleaned is None:
                        log_print("❌ 清理周版本Excel缓存失败（返回 None，与「清理 0 条」区分开）", 'CACHE', force=True)
                    elif weekly_cleaned:
                        log_print(f"🧹 清理周版本Excel缓存: {weekly_cleaned} 条", 'CACHE')
                    ai_cleaned = cleanup_expired_analysis_runs()
                    if ai_cleaned is None:
                        log_print("❌ 清理AI分析缓存失败", 'AI', force=True)
                    elif ai_cleaned:
                        log_print(f"🧹 清理AI分析缓存: {ai_cleaned} 条过期记录", 'AI')
            elif task['type'] == 'regenerate_cache':
                # 同上：regenerate_repository_cache 用 _db.session.get()，必须有 context，
                # 否则 RuntimeError 被它自己的 except 吞掉、且没有 return → 返回 None，
                # 调用方随即打印「✅ 缓存重新生成完成，已添加 None 个任务到队列」——
                # 失败被一个 ✅ 成功语句盖住。回归保护见
                # tests/test_worker_tasks_have_app_context.py。
                with _app.app_context():
                    log_print(f"🔄 重新生成缓存: 仓库 {task['repository_id']}", 'CACHE')
                    task_count = regenerate_repository_cache(task['repository_id'])
                    if task_count is None:
                        log_print(f"❌ 缓存重新生成失败: 仓库 {task['repository_id']}", 'CACHE', force=True)
                    else:
                        log_print(f"✅ 缓存重新生成完成，已添加 {task_count} 个任务到队列", 'CACHE')
            elif task['type'] == 'auto_sync':
                _handle_auto_sync_task(task)
            elif task['type'] == 'weekly_sync':
                _handle_weekly_sync_task(task)
            elif task['type'] == 'weekly_excel_cache':
                _handle_weekly_excel_cache_task(task)
            elif task['type'] == 'weekly_ai_analysis':
                _handle_weekly_ai_analysis_task(task)
            log_print(f"✅ 后台任务完成: {task['type']} (优先级: {priority}) | 队列剩余: {active_queue.qsize()}", 'TASK')
        except queue.Empty:
            continue
        except NON_CRITICAL_WORKER_LOOP_ERRORS as e:
            log_print(f"后台任务处理异常: {e}", 'APP', force=True)
            traceback.print_exc()
        finally:
            if task_processed:
                # 出队就注销「已入队」账本（见 weekly_handlers 里集合的注释）。放 finally：
                # 中途抛异常也得注销，否则 create_weekly_sync_task 会以为它还在队列里。
                # 取 task_data 用 getattr：这一句在 finally 里，一旦抛出去会把工作线程打死。
                _finished_payload = getattr(task_wrapper, 'task_data', None)
                forget_weekly_task_of_payload(_finished_payload)
                # **撤出租约续期名单也必须放 finally**：漏掉一条就是「执行者早走了、
                # 租约却一直被续」（值班线程还在替它刷新），那样它永远收不回来。
                # 抢占成功的登记在 `claim_task_row_for_execution` 里，两边用同一个 task_id。
                if isinstance(_finished_payload, dict):
                    forget_inflight_task(_finished_payload.get('task_id'))
                try:
                    active_queue.task_done()
                except ValueError:
                    pass
    log_print("后台任务工作线程停止", 'APP')


def _handle_auto_sync_task(task):
    """处理自动同步任务（含并发控制和超时处理）"""
    repo_id = task['repository_id']
    log_print(f"🔄 自动数据分析: 仓库 {repo_id}，等待并发许可...", 'SYNC')

    # 并发控制：最多同时5个仓库更新
    acquired = _sync_semaphore.acquire(timeout=SYNC_SEMAPHORE_TIMEOUT_SECONDS)
    if not acquired:
        message = f"等待同步并发许可超时({SYNC_SEMAPHORE_TIMEOUT_SECONDS}s)，本次同步未执行"
        log_print(f"⏰ 仓库 {repo_id} {message}", 'SYNC', force=True)
        _abandon_timed_out_auto_sync_task(task, message)
        return

    try:
        log_print(f"🔓 仓库 {repo_id} 获得并发许可，开始同步", 'SYNC')
        _handle_auto_sync_task_inner(task)
    finally:
        _sync_semaphore.release()
        log_print(f"🔒 仓库 {repo_id} 释放并发许可", 'SYNC')


def _handle_auto_sync_task_inner(task):
    """自动同步任务的实际逻辑"""
    with _app.app_context():
        if 'task_id' in task:
            try:
                update_task_status_with_retry(task['task_id'], 'processing')
            except NON_CRITICAL_TASK_STATUS_ERRORS as update_error:
                log_print(f"更新任务开始状态失败: {update_error}", 'TASK', force=True)
        try:
            repository = _db.session.get(_Repository, task['repository_id'])
            if repository:
                force_reclone = bool(task.get("force_reclone"))
                force_repair_update = bool(task.get("force_repair_update")) and not force_reclone
                log_print(f"开始自动分析仓库: {repository.name}", 'SYNC')
                if repository.type == 'git':
                    from services.threaded_git_service import ThreadedGitService
                    git_service = ThreadedGitService(
                        repository.url, repository.root_directory,
                        repository.username, repository.token, repository
                    )
                    log_print(f"🚀 [BACKGROUND_SYNC] 开始后台同步仓库 ID: {repository.id}", 'SYNC')
                    log_print(f"🔧 [BACKGROUND_SYNC] 本地路径: {git_service.local_path}", 'SYNC')
                    if force_reclone:
                        log_print(
                            f"🧹 [BACKGROUND_SYNC] 手动重试策略=重克隆，先清理本地目录: {git_service.local_path}",
                            "SYNC",
                            force=True,
                        )
                        if not force_remove_repo_worktree(git_service.local_path):
                            error_msg = f"重试失败：无法清理本地目录 {git_service.local_path}"
                            repository.clone_status = "failed"
                            repository.clone_error = error_msg
                            _record_sync_error(repository, error_msg)
                            if 'task_id' in task:
                                try:
                                    update_task_status_with_retry(task['task_id'], 'failed', error_msg)
                                except NON_CRITICAL_TASK_STATUS_ERRORS:
                                    pass
                            return
                    elif force_repair_update and os.path.isdir(git_service.local_path):
                        log_print(
                            f"🩺 [BACKGROUND_SYNC] 手动重试策略=修复后更新，先执行Git自愈: {git_service.local_path}",
                            "SYNC",
                            force=True,
                        )
                        try:
                            if hasattr(git_service, "_self_heal_repository_state"):
                                heal_ok, heal_msg = git_service._self_heal_repository_state()
                                if not heal_ok:
                                    log_print(f"⚠️ Git自愈未完全成功: {heal_msg}", "SYNC", force=True)
                        except NON_CRITICAL_VCS_PREHEAL_ERRORS as heal_exc:
                            log_print(f"⚠️ Git自愈异常，继续尝试同步: {heal_exc}", "SYNC", force=True)

                    # 使用5分钟超时的线程执行 clone_or_update
                    sync_result = [False, "未执行"]
                    sync_exception = [None]

                    def _do_clone_or_update():
                        try:
                            s, m = git_service.clone_or_update_repository()
                            sync_result[0] = s
                            sync_result[1] = m
                        except NON_CRITICAL_SYNC_THREAD_ERRORS as ex:
                            sync_exception[0] = ex

                    sync_thread = threading.Thread(target=_do_clone_or_update, daemon=True)
                    sync_thread.start()
                    sync_thread.join(timeout=300)  # 5分钟超时

                    if sync_thread.is_alive():
                        # 超时：记录错误并重置仓库
                        error_msg = f"Git pull 超时（超过5分钟），已中断并重置仓库"
                        log_print(f"⏰ [BACKGROUND_SYNC] {error_msg}: {repository.name}", 'SYNC', force=True)
                        if force_reclone:
                            repository.clone_status = "failed"
                            repository.clone_error = error_msg
                        _reset_repository_to_head(git_service, repository)
                        _record_sync_error(repository, error_msg)
                        if 'task_id' in task:
                            try:
                                update_task_status_with_retry(task['task_id'], 'failed', error_msg)
                            except NON_CRITICAL_TASK_STATUS_ERRORS:
                                pass
                        return

                    if sync_exception[0]:
                        error_msg = f"clone_or_update 异常: {sync_exception[0]}"
                        log_print(f"❌ [BACKGROUND_SYNC] {error_msg}", 'SYNC', force=True)
                        if force_reclone:
                            repository.clone_status = "failed"
                            repository.clone_error = error_msg
                        _reset_repository_to_head(git_service, repository)
                        _record_sync_error(repository, error_msg)
                        if 'task_id' in task:
                            try:
                                update_task_status_with_retry(task['task_id'], 'failed', error_msg)
                            except NON_CRITICAL_TASK_STATUS_ERRORS:
                                pass
                        return

                    success, message = sync_result
                    log_print(f"🔧 [BACKGROUND_SYNC] clone_or_update_repository 返回: success={success}, message={message}", 'SYNC')

                    if not success:
                        log_print(f"仓库克隆/更新失败: {message}", 'SYNC', force=True)
                        if force_reclone:
                            repository.clone_status = "failed"
                            repository.clone_error = str(message or "clone/update failed")
                        _reset_repository_to_head(git_service, repository)
                        _record_sync_error(repository, f"同步失败: {message}")
                        if 'task_id' in task:
                            try:
                                update_task_status_with_retry(task['task_id'], 'failed', message)
                            except NON_CRITICAL_TASK_STATUS_ERRORS:
                                pass
                        return

                    repository.clone_status = "completed"
                    repository.clone_error = None
                    # 同步成功 → 清除之前的错误状态
                    _clear_sync_error(repository)

                    # 这一轮按什么口径采集：**分支 tip 变了没有**优先，日期水位线兜底。
                    # 提交日期可以被回填（导表工具带上原始日期），而 `git log --since`
                    # 遇到日期更旧的 tip 会**当场停住整个遍历** —— 那一批提交（含日期
                    # 正常的）全都进不来，且没有任何提示。判据见
                    # `services/repository_sync_window.py`。
                    latest_commit = _Commit.query.filter_by(repository_id=repository.id)\
                        .order_by(_Commit.commit_time.desc()).first()
                    window = resolve_sync_window(
                        git_service, repository,
                        latest_known_commit_time=getattr(latest_commit, 'commit_time', None),
                    )
                    log_print(f"🔍 [BACKGROUND_SYNC] 采集口径：{window.reason}", 'SYNC')

                    start_time = time.time()
                    commits = git_service.get_commits_threaded(
                        since_date=window.since_date, limit=1000,
                        rev_range=window.rev_range,
                    )
                    if window.rev_range:
                        # 区间不看日期，但 `start_date` 是用户声明的下界，仍然作数。
                        commits = apply_start_date(commits, repository.start_date)
                    end_time = time.time()
                    log_print(f"⚡ [THREADED_GIT] 多线程获取提交记录耗时: {(end_time - start_time):.2f}秒, 提交数: {len(commits)}", 'GIT')
                    log_print(f"🔍 [BACKGROUND_SYNC] Git服务获取到 {len(commits)} 个提交记录", 'SYNC')
                    commits_added = 0
                    excel_tasks_added = 0

                    # 批量查询已存在的 (commit_id, path) 组合
                    #
                    # 【为什么必须带上 path】git_service.get_commits 对一次提交里
                    # **每一个**变更文件各产出一条记录，它们的 commit_id 都是同一个
                    # hexsha（见 git_service 里 for file_path in ... / diff 循环）。
                    # 原来只按 commit_id 判重，于是同一次提交的第 2 个文件起全被跳过
                    # —— 实测：一次提交改了 3 个文件，commits_log 里只落下第 1 个。
                    # 评审者在提交列表里看不到其余文件，确认了这条提交就等于确认了
                    # 没看过的改动。行标识是三元组 (repository_id, commit_id, path)，
                    # 与 _make_excel_task_key 的口径一致。
                    existing_pairs = set()
                    all_incoming_ids = list(set(cd['commit_id'] for cd in commits))
                    BATCH_SIZE = 500
                    for batch_start in range(0, len(all_incoming_ids), BATCH_SIZE):
                        batch_ids = all_incoming_ids[batch_start:batch_start + BATCH_SIZE]
                        existing_rows = _db.session.query(_Commit.commit_id, _Commit.path).filter(
                            _Commit.repository_id == repository.id,
                            _Commit.commit_id.in_(batch_ids)
                        ).all()
                        existing_pairs.update((row[0], row[1] or '') for row in existing_rows)
                    log_print(
                        f"🔍 [BACKGROUND_SYNC] 批量查询完成: {len(existing_pairs)} 条 (commit_id, path) 已存在",
                        'SYNC',
                    )

                    new_commit_objects = []
                    excel_task_list = []
                    for commit_data in commits:
                        pair = (commit_data['commit_id'], commit_data.get('path', '') or '')
                        if pair in existing_pairs:
                            continue
                        existing_pairs.add(pair)
                        new_commit = _Commit(
                            repository_id=repository.id,
                            commit_id=commit_data['commit_id'],
                            author=commit_data.get('author', ''),
                            message=commit_data.get('message', ''),
                            commit_time=commit_data.get('commit_time'),
                            path=commit_data.get('path', ''),
                            version=commit_data.get('version', commit_data['commit_id'][:8]),
                            operation=commit_data.get('operation', 'M'),
                            status='pending'
                        )
                        new_commit_objects.append(new_commit)
                        file_path = commit_data.get('path', '')
                        # 别在这里手写扩展名：口径统一到 is_excel_file()（平台配表清单
                        # .xlsx/.xls/.xlsm/.xlsb/.csv，本文件标记 is_excel 的那处也认它们）。
                        # 原先只写 ('.xlsx', '.xls')，.xlsm/.xlsb/.csv 的提交不会排进队列。
                        if _excel_cache_service.is_excel_file(file_path):
                            excel_task_list.append({
                                'type': 'excel_diff',
                                'repository_id': repository.id,
                                'commit_id': commit_data['commit_id'],
                                'file_path': file_path
                            })

                    if new_commit_objects:
                        _db.session.bulk_save_objects(new_commit_objects)
                        commits_added = len(new_commit_objects)
                        log_print(f"➕ [BACKGROUND_SYNC] 批量插入 {commits_added} 个新提交", 'SYNC')

                    for task_data in excel_task_list:
                        try:
                            task_counter = int(time.time() * 1000000)
                            tw = TaskWrapper(8, task_counter, task_data)
                            background_task_queue.put(tw)
                            excel_tasks_added += 1
                        except NON_CRITICAL_QUEUE_ENQUEUE_ERRORS as e:
                            log_print(f"❌ [BACKGROUND_SYNC] 添加Excel缓存任务失败: {e}", 'SYNC', force=True)
                    if excel_tasks_added > 0:
                        log_print(f"📊 [BACKGROUND_SYNC] 批量添加 {excel_tasks_added} 个Excel缓存任务", 'SYNC')

                    # 落库成功之后才记 tip：中途失败时下一轮还会用旧区间重采一遍，
                    # 而重采是安全的（判重键是 `(commit_id, path)`）。
                    if window.tip:
                        repository.last_synced_tip = window.tip
                    _db.session.commit()
                    log_print(f"✅ [BACKGROUND_SYNC] 后台同步完成，添加了 {commits_added} 个新提交，{excel_tasks_added} 个Excel缓存任务", 'SYNC')
                    log_print(f"✅ 自动数据分析完成: {repository.name}, 添加了 {commits_added} 个提交记录，{excel_tasks_added} 个Excel缓存任务", 'SYNC')
                elif repository.type == 'svn':
                    svn_service = _get_svn_service(repository)
                    if force_reclone:
                        log_print(
                            f"🧹 [BACKGROUND_SYNC] 手动重试策略=重检出，先清理SVN本地目录: {svn_service.local_path}",
                            "SYNC",
                            force=True,
                        )
                        if not force_remove_repo_worktree(svn_service.local_path):
                            error_msg = f"重试失败：无法清理SVN目录 {svn_service.local_path}"
                            repository.clone_status = "failed"
                            repository.clone_error = error_msg
                            _record_sync_error(repository, error_msg)
                            if 'task_id' in task:
                                try:
                                    update_task_status_with_retry(task['task_id'], 'failed', error_msg)
                                except NON_CRITICAL_TASK_STATUS_ERRORS:
                                    pass
                            return
                    elif force_repair_update and os.path.isdir(svn_service.local_path):
                        log_print(
                            f"🩺 [BACKGROUND_SYNC] 手动重试策略=修复后更新，先执行SVN cleanup/revert: {svn_service.local_path}",
                            "SYNC",
                            force=True,
                        )
                        try:
                            if hasattr(svn_service, "_run_svn_cleanup"):
                                svn_service._run_svn_cleanup()
                            if hasattr(svn_service, "_run_svn_revert"):
                                svn_service._run_svn_revert()
                        except NON_CRITICAL_VCS_PREHEAL_ERRORS as heal_exc:
                            log_print(f"⚠️ SVN预修复异常，继续尝试更新: {heal_exc}", "SYNC", force=True)

                    success, message = svn_service.checkout_or_update_repository()
                    if not success:
                        error_msg = f"SVN 同步失败: {message}"
                        log_print(f"❌ [BACKGROUND_SYNC] {error_msg}", 'SYNC', force=True)
                        if force_reclone:
                            repository.clone_status = "failed"
                            repository.clone_error = error_msg
                        _record_sync_error(repository, error_msg)
                        if 'task_id' in task:
                            try:
                                update_task_status_with_retry(task['task_id'], 'failed', error_msg)
                            except NON_CRITICAL_TASK_STATUS_ERRORS:
                                pass
                        return
                    repository.clone_status = "completed"
                    repository.clone_error = None
                    commits_added = svn_service.sync_repository_commits(_db, _Commit)
                    _clear_sync_error(repository)
                    log_print(f"✅ 自动数据分析完成: {repository.name}, 添加了 {commits_added} 个提交记录", 'SYNC')
                else:
                    raise ValueError(f"不支持的仓库类型: {repository.type}")
            else:
                raise ValueError(f"仓库不存在: {task['repository_id']}")
            if 'task_id' in task:
                try:
                    update_task_status_with_retry(task['task_id'], 'completed')
                except NON_CRITICAL_TASK_STATUS_ERRORS as update_error:
                    log_print(f"更新任务完成状态失败: {update_error}", 'TASK', force=True)
        except NON_CRITICAL_TASK_EXECUTION_ERRORS as e:
            log_print(f"❌ 自动数据分析失败: {e}", 'SYNC', force=True)
            try:
                repository_id = task.get('repository_id')
                repository = _db.session.get(_Repository, repository_id) if repository_id else None
                if repository:
                    _record_sync_error(repository, f"自动同步失败: {e}")
            except NON_CRITICAL_TASK_STATUS_ERRORS:
                pass
            if 'task_id' in task:
                db_task = _db.session.get(_BackgroundTask, task['task_id'])
                if db_task:
                    db_task.status = 'failed'
                    db_task.error_message = str(e)
                    db_task.completed_at = datetime.now(timezone.utc)
                    db_task.retry_count += 1
                    _db.session.commit()


def _handle_weekly_sync_task(task):
    """处理周版本同步任务"""
    try:
        return handle_weekly_sync_task_service(
            task=task,
            app=_app,
            update_task_status_with_retry=update_task_status_with_retry,
            process_weekly_version_sync=_process_weekly_version_sync,
            non_critical_task_status_errors=NON_CRITICAL_TASK_STATUS_ERRORS,
            non_critical_task_execution_errors=NON_CRITICAL_TASK_EXECUTION_ERRORS,
            log_print=log_print,
        )
    finally:
        # **同步一收尾就唤醒「等同步跑完就自动开始」的意图**（审计要求的那一半：
        # 用户点一次就够）。放在 finally 里的理由：这一轮同步是跑完了、跳过了还是失败了，
        # 对「缓存已经不再被写」这件事是一样的 —— 该被唤醒的时刻是同一个。
        wake_waiting_analysis_intents_safely()


def wake_waiting_analysis_intents_safely():
    """唤醒等待意图，**绝不把 worker 打死**（它在 worker 主循环的调用链上）。

    单机模式下这里跑在唯一那个工作线程里：抛出去会被循环的
    `NON_CRITICAL_WORKER_LOOP_ERRORS` 接住（只打一行日志），但已经领到的那个任务的收尾
    记账会被跳过；而这条路径上最坏的结果只是「这次没自动开始」（下一次同步收尾或下一个
    调度周期还会再试），不值得拿工作线程去赌。
    """
    try:
        # **必须自带应用上下文。** 本函数在 `_handle_weekly_sync_task` 的 `finally` 里调用，
        # 而那个位置已经在 `handle_weekly_sync_task_service` 内部的 `app_context()` **之外**
        # —— worker 线程裸调时，第一次读库就抛 `Working outside of application context.`，
        # 被下面那个宽 `except` 咽掉，于是这条「同步一收尾就自动开始」的**主路径静默失效**：
        # 日志里只剩一行「不影响这次同步」的警告，用户还得等下一个调度周期才被兜住。
        # 真机实测就是这么现形的（修复前日志原文）：
        #     ⚠️ 唤醒等待同步的分析意图失败（不影响这次同步）: Working outside of application context.
        # 嵌套 push 是安全的（Flask 允许），所以这里不必先判断「是不是已经在上下文里」。
        with _app.app_context():
            wake_waiting_analysis_intents()
    except Exception as wake_error:  # noqa: BLE001 —— 见 docstring
        log_print(f"⚠️ 唤醒等待同步的分析意图失败（不影响这次同步）: {wake_error}", 'AI', force=True)


def _handle_weekly_excel_cache_task(task):
    """处理周版本Excel缓存任务"""
    return handle_weekly_excel_cache_task_service(
        task=task,
        app=_app,
        update_task_status_with_retry=update_task_status_with_retry,
        process_weekly_excel_cache=_process_weekly_excel_cache,
        non_critical_task_status_errors=NON_CRITICAL_TASK_STATUS_ERRORS,
        non_critical_task_execution_errors=NON_CRITICAL_TASK_EXECUTION_ERRORS,
        log_print=log_print,
    )


# ---------------------------------------------------------------------------
#  任务创建 / 队列管理
# ---------------------------------------------------------------------------


def start_background_task_worker():
    """启动后台任务工作线程"""
    global background_task_running, background_task_thread, ai_task_thread
    if not background_task_running:
        background_task_running = True
        # **先清墓碑，再收活**：平台重启会留下 status='running' 的 AI 分析记录，
        # 那些 run 永远不会再被写完成（持有它们的进程已经没了）。它们会让
        # `/ai-analysis/.../latest` 报「没有结果」，界面据此又去自动开跑一次 ——
        # 用户看到的就是「重启后点一下 AI分析，它自己跑起来了」。
        # 必须在 `load_pending_tasks()`（会把上次残留的 processing 任务改回 pending
        # 重新入队）之前做，否则新一轮分析会与幽灵记录混在一起。
        fail_orphaned_analysis_runs()
        # **然后是 job**（第二波 P0-01 的恢复扫描，`job_service.recover_stale_jobs`）。
        # 顺序是**必须的**：上面那一步刚把重启前留下的幽灵 run 判成 `failed`，而持有
        # 它们的那些 job 还停在 `running` —— 判据 ①（关联的 run 已是终态）正好在这一刻
        # 成立，于是 job 与它那条 run 落进同一个终态，不给用户留一条「看起来还在跑、
        # 但永远不会动」的身份（那会让同一个 target 再也建不出 job：`active_key` 占着
        # 唯一索引）。它同时兜住另外两种**没有任何在途代码会去收口**的形态：
        # job 建好了但排程那一步炸了、任务行被人手工改成终态。
        #
        # 与上面那一步是同一套手法：一次独立的、幂等的启动扫描，自己提交、自己记日志，
        # 失败只留一行告警（不许把整个 worker 拖死）。**不另起入口** —— 就是这里。
        recover_stale_jobs()
        load_pending_tasks()
        # 租约续期线程：**必须在 worker 线程之外**（worker 跑任务那段时间正阻塞在任务里，
        # 轮不到它自己续租）。没有它，租约时长就等价于「任务最长能跑多久」，一次合法的
        # 长跑会在中途被判死 → 重投 → 跑两遍（AI 分析是同一份输入付两次费）。
        start_lease_renewer()
        background_task_thread = threading.Thread(target=background_task_worker, daemon=True)
        background_task_thread.start()
        ai_task_thread = threading.Thread(
            target=background_task_worker,
            args=(ai_task_queue, "AI "),
            daemon=True,
        )
        ai_task_thread.start()
        # single 模式下需要本地执行任务，顺带启用清理任务调度。
        start_scheduler(include_cleanup=not _use_agent_dispatch())
        log_print("后台任务工作线程已启动", 'APP')


def stop_background_task_worker():
    """停止后台任务工作线程"""
    global background_task_running, background_task_thread, ai_task_thread
    if background_task_running:
        log_print("正在停止后台任务工作线程...", 'APP')
        background_task_running = False
        if background_task_thread and background_task_thread.is_alive():
            try:
                background_task_thread.join(timeout=3)
                if background_task_thread.is_alive():
                    log_print("后台任务线程未能在3秒内正常停止", 'APP', force=True)
                else:
                    log_print("后台任务工作线程已停止", 'APP')
            except RuntimeError as e:
                log_print(f"停止后台任务线程时出现错误: {e}", 'APP', force=True)
        else:
            log_print("后台任务工作线程已停止", 'APP')
        if ai_task_thread and ai_task_thread.is_alive():
            try:
                ai_task_thread.join(timeout=3)
            except RuntimeError as e:
                log_print(f"停止 AI 任务线程时出现错误: {e}", 'APP', force=True)
        # 收工就不该再有人续租：此刻还挂在名单里的任务，本进程不会把它们跑完了。
        stop_lease_renewer()
        stop_scheduler()


def add_excel_diff_task(repository_id, commit_id, file_path, priority=_PRIORITY_EXCEL_DIFF_DEFAULT,
                        auto_commit=True):
    """添加Excel差异处理任务到优先级队列"""
    task_key = _make_excel_task_key(repository_id, commit_id, file_path)
    bypass_cooldown = priority <= 3

    existing_task = _BackgroundTask.query.filter(
        _BackgroundTask.task_type == 'excel_diff',
        _BackgroundTask.repository_id == repository_id,
        _BackgroundTask.commit_id == commit_id,
        _BackgroundTask.file_path == file_path,
        _BackgroundTask.status.in_(['pending', 'processing'])
    ).order_by(_BackgroundTask.id.desc()).first()
    if existing_task:
        if priority < existing_task.priority:
            existing_task.priority = priority
            if auto_commit:
                _db.session.commit()
            log_print(f"更新任务优先级: {file_path} (优先级: {priority})", 'TASK')
        _mark_excel_task_cooldown(task_key)
        return existing_task.id

    cooling_down, remain_seconds = _is_excel_task_cooling_down(task_key)
    if cooling_down and not bypass_cooldown:
        log_print(
            f"跳过冷却期内重复Excel任务: {file_path} (剩余 {remain_seconds:.1f}s)",
            'TASK'
        )
        return None

    task = _BackgroundTask(
        task_type='excel_diff',
        repository_id=repository_id,
        commit_id=commit_id,
        file_path=file_path,
        priority=priority
    )
    _db.session.add(task)
    _db.session.flush()
    _enqueue_agent_task_from_background_task(
        task,
        extra_payload={
            "repository_id": repository_id,
            "commit_id": commit_id,
            "file_path": file_path,
        },
    )
    if auto_commit:
        _db.session.commit()

    if not _use_agent_dispatch():
        task_data = {
            'type': 'excel_diff',
            'repository_id': repository_id,
            'commit_id': commit_id,
            'file_path': file_path,
            'task_id': task.id
        }
        task_counter = int(time.time() * 1000000)
        tw = TaskWrapper(priority, task_counter, task_data)
        background_task_queue.put(tw)
    _mark_excel_task_cooldown(task_key)
    priority_text = "高优先级" if priority < 5 else "普通优先级"
    log_print(f"添加Excel差异任务到队列 ({priority_text}): {file_path}", 'EXCEL')
    return task.id


def add_excel_diff_tasks_batch(repository_id, excel_commits, priority=_PRIORITY_EXCEL_DIFF_DEFAULT):
    """批量添加Excel差异处理任务到优先级队列"""
    if not excel_commits:
        return

    existing_tasks = set()
    existing_query = _BackgroundTask.query.filter(
        _BackgroundTask.task_type == 'excel_diff',
        _BackgroundTask.repository_id == repository_id,
        _BackgroundTask.status.in_(['pending', 'processing'])
    ).all()
    for task in existing_query:
        existing_tasks.add((task.commit_id, task.file_path))
    incoming_seen_tasks = set()
    new_tasks = []
    new_task_keys = []
    base_counter = int(time.time() * 1000000)
    for commit_data in excel_commits:
        commit_id = commit_data['commit_id']
        file_path = commit_data['path']
        task_pair = (commit_id, file_path)
        task_key = _make_excel_task_key(repository_id, commit_id, file_path)
        if task_pair in existing_tasks or task_pair in incoming_seen_tasks:
            continue
        incoming_seen_tasks.add(task_pair)
        cooling_down, _ = _is_excel_task_cooling_down(task_key)
        if cooling_down:
            continue
        new_tasks.append({
            'task_type': 'excel_diff',
            'repository_id': repository_id,
            'commit_id': commit_id,
            'file_path': file_path,
            'priority': priority
        })
        new_task_keys.append(task_pair)
    if new_tasks:
        _db.session.bulk_insert_mappings(_BackgroundTask, new_tasks)
        _db.session.commit()
        commit_ids = list({commit_id for commit_id, _ in new_task_keys})
        file_paths = list({file_path for _, file_path in new_task_keys})
        inserted_tasks = _BackgroundTask.query.filter(
            _BackgroundTask.task_type == 'excel_diff',
            _BackgroundTask.repository_id == repository_id,
            _BackgroundTask.status == 'pending',
            _BackgroundTask.commit_id.in_(commit_ids),
            _BackgroundTask.file_path.in_(file_paths)
        ).all()
        inserted_task_map = {
            (task.commit_id, task.file_path): task for task in inserted_tasks
        }
        requires_agent_dispatch = _use_agent_dispatch()
        for i, (commit_id, file_path) in enumerate(new_task_keys):
            task = inserted_task_map.get((commit_id, file_path))
            if not task:
                continue
            if requires_agent_dispatch:
                _enqueue_agent_task_from_background_task(
                    task,
                    extra_payload={
                        "repository_id": repository_id,
                        "commit_id": task.commit_id,
                        "file_path": task.file_path,
                    },
                )
            else:
                task_data = {
                    'type': 'excel_diff',
                    'repository_id': repository_id,
                    'commit_id': task.commit_id,
                    'file_path': task.file_path,
                    'task_id': task.id
                }
                task_counter = base_counter + i
                tw = TaskWrapper(priority, task_counter, task_data)
                background_task_queue.put(tw)
            _mark_excel_task_cooldown(_make_excel_task_key(repository_id, task.commit_id, task.file_path))
        if requires_agent_dispatch:
            _db.session.commit()
        log_print(f"批量添加了 {len(new_tasks)} 个Excel缓存任务到队列", 'TASK')


# ---------------------------------------------------------------------------
#  定时调度
# ---------------------------------------------------------------------------


# 「卡在 processing 多久就不再算它在跑」。
#
# 这一条**与租约同一个界**，因为问的是同一个问题：「这行还有人在管它吗」。原先这里是
# 3600 秒的字面量（页面那条判据 `is_stale_sync_task` 的 1800 秒的两倍：正常同步是
# 分钟级，实测大仓库 4.7 分钟，两倍余量能把「真的很慢」与「已经没人管了」分开；
# 误判成卡死会多起一个同步任务、与正在跑的那个抢着写同一份缓存，代价比多等半小时大）。
#
# 合并的理由：同一条 `processing` 行如果同时被两套判据看着，两套就会给出两个答案 ——
# 去重这边按「跑了 3600 秒」判死、租约那边按「租约还没到期」说它还活着，于是
# 去重会重建一条同步，而原执行者（租约续期还在替它刷着）**还在写同一份缓存**。
# 现在只有一个实现：`lease_is_expired`（有租约看租约、没租约退回年龄判据，
# 见 `task_worker_queue_service`）。租约在合法运行期间会被续，所以这一条对
# 「真的还在跑的长同步」也不会误判 —— 误判的代价前面说过，是两头都出事。
WEDGED_SYNC_PROCESSING_SECONDS = TASK_LEASE_SECONDS


def _is_wedged_processing_sync_task(task):
    """这条 `processing` 是不是已经没人管了（进程还活着，但没人会再写它的终态）。

    去重把 `processing` 一起算进来之后，多了一种**静默冻结**的可能：任务被 worker 拿走
    之后中途以非 `NON_CRITICAL_*` 的异常死掉（或写终态连续失败），那一行会一直停在
    `processing`；而 `load_pending_tasks()`（启动时把 processing 收回 pending 的那条路）
    只在进程启动时跑一次。此时去重会一直命中它 —— 同步永不重建、也不报错。
    所以超过阈值就按「没人管了」处理，置 failed 并照常重建。

    **判据只有一份**：这里只是把「还是不是 processing」与 `lease_is_expired` 串起来，
    判据本身的实现（租约 → `started_at` → `created_at`）在
    `task_worker_queue_service.lease_is_expired`。返回值的含义与合并前逐字一致：
    这条行还有人在管 → False，没人管了 → True。
    """
    if str(getattr(task, 'status', '') or '').lower() != 'processing':
        return False
    return lease_is_expired(
        task,
        now=datetime.now(timezone.utc).replace(tzinfo=None),
        timeout_seconds=WEDGED_SYNC_PROCESSING_SECONDS,
    )


def create_weekly_sync_task(config_id, auto_commit=True):
    """为周版本配置创建同步任务。

    ## 去重为什么要连 `processing` 一起看（这一条是**队列饿死**的根因）

    队列只有一个 worker，而调度器每 2 分钟就 tick 一次、每 tick 都为活跃配置建一条
    `weekly_sync`。**只看 `pending` 时**：正在跑的那条是 `processing`，于是每次 tick 都
    能再建一条新的排进队列 —— 而一条大仓库的同步要跑 4~5 分钟（820 个文件），tick 却
    每 2 分钟就来一次，队列里于是**永远有一条优先级 3 的同步在等**。worker 每次都先取
    优先级最小的那条，结果优先级 ≥5 的任务**永远轮不到**：

    * `auto_sync`（优先级 5）—— 实测自 06:08 起 12 小时一次都没跑过，也就是
      **仓库再也没被 fetch 过**，平台看到的提交停在那之前；
    * `weekly_excel_cache`（优先级 5）—— 27 条挂了 12 小时没跑，周版本 Excel 的
      HTML 缓存从来没被生成过，每次打开都实时重算；
    * `weekly_ai_analysis`（优先级 6）—— 库里的定时分析**一条都没真正跑过**
      （唯一那条被当成超时重置了）。手动分析走请求线程，不受影响，所以这个病一直
      没在界面上暴露出来。

    把 `processing` 一起纳入去重之后，一个配置同时只会有**一条**同步在排队或执行（原先
    可以是「一条在跑 + 一条在等」）。这压住的是队列**深度**，`p=5` 的任务因此排得更靠前 ——
    但**它自己不足以解除饿死**：一条带改动的同步要跑 4~5 分钟，而调度器每 2 分钟就补一条，
    两个配置就能让队列永不空（实测重启后 `auto_sync` 与 27 条 `weekly_excel_cache` 从
    06:10 一直挂到第二天；那一轮排空只是因为当周范围里暂时没有新提交，同步退化成秒级）。
    真正给低优先级任务留窗口的是 `_starvation_yield_note`：有人等太久时本轮不再补同步。
    """
    try:
        existing_task = _BackgroundTask.query.filter(
            _BackgroundTask.task_type == 'weekly_sync',
            _BackgroundTask.commit_id == str(config_id),
            _BackgroundTask.status.in_(['pending', 'processing']),
        ).order_by(_BackgroundTask.id.desc()).first()
        if existing_task is not None and _is_wedged_processing_sync_task(existing_task):
            wedged_id = existing_task.id
            existing_task.status = 'failed'
            existing_task.error_message = '任务长时间停留在处理中，已被下一次调度重置'
            if auto_commit:
                _db.session.commit()
            log_print(
                f"⚠️ 周版本同步任务 {wedged_id} 卡在处理中超过 "
                f"{WEDGED_SYNC_PROCESSING_SECONDS} 秒，已置 failed 并重建",
                'WEEKLY',
                force=True,
            )
            existing_task = None
        if existing_task:
            if _use_agent_dispatch():
                _ensure_agent_dispatch_for_background_task(
                    existing_task,
                    extra_payload={"config_id": config_id},
                )
                if auto_commit:
                    _db.session.commit()
            elif existing_task.status == 'pending' and not is_weekly_sync_task_enqueued(existing_task.id):
                # 单机模式下「库里是 pending」**不等于**「在内存队列里」：进程重启会清空
                # 内存队列、那行却还是 pending，于是每次调度都命中这条去重分支直接返回 ——
                # 任务永远不跑（线上「永久排队中」）。账本里没有它 = 队列已经丢了，补一次；
                # 有它 = 还在队列里，绝不能重入队（会跑两遍）。
                #
                # 只对 `pending` 补入队：`processing` 的那条 worker 正拿在手里，
                # 补一份就是同一个同步跑两遍。
                enqueue_weekly_sync_task(background_task_queue, TaskWrapper, existing_task.id, config_id)
            log_print(f"周版本配置 {config_id} 已存在待处理的同步任务", 'SYNC')
            return existing_task.id

        new_task = _BackgroundTask(
            task_type='weekly_sync',
            repository_id=None,
            commit_id=str(config_id),
            # 这一处**有意保留字面量**：`tests/test_business_flow_comprehensive.py`
            # 按源码文本断言了 `"priority=3" in create_weekly_sync_task 的函数体`，
            # 而那个文件不在本次施工的文件主权清单里。单一来源仍是
            # `services/task_worker_priority.WEEKLY_SYNC`（= 3），
            # `tests/test_task_lease_and_source.py` 里有一条漂移守卫钉着两者一致。
            priority=3,
            status='pending'
        )
        _db.session.add(new_task)
        _db.session.flush()
        _ensure_agent_dispatch_for_background_task(
            new_task,
            extra_payload={"config_id": config_id},
        )
        if auto_commit:
            _db.session.commit()
        if not _use_agent_dispatch():
            enqueue_weekly_sync_task(background_task_queue, TaskWrapper, new_task.id, config_id)
        log_print(f"创建周版本同步任务: config_id={config_id}, task_id={new_task.id}", 'SYNC')
        return new_task.id
    except SQLAlchemyError as e:
        if not auto_commit:
            raise
        _db.session.rollback()
        log_print(f"创建周版本同步任务数据库失败: {e}", 'ERROR', force=True)
        return None
    except (TypeError, ValueError, RuntimeError, AttributeError) as e:
        if not auto_commit:
            raise
        _db.session.rollback()
        log_print(f"创建周版本同步任务失败: {e}", 'ERROR', force=True)
        return None


# 「低优先级任务等了多久就算被饿死」。单 worker + 每 2 分钟补一条的 `weekly_sync`
# （优先级 3）构成的是一条**永不空**的队列：一条带改动的同步要跑 4~5 分钟，而调度器每
# 2 分钟就补一条，于是 worker 每次醒来都有优先级 3 可取 —— 优先级 ≥5 的任务
# （`auto_sync` / `weekly_excel_cache` / `weekly_ai_analysis`）**无限等待**：不报错、
# 不失败，只是永远「排队中」。实测过 19 小时（`auto_sync` 与 27 条 `weekly_excel_cache`
# 从 06:10 一直挂到第二天重启）。
#
# 这个阈值就是给它们留的窗口：只要有非同步任务等过了这么久，本轮就不再补新同步，让
# worker 把积压消化掉；消化完（没有任务再等过阈值）同步自然恢复。
#
# 为什么是**生产侧让步**而不是让队列按等待时长老化：老化要动 `TaskWrapper.__lt__` 或
# 换掉 `queue.PriorityQueue` 的取值方式，影响的就不只是这一处了；而这里要的只是
# 「别让高优先级的来源无限补给」，判据放在唯一那个来源上最清楚。
STARVED_TASK_WAIT_SECONDS = 900

# **不算「等太久」的那两类**：让路判据只该看 worker **真会去取**的任务。
#
# * `weekly_sync` —— 让路要挡的正是它这个高频来源（见上面那段注释）；算进来就变成
#   「因为有同步在排队，所以不再排同步」，队列一旦积压一条就永远不再补。
# * `weekly_ai_waiting` —— **等待意图根本不可执行**：`_enqueue_pending_row` 对它有专门
#   分支（worker 不取它、不执行它，`load_pending_tasks` 也不装载它），它等的是某一次
#   同步跑完，与队列积压毫无关系。而它 pending 的时长**就是**用户合法等待的时长
#   （判据是闸门「执行者还在不在」，大仓的同步是小时级 —— 见 `WAITING_INTENT_TTL_SECONDS`），
#   于是把它算进来 = 只要有一个人在等同步，**全局**就让路：那几小时里别的配置一条新
#   同步都补不到。这与「同步直接停摆」是同一个病，只是触发源从同步自己换成了意图行
#   （2026-09-26 CI 实测：一条 60 分钟前的意图让本文件那两条用例双双变红）。
_NOT_STARVABLE_TASK_TYPES = ('weekly_sync', WAITING_INTENT_TASK_TYPE)


def _starvation_yield_note(now_utc_naive, *, limit=3):
    """本轮该让路吗；该就让返回那句日志，不该返回 ""（见上面常量的注释）。

    **只看可执行任务**（`_NOT_STARVABLE_TASK_TYPES`）：`weekly_sync` 是那个高频来源，
    算进来就变成「因为有同步在排队，所以不再排同步」—— 队列一旦积压一条就永远不再补，
    同步直接停摆；等待意图行 worker 根本不取，算进来会让每一次「大仓同步期间用户点了
    一次分析」都变成一次几小时的全局让路。
    `created_at` 是 naive-UTC，与 `now_utc_naive` 同口径（混用会把年龄算错 8 小时）。
    """
    cutoff = now_utc_naive - timedelta(seconds=STARVED_TASK_WAIT_SECONDS)
    starved = _BackgroundTask.query.filter(
        _BackgroundTask.status == 'pending',
        _BackgroundTask.task_type.notin_(_NOT_STARVABLE_TASK_TYPES),
        _BackgroundTask.created_at <= cutoff,
    ).order_by(_BackgroundTask.created_at.asc()).limit(limit).all()
    if not starved:
        return ""
    oldest = starved[0]
    waited_minutes = int(
        (now_utc_naive - (oldest.created_at or now_utc_naive)).total_seconds() // 60
    )
    kinds = ", ".join(
        f"{getattr(task, 'task_type', '?')}#{getattr(task, 'id', '?')}" for task in starved
    )
    return (
        f"⏸️ 本轮不为周版本补新的同步任务（队列里有等待超过 "
        f"{STARVED_TASK_WAIT_SECONDS // 60} 分钟的任务，最久的等了 {waited_minutes} 分钟: {kinds}）"
    )


def _group_is_writing_cache(config, *, created_before=None):
    """本批（同一个 `group_key`）**此刻**正被 worker 写缓存吗。

    ## 为什么要有这条判据

    `create_weekly_sync_task` 的去重是按 **config** 的：同一批里的另一条配置照样会在
    同一个 tick 里被排上。实测大仓一轮同步 340~420 秒（421=339s、427=352s、431=418s），
    而 tick 原来每 2 分钟就来一次 —— 于是队列里**永远**有一条优先级 3 的同步在等：低
    优先级任务永远轮不到，而 AI 分析那道闸门判的正是「本批有没有同步在写缓存」，
    「同步在跑」因此接近于稳态，用户才会**反复**撞上「等待 Diff 同步完成」。

    ## 判据为什么只认 `processing`（而不是「有 pending/processing」）

    「有排队中的同步就不再排」是一条**已经踩过的坑**：队列一旦积压一条就再也不补，
    同步直接停摆（比饿死更难查 —— 连日志都是「正常让路」，见
    `test_a_starved_sync_task_does_not_stop_syncing`）。而 `processing` 是**worker 正拿在
    手里写**：这一刻再排一条同批的同步只会增加队列深度、不会让缓存更早写完 ——
    下一轮跑完（下一个 tick）再排，语义正好是「一轮跑完才有下一次机会」。

    ## `created_before`：本 tick 自己刚建的**不算**（2026-09-23 真机饿死形态）

    「一轮跑完才有下一次机会」要挡的是**上一批**还在写；本 tick 刚给组里第一条配置
    排上的任务，是这一批自己的成员。调度器给 config 1 建完任务到走到 config 2 之间，
    worker 完全来得及把那条认领成 `processing`（真机实测 ~3ms，2026-09-23 PID 45456：
    config 2 从进程启动起每个 tick 都被「本批正在写缓存」挡住、一次同步都没跑过，
    1141 行周版本缓存停在前一天；而前一天 worker 认领慢，同一 tick 三条任务 6ms 内
    全部建出 —— 同一份代码，竞速输赢决定饿不饿死）。传 `created_before`（tick 起点）
    就把这条竞速钉死了：本 tick 建的任务 `created_at >= tick_started`，被它挡住的
    只能是**它自己**。

    卡死的 `processing`（超过 `WEDGED_SYNC_PROCESSING_SECONDS` 没人再写它）**不算**：
    那种行会把这一批永久挡住，而它自己另有机制兜（`create_weekly_sync_task` 会把它
    置 failed 重建）。

    读不动（模型桩不认识分组键、库连不上）时返回 False —— **照旧排**：少一道节流，
    而不是让同步停摆。
    """
    try:
        config_ids = [str(item) for item in (group_config_ids(config) or [])]
        if not config_ids:
            return False
        query = _BackgroundTask.query.filter(
            _BackgroundTask.task_type == 'weekly_sync',
            _BackgroundTask.commit_id.in_(config_ids),
            _BackgroundTask.status == 'processing',
        )
        if created_before is not None:
            # 本 tick 自己刚建的那条不算：它就是这一批的一部分（理由见 docstring）。
            query = query.filter(_BackgroundTask.created_at < created_before)
        rows = query.all()
    except Exception:  # noqa: BLE001 —— 见 docstring：少一道节流，不让同步停摆
        return False
    for row in rows:
        if not _is_wedged_processing_sync_task(row):
            return True
    return False


def schedule_weekly_sync_tasks():
    """调度周版本同步任务"""
    try:
        with _app.app_context():
            active_configs = _WeeklyVersionConfig.query.filter_by(
                is_active=True, auto_sync=True
            ).all()
            # 本轮要不要**让路**（见 `_starvation_yield_note`）：一次 tick 算一次，多个配置
            # 共用同一个结论，日志也就只出一条。
            starvation_yield = None
            # 本 tick 的起点。组闸门（`_group_is_writing_cache`）只数**这之前**建的任务：
            # 本 tick 里刚排上的任务是这一批自己的成员，不许拿它挡同批的其他配置
            # （真机饿死形态见 `_group_is_writing_cache` 的 docstring）。
            tick_started_naive = datetime.now(timezone.utc).replace(tzinfo=None)
            for config in active_configs:
                # ⚠️ 这里有两个不同口径的「现在」，混用会出静默错误：
                #   * config.end_time 是**北京墙钟**（用户在 datetime-local 里填的）
                #     → 判断窗口是否结束要用北京墙钟；
                #   * BackgroundTask.created_at 是 **naive-UTC**（ORM 默认
                #     datetime.now(timezone.utc)，SQLite 丢 tzinfo）
                #     → 算任务年龄要用 naive-UTC。
                # 原实现两处都用 datetime.now()（**宿主机**本地时间）：在 UTC+8 开发机上
                # 「任务年龄」恒定多算 28800 秒，两个阈值（300s / WEEKLY_AI_TASK_STALE_SECONDS）
                # 必定被击穿 → 每 2 分钟把所有 pending 任务直接置 failed；
                # 在 UTC 容器上则是版本结束判定晚 8 小时。
                now_beijing_naive = now_beijing().replace(tzinfo=None)
                now_utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
                config_end = config.end_time.replace(tzinfo=None) if config.end_time.tzinfo else config.end_time
                # 重置卡死的 pending 是**清理动作，对查到的每个配置都要做**，与「这个配置
                # 是否活跃」无关。所以必须放在下面那条「窗口结束 → 置 completed →
                # continue」之前：原先重置整段圈在 `status == 'active'` 里，窗口一结束
                # 就再也没人重置它了（详见 reset_stale_weekly_sync_tasks 的注释）。
                reset_stale_weekly_sync_tasks(config, now_utc_naive, db=_db,
                                              background_task_model=_BackgroundTask, log_print=log_print)
                if now_beijing_naive > config_end and config.status == 'active':
                    config.status = 'completed'
                    _db.session.commit()
                    log_print(f"周版本配置已完成: {config.name}", 'WEEKLY')
                    continue
                if config.status == 'active':
                    if starvation_yield is None:
                        starvation_yield = _starvation_yield_note(now_utc_naive)
                        if starvation_yield:
                            log_print(starvation_yield, 'WEEKLY', force=True)
                    if _group_is_writing_cache(config, created_before=tick_started_naive):
                        # 「一轮跑完才有下一次机会」：这一批**此刻**正被写缓存，本轮不补新的。
                        # 判据按**批**而不是按 config —— 见 `_group_is_writing_cache`。
                        log_print(
                            f"⏳ 本批正在写缓存，本轮不补新的同步任务: config_id={config.id}",
                            'WEEKLY',
                        )
                        continue
                    if not starvation_yield:
                        create_weekly_sync_task(config.id)
            log_print(f"检查了 {len(active_configs)} 个周版本配置", 'WEEKLY')
    except SQLAlchemyError as e:
        _db.session.rollback()
        log_print(f"调度周版本同步任务数据库失败: {e}", 'WEEKLY', force=True)
    except (TypeError, ValueError, RuntimeError, AttributeError) as e:
        log_print(f"调度周版本同步任务失败: {e}", 'WEEKLY', force=True)


def _pending_weekly_analysis_task_exists(group_key):
    """这个分组已经有一条排队中/正在跑的周版本分析任务吗。

    **只用于日志记账**（「新建」与「复用」要分开），不参与任何控制流：去重仍由
    `create_weekly_ai_analysis_task` 内部那条查询裁决 —— 把它抄一份到这里就等于
    多了一个会漂移的第二事实源。读不动时按「没有」处理（记账少一分，不影响调度）。
    """
    try:
        return (
            _BackgroundTask.query.filter(
                _BackgroundTask.task_type == 'weekly_ai_analysis',
                _BackgroundTask.file_path == group_key,
                _BackgroundTask.status.in_(['pending', 'processing']),
            ).first()
            is not None
        )
    except SQLAlchemyError:
        return False


def _note_weekly_ai_skip(counts, reason_key):
    """记一次「这一组被跳过了」，按原因分桶（桶名见 `_WEEKLY_AI_SKIP_LABELS`）。"""
    counts["skipped"] += 1
    counts["skip_reasons"][reason_key] = counts["skip_reasons"].get(reason_key, 0) + 1


def _weekly_ai_schedule_summary(counts):
    """这一轮调度**实际发生了什么**的一句话。

    ## 为什么这三个数是真的（2026-09-21 修的那条「日志在说谎」）

    旧实现无条件打印 `调度了 {len(grouped)} 组周版本AI分析任务`，而 `len(grouped)` 是
    **扫到的分组数**、不是**建成几个任务**：循环体里至少有四条 `continue`（开关关闭、
    未到间隔、无变更、闸门拦下）都不会阻止它打印。实测 14:19→15:20 整 61 分钟打了 61 次
    「调度了 1 组」，而同期 `background_tasks` 里只建了 **1 条** `weekly_ai_analysis`
    —— 排查时据此把「调度器在空转」当成了事实。

    现在的口径：`checked` 是扫到的分组数、`created` 是**真的建了新行**的任务数、
    `reused` 是去重命中了已有任务（`create_weekly_ai_analysis_task` 返回的是既有 id）、
    `skipped` 按 `continue` 的原因分桶，四者之和 = `checked`。

    ## 为什么「没事也要打印」

    静默与说谎一样坏：把「无变化就不打印」当成省事，会让**「调度器死了」与「调度器空转」
    长得一模一样**。这一行必须每分钟都在，且如实说它这一分钟没做事。
    """
    reasons = counts["skip_reasons"]
    detail = " / ".join(
        f"{label} {reasons.get(key, 0)}" for key, label in _WEEKLY_AI_SKIP_LABELS
    )
    return (
        f"周版本AI分析调度：检查 {counts['checked']} 组，新建 {counts['created']} 个，"
        f"复用 {counts['reused']} 个，跳过 {counts['skipped']} 个（{detail}）"
    )


# 跳过原因 → 桶名。顺序就是日志里出现的顺序（**全部**要出现：少一个桶，
# 「为什么这一组没排上」就答不上来）。
_WEEKLY_AI_SKIP_LABELS = (
    ("auto_disabled", "开关关闭"),
    ("not_due", "未到间隔"),
    ("over_budget", "超预算"),
    ("no_change", "无变更"),
    ("same_snapshot", "输入相同"),
    ("sync_in_flight", "同步中"),
)


def schedule_weekly_ai_analysis_tasks():
    """按组调度周版本AI分析任务（默认每小时执行）。

    ## 收尾那一行的契约（**不许省**）

    无论这一轮做了什么、甚至什么都没做，函数结束前一定打印一行
    `周版本AI分析调度：检查 N 组，新建 N 个，复用 N 个，跳过 N 个（… 分桶 …）`
    （见 `_weekly_ai_schedule_summary`）。它挂在 `finally` 上，所以连
    「数据库报错」那条路径也会先如实报出这一轮扫到/跳过多少 —— 报错另行一行。
    """
    counts = {"checked": 0, "created": 0, "reused": 0, "skipped": 0, "skip_reasons": {}}
    try:
        with _app.app_context():
            # **兜底唤醒等待意图**：同步收尾那个钩子只在「本进程跑过那条同步」时才会响
            # （agent 派发模式下同步由别的节点执行；进程重启也会丢掉那一瞬间），所以
            # 每个分析调度周期再扫一遍 —— 没有意图时这条查询是空的，代价可以忽略。
            wake_waiting_analysis_intents_safely()
            active_configs = _WeeklyVersionConfig.query.filter_by(
                is_active=True, auto_sync=True, status='active'
            ).all()
            # **没有活跃配置也要走到收尾那一行**（`检查 0 组…`）：静默与说谎一样坏，
            # 见 `_weekly_ai_schedule_summary`。
            grouped = {}
            for cfg in active_configs:
                group_key = build_weekly_group_key(cfg)
                grouped.setdefault(group_key, []).append(cfg)

            for group_key, configs in grouped.items():
                counts["checked"] += 1
                project_id = configs[0].project_id
                project_cfg = get_project_analysis_config(project_id)
                if not project_cfg.get("auto_weekly_enabled", DEFAULT_AUTO_WEEKLY_ENABLED):
                    _note_weekly_ai_skip(counts, "auto_disabled")
                    continue
                # 兜底不写 `or 60` 这种字面量：与 `DEFAULT_WEEKLY_INTERVAL_MINUTES` 是同一件事，
                # 各写一份的结果是「改了默认间隔，只有配置行是 NULL 的项目跟着变」——
                # 而配置行**绝大多数**不是 NULL（`resolved()` 会兜），于是这个字面量长期是死的，
                # 直到有人把默认值改掉才发现它拦在中间。
                interval_minutes = int(
                    project_cfg.get("weekly_interval_minutes")
                    or DEFAULT_WEEKLY_INTERVAL_MINUTES
                )
                now_utc = datetime.now(timezone.utc)
                state = AiWeeklyAnalysisState.query.filter_by(group_key=group_key).first()
                if state and state.last_triggered_at:
                    last_triggered = state.last_triggered_at
                    if getattr(last_triggered, "tzinfo", None) is None:
                        last_triggered = last_triggered.replace(tzinfo=timezone.utc)
                    if (now_utc - last_triggered).total_seconds() < interval_minutes * 60:
                        _note_weekly_ai_skip(counts, "not_due")
                        continue

                # 预算闸门：超预算就不排队。放在间隔判定**之后**，所以这条日志最多
                # 每个分析间隔出现一条，不会每分钟刷屏。执行前
                # `run_weekly_analysis_background` 还会再查一次 —— 那是最后一道，
                # 覆盖「排好队之后才超预算」的情况。放在这里是为了不产生一个注定
                # 被跳过的后台任务（任务列表里会多出一堆 skipped 记录）。
                over_budget_reason = budget_gate_reason(project_id, entry="weekly_schedule")
                if over_budget_reason:
                    log_print(
                        f"⏸️ 周版本自动分析不排队（{over_budget_reason}）: group_key={group_key}",
                        "AI",
                        force=True,
                    )
                    # 推进触发水位线让这次判定也被间隔节流；预算回到额度内（例如跨月）
                    # 之后，下一个间隔自然会重新尝试。
                    if state is not None:
                        state.last_triggered_at = now_utc
                        _db.session.commit()
                    _note_weekly_ai_skip(counts, "over_budget")
                    continue

                # 用 naive-UTC 的「现在」与 created_at 同口径。
                # 原本用 datetime.now()（宿主机本地时间）：UTC+8 开发机上任务年龄恒多算
                # 28800 秒，会让**刚创建**的 pending 任务立刻被判定超时并置 failed。
                #
                # 判据与同步任务**同一把尺子**（先看内存账本、不在账本里才按年龄判超时），
                # 实现在 `task_worker_weekly_handlers.reset_stale_weekly_analysis_tasks`
                # 里 —— 原先这里只有年龄判据，于是「排在队列里没轮到」与「进程重启把它丢了」
                # 在 AI 这边长得一模一样，而同一条任务在同步那边算「只是没排到」。
                reset_stale_weekly_analysis_tasks(
                    group_key,
                    datetime.now(timezone.utc).replace(tzinfo=None),
                    db=_db,
                    background_task_model=_BackgroundTask,
                    log_print=log_print,
                )

                primary = select_primary_weekly_config(configs)
                config_ids = [cfg.id for cfg in configs]
                if not has_weekly_changes(config_ids, state.last_analyzed_at if state else None):
                    _note_weekly_ai_skip(counts, "no_change")
                    continue
                # **输入一字未变就别再分析一遍**（内容判据，与上面那条时间判据互补）。
                # 时间水位线只在跑完整了时推进（降级不推进是有意的：模型没真读到的变更
                # 不许被标成「已看过」），于是「降级跑完 → 水位线不动 → 下个周期又判有
                # 新变化」会一直转，每小时烧一次全量分析。指纹相同 = 同一份输入，
                # 再跑一遍只会得到同样的结果 —— 尤其上一轮正是「额度用尽、基于已有证据
                # 出结论」时。手动触发不受这条限制（用户的明确动作照跑）。
                if snapshot_already_analyzed(state, config_ids):
                    log_print(
                        f"⏭️ 周版本自动分析跳过（输入与上次分析逐字相同）: group_key={group_key}",
                        "AI",
                    )
                    # 与上面的预算闸门同样处理：**推进触发水位线**让这条判定也被间隔节流。
                    # 这个条件会持续存在（输入没变可能一整天），而本函数是
                    # `every(1).minutes` —— 不推进就是每分钟一条同样的日志 + 每分钟算一次
                    # 指纹。下一个间隔自然会重新判一次，那时输入变了就照常分析。
                    if state is not None:
                        state.last_triggered_at = now_utc
                        _db.session.commit()
                    _note_weekly_ai_skip(counts, "same_snapshot")
                    continue

                # **同步没写完就不要分析。** 变更清单来自周版本缓存行，而同步是逐文件
                # 写它们的 —— 跑到一半时的快照只有已写好的那批文件，清单会静默变小
                # （用户看到的是「代码改动均未取到 diff 正文」）。详见 weekly_sync_gate。
                # 这里 `continue` 且**不推进 last_triggered_at**：下一个周期自然重试。
                sync_note = weekly_sync_stuck_note(config_ids)
                if sync_note:
                    log_print(f"{sync_note} group_key={group_key}", "AI", force=True)
                sync_reason = weekly_sync_in_flight(config_ids)
                if sync_reason:
                    log_print(f"⏸️ 周版本自动分析推迟（{sync_reason}）: group_key={group_key}", "AI", force=True)
                    _note_weekly_ai_skip(counts, "sync_in_flight")
                    continue

                # 「复用」= 去重命中了已有任务（`create_weekly_ai_analysis_task` 返回的是
                # 既有那一行的 id）。**只看返回值分不出「新建」与「复用」**，所以先看一眼
                # 队列里有没有 —— 这一眼只用于记账，不参与控制流（去重仍由 create 内部裁决）。
                reused = _pending_weekly_analysis_task_exists(group_key)
                task_id = create_weekly_ai_analysis_task(primary.id, group_key=group_key)
                if task_id:
                    if reused:
                        counts["reused"] += 1
                    else:
                        counts["created"] += 1
                    if not state:
                        # 与 `_update_weekly_state` 共用同一个 get-or-create：
                        # 「先查后插」在这里同样会撞唯一约束 —— 首次手动分析正在进行中时
                        # 调度器又 tick 到同一分组（此时没有 state，间隔节流判据失效），
                        # 两条路各自建行，后提交的那个抛 IntegrityError，而它把这一 tick
                        # **剩下的分组全部放弃**（`except SQLAlchemyError` 在循环之外）。
                        state = get_or_create_weekly_state(
                            project_id=project_id,
                            group_key=group_key,
                            base_name=primary.name,
                            start_time=primary.start_time,
                            end_time=primary.end_time,
                        )
                    state.last_triggered_at = now_utc
                    _db.session.commit()
    except SQLAlchemyError as e:
        _db.session.rollback()
        log_print(f"调度周版本AI分析任务数据库失败: {e}", "AI", force=True)
    except (TypeError, ValueError, RuntimeError, AttributeError) as e:
        log_print(f"调度周版本AI分析任务失败: {e}", "AI", force=True)
    finally:
        # **收尾那一行不许省、也不许挂在循环之外无条件打印一句假的。**
        # 旧实现打印的是「调度了 {扫到的分组数} 组」，四条 `continue` 都拦不住它 ——
        # 实测 61 分钟打了 61 次「调度了 1 组」，而同期只建了 1 条任务，排查时被它带偏。
        # 详见 `_weekly_ai_schedule_summary`。放 `finally`：报错那条路径也要如实报出
        # 这一轮扫到/跳过多少（错误原因另有一行）。
        log_print(_weekly_ai_schedule_summary(counts), "AI")


def schedule_repository_sync_tasks():
    """定时同步所有已克隆仓库的新提交记录"""
    try:
        with _app.app_context():
            repositories = _Repository.query.filter_by(clone_status='completed').all()
            if not repositories:
                return
            synced_count = 0
            backed_off = 0
            for repository in repositories:
                try:
                    if sync_failure_backoff_active(repository):
                        backed_off += 1
                        continue
                    existing_task = _BackgroundTask.query.filter_by(
                        repository_id=repository.id,
                        task_type='auto_sync',
                        status='pending'
                    ).first()
                    if existing_task:
                        continue
                    task_id = create_auto_sync_task(repository.id)
                    if task_id:
                        synced_count += 1
                except (SQLAlchemyError, TypeError, ValueError, RuntimeError, AttributeError) as repo_err:
                    log_print(f"⚠️ 仓库 {repository.name} 自动同步调度失败: {repo_err}", 'SCHEDULER', force=True)
                    continue
            if synced_count > 0:
                log_print(f"📋 已调度 {synced_count} 个仓库自动同步任务", 'SCHEDULER')
            if backed_off > 0:
                # 只说一次「有几个仓库在退避窗口里」，不再逐个刷失败日志
                log_print(backoff_skip_message(backed_off), 'SCHEDULER')
    except SQLAlchemyError as e:
        _db.session.rollback()
        log_print(f"❌ 定时仓库同步调度数据库失败: {e}", 'SCHEDULER', force=True)
    except (TypeError, ValueError, RuntimeError, AttributeError) as e:
        log_print(f"❌ 定时仓库同步调度失败: {e}", 'SCHEDULER', force=True)


def setup_schedule(include_cleanup=True):
    """设置定时任务（由 app.py 调用）"""
    global _schedule_initialized
    if _schedule_initialized:
        return
    import schedule as sched_module
    sched_module.clear()
    if include_cleanup:
        sched_module.every().day.at("04:00").do(schedule_cleanup_task)
    # **周版本同步：15 分钟一轮，而不是每 2 分钟。**
    #
    # 一条 weekly_sync 要逐仓走，实测大仓一轮 340~420 秒（421=339s、425=342s、427=352s、
    # 431=418s、436=409s）；两仓一批合计约 7 分钟。原来每 2 分钟补一条，队列里于是
    # **永远**有一条优先级 3 的同步在等：低优先级任务（auto_sync=5 / weekly_excel_cache=5
    # / weekly_ai_analysis=6）永远轮不到，而 AI 分析那道闸门判的正是「有没有同步在写缓存」
    # —— 「同步在跑」接近稳态，用户点「重新分析」才会**反复**只看到「等待 Diff 同步完成」。
    #
    # 15 分钟 = 单轮最长实测值（418 秒）的两倍以上；再配合 `schedule_weekly_sync_tasks`
    # 里那条「本批已有同步在跑就本轮不补」的判据，节拍真正变成「上一轮跑完才排下一轮」：
    # 批次再大也只是把下一次机会推后到之后的某个 tick，队列深度不会累积。
    sched_module.every(15).minutes.do(schedule_weekly_sync_tasks)
    sched_module.every(1).minutes.do(schedule_weekly_ai_analysis_tasks)
    sched_module.every(2).minutes.do(schedule_repository_sync_tasks)
    # **租约到期的回收**：一条任务被 worker 取走后如果那个进程死了（或它自己卡住永远
    # 不写终态），租约到期就把它放回 pending 重投。判据与阈值见
    # `task_worker_queue_service` 的「平台侧租约」一节 —— 它是启动恢复（`load_pending_tasks`）
    # 那条租约判据的**运行期**对应物：只靠启动恢复的话，进程一直活着时没人收。
    # 5 分钟一轮：租约本身是 3600 秒，扫描频率只影响「死掉的任务多久被重新捡起来」。
    sched_module.every(5).minutes.do(reclaim_expired_task_leases_safely)
    _schedule_initialized = True


def run_scheduled_tasks():
    """运行定时任务检查器"""
    import schedule as sched_module
    while scheduler_running:
        try:
            with _app.app_context():
                sched_module.run_pending()
        except (SQLAlchemyError, RuntimeError, AttributeError) as schedule_error:
            log_print(f"定时任务执行异常: {schedule_error}", 'APP', force=True)
        time.sleep(60)


def start_scheduler(include_cleanup=True):
    """启动定时任务调度器"""
    global scheduler_running, scheduler_thread
    setup_schedule(include_cleanup=include_cleanup)
    if scheduler_running and scheduler_thread and scheduler_thread.is_alive():
        return
    scheduler_running = True
    scheduler_thread = threading.Thread(target=run_scheduled_tasks, daemon=True)
    scheduler_thread.start()
    log_print("定时任务调度器已启动", 'APP')


def stop_scheduler():
    """停止定时任务调度器。"""
    global scheduler_running, scheduler_thread
    if not scheduler_running:
        return
    scheduler_running = False
    if scheduler_thread and scheduler_thread.is_alive():
        try:
            scheduler_thread.join(timeout=2)
        except RuntimeError as exc:
            log_print(f"停止定时任务调度器失败: {exc}", "APP", force=True)


# ---------------------------------------------------------------------------
#  异步分支刷新
# ---------------------------------------------------------------------------
def queue_missing_git_branch_refresh(project_id, repository_ids):
    """Asynchronously refresh missing git branches to avoid blocking page rendering."""
    return queue_missing_git_branch_refresh_service(
        project_id=project_id,
        repository_ids=repository_ids,
        branch_refresh_lock=branch_refresh_lock,
        branch_refresh_cooldown_until=branch_refresh_cooldown_until,
        branch_refresh_cooldown_seconds=BRANCH_REFRESH_COOLDOWN_SECONDS,
        app=_app,
        db=_db,
        repository_model=_Repository,
        get_git_service=_get_git_service,
        log_print=log_print,
    )
