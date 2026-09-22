"""Repository maintenance API handlers extracted from app.py."""

from __future__ import annotations

from sqlalchemy.exc import SQLAlchemyError
from services.repository_sync_window import apply_start_date, resolve_sync_window
from services.task_worker_priority import EXCEL_DIFF_DEFAULT, MAINTENANCE_REBUILD


REPOSITORY_MAINTENANCE_CACHE_REBUILD_ERRORS = (
    SQLAlchemyError,
    RuntimeError,
    ValueError,
    TypeError,
    AttributeError,
)
REPOSITORY_MAINTENANCE_CACHE_STATUS_ERRORS = (
    SQLAlchemyError,
    RuntimeError,
    ValueError,
    TypeError,
    AttributeError,
)
REPOSITORY_MAINTENANCE_SYNC_ERRORS = (
    SQLAlchemyError,
    RuntimeError,
    ValueError,
    TypeError,
    AttributeError,
    KeyError,
    OSError,
)


def handle_regenerate_cache(
    *,
    repository_id,
    Repository,
    DiffCache,
    ExcelHtmlCache,
    db,
    excel_cache_service,
    add_excel_diff_task,
    jsonify,
    log_print,
):
    """Regenerate excel diff cache tasks for one repository."""
    try:
        repository = Repository.query.get_or_404(repository_id)
        recent_commits = excel_cache_service.get_recent_excel_commits(repository, limit=1000)
        task_count = len(recent_commits)
        if task_count > 0:
            DiffCache.query.filter_by(repository_id=repository_id).delete()
            ExcelHtmlCache.query.filter_by(repository_id=repository_id).delete()
            db.session.commit()
            log_print(f"已清理仓库 {repository_id} 的所有缓存数据", "INFO")
            for commit in recent_commits:
                add_excel_diff_task(repository_id, commit.commit_id, commit.path, priority=MAINTENANCE_REBUILD)
            message = f"已将 {task_count} 个文件差异放入缓存队列，正在后台处理中..."
            excel_cache_service.log_cache_operation(
                f"🔄 重新生成缓存: 仓库 {repository.name}, 任务数量 {task_count}",
                "info",
            )
        else:
            message = f"仓库 {repository.name} 最近2周内没有文件变更，无需重新生成缓存。"
        return jsonify({"success": True, "message": message, "task_count": task_count})
    except REPOSITORY_MAINTENANCE_CACHE_REBUILD_ERRORS as exc:
        log_print(f"重新生成缓存失败: {exc}", "INFO")
        return jsonify({"success": False, "message": f"重新生成缓存失败: {str(exc)}"}), 500


def handle_get_cache_status(
    *,
    repository_id,
    Repository,
    DiffCache,
    excel_cache_service,
    ensure_repository_access_or_403,
    jsonify,
    log_print,
):
    """Query cache coverage status for one repository."""
    try:
        repository = Repository.query.get_or_404(repository_id)
        ensure_repository_access_or_403(repository)

        total_cache = DiffCache.query.filter_by(repository_id=repository_id).count()
        completed_cache = DiffCache.query.filter_by(repository_id=repository_id, cache_status="completed").count()
        failed_cache = DiffCache.query.filter_by(repository_id=repository_id, cache_status="failed").count()
        processing_cache = DiffCache.query.filter_by(repository_id=repository_id, cache_status="processing").count()

        recent_commits = excel_cache_service.get_recent_excel_commits(repository, limit=1000)
        total_excel_commits = len(recent_commits)
        return jsonify(
            {
                "success": True,
                "repository_name": repository.name,
                "total_cache": total_cache,
                "completed_cache": completed_cache,
                "failed_cache": failed_cache,
                "processing_cache": processing_cache,
                "total_excel_commits": total_excel_commits,
                "cache_coverage": f"{completed_cache}/{total_excel_commits}" if total_excel_commits > 0 else "0/0",
            }
        )
    except REPOSITORY_MAINTENANCE_CACHE_STATUS_ERRORS as exc:
        log_print(f"获取缓存状态失败: {exc}", "INFO")
        return jsonify({"success": False, "message": f"获取缓存状态失败: {str(exc)}"}), 500


def handle_get_clone_status(
    *,
    repository_id,
    db,
    Repository,
    Commit,
    ensure_repository_access_or_403,
    jsonify,
):
    """Return lightweight clone status payload."""
    repo = db.session.get(Repository, repository_id)
    if not repo:
        return jsonify({"success": False, "message": "仓库不存在"}), 404
    ensure_repository_access_or_403(repo)
    commit_count = Commit.query.filter_by(repository_id=repository_id).count()
    is_data_ready = repo.clone_status == "completed" and commit_count > 0
    return jsonify(
        {
            "success": True,
            "clone_status": repo.clone_status or "pending",
            "clone_error": getattr(repo, "clone_error", None) or "",
            "commit_count": commit_count,
            "is_data_ready": is_data_ready,
        }
    )


