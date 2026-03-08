"""Async branch refresh helpers extracted from task_worker_service.py."""

from __future__ import annotations

import threading
import time

from sqlalchemy.exc import SQLAlchemyError


NON_CRITICAL_BRANCH_REFRESH_ERRORS = (
    SQLAlchemyError,
    OSError,
    RuntimeError,
    AttributeError,
    TypeError,
    ValueError,
)


def queue_missing_git_branch_refresh(
    *,
    project_id,
    repository_ids,
    branch_refresh_lock,
    branch_refresh_cooldown_until,
    branch_refresh_cooldown_seconds,
    app,
    db,
    repository_model,
    get_git_service,
    log_print,
):
    """Asynchronously refresh missing git branches to avoid blocking page rendering."""
    unique_repo_ids = sorted({int(repo_id) for repo_id in (repository_ids or []) if repo_id})
    if not unique_repo_ids:
        return False

    now_ts = time.time()
    with branch_refresh_lock:
        cooldown_until = branch_refresh_cooldown_until.get(project_id, 0.0)
        if cooldown_until > now_ts:
            return False
        branch_refresh_cooldown_until[project_id] = now_ts + branch_refresh_cooldown_seconds

    def refresh_worker(target_project_id, target_repo_ids):
        updated_count = 0
        try:
            with app.app_context():
                repositories = repository_model.query.filter(
                    repository_model.project_id == target_project_id,
                    repository_model.type == "git",
                    repository_model.id.in_(target_repo_ids),
                    (repository_model.branch.is_(None)) | (repository_model.branch == ""),
                ).all()
                if not repositories:
                    return
                for repo in repositories:
                    try:
                        git_service = get_git_service(repo)
                        branches = git_service.get_branches()
                        if branches:
                            repo.branch = branches[0]
                            updated_count += 1
                    except NON_CRITICAL_BRANCH_REFRESH_ERRORS as branch_error:
                        log_print(f"异步刷新仓库分支失败: repo_id={repo.id}, error={branch_error}", "APP")
                if updated_count > 0:
                    db.session.commit()
                    log_print(
                        f"异步刷新仓库分支完成: project_id={target_project_id}, updated={updated_count}",
                        "APP",
                    )
                else:
                    db.session.rollback()
        except NON_CRITICAL_BRANCH_REFRESH_ERRORS as worker_error:
            try:
                db.session.rollback()
            except SQLAlchemyError:
                pass
            log_print(
                f"异步刷新仓库分支异常: project_id={target_project_id}, error={worker_error}",
                "APP",
                force=True,
            )

    refresh_thread = threading.Thread(
        target=refresh_worker,
        args=(project_id, unique_repo_ids),
        daemon=True,
        name=f"branch-refresh-{project_id}",
    )
    refresh_thread.start()
    return True
