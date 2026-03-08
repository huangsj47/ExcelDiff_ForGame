"""Repository update API handlers extracted from app.py."""

from __future__ import annotations

from sqlalchemy.exc import SQLAlchemyError


REPOSITORY_UPDATE_WORKER_ERRORS = (
    RuntimeError,
    TypeError,
    ValueError,
    LookupError,
    AttributeError,
    SQLAlchemyError,
)


def run_repository_update_and_cache_worker(
    *,
    repository_id,
    app,
    db,
    Repository,
    Commit,
    get_git_service,
    get_svn_service,
    dispatch_auto_sync_task_when_agent_mode,
    clear_repository_sync_error,
    record_repository_sync_error,
    log_print,
):
    """Async worker: update repository and refresh commit cache."""
    repository = None
    try:
        with app.app_context():
            handled_by_agent, task_id = dispatch_auto_sync_task_when_agent_mode(repository_id)
        if handled_by_agent:
            if task_id:
                log_print(
                    f"✅ [REUSE_SYNC] 已派发到Agent执行仓库同步: repository_id={repository_id}, task_id={task_id}",
                    "SYNC",
                )
            else:
                log_print(
                    f"❌ [REUSE_SYNC] 派发Agent同步任务失败: repository_id={repository_id}",
                    "SYNC",
                    force=True,
                )
            return

        with app.app_context():
            repository = db.session.get(Repository, repository_id)
            if not repository:
                log_print(f"❌ 异步更新失败：仓库不存在 {repository_id}", "API", force=True)
                return

            if repository.type == "git":
                service = get_git_service(repository)
                success, message = service.clone_or_update_repository()
                log_print(f"Git更新结果: {success}, {message}", "GIT")
            elif repository.type == "svn":
                service = get_svn_service(repository)
                success, message = service.checkout_or_update_repository()
                log_print(f"SVN更新结果: {success}, {message}", "SVN")
            else:
                log_print(f"不支持的仓库类型: {repository.type}", "API", force=True)
                return

            if success:
                log_print("仓库更新成功，开始触发缓存操作...", "CACHE")
                commits_added = service.sync_repository_commits(db, Commit)
                clear_repository_sync_error(
                    db.session,
                    repository,
                    log_func=log_print,
                    log_type="SYNC",
                    commit=True,
                )
                log_print(f"{repository.type.upper()} 同步完成，添加了 {commits_added} 个新提交", "SYNC")
                log_print(f"✅ 仓库 {repository.name} 更新和缓存完成", "CACHE")
            else:
                log_print(f"❌ 仓库 {repository.name} 更新失败: {message}", "API", force=True)
                record_repository_sync_error(
                    db.session,
                    repository,
                    f"异步更新失败: {message}",
                    log_func=log_print,
                    log_type="SYNC",
                    commit=True,
                )
    except REPOSITORY_UPDATE_WORKER_ERRORS as exc:
        log_print(f"❌ 异步更新和缓存操作异常: {exc}", "API", force=True)
        if repository is not None:
            record_repository_sync_error(
                db.session,
                repository,
                f"异步更新异常: {exc}",
                log_func=log_print,
                log_type="SYNC",
                commit=True,
            )
        import traceback

        traceback.print_exc()


def handle_reuse_repository_and_update(
    *,
    repository_id,
    request,
    jsonify,
    Repository,
    db,
    NotFound,
    SQLAlchemyError,
    log_print,
    dispatch_auto_sync_task_when_agent_mode,
    spawn_update_worker,
):
    """Handle API: reuse repository and trigger sync/cache task."""
    try:
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return (
                jsonify(
                    {
                        "success": False,
                        "message": "请求体必须为JSON对象",
                        "error_type": "invalid_request",
                    }
                ),
                400,
            )
        action = data.get("action", "pull_and_cache")
        repository = Repository.query.get_or_404(repository_id)
        log_print(f"🔄 收到仓库复用更新请求: {repository.name} (ID: {repository_id})", "API")
        handled_by_agent, task_id = dispatch_auto_sync_task_when_agent_mode(repository_id)
        if handled_by_agent:
            if task_id:
                return (
                    jsonify(
                        {
                            "success": True,
                            "message": f"仓库 {repository.name} 已派发到Agent执行同步",
                            "repository_id": repository_id,
                            "action": action,
                            "task_id": task_id,
                        }
                    ),
                    202,
                )
            return (
                jsonify(
                    {
                        "success": False,
                        "message": "派发Agent同步任务失败，请检查项目绑定和Agent在线状态",
                        "repository_id": repository_id,
                        "action": action,
                    }
                ),
                409,
            )
        spawn_update_worker(repository_id)
        return jsonify(
            {
                "success": True,
                "message": f"仓库 {repository.name} 更新和缓存任务已启动",
                "repository_id": repository_id,
                "action": action,
            }
        )
    except NotFound:
        return (
            jsonify(
                {
                    "success": False,
                    "message": "仓库不存在",
                    "error_type": "repository_not_found",
                }
            ),
            404,
        )
    except SQLAlchemyError as exc:
        db.session.rollback()
        log_print(f"❌ 仓库复用更新数据库异常: {exc}", "API", force=True)
        return (
            jsonify(
                {
                    "success": False,
                    "message": "数据库操作失败，请稍后重试",
                    "error_type": "database_error",
                }
            ),
            500,
        )
    except RuntimeError as exc:
        log_print(f"❌ 仓库复用更新运行时异常: {exc}", "API", force=True)
        return (
            jsonify(
                {
                    "success": False,
                    "message": f"更新失败: {str(exc)}",
                    "error_type": "runtime_error",
                }
            ),
            500,
        )