def should_retry_with_reclone(*, repository, db, Commit) -> bool:
    """Infer retry strategy from repository state."""
    if repository is None:
        return False
    status = str(getattr(repository, "clone_status", "") or "").strip().lower()
    has_commits = (
        db.session.query(Commit.id)
        .filter(Commit.repository_id == repository.id)
        .limit(1)
        .first()
        is not None
    )
    if status in {"pending", "cloning"}:
        return True
    if status == "failed" and not has_commits:
        return True
    clone_error = str(getattr(repository, "clone_error", "") or "").strip()
    if clone_error and not has_commits:
        return True
    return False


def handle_retry_clone_repository(
    *,
    repository_id,
    Repository,
    dispatch_auto_sync_task_when_agent_mode,
    create_auto_sync_task,
    should_retry_with_reclone_func,
    flash,
    redirect,
    url_for,
):
    """Retry repository sync and choose reclone or repair strategy."""
    repository = Repository.query.get_or_404(repository_id)
    project_id = repository.project_id
    force_reclone = should_retry_with_reclone_func(repository)
    extra_payload = {
        "force_reclone": bool(force_reclone),
        "force_repair_update": bool(not force_reclone),
        "retry_source": "manual_retry",
    }

    handled_by_agent, task_id = dispatch_auto_sync_task_when_agent_mode(
        repository_id,
        extra_payload=extra_payload,
    )
    if not handled_by_agent:
        task_id = create_auto_sync_task(repository_id, extra_payload=extra_payload)
    if task_id:
        if force_reclone:
            flash("已启动重试：将先清理本地目录，再重新克隆并同步", "success")
        else:
            flash("已启动重试：将先修复本地Git/SVN目录，再重新同步", "success")
    else:
        if handled_by_agent:
            flash("派发Agent重试任务失败，请检查项目绑定与Agent在线状态", "error")
        else:
            flash("创建重试任务失败，请查看平台日志", "error")
    return redirect(url_for("repository_config", project_id=project_id))


