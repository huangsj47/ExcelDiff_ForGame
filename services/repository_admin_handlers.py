"""Repository admin handlers extracted from app.py."""

from __future__ import annotations

import os

from flask import flash, jsonify, redirect, request, url_for
from sqlalchemy import or_

from services.model_loader import get_runtime_model, get_runtime_models
from services.repository_cleanup_helpers import delete_local_repository_directory
from utils.path_security import build_repository_local_path
from utils.request_security import require_admin


def _runtime(*names):
    return get_runtime_models(*names)


def _optional_runtime(name):
    try:
        return get_runtime_model(name)
    except Exception:
        return None


@require_admin
def update_repository_order():
    db, Repository = _runtime("db", "Repository")
    try:
        data = request.get_json(silent=True) or {}
        repo_id = data.get("repo_id")
        new_order = data.get("new_order")
        project_id = data.get("project_id")

        if not repo_id or new_order is None or not project_id:
            return jsonify({"status": "error", "message": "缺少必要参数"}), 400

        repositories = (
            Repository.query.filter_by(project_id=project_id)
            .order_by(Repository.display_order.asc())
            .all()
        )
        target_repo = None
        for repo in repositories:
            if repo.id == repo_id:
                target_repo = repo
                break

        if not target_repo:
            return jsonify({"status": "error", "message": "仓库不存在"}), 404

        repositories.remove(target_repo)
        repositories.insert(new_order, target_repo)

        for index, repo in enumerate(repositories):
            repo.display_order = index

        db.session.commit()
        return jsonify({"status": "success", "message": "仓库排序更新成功"})
    except Exception as exc:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(exc)}), 500


@require_admin
def swap_repository_order():
    db, Repository = _runtime("db", "Repository")
    try:
        data = request.get_json(silent=True) or {}
        first_repo_id = data.get("first_repo_id")
        second_repo_id = data.get("second_repo_id")
        project_id = data.get("project_id")

        if not first_repo_id or not second_repo_id or not project_id:
            return jsonify({"status": "error", "message": "缺少必要参数"}), 400

        if first_repo_id == second_repo_id:
            return jsonify({"status": "error", "message": "不能选择相同仓库"}), 400

        first_repo = Repository.query.filter_by(id=first_repo_id, project_id=project_id).first()
        second_repo = Repository.query.filter_by(id=second_repo_id, project_id=project_id).first()
        if not first_repo or not second_repo:
            return jsonify({"status": "error", "message": "仓库不存在或不属于当前项目"}), 404

        first_order = first_repo.display_order
        second_order = second_repo.display_order
        first_repo.display_order = second_order
        second_repo.display_order = first_order

        db.session.commit()
        return jsonify(
            {
                "status": "success",
                "message": f"成功交换仓库 {first_repo.name} 和 {second_repo.name} 的顺序",
            }
        )
    except Exception as exc:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(exc)}), 500


