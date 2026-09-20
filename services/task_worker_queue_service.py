"""任务行的创建、装载与队列维护（worker 的「生产端」）。

## 为什么单独一个文件

`task_worker_service.py` 里有两件事容易混：**任务怎么进队列**（本文件）和
**任务怎么被执行**（worker 主循环 + `task_worker_task_handlers.py`）。这里收的是前者：
建 `BackgroundTask` 行、按 `status='pending'` 去重、进程启动时把库里残留的任务装回
内存队列、以及配表缓存的重建与清理。搬出来之后，「为什么这次同步没排上队」这类问题
只需要看这一个文件。

搬出来的直接原因仍然是 `scripts/check_file_length.py --strict`：`task_worker_service.py`
贴着 2000 行的 ERROR 门槛。

## 与 `task_worker_service.py` 的耦合方式

见 `task_worker_task_handlers.py` 抬头：模块级槽位一律 `worker.X` 现取，不 `from ... import`。
本文件里尤其关键的是 `create_auto_sync_task` / `check_and_create_auto_sync_tasks` ——
它们既被 `services/repository_*_handlers.py` 用 `get_runtime_model()` 按**模块属性**
取用，又被测试 monkeypatch 掉，所以本文件内部互相调用时也走 `worker.`，
保证「补丁打在哪，调用就从哪走」。
"""

from __future__ import annotations

import time
import traceback

import services.task_worker_service as worker


def create_auto_sync_task(repository_id, extra_payload=None):
    """为仓库创建自动数据分析任务"""
    try:
        payload = dict(extra_payload or {})
        force_retry = bool(payload.get("force_reclone") or payload.get("force_repair_update"))
        existing_task = worker._BackgroundTask.query.filter_by(
            repository_id=repository_id,
            task_type='auto_sync',
            status='pending'
        ).first()
        if existing_task and not force_retry:
            worker.log_print(f"仓库 {repository_id} 已存在待处理的自动同步任务", 'SYNC')
            return existing_task.id
        if existing_task and force_retry:
            worker.log_print(
                f"仓库 {repository_id} 存在待处理 auto_sync，手动重试将创建新任务并附加重试策略",
                'SYNC',
            )

        new_task = worker._BackgroundTask(
            task_type='auto_sync',
            repository_id=repository_id,
            priority=2 if force_retry else 5,
            status='pending'
        )
        worker._db.session.add(new_task)
        worker._db.session.flush()
        enqueue_payload = {"repository_id": repository_id}
        enqueue_payload.update(payload)
        worker._enqueue_agent_task_from_background_task(
            new_task,
            extra_payload=enqueue_payload,
        )
        worker._db.session.commit()
        if not worker._use_agent_dispatch():
            task_data = {
                'type': 'auto_sync',
                'repository_id': repository_id,
                'task_id': new_task.id
            }
            task_data.update(payload)
            task_counter = int(time.time() * 1000000)
            tw = worker.TaskWrapper(5, task_counter, task_data)
            worker.background_task_queue.put(tw)
        worker.log_print(f"✅ 为仓库 {repository_id} 创建自动数据分析任务 (ID: {new_task.id})", 'SYNC')
        return new_task.id
    except worker.SQLAlchemyError as e:
        worker._db.session.rollback()
        worker.log_print(f"❌ 创建自动同步任务数据库失败: {e}", 'SYNC', force=True)
        return None
    except (TypeError, ValueError, RuntimeError, AttributeError) as e:
        worker.log_print(f"❌ 创建自动同步任务失败: {e}", 'SYNC', force=True)
        return None


def dispatch_auto_sync_task_when_agent_mode(repository_id, extra_payload=None):
    """Agent 模式下派发 auto_sync；single 模式直接返回未处理。"""
    if not worker._use_agent_dispatch():
        return False, None
    task_id = worker.create_auto_sync_task(repository_id, extra_payload=extra_payload)
    return True, task_id


def check_and_create_auto_sync_tasks():
    """检查已克隆但未分析的仓库，自动创建数据分析任务"""
    try:
        repositories = worker._Repository.query.filter_by(clone_status='completed').all()
        created_tasks = 0
        for repo in repositories:
            commit_count = worker._Commit.query.filter_by(repository_id=repo.id).count()
            if commit_count == 0:
                worker.log_print(f"🔍 发现已克隆但未分析的仓库: {repo.name} (ID: {repo.id})", 'SYNC')
                task_id = worker.create_auto_sync_task(repo.id)
                if task_id:
                    created_tasks += 1
        if created_tasks > 0:
            worker.log_print(f"✅ 为 {created_tasks} 个仓库创建了自动数据分析任务", 'SYNC')
        else:
            worker.log_print("ℹ️ 没有发现需要自动分析的仓库", 'SYNC')
    except worker.SQLAlchemyError as e:
        worker._db.session.rollback()
        worker.log_print(f"❌ 检查自动同步任务数据库失败: {e}", 'SYNC', force=True)
    except (TypeError, ValueError, RuntimeError, AttributeError) as e:
        worker.log_print(f"❌ 检查自动同步任务失败: {e}", 'SYNC', force=True)