def handle_sync_repository(
    *,
    repository_id,
    db,
    Repository,
    Commit,
    get_git_service,
    get_svn_service,
    dispatch_auto_sync_task_when_agent_mode,
    record_repository_sync_error,
    clear_repository_sync_error,
    add_excel_diff_task,
    excel_cache_service,
    jsonify,
    log_print,
):
    """Manual sync endpoint: execute pull/update and ingest commits immediately."""
    repository = None
    try:
        log_print(f"🚀 [MANUAL_SYNC] 手动同步开始 - 仓库ID: {repository_id}", "INFO")
        repository = db.session.get(Repository, repository_id)
        if not repository:
            return jsonify({"status": "error", "message": "仓库不存在"}), 404

        log_print(f"📂 [MANUAL_SYNC] 仓库信息: {repository.name} ({repository.type})")
        handled_by_agent, task_id = dispatch_auto_sync_task_when_agent_mode(repository_id)
        if handled_by_agent:
            if task_id:
                return jsonify({"status": "accepted", "message": "已派发到绑定Agent执行同步", "task_id": task_id}), 202
            return (
                jsonify({"status": "error", "message": "未能派发Agent同步任务，请检查项目绑定与Agent在线状态"}),
                409,
            )

        if repository.type == "git":
            git_service = get_git_service(repository)
            log_print("🔄 [MANUAL_SYNC] 开始git pull操作...", "INFO")
            success, message = git_service.clone_or_update_repository()
            if not success:
                log_print(f"❌ [MANUAL_SYNC] Git操作失败: {message}", "INFO")
                record_repository_sync_error(
                    db.session,
                    repository,
                    f"手动同步失败（Git）: {message}",
                    log_func=log_print,
                    log_type="SYNC",
                    commit=True,
                )
                return jsonify({"status": "error", "message": f"Git操作失败: {message}"}), 500

            log_print(f"✅ [MANUAL_SYNC] Git操作成功: {message}", "INFO")
            # 与后台同步同一个口径：**分支 tip 变了没有**优先，日期水位线兜底
            # （理由见 `services/repository_sync_window.py`）。
            latest_commit = Commit.query.filter_by(repository_id=repository_id).order_by(Commit.commit_time.desc()).first()
            repository = db.session.get(Repository, repository_id)
            window = resolve_sync_window(
                git_service, repository,
                latest_known_commit_time=getattr(latest_commit, 'commit_time', None),
            )
            log_print(f"🔍 [MANUAL_SYNC] 采集口径：{window.reason}", "INFO")

            limit = 800 if not window.since_date else 1000
            import time

            start_time = time.time()
            commits = git_service.get_commits_threaded(
                since_date=window.since_date, limit=limit,
                rev_range=window.rev_range,
            )
            if window.rev_range:
                # 区间不看日期，但 `start_date` 是用户声明的下界，仍然作数。
                commits = apply_start_date(commits, repository.start_date)
            end_time = time.time()
            log_print(
                f"⚡ [THREADED_GIT] 多线程获取提交记录耗时: {(end_time - start_time):.2f}秒, 提交数: {len(commits)}",
                "GIT",
            )
            log_print(f"🔍 [MANUAL_SYNC] 获取到 {len(commits)} 个提交记录")
            commits_added = 0
            excel_tasks_added = 0
            # 判重键必须是 `(commit_id, path)`：`get_commits` 对一次提交里**每一个**
            # 变更文件各产出一条记录（commit_id 是同一个 hexsha），只按 commit_id 判重
            # 会让同一次提交的第 2 个文件起全被跳过 —— 评审者看到的那条提交等于
            # 确认了没看过的改动（后台同步那条路修过同一个坑）。
            existing_pairs = {
                (row[0], row[1] or '')
                for row in db.session.query(Commit.commit_id, Commit.path)
                .filter(Commit.repository_id == repository_id)
                .filter(Commit.commit_id.in_([cd['commit_id'] for cd in commits] or ['']))
                .all()
            }
            for i, commit_data in enumerate(commits):
                pair = (commit_data["commit_id"], commit_data.get("path", "") or "")
                if pair not in existing_pairs:
                    existing_pairs.add(pair)
                    new_commit = Commit(
                        repository_id=repository_id,
                        commit_id=commit_data["commit_id"],
                        author=commit_data.get("author", ""),
                        message=commit_data.get("message", ""),
                        commit_time=commit_data.get("commit_time"),
                        path=commit_data.get("path", ""),
                        version=commit_data.get("version", commit_data["commit_id"][:8]),
                        operation=commit_data.get("operation", "M"),
                        status="pending",
                    )
                    db.session.add(new_commit)
                    commits_added += 1
                    log_print(f"➕ [MANUAL_SYNC] 添加新提交 {i + 1}/{len(commits)}: {commit_data['commit_id'][:8]}")
                    if excel_cache_service.is_excel_file(commit_data.get("path", "")):
                        add_excel_diff_task(
                            repository_id,
                            commit_data["commit_id"],
                            commit_data.get("path", ""),
                            priority=EXCEL_DIFF_DEFAULT,
                            auto_commit=False,
                        )
                        excel_tasks_added += 1
                        log_print(f"📊 [MANUAL_SYNC] 添加Excel缓存任务: {commit_data.get('path', '')}")
                else:
                    log_print(f"⏭️ [MANUAL_SYNC] 跳过已存在提交 {i + 1}/{len(commits)}: {commit_data['commit_id'][:8]}")
            # 记下这一轮的 tip：不记的话下一轮（不论手工还是后台）还会拿同一个旧区间
            # 再采一遍 —— 判重键是 `(commit_id, path)`，重采不会插重行，但白扫一次。
            if window.tip:
                repository.last_synced_tip = window.tip
            db.session.commit()
            clear_repository_sync_error(
                db.session,
                repository,
                log_func=log_print,
                log_type="SYNC",
                commit=True,
            )
            log_print(
                f"✅ [MANUAL_SYNC] 手动同步完成，添加了 {commits_added} 个新提交，{excel_tasks_added} 个Excel缓存任务",
                "INFO",
            )
            return (
                jsonify(
                    {
                        "status": "success",
                        "message": f"同步成功，添加了 {commits_added} 个新提交",
                        "commits_added": commits_added,
                    }
                ),
                200,
            )

        if repository.type == "svn":
            svn_service = get_svn_service(repository)
            success, message = svn_service.checkout_or_update_repository()
            if not success:
                record_repository_sync_error(
                    db.session,
                    repository,
                    f"手动同步失败（SVN）: {message}",
                    log_func=log_print,
                    log_type="SYNC",
                    commit=True,
                )
                return jsonify({"status": "error", "message": f"SVN操作失败: {message}"}), 500
            commits_added = svn_service.sync_repository_commits(db, Commit)
            clear_repository_sync_error(
                db.session,
                repository,
                log_func=log_print,
                log_type="SYNC",
                commit=True,
            )
            return jsonify({"status": "success", "message": f"同步成功，添加了 {commits_added} 个新提交", "commits_added": commits_added}), 200

        return jsonify({"status": "error", "message": f"不支持的仓库类型: {repository.type}"}), 400
    except REPOSITORY_MAINTENANCE_SYNC_ERRORS as exc:
        import traceback

        error_details = traceback.format_exc()
        log_print(f"❌ [MANUAL_SYNC] 手动同步失败: {str(exc)}")
        log_print(f"错误详情: {error_details}", "INFO")
        if repository is not None:
            record_repository_sync_error(
                db.session,
                repository,
                f"手动同步异常: {exc}",
                log_func=log_print,
                log_type="SYNC",
                commit=True,
            )
        return jsonify({"status": "error", "message": f"同步失败: {str(exc)}"}), 500