@require_admin
def delete_repository(repository_id):
    (
        db,
        Repository,
        BackgroundTask,
        DiffCache,
        ExcelHtmlCache,
        MergedDiffCache,
        WeeklyVersionDiffCache,
        WeeklyVersionExcelCache,
        Commit,
        log_print,
    ) = _runtime(
        "db",
        "Repository",
        "BackgroundTask",
        "DiffCache",
        "ExcelHtmlCache",
        "MergedDiffCache",
        "WeeklyVersionDiffCache",
        "WeeklyVersionExcelCache",
        "Commit",
        "log_print",
    )
    AgentTask = _optional_runtime("AgentTask")
    repository = Repository.query.get_or_404(repository_id)
    project_id = repository.project_id
    repo_name = repository.name
    local_path = build_repository_local_path(
        repository.project.code,
        repository.name,
        repository.id,
        strict=False,
    )

    log_print(f"开始删除仓库 {repo_name} (ID: {repository_id})", "DELETE")
    try:
        background_task_ids = [
            row[0]
            for row in db.session.query(BackgroundTask.id).filter(
                BackgroundTask.repository_id == repository_id
            ).all()
        ]

        if AgentTask is not None:
            agent_task_filters = [AgentTask.repository_id == repository_id]
            if background_task_ids:
                agent_task_filters.append(AgentTask.source_task_id.in_(background_task_ids))
            agent_tasks_deleted = AgentTask.query.filter(or_(*agent_task_filters)).delete(synchronize_session=False)
            log_print(f"删除了 {agent_tasks_deleted} 个AgentTask记录", "DELETE")

        background_tasks_deleted = BackgroundTask.query.filter_by(repository_id=repository_id).delete(synchronize_session=False)
        log_print(f"删除了 {background_tasks_deleted} 个BackgroundTask记录", "DELETE")

        diff_cache_deleted = DiffCache.query.filter_by(repository_id=repository_id).delete()
        log_print(f"删除了 {diff_cache_deleted} 个DiffCache记录", "DELETE")

        excel_cache_deleted = ExcelHtmlCache.query.filter_by(repository_id=repository_id).delete()
        log_print(f"删除了 {excel_cache_deleted} 个ExcelHtmlCache记录", "DELETE")

        try:
            merged_cache_deleted = MergedDiffCache.query.filter_by(repository_id=repository_id).delete()
            log_print(f"删除了 {merged_cache_deleted} 个MergedDiffCache记录", "DELETE")
        except Exception as exc:
            log_print(f"删除MergedDiffCache记录时出错（可能是表结构问题）: {exc}", "DELETE")
            try:
                db.session.execute(
                    "DELETE FROM merged_diff_cache WHERE repository_id = :repo_id",
                    {"repo_id": repository_id},
                )
                log_print("通过SQL成功删除MergedDiffCache记录", "DELETE")
            except Exception as sql_exc:
                log_print(f"SQL删除MergedDiffCache记录也失败: {sql_exc}", "DELETE")

        try:
            weekly_cache_deleted = WeeklyVersionDiffCache.query.filter_by(repository_id=repository_id).delete()
            log_print(f"删除了 {weekly_cache_deleted} 个WeeklyVersionDiffCache记录", "DELETE")
        except Exception as exc:
            log_print(f"删除WeeklyVersionDiffCache记录时出错: {exc}", "DELETE")

        try:
            weekly_excel_cache_deleted = WeeklyVersionExcelCache.query.filter_by(repository_id=repository_id).delete()
            log_print(f"删除了 {weekly_excel_cache_deleted} 个WeeklyVersionExcelCache记录", "DELETE")
        except Exception as exc:
            log_print(f"删除WeeklyVersionExcelCache记录时出错: {exc}", "DELETE")

        commit_deleted = Commit.query.filter_by(repository_id=repository_id).delete()
        log_print(f"删除了 {commit_deleted} 个Commit记录", "DELETE")

        repository.last_sync_commit_id = None
        repository.last_sync_time = None
        repository.cache_version = None
        repository.sync_mode = "full"
        log_print(f"清空了仓库 {repo_name} 的增量缓存同步字段", "DELETE")

        db.session.delete(repository)
        db.session.commit()
        log_print(f"成功删除仓库 {repo_name} 的所有数据库记录", "DELETE")
        flash(f"仓库 {repo_name} 及其所有关联数据已成功删除", "success")
    except Exception as exc:
        db.session.rollback()
        log_print(f"删除仓库失败: {str(exc)}", "ERROR")
        flash(f"删除仓库失败: {str(exc)}", "error")
        return redirect(url_for("repository_config", project_id=project_id))

    delete_local_repository_directory(local_path, repo_name)
    return redirect(url_for("repository_config", project_id=project_id))


