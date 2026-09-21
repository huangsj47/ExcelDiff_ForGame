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
from datetime import datetime, timezone

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
    check_and_create_auto_sync_tasks,  # noqa: F401 —— 外部调用方与测试仍按 worker.check_and_create_auto_sync_tasks 取
    create_auto_sync_task,
    create_weekly_ai_analysis_task,
    dispatch_auto_sync_task_when_agent_mode,  # noqa: F401 —— 外部调用方与测试仍按 worker.dispatch_auto_sync_task_when_agent_mode 取
    load_pending_tasks,
    regenerate_repository_cache,
    schedule_cleanup_task,
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
    enqueue_weekly_sync_task,
    forget_weekly_sync_task_of_payload,
    handle_weekly_excel_cache_task as handle_weekly_excel_cache_task_service,
    handle_weekly_sync_task as handle_weekly_sync_task_service,
    is_weekly_sync_task_enqueued,
    parse_config_id_from_commit_id,
    reset_stale_weekly_sync_tasks,
)
from services.weekly_version_files_api_helpers import is_stale_sync_task
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
from services.ai.analysis_budget import budget_gate_reason
from services.ai.scope_sampling import snapshot_already_analyzed
from services.ai.weekly_state import get_or_create_weekly_state
# 「周版本同步还在跑就先别分析」的闸门（同步逐文件写缓存，跑到一半的清单会静默变小）
from services.ai.weekly_sync_gate import weekly_sync_in_flight, weekly_sync_stuck_note
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
background_task_running = False
background_task_thread = None
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
WEEKLY_AI_TASK_STALE_SECONDS = max(600, int(os.environ.get("WEEKLY_AI_TASK_STALE_SECONDS", "7200") or 7200))

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
    def __init__(self, priority, counter, task_data):
        self.priority = priority
        self.counter = counter
        self.task_data = task_data

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
            elif status in TERMINAL_TASK_STATUSES:
                db_task.completed_at = datetime.now(timezone.utc)
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
def background_task_worker():
    """后台任务工作线程"""
    global background_task_running
    log_print("后台任务工作线程启动", 'APP')
    log_print(f"初始队列大小: {background_task_queue.qsize()}", 'APP')
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
            task_wrapper = background_task_queue.get(timeout=1)
            task_processed = True
            priority = task_wrapper.priority
            task = task_wrapper.task_data
            log_print(f"🔧 后台任务开始处理: {task['type']} (优先级: {priority}) | 队列剩余: {background_task_queue.qsize()}", 'EXCEL')

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
            log_print(f"✅ 后台任务完成: {task['type']} (优先级: {priority}) | 队列剩余: {background_task_queue.qsize()}", 'TASK')
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
                forget_weekly_sync_task_of_payload(getattr(task_wrapper, 'task_data', None))
                try:
                    background_task_queue.task_done()
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

                    # 确定同步起始日期
                    since_date = None
                    if repository.start_date:
                        since_date = repository.start_date
                        log_print(f"🔍 [BACKGROUND_SYNC] 应用仓库配置的起始日期限制: {since_date}", 'SYNC')
                    latest_commit = _Commit.query.filter_by(repository_id=repository.id)\
                        .order_by(_Commit.commit_time.desc()).first()
                    if latest_commit and latest_commit.commit_time:
                        if since_date is None or latest_commit.commit_time > since_date:
                            since_date = latest_commit.commit_time
                            log_print(f"🔍 [BACKGROUND_SYNC] 从最新提交时间开始增量同步: {since_date}", 'SYNC')

                    start_time = time.time()
                    commits = git_service.get_commits_threaded(since_date=since_date, limit=1000)
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
    return handle_weekly_sync_task_service(
        task=task,
        app=_app,
        update_task_status_with_retry=update_task_status_with_retry,
        process_weekly_version_sync=_process_weekly_version_sync,
        non_critical_task_status_errors=NON_CRITICAL_TASK_STATUS_ERRORS,
        non_critical_task_execution_errors=NON_CRITICAL_TASK_EXECUTION_ERRORS,
        log_print=log_print,
    )


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
    global background_task_running, background_task_thread
    if not background_task_running:
        background_task_running = True
        # **先清墓碑，再收活**：平台重启会留下 status='running' 的 AI 分析记录，
        # 那些 run 永远不会再被写完成（持有它们的进程已经没了）。它们会让
        # `/ai-analysis/.../latest` 报「没有结果」，界面据此又去自动开跑一次 ——
        # 用户看到的就是「重启后点一下 AI分析，它自己跑起来了」。
        # 必须在 `load_pending_tasks()`（会把上次残留的 processing 任务改回 pending
        # 重新入队）之前做，否则新一轮分析会与幽灵记录混在一起。
        fail_orphaned_analysis_runs()
        load_pending_tasks()
        background_task_thread = threading.Thread(target=background_task_worker, daemon=True)
        background_task_thread.start()
        # single 模式下需要本地执行任务，顺带启用清理任务调度。
        start_scheduler(include_cleanup=not _use_agent_dispatch())
        log_print("后台任务工作线程已启动", 'APP')


def stop_background_task_worker():
    """停止后台任务工作线程"""
    global background_task_running, background_task_thread
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
        stop_scheduler()


def add_excel_diff_task(repository_id, commit_id, file_path, priority=10, auto_commit=True):
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


def add_excel_diff_tasks_batch(repository_id, excel_commits, priority=10):
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


# 「卡在 processing 多久就不再算它在跑」。页面那条判据（`is_stale_sync_task`）用的是
# 1800 秒，这里取它的两倍：正常的同步是分钟级（实测大仓库 4.7 分钟），两倍余量能把
# 「真的很慢」与「已经没人管了」分开 —— 误判成卡死会多起一个同步任务和正在跑的那个
# 抢着写同一份缓存，代价比多等半小时大。
WEDGED_SYNC_PROCESSING_SECONDS = 3600


