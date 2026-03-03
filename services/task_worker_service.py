#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
后台任务工作服务 - 从 app.py 拆分
包含 TaskWrapper、后台任务队列管理、定时调度等
"""

import atexit
import queue
import signal
import sys
import threading
import time
import traceback
from datetime import datetime, timezone

from utils.logger import log_print
from utils.db_retry import db_retry

# ---------------------------------------------------------------------------
#  全局状态（由 app.py 通过 configure_task_worker 注入）
# ---------------------------------------------------------------------------
_app = None
_db = None
_excel_cache_service = None
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

# 同步并发控制：同时最多5个仓库更新
_sync_semaphore = threading.Semaphore(5)

# Git 进程集合（由 configure_task_worker 注入）
_active_git_processes = None

# 分支刷新锁与冷却
branch_refresh_lock = threading.Lock()
branch_refresh_cooldown_until = {}
import os
BRANCH_REFRESH_COOLDOWN_SECONDS = max(10, int(os.environ.get("BRANCH_REFRESH_COOLDOWN_SECONDS", "120") or 120))


def configure_task_worker(*, app, db, excel_cache_service,
                          BackgroundTask, Commit, Repository, DiffCache,
                          WeeklyVersionConfig,
                          active_git_processes,
                          get_git_service, get_svn_service,
                          get_unified_diff_data,
                          process_weekly_version_sync,
                          process_weekly_excel_cache,
                          db_retry):
    """注入 Flask 应用和数据库等依赖"""
    global _app, _db, _excel_cache_service
    global _BackgroundTask, _Commit, _Repository, _DiffCache
    global _WeeklyVersionConfig
    global _active_git_processes
    global _get_git_service, _get_svn_service, _get_unified_diff_data
    global _process_weekly_version_sync, _process_weekly_excel_cache
    global _db_retry
    _app = app
    _db = db
    _excel_cache_service = excel_cache_service
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
        except Exception as e:
            log_print(f"清理Git进程时出错: {e}", 'GIT', force=True)
            try:
                proc.kill()
                _active_git_processes.discard(proc)
            except:
                pass


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
            elif status in ['completed', 'failed']:
                db_task.completed_at = datetime.now(timezone.utc)
                if status == 'failed':
                    db_task.error_message = error_message
                    db_task.retry_count += 1
            _db.session.commit()
            log_print(f"✅ 任务状态更新成功: {task_id} -> {status}", 'TASK')
        else:
            log_print(f"⚠️ 未找到任务: {task_id}", 'TASK')
    except Exception as e:
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
                log_print(f"🧹 清理缓存: {task.get('days', 30)} 天前的数据", 'CACHE')
                _excel_cache_service.cleanup_old_cache(task.get('days', 30))
            elif task['type'] == 'regenerate_cache':
                log_print(f"🔄 重新生成缓存: 仓库 {task['repository_id']}", 'CACHE')
                task_count = regenerate_repository_cache(task['repository_id'])
                log_print(f"✅ 缓存重新生成完成，已添加 {task_count} 个任务到队列", 'CACHE')
            elif task['type'] == 'auto_sync':
                _handle_auto_sync_task(task)
            elif task['type'] == 'weekly_sync':
                _handle_weekly_sync_task(task)
            elif task['type'] == 'weekly_excel_cache':
                _handle_weekly_excel_cache_task(task)
            log_print(f"✅ 后台任务完成: {task['type']} (优先级: {priority}) | 队列剩余: {background_task_queue.qsize()}", 'TASK')
        except queue.Empty:
            continue
        except Exception as e:
            log_print(f"后台任务处理异常: {e}", 'APP', force=True)
            traceback.print_exc()
        finally:
            if task_processed:
                try:
                    background_task_queue.task_done()
                except ValueError:
                    pass
    log_print("后台任务工作线程停止", 'APP')


def _handle_excel_diff_task(task, priority):
    """处理Excel差异任务"""
    log_print(f"📊 处理Excel差异: repo={task['repository_id']}, commit={task['commit_id'][:8]}, file={task['file_path']}", 'EXCEL')
    with _app.app_context():
        if 'task_id' in task:
            try:
                update_task_status_with_retry(task['task_id'], 'processing')
            except Exception as update_error:
                log_print(f"更新任务开始状态失败: {update_error}", 'TASK', force=True)
        try:
            _excel_cache_service.process_excel_diff_background(
                task['repository_id'], task['commit_id'], task['file_path']
            )
            if 'task_id' in task:
                try:
                    update_task_status_with_retry(task['task_id'], 'completed')
                except Exception as update_error:
                    log_print(f"更新任务完成状态失败: {update_error}", 'TASK', force=True)
        except Exception as e:
            log_print(f"❌ Excel差异处理失败: {e}", 'EXCEL', force=True)
            try:
                _db.session.rollback()
            except Exception as rollback_error:
                log_print(f"会话回滚失败: {rollback_error}", 'DB', force=True)
            if 'task_id' in task:
                try:
                    update_task_status_with_retry(task['task_id'], 'failed', str(e))
                except Exception as update_error:
                    log_print(f"更新任务状态失败: {update_error}", 'TASK', force=True)


def _reset_repository_to_head(git_service, repository):
    """超时或失败后重置仓库到 HEAD 状态"""
    try:
        log_print(f"🔄 [RESET] 正在重置仓库 {repository.name} 到 HEAD 状态...", 'SYNC', force=True)
        reset_result = git_service._run_git_command(['git', 'reset', '--hard', 'HEAD'], timeout=60)
        if reset_result and reset_result.returncode == 0:
            log_print(f"✅ [RESET] git reset --hard HEAD 成功", 'SYNC')
        else:
            log_print(f"⚠️ [RESET] git reset --hard HEAD 失败", 'SYNC', force=True)
        clean_result = git_service._run_git_command(['git', 'clean', '-fd'], timeout=60)
        if clean_result and clean_result.returncode == 0:
            log_print(f"✅ [RESET] git clean -fd 成功", 'SYNC')
        else:
            log_print(f"⚠️ [RESET] git clean -fd 失败", 'SYNC', force=True)
    except Exception as reset_err:
        log_print(f"❌ [RESET] 重置仓库异常: {reset_err}", 'SYNC', force=True)


def _record_sync_error(repository, error_message):
    """将同步错误信息记录到仓库模型"""
    try:
        repository.last_sync_error = str(error_message)[:2000]  # 限制长度
        repository.last_sync_error_time = datetime.now(timezone.utc)
        _db.session.commit()
        log_print(f"📝 已记录仓库 {repository.name} 的同步错误", 'SYNC')
    except Exception as db_err:
        log_print(f"❌ 记录同步错误到数据库失败: {db_err}", 'SYNC', force=True)
        try:
            _db.session.rollback()
        except Exception:
            pass


def _clear_sync_error(repository):
    """同步成功后清除仓库的错误信息"""
    if repository.last_sync_error:
        try:
            repository.last_sync_error = None
            repository.last_sync_error_time = None
            _db.session.commit()
            log_print(f"✅ 已清除仓库 {repository.name} 的同步错误状态", 'SYNC')
        except Exception as db_err:
            log_print(f"⚠️ 清除同步错误状态失败: {db_err}", 'SYNC', force=True)
            try:
                _db.session.rollback()
            except Exception:
                pass


def _handle_auto_sync_task(task):
    """处理自动同步任务（含并发控制和超时处理）"""
    repo_id = task['repository_id']
    log_print(f"🔄 自动数据分析: 仓库 {repo_id}，等待并发许可...", 'SYNC')

    # 并发控制：最多同时5个仓库更新
    acquired = _sync_semaphore.acquire(timeout=120)
    if not acquired:
        log_print(f"⏰ 仓库 {repo_id} 等待并发许可超时(120s)，跳过本次同步", 'SYNC', force=True)
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
            except Exception as update_error:
                log_print(f"更新任务开始状态失败: {update_error}", 'TASK', force=True)
        try:
            repository = _db.session.get(_Repository, task['repository_id'])
            if repository:
                log_print(f"开始自动分析仓库: {repository.name}", 'SYNC')
                if repository.type == 'git':
                    from services.threaded_git_service import ThreadedGitService
                    git_service = ThreadedGitService(
                        repository.url, repository.root_directory,
                        repository.username, repository.token, repository
                    )
                    log_print(f"🚀 [BACKGROUND_SYNC] 开始后台同步仓库 ID: {repository.id}", 'SYNC')
                    log_print(f"🔧 [BACKGROUND_SYNC] 本地路径: {git_service.local_path}", 'SYNC')

                    # 使用5分钟超时的线程执行 clone_or_update
                    sync_result = [False, "未执行"]
                    sync_exception = [None]

                    def _do_clone_or_update():
                        try:
                            s, m = git_service.clone_or_update_repository()
                            sync_result[0] = s
                            sync_result[1] = m
                        except Exception as ex:
                            sync_exception[0] = ex

                    sync_thread = threading.Thread(target=_do_clone_or_update, daemon=True)
                    sync_thread.start()
                    sync_thread.join(timeout=300)  # 5分钟超时

                    if sync_thread.is_alive():
                        # 超时：记录错误并重置仓库
                        error_msg = f"Git pull 超时（超过5分钟），已中断并重置仓库"
                        log_print(f"⏰ [BACKGROUND_SYNC] {error_msg}: {repository.name}", 'SYNC', force=True)
                        _reset_repository_to_head(git_service, repository)
                        _record_sync_error(repository, error_msg)
                        if 'task_id' in task:
                            try:
                                update_task_status_with_retry(task['task_id'], 'failed', error_msg)
                            except Exception:
                                pass
                        return

                    if sync_exception[0]:
                        error_msg = f"clone_or_update 异常: {sync_exception[0]}"
                        log_print(f"❌ [BACKGROUND_SYNC] {error_msg}", 'SYNC', force=True)
                        _reset_repository_to_head(git_service, repository)
                        _record_sync_error(repository, error_msg)
                        if 'task_id' in task:
                            try:
                                update_task_status_with_retry(task['task_id'], 'failed', error_msg)
                            except Exception:
                                pass
                        return

                    success, message = sync_result
                    log_print(f"🔧 [BACKGROUND_SYNC] clone_or_update_repository 返回: success={success}, message={message}", 'SYNC')

                    if not success:
                        log_print(f"仓库克隆/更新失败: {message}", 'SYNC', force=True)
                        _reset_repository_to_head(git_service, repository)
                        _record_sync_error(repository, f"同步失败: {message}")
                        if 'task_id' in task:
                            try:
                                update_task_status_with_retry(task['task_id'], 'failed', message)
                            except Exception:
                                pass
                        return

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

                    # 批量查询已存在的commit
                    existing_commit_ids = set()
                    all_incoming_ids = list(set(cd['commit_id'] for cd in commits))
                    BATCH_SIZE = 500
                    for batch_start in range(0, len(all_incoming_ids), BATCH_SIZE):
                        batch_ids = all_incoming_ids[batch_start:batch_start + BATCH_SIZE]
                        existing_rows = _db.session.query(_Commit.commit_id).filter(
                            _Commit.repository_id == repository.id,
                            _Commit.commit_id.in_(batch_ids)
                        ).all()
                        existing_commit_ids.update(row[0] for row in existing_rows)
                    log_print(f"🔍 [BACKGROUND_SYNC] 批量查询完成: {len(existing_commit_ids)}/{len(all_incoming_ids)} 已存在", 'SYNC')

                    new_commit_objects = []
                    excel_task_list = []
                    for commit_data in commits:
                        if commit_data['commit_id'] in existing_commit_ids:
                            continue
                        existing_commit_ids.add(commit_data['commit_id'])
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
                        if file_path.lower().endswith(('.xlsx', '.xls')):
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
                        except Exception as e:
                            log_print(f"❌ [BACKGROUND_SYNC] 添加Excel缓存任务失败: {e}", 'SYNC', force=True)
                    if excel_tasks_added > 0:
                        log_print(f"📊 [BACKGROUND_SYNC] 批量添加 {excel_tasks_added} 个Excel缓存任务", 'SYNC')

                    _db.session.commit()
                    log_print(f"✅ [BACKGROUND_SYNC] 后台同步完成，添加了 {commits_added} 个新提交，{excel_tasks_added} 个Excel缓存任务", 'SYNC')
                    log_print(f"✅ 自动数据分析完成: {repository.name}, 添加了 {commits_added} 个提交记录，{excel_tasks_added} 个Excel缓存任务", 'SYNC')
                elif repository.type == 'svn':
                    svn_service = _get_svn_service(repository)
                    commits_added = svn_service.sync_repository_commits(_db, _Commit)
                    log_print(f"✅ 自动数据分析完成: {repository.name}, 添加了 {commits_added} 个提交记录", 'SYNC')
                else:
                    raise Exception(f"不支持的仓库类型: {repository.type}")
            else:
                raise Exception(f"仓库不存在: {task['repository_id']}")
            if 'task_id' in task:
                try:
                    update_task_status_with_retry(task['task_id'], 'completed')
                except Exception as update_error:
                    log_print(f"更新任务完成状态失败: {update_error}", 'TASK', force=True)
        except Exception as e:
            log_print(f"❌ 自动数据分析失败: {e}", 'SYNC', force=True)
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
    log_print(f"📅 周版本同步: 配置 {task['config_id']}", 'WEEKLY')
    with _app.app_context():
        if 'task_id' in task:
            try:
                update_task_status_with_retry(task['task_id'], 'processing')
            except Exception as update_error:
                log_print(f"更新任务开始状态失败: {update_error}", 'TASK', force=True)
        try:
            _process_weekly_version_sync(task['config_id'])
            if 'task_id' in task:
                try:
                    update_task_status_with_retry(task['task_id'], 'completed')
                except Exception as update_error:
                    log_print(f"更新任务完成状态失败: {update_error}", 'TASK', force=True)
        except Exception as e:
            log_print(f"❌ 周版本同步失败: {e}", 'WEEKLY', force=True)
            if 'task_id' in task:
                try:
                    update_task_status_with_retry(task['task_id'], 'failed', str(e))
                except Exception as update_error:
                    log_print(f"更新任务状态失败: {update_error}", 'TASK', force=True)


def _handle_weekly_excel_cache_task(task):
    """处理周版本Excel缓存任务"""
    log_print(f"📊 周版本Excel缓存: 配置 {task['data']['config_id']}, 文件 {task['data']['file_path']}", 'WEEKLY')
    with _app.app_context():
        if 'id' in task:
            try:
                update_task_status_with_retry(task['id'], 'processing')
            except Exception as update_error:
                log_print(f"更新任务开始状态失败: {update_error}", 'TASK', force=True)
        try:
            _process_weekly_excel_cache(task['data']['config_id'], task['data']['file_path'])
            if 'id' in task:
                try:
                    update_task_status_with_retry(task['id'], 'completed')
                except Exception as update_error:
                    log_print(f"更新任务完成状态失败: {update_error}", 'TASK', force=True)
        except Exception as e:
            log_print(f"❌ 周版本Excel缓存生成失败: {e}", 'WEEKLY', force=True)
            if 'id' in task:
                try:
                    update_task_status_with_retry(task['id'], 'failed', str(e))
                except Exception as update_error:
                    log_print(f"更新任务状态失败: {update_error}", 'TASK', force=True)


# ---------------------------------------------------------------------------
#  任务创建 / 队列管理
# ---------------------------------------------------------------------------
def create_auto_sync_task(repository_id):
    """为仓库创建自动数据分析任务"""
    try:
        existing_task = _BackgroundTask.query.filter_by(
            repository_id=repository_id,
            task_type='auto_sync',
            status='pending'
        ).first()
        if existing_task:
            log_print(f"仓库 {repository_id} 已存在待处理的自动同步任务", 'SYNC')
            return existing_task.id

        new_task = _BackgroundTask(
            task_type='auto_sync',
            repository_id=repository_id,
            priority=5,
            status='pending'
        )
        _db.session.add(new_task)
        _db.session.commit()
        task_data = {
            'type': 'auto_sync',
            'repository_id': repository_id,
            'task_id': new_task.id
        }
        task_counter = int(time.time() * 1000000)
        tw = TaskWrapper(5, task_counter, task_data)
        background_task_queue.put(tw)
        log_print(f"✅ 为仓库 {repository_id} 创建自动数据分析任务 (ID: {new_task.id})", 'SYNC')
        return new_task.id
    except Exception as e:
        log_print(f"❌ 创建自动同步任务失败: {e}", 'SYNC', force=True)
        return None


def check_and_create_auto_sync_tasks():
    """检查已克隆但未分析的仓库，自动创建数据分析任务"""
    try:
        repositories = _Repository.query.filter_by(clone_status='completed').all()
        created_tasks = 0
        for repo in repositories:
            commit_count = _Commit.query.filter_by(repository_id=repo.id).count()
            if commit_count == 0:
                log_print(f"🔍 发现已克隆但未分析的仓库: {repo.name} (ID: {repo.id})", 'SYNC')
                task_id = create_auto_sync_task(repo.id)
                if task_id:
                    created_tasks += 1
        if created_tasks > 0:
            log_print(f"✅ 为 {created_tasks} 个仓库创建了自动数据分析任务", 'SYNC')
        else:
            log_print("ℹ️ 没有发现需要自动分析的仓库", 'SYNC')
    except Exception as e:
        log_print(f"❌ 检查自动同步任务失败: {e}", 'SYNC', force=True)


def load_pending_tasks():
    """从数据库加载待处理的任务到内存队列"""
    try:
        pending_tasks = _BackgroundTask.query.filter_by(status='pending').order_by(
            _BackgroundTask.priority.asc(), _BackgroundTask.created_at.asc()
        ).all()
        for db_task in pending_tasks:
            if db_task.task_type == 'weekly_excel_cache':
                task_data = {
                    'id': db_task.id,
                    'type': 'weekly_excel_cache',
                    'data': {
                        'config_id': db_task.repository_id,
                        'file_path': db_task.file_path
                    }
                }
            else:
                task_data = {
                    'type': db_task.task_type,
                    'repository_id': db_task.repository_id,
                    'commit_id': db_task.commit_id,
                    'file_path': db_task.file_path,
                    'task_id': db_task.id
                }
            task_counter = int(time.time() * 1000000)
            priority = db_task.priority if db_task.priority is not None else 10
            tw = TaskWrapper(priority, task_counter, task_data)
            background_task_queue.put(tw)
        log_print(f"从数据库加载了 {len(pending_tasks)} 个待处理任务到队列", 'TASK')
        processing_tasks = _BackgroundTask.query.filter_by(status='processing').all()
        for task in processing_tasks:
            task.status = 'pending'
            task.started_at = None
        if processing_tasks:
            _db.session.commit()
            log_print(f"重置了 {len(processing_tasks)} 个处理中的任务状态为待处理", 'TASK')
        check_and_create_auto_sync_tasks()
    except Exception as e:
        log_print(f"加载待处理任务失败: {e}", 'TASK', force=True)


def start_background_task_worker():
    """启动后台任务工作线程"""
    global background_task_running, background_task_thread
    if not background_task_running:
        background_task_running = True
        load_pending_tasks()
        background_task_thread = threading.Thread(target=background_task_worker, daemon=True)
        background_task_thread.start()
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
            except Exception as e:
                log_print(f"停止后台任务线程时出现错误: {e}", 'APP', force=True)
        else:
            log_print("后台任务工作线程已停止", 'APP')


def add_excel_diff_task(repository_id, commit_id, file_path, priority=10, auto_commit=True):
    """添加Excel差异处理任务到优先级队列"""
    existing_task = _BackgroundTask.query.filter_by(
        task_type='excel_diff',
        repository_id=repository_id,
        commit_id=commit_id,
        file_path=file_path,
        status='pending'
    ).first()
    if existing_task:
        if priority < existing_task.priority:
            existing_task.priority = priority
            if auto_commit:
                _db.session.commit()
            log_print(f"更新任务优先级: {file_path} (优先级: {priority})", 'TASK')
        return existing_task.id if existing_task else None

    task = _BackgroundTask(
        task_type='excel_diff',
        repository_id=repository_id,
        commit_id=commit_id,
        file_path=file_path,
        priority=priority
    )
    _db.session.add(task)
    if auto_commit:
        _db.session.commit()
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
    priority_text = "高优先级" if priority < 5 else "普通优先级"
    log_print(f"添加Excel差异任务到队列 ({priority_text}): {file_path}", 'EXCEL')


def add_excel_diff_tasks_batch(repository_id, excel_commits, priority=10):
    """批量添加Excel差异处理任务到优先级队列"""
    if not excel_commits:
        return

    existing_tasks = set()
    existing_query = _BackgroundTask.query.filter_by(
        task_type='excel_diff',
        repository_id=repository_id,
        status='pending'
    ).all()
    for task in existing_query:
        existing_tasks.add((task.commit_id, task.file_path))
    new_tasks = []
    base_counter = int(time.time() * 1000000)
    for i, commit_data in enumerate(excel_commits):
        commit_id = commit_data['commit_id']
        file_path = commit_data['path']
        if (commit_id, file_path) in existing_tasks:
            continue
        new_tasks.append({
            'task_type': 'excel_diff',
            'repository_id': repository_id,
            'commit_id': commit_id,
            'file_path': file_path,
            'priority': priority
        })
    if new_tasks:
        _db.session.bulk_insert_mappings(_BackgroundTask, new_tasks)
        _db.session.commit()
        inserted_tasks = _BackgroundTask.query.filter_by(
            task_type='excel_diff',
            repository_id=repository_id,
            status='pending'
        ).filter(_BackgroundTask.id > (_db.session.query(_db.func.max(_BackgroundTask.id)).scalar() or 0) - len(new_tasks)).all()
        for i, task in enumerate(inserted_tasks):
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
        log_print(f"批量添加了 {len(new_tasks)} 个Excel缓存任务到队列", 'TASK')


def regenerate_repository_cache(repository_id):
    """重新生成仓库的Excel文件缓存"""
    try:
        log_print(f"开始重新生成仓库缓存: {repository_id}", 'CACHE')
        repository = _db.session.get(_Repository, repository_id)
        if not repository:
            log_print(f"仓库不存在: {repository_id}", 'CACHE', force=True)
            return 0
        log_print(f"清理仓库 {repository_id} 的现有队列任务", 'CACHE')
        pending_tasks_deleted = _BackgroundTask.query.filter(
            _BackgroundTask.repository_id == repository_id,
            _BackgroundTask.status.in_(['pending', 'processing'])
        ).delete(synchronize_session=False)
        log_print(f"删除了 {pending_tasks_deleted} 个现有队列任务", 'CACHE')
        log_print(f"清理仓库 {repository_id} 的现有缓存数据", 'CACHE')
        cache_deleted = _DiffCache.query.filter_by(repository_id=repository_id).delete()
        log_print(f"删除了 {cache_deleted} 个缓存记录", 'CACHE')
        _db.session.commit()
        recent_commits = _excel_cache_service.get_recent_excel_commits(repository, limit=1000)
        log_print(f"找到 {len(recent_commits)} 个最近的Excel文件提交", 'CACHE')
        for commit in recent_commits:
            add_excel_diff_task(repository_id, commit.commit_id, commit.path)
        log_print(f"已添加 {len(recent_commits)} 个缓存重建任务", 'CACHE')
        return len(recent_commits)
    except Exception as e:
        log_print(f"重新生成仓库缓存失败: {e}", 'CACHE', force=True)
        traceback.print_exc()


# ---------------------------------------------------------------------------
#  定时调度
# ---------------------------------------------------------------------------
def schedule_cleanup_task():
    """调度清理任务"""
    task = {
        'type': 'cleanup_cache',
        'days': 30,
        'task_id': None
    }
    task_counter = int(time.time() * 1000000)
    tw = TaskWrapper(20, task_counter, task)
    background_task_queue.put(tw)
    log_print("添加缓存清理任务到队列", 'TASK')


def create_weekly_sync_task(config_id):
    """为周版本配置创建同步任务"""
    try:
        existing_task = _BackgroundTask.query.filter_by(
            task_type='weekly_sync',
            commit_id=str(config_id),
            status='pending'
        ).first()
        if existing_task:
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
        _db.session.commit()
        task_data = {
            'type': 'weekly_sync',
            'config_id': config_id,
            'task_id': new_task.id
        }
        task_counter = int(time.time() * 1000000)
        tw = TaskWrapper(3, task_counter, task_data)
        background_task_queue.put(tw)
        log_print(f"创建周版本同步任务: config_id={config_id}, task_id={new_task.id}", 'SYNC')
        return new_task.id
    except Exception as e:
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
                now_local = datetime.now()
                config_end = config.end_time.replace(tzinfo=None) if config.end_time.tzinfo else config.end_time
                if now_local > config_end and config.status == 'active':
                    config.status = 'completed'
                    _db.session.commit()
                    log_print(f"周版本配置已完成: {config.name}", 'WEEKLY')
                    continue
                if config.status == 'active':
                    stale_tasks = _BackgroundTask.query.filter_by(
                        task_type='weekly_sync',
                        commit_id=str(config.id),
                        status='pending'
                    ).all()
                    for stale in stale_tasks:
                        stale_created = stale.created_at.replace(tzinfo=None) if stale.created_at and stale.created_at.tzinfo else stale.created_at
                        if stale_created and (datetime.now() - stale_created).total_seconds() > 300:
                            stale.status = 'failed'
                            stale.error_message = '任务超时，已被调度器重置'
                            _db.session.commit()
                            log_print(f"重置卡死的周版本同步任务: task_id={stale.id}, config_id={config.id}", 'WEEKLY', force=True)
                    create_weekly_sync_task(config.id)
            log_print(f"检查了 {len(active_configs)} 个周版本配置", 'WEEKLY')
    except Exception as e:
        log_print(f"调度周版本同步任务失败: {e}", 'WEEKLY', force=True)


def schedule_repository_sync_tasks():
    """定时同步所有已克隆仓库的新提交记录"""
    try:
        with _app.app_context():
            repositories = _Repository.query.filter_by(clone_status='completed').all()
            if not repositories:
                return
            synced_count = 0
            for repository in repositories:
                try:
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
                except Exception as repo_err:
                    log_print(f"⚠️ 仓库 {repository.name} 自动同步调度失败: {repo_err}", 'SCHEDULER', force=True)
                    continue
            if synced_count > 0:
                log_print(f"📋 已调度 {synced_count} 个仓库自动同步任务", 'SCHEDULER')
    except Exception as e:
        log_print(f"❌ 定时仓库同步调度失败: {e}", 'SCHEDULER', force=True)


def setup_schedule():
    """设置定时任务（由 app.py 调用）"""
    import schedule as sched_module
    sched_module.every().day.at("04:00").do(schedule_cleanup_task)
    sched_module.every(2).minutes.do(schedule_weekly_sync_tasks)
    sched_module.every(2).minutes.do(schedule_repository_sync_tasks)


def run_scheduled_tasks():
    """运行定时任务检查器"""
    import schedule as sched_module
    while background_task_running:
        try:
            with _app.app_context():
                sched_module.run_pending()
        except Exception as schedule_error:
            log_print(f"定时任务执行异常: {schedule_error}", 'APP', force=True)
        time.sleep(60)


def start_scheduler():
    """启动定时任务调度器"""
    scheduler_thread = threading.Thread(target=run_scheduled_tasks, daemon=True)
    scheduler_thread.start()
    log_print("定时任务调度器已启动", 'APP')


# ---------------------------------------------------------------------------
#  异步分支刷新
# ---------------------------------------------------------------------------
def queue_missing_git_branch_refresh(project_id, repository_ids):
    """Asynchronously refresh missing git branches to avoid blocking page rendering."""
    unique_repo_ids = sorted({int(repo_id) for repo_id in (repository_ids or []) if repo_id})
    if not unique_repo_ids:
        return False

    now_ts = time.time()
    with branch_refresh_lock:
        cooldown_until = branch_refresh_cooldown_until.get(project_id, 0.0)
        if cooldown_until > now_ts:
            return False
        branch_refresh_cooldown_until[project_id] = now_ts + BRANCH_REFRESH_COOLDOWN_SECONDS

    def refresh_worker(target_project_id, target_repo_ids):
        updated_count = 0
        try:
            with _app.app_context():
                repositories = _Repository.query.filter(
                    _Repository.project_id == target_project_id,
                    _Repository.type == 'git',
                    _Repository.id.in_(target_repo_ids),
                    (_Repository.branch.is_(None)) | (_Repository.branch == '')
                ).all()
                if not repositories:
                    return
                for repo in repositories:
                    try:
                        git_service = _get_git_service(repo)
                        branches = git_service.get_branches()
                        if branches:
                            repo.branch = branches[0]
                            updated_count += 1
                    except Exception as branch_error:
                        log_print(f"异步刷新仓库分支失败: repo_id={repo.id}, error={branch_error}", 'APP')
                if updated_count > 0:
                    _db.session.commit()
                    log_print(f"异步刷新仓库分支完成: project_id={target_project_id}, updated={updated_count}", 'APP')
                else:
                    _db.session.rollback()
        except Exception as worker_error:
            try:
                _db.session.rollback()
            except Exception:
                pass
            log_print(f"异步刷新仓库分支异常: project_id={target_project_id}, error={worker_error}", 'APP', force=True)

    refresh_thread = threading.Thread(
        target=refresh_worker,
        args=(project_id, unique_repo_ids),
        daemon=True,
        name=f"branch-refresh-{project_id}",
    )
    refresh_thread.start()
    return True
