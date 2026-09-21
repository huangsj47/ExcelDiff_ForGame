"""Agent 派发模式下的任务投递与内联执行。

## 为什么单独一个文件

平台跑在「Agent 模式」时，任务不由本机 worker 执行，而是作为一条 agent 任务投出去
（`_enqueue_agent_task_from_background_task`），由远端的 agent 领走、执行完把结果回传；
回传后本机在 `execute_task_inline_for_agent` 里同步地把这次的结果落库。这两件事**只有
Agent 模式才会走到**，单机模式下它们是死路径 —— 单独一个文件让「Agent 模式多了哪些
行为」一眼可见，而不是散在 2000 行的 worker 里。

搬出来的直接原因仍然是 `scripts/check_file_length.py --strict`：`task_worker_service.py`
贴着 2000 行的 ERROR 门槛。

## 与 `task_worker_service.py` 的耦合方式

见 `task_worker_task_handlers.py` 抬头：模块级槽位一律 `worker.X` 现取，不 `from ... import`。
本文件额外有一处要注意 —— `execute_task_inline_for_agent` 会回调 worker 的
`_handle_auto_sync_task` / `_process_weekly_version_sync` 等，同样走 `worker.`，
这样 `monkeypatch.setattr(worker, "_process_weekly_version_sync", fake)` 才会生效。
"""

from __future__ import annotations

import services.task_worker_service as worker


def _enqueue_agent_task_from_background_task(db_task, extra_payload=None):
    """将 BackgroundTask 映射为 AgentTask（平台模式）。"""
    if not worker._use_agent_dispatch() or db_task is None:
        return None

    from services.agent_management_handlers import enqueue_agent_task

    task_type = db_task.task_type
    repository_id = None
    project_id = None
    payload = dict(extra_payload or {})

    if task_type in {"excel_diff", "auto_sync"}:
        repository_id = db_task.repository_id
        repo = worker._db.session.get(worker._Repository, repository_id) if repository_id else None
        project_id = repo.project_id if repo else None
        if repo and task_type == "auto_sync":
            payload["repository"] = {
                "repository_id": repo.id,
                "type": repo.type,
                "url": repo.url,
                "root_directory": repo.root_directory,
                "username": repo.username,
                "password": repo.password,
                "token": repo.token,
                "branch": repo.branch,
                "current_version": repo.current_version,
                "path_regex": repo.path_regex,
                "log_filter_regex": repo.log_filter_regex,
                "commit_filter": repo.commit_filter,
                "project_code": (repo.project.code if getattr(repo, "project", None) else None),
                "repository_name": repo.name,
            }
            payload.setdefault("limit", 1000)
    elif task_type == "weekly_sync":
        try:
            config_id = int(db_task.commit_id)
        except (TypeError, ValueError):
            config_id = None
        config = worker._db.session.get(worker._WeeklyVersionConfig, config_id) if config_id else None
        if config:
            repository_id = config.repository_id
            project_id = config.project_id
            payload.setdefault("config_id", config_id)
    elif task_type == "weekly_excel_cache":
        config_id = db_task.repository_id
        config = worker._db.session.get(worker._WeeklyVersionConfig, config_id) if config_id else None
        if config:
            repository_id = config.repository_id
            project_id = config.project_id
            payload.setdefault("config_id", config_id)
            payload.setdefault("file_path", db_task.file_path)
    elif task_type == "weekly_ai_analysis":
        try:
            config_id = int(db_task.commit_id)
        except (TypeError, ValueError):
            config_id = None
        config = worker._db.session.get(worker._WeeklyVersionConfig, config_id) if config_id else None
        if config:
            repository_id = config.repository_id
            project_id = config.project_id
            payload.setdefault("config_id", config_id)
            payload.setdefault("group_key", db_task.file_path)

    if not project_id:
        return None

    payload.setdefault("background_task_id", db_task.id)
    payload.setdefault("repository_id", repository_id)
    payload.setdefault("commit_id", db_task.commit_id)
    payload.setdefault("file_path", db_task.file_path)

    agent_task = enqueue_agent_task(
        task_type=task_type,
        project_id=project_id,
        repository_id=repository_id,
        source_task_id=db_task.id,
        priority=db_task.priority if db_task.priority is not None else 10,
        payload=payload,
    )
    worker.log_structured_event(
        "background_task_dispatched_to_agent",
        log_type="AGENT",
        background_task_id=db_task.id,
        source_task_id=db_task.id,
        agent_task_id=getattr(agent_task, "id", None),
        task_type=task_type,
        project_id=project_id,
        repository_id=repository_id,
        payload_schema_version=payload.get("schema_version"),
    )
    return True


