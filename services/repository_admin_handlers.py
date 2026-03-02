"""Repository admin handlers extracted from app.py."""

from __future__ import annotations

from flask import flash, jsonify, redirect, request, url_for

from services.model_loader import get_runtime_model, get_runtime_models
from services.repository_cleanup_helpers import delete_local_repository_directory
from utils.path_security import build_repository_local_path
from utils.request_security import require_admin


def _runtime(*names):
    return get_runtime_models(*names)


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
        background_tasks_deleted = BackgroundTask.query.filter_by(repository_id=repository_id).delete()
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

    repository = Repository.query.get_or_404(repository_id)
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
                flash("SSH连接测试失败，请检查网络连接和SSH配置", "warning")
            else:
                success, message = service.clone_or_update_repository()
                if success:
                    flash(f"仓库连接测试成功: {message}", "success")
                else:
                    flash(f"仓库连接测试失败: {message}", "error")
        else:
            flash("暂时只支持Git仓库测试", "warning")
    except Exception as exc:
        log_print(f"测试过程中发生错误: {str(exc)}", "TEST", force=True)
        import traceback

        traceback.print_exc()
        flash(f"测试失败: {str(exc)}", "error")
    return redirect(url_for("repository_config", project_id=repository.project_id))


@require_admin
def delete_project(project_id):
    db, Project = _runtime("db", "Project")
    project = Project.query.get_or_404(project_id)
    db.session.delete(project)
    db.session.commit()
    flash("项目删除成功", "success")
    return redirect(url_for("index"))