def test_repository(repository_id):
    Repository, log_print = _runtime("Repository", "log_print")
    get_git_service = get_runtime_model("get_git_service")
    create_auto_sync_task = get_runtime_model("create_auto_sync_task")

    repository = Repository.query.get_or_404(repository_id)

    def _is_ajax_request():
        accept = request.headers.get("Accept", "")
        return (
            request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or request.is_json
            or "application/json" in accept
        )

    def _respond(success: bool, message: str, *, category: str, status_code: int):
        if _is_ajax_request():
            return (
                jsonify(
                    {
                        "success": bool(success),
                        "status": "success" if success else "error",
                        "message": message,
                        "scope": "platform_local",
                    }
                ),
                status_code,
            )
        flash(message, category)
        return redirect(url_for("repository_config", project_id=repository.project_id))

    deployment_mode = (os.environ.get("DEPLOYMENT_MODE") or "single").strip().lower()
    if deployment_mode in {"platform", "agent"}:
        task_id = create_auto_sync_task(repository.id)
        if task_id:
            return _respond(
                True,
                f"已派发到 Agent 执行连通性检查与同步 (task_id={task_id})，平台不再本地 clone 仓库",
                category="success",
                status_code=200,
            )
        return _respond(
            False,
            "平台+Agent 模式下未能派发测试任务，请检查项目与Agent绑定状态",
            category="error",
            status_code=409,
        )

    try:
        log_print(f"测试仓库连接: {repository.name}", "TEST")
        log_print(f"仓库类型: {repository.type}", "TEST")
        log_print(f"仓库URL: {repository.url}", "TEST")
        log_print(f"分支: {repository.branch}", "TEST")
        log_print(f"Token: {'已设置' if repository.token else '未设置'}", "TEST")

        if repository.type == "git":
            service = get_git_service(repository)
            log_print(f"本地路径: {service.local_path}", "TEST")
            ssh_test_result = service.test_ssh_connection()
            log_print(f"SSH连接测试结果: {ssh_test_result}", "TEST")
            if not ssh_test_result:
                return _respond(
                    False,
                    "SSH连接测试失败，请检查网络连接和SSH配置",
                    category="error",
                    status_code=400,
                )
            else:
                success, message = service.clone_or_update_repository()
                if success:
                    return _respond(
                        True,
                        f"仓库连接测试成功: {message}",
                        category="success",
                        status_code=200,
                    )
                else:
                    return _respond(
                        False,
                        f"仓库连接测试失败: {message}",
                        category="error",
                        status_code=400,
                    )
        else:
            return _respond(
                False,
                "暂时只支持Git仓库测试",
                category="warning",
                status_code=400,
            )
    except Exception as exc:
        log_print(f"测试过程中发生错误: {str(exc)}", "TEST", force=True)
        import traceback

        traceback.print_exc()
        return _respond(
            False,
            f"测试失败: {str(exc)}",
            category="error",
            status_code=500,
        )