def _ensure_agent_dispatch_for_background_task(db_task, extra_payload=None):
    """确保 pending 的 BackgroundTask 在 platform/agent 模式下有可领取的 AgentTask。"""
    if not worker._use_agent_dispatch() or db_task is None:
        return None
    try:
        from services.model_loader import get_runtime_models

        (AgentTask,) = get_runtime_models("AgentTask")
        existing_agent_task = AgentTask.query.filter(
            AgentTask.source_task_id == db_task.id,
            AgentTask.task_type == db_task.task_type,
            AgentTask.status.in_(["pending", "processing"]),
        ).first()
        if existing_agent_task:
            return False
    except (ImportError, worker.SQLAlchemyError, RuntimeError, AttributeError) as exc:
        worker.log_print(f"检查 AgentTask 关联关系失败，改为直接补下发: {exc}", "SYNC", force=True)

    return worker._enqueue_agent_task_from_background_task(db_task, extra_payload=extra_payload)


def execute_task_inline_for_agent(task_type, payload):
    """供 Agent 代理调用：在当前进程内直接执行任务逻辑并返回摘要。"""
    normalized_type = str(task_type or "").strip()
    payload = payload or {}

    if normalized_type == 'commit_diff':
        from services.commit_diff_logic import get_diff_data, resolve_previous_commit
        from services.commit_operation_handlers import _attach_author_display
        from utils.diff_data_utils import clean_json_data
        from utils.timezone_utils import format_beijing_time

        commit_record_id = payload.get('commit_record_id') or payload.get('commit_id')
        try:
            commit_record_id = int(commit_record_id)
        except (TypeError, ValueError):
            raise ValueError("commit_diff 任务缺少有效 commit_record_id")

        commit = worker._db.session.get(worker._Commit, commit_record_id)
        if not commit:
            raise ValueError(f"commit_diff 任务目标提交不存在: {commit_record_id}")

        repository = getattr(commit, 'repository', None)
        if repository is None:
            raise ValueError(f"commit_diff 任务目标提交缺少仓库信息: {commit_record_id}")

        file_commits = worker._Commit.query.filter(
            worker._Commit.repository_id == repository.id,
            worker._Commit.path == commit.path
        ).order_by(worker._Commit.commit_time.desc(), worker._Commit.id.desc()).all()
        previous_commit = resolve_previous_commit(commit, file_commits=file_commits)

        try:
            commits_for_author = [commit]
            if previous_commit:
                commits_for_author.append(previous_commit)
            _attach_author_display(commits_for_author)
        except (TypeError, ValueError, RuntimeError, AttributeError):
            pass

        diff_data = get_diff_data(commit, previous_commit=previous_commit)
        if diff_data:
            diff_data = clean_json_data(diff_data)

        previous_payload = None
        if previous_commit:
            previous_payload = {
                'commit_id': (previous_commit.commit_id or '')[:8] if getattr(previous_commit, 'commit_id', None) else 'N/A',
                'commit_time': format_beijing_time(previous_commit.commit_time, '%Y-%m-%d %H:%M:%S')
                if getattr(previous_commit, 'commit_time', None) else 'N/A',
                'author': (getattr(previous_commit, 'author_display', None) or getattr(previous_commit, 'author', None) or 'N/A'),
                'message': getattr(previous_commit, 'message', None) or 'N/A',
            }

        return {
            'success': True,
            'commit_id': commit_record_id,
            'is_excel': bool(worker._excel_cache_service.is_excel_file(commit.path)),
            'diff_data': diff_data,
            'previous_commit': previous_payload,
        }

    if normalized_type == 'excel_diff':
        repository_id = payload.get('repository_id')
        commit_id = payload.get('commit_id')
        file_path = payload.get('file_path')
        if not repository_id or not commit_id or not file_path:
            raise ValueError("excel_diff 任务缺少 repository_id/commit_id/file_path")
        worker._excel_cache_service.process_excel_diff_background(repository_id, commit_id, file_path)
        return {"message": "excel_diff completed"}

    if normalized_type == 'auto_sync':
        repository_id = payload.get('repository_id')
        if not repository_id:
            raise ValueError("auto_sync 任务缺少 repository_id")
        worker._handle_auto_sync_task(
            {
                "repository_id": repository_id,
                "force_reclone": bool(payload.get("force_reclone")),
                "force_repair_update": bool(payload.get("force_repair_update")),
            }
        )
        return {"message": "auto_sync completed"}

    if normalized_type == 'weekly_sync':
        config_id = payload.get('config_id')
        if not config_id:
            raise ValueError("weekly_sync 任务缺少 config_id")
        outcome = worker._process_weekly_version_sync(int(config_id))
        sync_status, sync_message = worker.weekly_sync_task_status_and_message(outcome)
        # Agent 端（agent/executor.py）把「返回值」一律当成 completed，只有抛异常才
        # 上报 failed。所以真正的失败必须用异常表达，否则配置不存在/被禁用/部分失败
        # 又会被 Agent 报成成功。无数据的 skipped 不算失败，照常返回。
        if sync_status in worker.FAILURE_TASK_STATUSES:
            raise RuntimeError(sync_message)
        return {"message": sync_message, "status": sync_status}

    if normalized_type == 'weekly_excel_cache':
        config_id = payload.get('config_id')
        file_path = payload.get('file_path')
        if not config_id or not file_path:
            raise ValueError("weekly_excel_cache 任务缺少 config_id/file_path")
        worker._process_weekly_excel_cache(int(config_id), file_path)
        return {"message": "weekly_excel_cache completed"}

    if normalized_type in ('file_content', 'file_diff', 'find_references'):
        # 在 Agent 自己的工作副本上读文件：正文 / 某一条提交改了这个文件的什么 / 一个关键词
        # 出现在哪些文件的哪几行。平台侧的调用方是 `services/agent_file_content_dispatch.py`，
        # 它读回的就是这里的返回值。实现在单独的模块里（本文件贴着 2000 行的硬上限，
        # 见 `scripts/check_file_length.py`）。
        if normalized_type == 'file_content':
            from services.agent_file_content_reader import read_file_content_for_agent as reader
        elif normalized_type == 'file_diff':
            from services.agent_file_diff_reader import read_file_diff_for_agent as reader
        else:
            from services.agent_reference_search import search_references_for_agent as reader
        return reader(payload)

    if normalized_type == 'weekly_ai_analysis':
        config_id = payload.get('config_id') or payload.get('commit_id')
        if not config_id:
            raise ValueError("weekly_ai_analysis 任务缺少 config_id")
        result = worker.run_weekly_analysis_background(
            int(config_id),
            # 与本地 handler（`task_worker_task_handlers` 的 weekly_ai_analysis 分支）
            # 同一口径：载荷里带 `trigger_source` 就照传，没带就是调度器排的。
            # 键确实能到这一行：`create_weekly_ai_analysis_task` 把它放进
            # `payload_extra`（`task_worker_queue_service.py`），随 AgentTask 下发，
            # Agent 原样回传（`agent/executor.py` 调本函数时用的就是那份载荷）。
            #
            # 不传的后果是**账会撒谎**：「等同步跑完就自动开始」转交出来的那一次是
            # **用户点出来的**（manual），漏传就被记成 `scheduled`，用量面板显示
            # 「定时」—— 用户明明点过，面板上却写着系统自己跑的。
            trigger_source=payload.get("trigger_source") or "scheduled",
        )
        return {"message": "weekly_ai_analysis completed", "result": result}

    raise ValueError(f"不支持的任务类型: {normalized_type}")