def _is_wedged_processing_sync_task(task):
    """这条 `processing` 是不是已经没人管了（进程还活着，但没人会再写它的终态）。

    去重把 `processing` 一起算进来之后，多了一种**静默冻结**的可能：任务被 worker 拿走
    之后中途以非 `NON_CRITICAL_*` 的异常死掉（或写终态连续失败），那一行会一直停在
    `processing`；而 `load_pending_tasks()`（启动时把 processing 收回 pending 的那条路）
    只在进程启动时跑一次。此时去重会一直命中它 —— 同步永不重建、也不报错。
    所以超过阈值就按「没人管了」处理，置 failed 并照常重建。

    阈值口径：`started_at`（worker 拿到任务时写）是 naive-UTC，与 `is_stale_sync_task`
    内部取时刻的方式一致；没有 `started_at` 时它会回落到 `created_at`。
    """
    if str(getattr(task, 'status', '') or '').lower() != 'processing':
        return False
    return is_stale_sync_task(
        task,
        datetime.now(timezone.utc).replace(tzinfo=None),
        processing_timeout_seconds=WEDGED_SYNC_PROCESSING_SECONDS,
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

    把 `processing` 一起纳入去重之后，同步就不会在自己跑的时候再给自己排一条：一条跑完
    到下一个 tick 之间队列里没有优先级 3，低优先级的任务自然轮得到。代价是同步不再
    「背靠背连跑」，间隔变成「上一轮跑完 + 最多一个 tick」，这类后台预热本来就该如此。
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


def schedule_weekly_sync_tasks():
    """调度周版本同步任务"""
    try:
        with _app.app_context():
            active_configs = _WeeklyVersionConfig.query.filter_by(
                is_active=True, auto_sync=True
            ).all()
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
                    create_weekly_sync_task(config.id)
            log_print(f"检查了 {len(active_configs)} 个周版本配置", 'WEEKLY')
    except SQLAlchemyError as e:
        _db.session.rollback()
        log_print(f"调度周版本同步任务数据库失败: {e}", 'WEEKLY', force=True)
    except (TypeError, ValueError, RuntimeError, AttributeError) as e:
        log_print(f"调度周版本同步任务失败: {e}", 'WEEKLY', force=True)


def schedule_weekly_ai_analysis_tasks():
    """按组调度周版本AI分析任务（默认每小时执行）。"""
    try:
        with _app.app_context():
            active_configs = _WeeklyVersionConfig.query.filter_by(
                is_active=True, auto_sync=True, status='active'
            ).all()
            if not active_configs:
                return
            grouped = {}
            for cfg in active_configs:
                group_key = build_weekly_group_key(cfg)
                grouped.setdefault(group_key, []).append(cfg)

            for group_key, configs in grouped.items():
                project_id = configs[0].project_id
                project_cfg = get_project_analysis_config(project_id)
                if not project_cfg.get("auto_weekly_enabled", True):
                    continue
                interval_minutes = int(project_cfg.get("weekly_interval_minutes") or 60)
                now_utc = datetime.now(timezone.utc)
                state = AiWeeklyAnalysisState.query.filter_by(group_key=group_key).first()
                if state and state.last_triggered_at:
                    last_triggered = state.last_triggered_at
                    if getattr(last_triggered, "tzinfo", None) is None:
                        last_triggered = last_triggered.replace(tzinfo=timezone.utc)
                    if (now_utc - last_triggered).total_seconds() < interval_minutes * 60:
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
                    continue

                stale_tasks = _BackgroundTask.query.filter(
                    _BackgroundTask.task_type == 'weekly_ai_analysis',
                    _BackgroundTask.file_path == group_key,
                    _BackgroundTask.status == 'pending',
                ).all()
                for stale in stale_tasks:
                    stale_created = stale.created_at.replace(tzinfo=None) if stale.created_at and stale.created_at.tzinfo else stale.created_at
                    # 用 naive-UTC 的「现在」与 created_at 同口径。
                    # 原本用 datetime.now()（宿主机本地时间）：UTC+8 开发机上任务年龄恒多算
                    # 28800 秒，会让**刚创建**的 pending 任务立刻被判定超时并置 failed。
                    _now_utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
                    if stale_created and (_now_utc_naive - stale_created).total_seconds() > WEEKLY_AI_TASK_STALE_SECONDS:
                        stale.status = 'failed'
                        stale.error_message = 'AI分析任务超时，已被调度器重置'
                        _db.session.commit()
                        log_print(f"重置卡死的周版本AI分析任务: task_id={stale.id}, group_key={group_key}", "AI", force=True)

                primary = select_primary_weekly_config(configs)
                config_ids = [cfg.id for cfg in configs]
                if not has_weekly_changes(config_ids, state.last_analyzed_at if state else None):
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
                    continue

                task_id = create_weekly_ai_analysis_task(primary.id, group_key=group_key)
                if task_id:
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
            log_print(f"调度了 {len(grouped)} 组周版本AI分析任务", "AI")
    except SQLAlchemyError as e:
        _db.session.rollback()
        log_print(f"调度周版本AI分析任务数据库失败: {e}", "AI", force=True)
    except (TypeError, ValueError, RuntimeError, AttributeError) as e:
        log_print(f"调度周版本AI分析任务失败: {e}", "AI", force=True)


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
    sched_module.every(2).minutes.do(schedule_weekly_sync_tasks)
    sched_module.every(1).minutes.do(schedule_weekly_ai_analysis_tasks)
    sched_module.every(2).minutes.do(schedule_repository_sync_tasks)
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