@require_admin
def delete_project(project_id):
    (
        db,
        Project,
        Repository,
        Commit,
        DiffCache,
        ExcelHtmlCache,
        MergedDiffCache,
        BackgroundTask,
        WeeklyVersionConfig,
        WeeklyVersionDiffCache,
        WeeklyVersionExcelCache,
        OperationLog,
        AgentProjectBinding,
        AgentTask,
        log_print,
    ) = _runtime(
        "db",
        "Project",
        "Repository",
        "Commit",
        "DiffCache",
        "ExcelHtmlCache",
        "MergedDiffCache",
        "BackgroundTask",
        "WeeklyVersionConfig",
        "WeeklyVersionDiffCache",
        "WeeklyVersionExcelCache",
        "OperationLog",
        "AgentProjectBinding",
        "AgentTask",
        "log_print",
    )

    project = Project.query.get_or_404(project_id)
    repo_rows = Repository.query.filter_by(project_id=project_id).all()
    repo_ids = [repo.id for repo in repo_rows]
    weekly_config_ids = [row.id for row in WeeklyVersionConfig.query.filter_by(project_id=project_id).all()]
    project_name = project.name

    repo_local_paths = [
        (
            build_repository_local_path(project.code, repo.name, repo.id, strict=False),
            repo.name,
        )
        for repo in repo_rows
    ]

    AuthUserFunction = _optional_runtime("AuthUserFunction")
    AuthUserProject = _optional_runtime("AuthUserProject")
    AuthProjectJoinRequest = _optional_runtime("AuthProjectJoinRequest")
    AuthProjectCreateRequest = _optional_runtime("AuthProjectCreateRequest")
    AuthProjectPreAssignment = _optional_runtime("AuthProjectPreAssignment")
    QkitAuthUserProject = _optional_runtime("QkitAuthUserProject")
    QkitAuthProjectJoinRequest = _optional_runtime("QkitAuthProjectJoinRequest")
    QkitAuthProjectCreateRequest = _optional_runtime("QkitAuthProjectCreateRequest")
    QkitAuthProjectPreAssignment = _optional_runtime("QkitAuthProjectPreAssignment")
    QkitAuthProjectImportConfig = _optional_runtime("QkitAuthProjectImportConfig")
    QkitAuthImportBlock = _optional_runtime("QkitAuthImportBlock")

    def _safe_delete(query, label):
        try:
            count = query.delete(synchronize_session=False)
            if count:
                log_print(f"删除项目关联数据: {label} -> {count} 条", "DELETE")
            return count
        except Exception as exc:
            log_print(f"⚠️ 删除项目关联数据失败({label}): {exc}", "DELETE", force=True)
            return 0

    def _safe_nullify_created_project(model, label):
        if model is None:
            return 0
        try:
            count = (
                model.query.filter_by(created_project_id=project_id).update(
                    {"created_project_id": None},
                    synchronize_session=False,
                )
            )
            if count:
                log_print(f"清理项目创建申请引用: {label} -> {count} 条", "DELETE")
            return count
        except Exception as exc:
            log_print(f"⚠️ 清理项目创建申请引用失败({label}): {exc}", "DELETE", force=True)
            return 0

    try:
        # 先清理 auth / qkit 对项目的直接引用，避免 Project 删除时外键冲突
        if AuthUserFunction is not None:
            _safe_delete(AuthUserFunction.query.filter(AuthUserFunction.project_id == project_id), "AuthUserFunction")
        if AuthUserProject is not None:
            _safe_delete(AuthUserProject.query.filter(AuthUserProject.project_id == project_id), "AuthUserProject")
        if AuthProjectJoinRequest is not None:
            _safe_delete(
                AuthProjectJoinRequest.query.filter(AuthProjectJoinRequest.project_id == project_id),
                "AuthProjectJoinRequest",
            )
        if AuthProjectPreAssignment is not None:
            _safe_delete(
                AuthProjectPreAssignment.query.filter(AuthProjectPreAssignment.project_id == project_id),
                "AuthProjectPreAssignment",
            )
        _safe_nullify_created_project(AuthProjectCreateRequest, "AuthProjectCreateRequest")

        if QkitAuthUserProject is not None:
            _safe_delete(
                QkitAuthUserProject.query.filter(QkitAuthUserProject.project_id == project_id),
                "QkitAuthUserProject",
            )
        if QkitAuthProjectJoinRequest is not None:
            _safe_delete(
                QkitAuthProjectJoinRequest.query.filter(QkitAuthProjectJoinRequest.project_id == project_id),
                "QkitAuthProjectJoinRequest",
            )
        if QkitAuthProjectPreAssignment is not None:
            _safe_delete(
                QkitAuthProjectPreAssignment.query.filter(QkitAuthProjectPreAssignment.project_id == project_id),
                "QkitAuthProjectPreAssignment",
            )
        if QkitAuthProjectImportConfig is not None:
            _safe_delete(
                QkitAuthProjectImportConfig.query.filter(QkitAuthProjectImportConfig.project_id == project_id),
                "QkitAuthProjectImportConfig",
            )
        if QkitAuthImportBlock is not None:
            _safe_delete(
                QkitAuthImportBlock.query.filter(QkitAuthImportBlock.project_id == project_id),
                "QkitAuthImportBlock",
            )
        _safe_nullify_created_project(QkitAuthProjectCreateRequest, "QkitAuthProjectCreateRequest")

        # 删除 Agent 相关引用
        background_task_ids = []
        if repo_ids:
            background_task_ids = [row[0] for row in db.session.query(BackgroundTask.id).filter(
                BackgroundTask.repository_id.in_(repo_ids)
            ).all()]

        agent_task_filters = [AgentTask.project_id == project_id]
        if repo_ids:
            agent_task_filters.append(AgentTask.repository_id.in_(repo_ids))
        if background_task_ids:
            agent_task_filters.append(AgentTask.source_task_id.in_(background_task_ids))
        _safe_delete(AgentTask.query.filter(or_(*agent_task_filters)), "AgentTask")
        _safe_delete(
            AgentProjectBinding.query.filter(AgentProjectBinding.project_id == project_id),
            "AgentProjectBinding",
        )

        # 删除周版本缓存和配置
        weekly_diff_filters = []
        weekly_excel_filters = []
        if weekly_config_ids:
            weekly_diff_filters.append(WeeklyVersionDiffCache.config_id.in_(weekly_config_ids))
            weekly_excel_filters.append(WeeklyVersionExcelCache.config_id.in_(weekly_config_ids))
        if repo_ids:
            weekly_diff_filters.append(WeeklyVersionDiffCache.repository_id.in_(repo_ids))
            weekly_excel_filters.append(WeeklyVersionExcelCache.repository_id.in_(repo_ids))
        if weekly_diff_filters:
            _safe_delete(
                WeeklyVersionDiffCache.query.filter(or_(*weekly_diff_filters)),
                "WeeklyVersionDiffCache",
            )
        if weekly_excel_filters:
            _safe_delete(
                WeeklyVersionExcelCache.query.filter(or_(*weekly_excel_filters)),
                "WeeklyVersionExcelCache",
            )
        _safe_delete(
            WeeklyVersionConfig.query.filter(WeeklyVersionConfig.project_id == project_id),
            "WeeklyVersionConfig",
        )

        # 删除仓库相关记录（按依赖顺序）
        if repo_ids:
            _safe_delete(OperationLog.query.filter(OperationLog.repository_id.in_(repo_ids)), "OperationLog")
            _safe_delete(DiffCache.query.filter(DiffCache.repository_id.in_(repo_ids)), "DiffCache")
            _safe_delete(ExcelHtmlCache.query.filter(ExcelHtmlCache.repository_id.in_(repo_ids)), "ExcelHtmlCache")
            _safe_delete(MergedDiffCache.query.filter(MergedDiffCache.repository_id.in_(repo_ids)), "MergedDiffCache")
            _safe_delete(Commit.query.filter(Commit.repository_id.in_(repo_ids)), "Commit")
            _safe_delete(
                BackgroundTask.query.filter(BackgroundTask.repository_id.in_(repo_ids)),
                "BackgroundTask",
            )

        _safe_delete(Repository.query.filter(Repository.project_id == project_id), "Repository")

        db.session.delete(project)
        db.session.commit()
        log_print(f"项目删除成功: {project_name} (ID: {project_id})", "DELETE")
    except Exception as exc:
        db.session.rollback()
        log_print(f"删除项目失败: {project_name} (ID: {project_id}) -> {exc}", "ERROR", force=True)
        flash(f"删除项目失败: {exc}", "error")
        return redirect(url_for("index"))

    for local_path, repo_name in repo_local_paths:
        delete_local_repository_directory(local_path, repo_name)

    flash("项目删除成功", "success")
    return redirect(url_for("index"))