def load_pending_tasks():
    """从数据库加载待处理的任务到内存队列"""
    try:
        # **先收回上次残留的 processing，再查 pending。** 顺序曾经是反的：收回只改库、
        # 不再入队，而单机模式下内存队列是唯一执行路径（worker 只从队列取任务），
        # 于是崩溃/重启时被中断的任务在本轮进程里永远不会被执行；期间它还是 pending，
        # 会占着业务键堵住同键任务的重建（add_excel_diff_task 等按 pending/processing
        # 去重）。收回必须发生在下面那条 pending 查询之前，这些行才会在同一次调用里
        # 被捞到并真正入队。
        processing_tasks = worker._BackgroundTask.query.filter_by(status='processing').all()
        for task in processing_tasks:
            task.status = 'pending'
            task.started_at = None
        if processing_tasks:
            worker._db.session.commit()
            worker.log_print(f"重置了 {len(processing_tasks)} 个处理中的任务状态为待处理", 'TASK')
        pending_tasks = worker._BackgroundTask.query.filter_by(status='pending').order_by(
            worker._BackgroundTask.priority.asc(), worker._BackgroundTask.created_at.asc()
        ).all()
        for db_task in pending_tasks:
            if db_task.task_type == 'weekly_sync':
                # 载荷必须带 config_id（周版本任务把它存在 commit_id 列里，见
                # create_weekly_sync_task）。原先没有这条分支 → 落进下面的通用分支、
                # 载荷里没有 config_id → 处理器第一句 KeyError，被 worker 循环的
                # NON_CRITICAL_WORKER_LOOP_ERRORS 吞掉，写终态那句在 try 里执行不到
                # → 库里那行永久 pending（线上「永久排队中」的成因）。
                # 走 enqueue_weekly_sync_task 而不是下面那个 put：它同时登记「已入队」账本。
                worker.enqueue_weekly_sync_task(worker.background_task_queue, worker.TaskWrapper, db_task.id,
                                         worker.parse_config_id_from_commit_id(db_task.commit_id))
                continue
            if db_task.task_type == 'weekly_excel_cache':
                task_data = {
                    'id': db_task.id,
                    'type': 'weekly_excel_cache',
                    'data': {
                        'config_id': db_task.repository_id,
                        'file_path': db_task.file_path
                    }
                }
            elif db_task.task_type == 'weekly_ai_analysis':
                config_id = worker.parse_config_id_from_commit_id(db_task.commit_id)
                task_data = {
                    'type': 'weekly_ai_analysis',
                    'config_id': config_id,
                    'group_key': db_task.file_path,
                    'commit_id': db_task.commit_id,
                    'task_id': db_task.id
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
            tw = worker.TaskWrapper(priority, task_counter, task_data)
            worker.background_task_queue.put(tw)
        worker.log_print(f"从数据库加载了 {len(pending_tasks)} 个待处理任务到队列", 'TASK')
        worker.check_and_create_auto_sync_tasks()
    except worker.SQLAlchemyError as e:
        worker._db.session.rollback()
        worker.log_print(f"加载待处理任务数据库失败: {e}", 'TASK', force=True)
    except (TypeError, ValueError, RuntimeError, AttributeError) as e:
        worker.log_print(f"加载待处理任务失败: {e}", 'TASK', force=True)


def regenerate_repository_cache(repository_id):
    """重新生成仓库的Excel文件缓存"""
    try:
        worker.log_print(f"开始重新生成仓库缓存: {repository_id}", 'CACHE')
        repository = worker._db.session.get(worker._Repository, repository_id)
        if not repository:
            worker.log_print(f"仓库不存在: {repository_id}", 'CACHE', force=True)
            # 返回 None（失败）而不是 0：调用方原先把 0 渲染成
            # 「✅ 已添加 0 个任务到队列」，仓库已删这种情况看起来像「正常，只是没东西」。
            return None
        worker.log_print(f"清理仓库 {repository_id} 的现有队列任务", 'CACHE')
        pending_tasks_deleted = worker._BackgroundTask.query.filter(
            worker._BackgroundTask.repository_id == repository_id,
            worker._BackgroundTask.status.in_(['pending', 'processing'])
        ).delete(synchronize_session=False)
        worker.log_print(f"删除了 {pending_tasks_deleted} 个现有队列任务", 'CACHE')
        worker.log_print(f"清理仓库 {repository_id} 的现有缓存数据", 'CACHE')
        cache_deleted = worker._DiffCache.query.filter_by(repository_id=repository_id).delete()
        worker.log_print(f"删除了 {cache_deleted} 个缓存记录", 'CACHE')
        worker._db.session.commit()
        recent_commits = worker._excel_cache_service.get_recent_excel_commits(repository, limit=1000)
        worker.log_print(f"找到 {len(recent_commits)} 个最近的Excel文件提交", 'CACHE')
        for commit in recent_commits:
            worker.add_excel_diff_task(repository_id, commit.commit_id, commit.path)
        worker.log_print(f"已添加 {len(recent_commits)} 个缓存重建任务", 'CACHE')
        return len(recent_commits)
    except (worker.SQLAlchemyError, RuntimeError, AttributeError, TypeError, ValueError) as e:
        # 必须 rollback：本函数在 commit 之前有两次 DELETE，失败时不清会话会留下
        # 脏事务（后续同一 session 上的写入会抛 PendingRollbackError），
        # 违反 .claude/rules/rules.mdc 第 8.5 条。
        try:
            worker._db.session.rollback()
        except worker.SQLAlchemyError as rollback_error:
            worker.log_print(f"重新生成仓库缓存失败后回滚也失败: {rollback_error}", 'CACHE', force=True)
        worker.log_print(f"重新生成仓库缓存失败: {e}", 'CACHE', force=True)
        traceback.print_exc()
        # 返回 None 而不是「掉出函数末尾」：调用方（worker 的 regenerate_cache 分支）
        # 原本据此打印「✅ ... 已添加 None 个任务」，把失败伪装成成功。
        return None


def schedule_cleanup_task():
    """调度清理任务"""
    task = {
        'type': 'cleanup_cache',
        'days': 30,
        'task_id': None
    }
    task_counter = int(time.time() * 1000000)
    tw = worker.TaskWrapper(20, task_counter, task)
    worker.background_task_queue.put(tw)
    worker.log_print("添加缓存清理任务到队列", 'TASK')


def create_weekly_ai_analysis_task(config_id, group_key=None):
    """为周版本配置创建AI分析任务（按组去重）。"""
    try:
        config = worker._db.session.get(worker._WeeklyVersionConfig, config_id)
        if not config:
            worker.log_print(f"周版本配置不存在，无法创建AI分析任务: {config_id}", "AI", force=True)
            return None

        group_key = group_key or worker.build_weekly_group_key(config)
        existing_task = worker._BackgroundTask.query.filter(
            worker._BackgroundTask.task_type == 'weekly_ai_analysis',
            worker._BackgroundTask.file_path == group_key,
            worker._BackgroundTask.status.in_(['pending', 'processing']),
        ).first()
        if existing_task:
            if worker._use_agent_dispatch():
                worker._ensure_agent_dispatch_for_background_task(
                    existing_task,
                    extra_payload={"config_id": config_id, "group_key": group_key},
                )
                worker._db.session.commit()
            worker.log_print(f"周版本AI分析任务已存在: group_key={group_key}", "AI")
            return existing_task.id

        new_task = worker._BackgroundTask(
            task_type='weekly_ai_analysis',
            repository_id=None,
            commit_id=str(config_id),
            file_path=group_key,
            priority=6,
            status='pending',
        )
        worker._db.session.add(new_task)
        worker._db.session.flush()
        worker._ensure_agent_dispatch_for_background_task(
            new_task,
            extra_payload={"config_id": config_id, "group_key": group_key},
        )
        worker._db.session.commit()
        if not worker._use_agent_dispatch():
            task_data = {
                'type': 'weekly_ai_analysis',
                'config_id': config_id,
                'group_key': group_key,
                'commit_id': str(config_id),
                'task_id': new_task.id,
            }
            task_counter = int(time.time() * 1000000)
            tw = worker.TaskWrapper(6, task_counter, task_data)
            worker.background_task_queue.put(tw)
        worker.log_print(f"创建周版本AI分析任务: config_id={config_id}, task_id={new_task.id}", "AI")
        return new_task.id
    except worker.SQLAlchemyError as e:
        worker._db.session.rollback()
        worker.log_print(f"创建周版本AI分析任务数据库失败: {e}", "AI", force=True)
        return None
    except (TypeError, ValueError, RuntimeError, AttributeError) as e:
        worker._db.session.rollback()
        worker.log_print(f"创建周版本AI分析任务失败: {e}", "AI", force=True)
        return None