def handle_update_repository_and_cache(
    *,
    repository_id,
    request,
    jsonify,
    Repository,
    db,
    NotFound,
    SQLAlchemyError,
    log_print,
    spawn_update_worker,
):
    """Handle API: trigger repository update and cache task."""
    try:
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return (
                jsonify(
                    {
                        "success": False,
                        "message": "请求体必须为JSON对象",
                        "error_type": "invalid_request",
                    }
                ),
                400,
            )
        action = data.get("action", "pull_and_cache")
        repository = Repository.query.get_or_404(repository_id)
        log_print(f"🔄 收到仓库更新请求: {repository.name} (ID: {repository_id})", "API")
        spawn_update_worker(repository_id)
        return jsonify(
            {
                "success": True,
                "message": f"仓库 {repository.name} 更新和缓存任务已启动",
                "repository_id": repository_id,
                "action": action,
            }
        )
    except NotFound:
        return (
            jsonify(
                {
                    "success": False,
                    "message": "仓库不存在",
                    "error_type": "repository_not_found",
                }
            ),
            404,
        )
    except SQLAlchemyError as exc:
        db.session.rollback()
        log_print(f"❌ 仓库更新数据库异常: {exc}", "API", force=True)
        return (
            jsonify(
                {
                    "success": False,
                    "message": "数据库操作失败，请稍后重试",
                    "error_type": "database_error",
                }
            ),
            500,
        )
    except RuntimeError as exc:
        log_print(f"❌ 仓库更新运行时异常: {exc}", "API", force=True)
        return (
            jsonify(
                {
                    "success": False,
                    "message": f"更新失败: {str(exc)}",
                    "error_type": "runtime_error",
                }
            ),
            500,
        )


def handle_batch_update_credentials(
    *,
    request,
    jsonify,
    Repository,
    db,
    SQLAlchemyError,
    app_logger,
):
    """Handle API: bulk update repository credentials."""
    try:
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "请求体必须为JSON对象",
                        "error_type": "invalid_request",
                    }
                ),
                400,
            )
        project_id = data.get("project_id")
        repo_type = data.get("repo_type")
        if not project_id or not repo_type:
            return jsonify({"status": "error", "message": "缺少必要参数"}), 400

        repositories = Repository.query.filter_by(project_id=project_id, type=repo_type).all()
        if not repositories:
            return jsonify({"status": "error", "message": f"项目下没有找到{repo_type.upper()}仓库"}), 404

        updated_count = 0
        if repo_type == "git":
            git_token = data.get("git_token")
            if not git_token:
                return jsonify({"status": "error", "message": "缺少Git Token"}), 400
            for repo in repositories:
                repo.token = git_token
                updated_count += 1
        elif repo_type == "svn":
            svn_username = data.get("svn_username")
            svn_password = data.get("svn_password")
            if not svn_username or not svn_password:
                return jsonify({"status": "error", "message": "缺少SVN用户名或密码"}), 400
            for repo in repositories:
                repo.username = svn_username
                repo.password = svn_password
                updated_count += 1
        else:
            return jsonify({"status": "error", "message": "不支持的仓库类型"}), 400

        db.session.commit()
        return jsonify(
            {
                "status": "success",
                "message": f"成功更新{updated_count}个{repo_type.upper()}仓库",
                "updated_count": updated_count,
            }
        )
    except SQLAlchemyError as exc:
        db.session.rollback()
        app_logger.error(f"批量更新仓库凭据失败: {str(exc)}")
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "数据库更新失败，请稍后重试",
                    "error_type": "database_error",
                }
            ),
            500,
        )
    except (TypeError, ValueError) as exc:
        return (
            jsonify(
                {
                    "status": "error",
                    "message": f"请求参数错误: {exc}",
                    "error_type": "invalid_request",
                }
            ),
            400,
        )
