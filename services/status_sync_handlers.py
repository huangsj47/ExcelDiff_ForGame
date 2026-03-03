"""Status-sync and weekly confirm handlers extracted from app.py."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from flask import flash, jsonify, redirect, render_template, request, url_for

from services.model_loader import get_runtime_model, get_runtime_models
from utils.request_security import require_admin


@require_admin
def clear_all_confirmation_status():
    """Clear all confirmation statuses."""
    db, log_print = get_runtime_models("db", "log_print")
    try:
        from services.status_sync_service import StatusSyncService

        sync_service = StatusSyncService(db)
        result = sync_service.clear_all_confirmation_status()
        if result.get("success"):
            return jsonify(result)
        return jsonify(result), 500
    except Exception as exc:
        log_print(f"清空确认状态失败: {exc}", "ERROR", force=True)
        return jsonify({"success": False, "message": str(exc)}), 500


def get_sync_mapping_info():
    """Get status-sync mapping info."""
    db, log_print = get_runtime_models("db", "log_print")
    try:
        config_id = request.args.get("config_id", type=int)
        repository_id = request.args.get("repository_id", type=int)
        project_id = request.args.get("project_id", type=int)

        from services.status_sync_service import StatusSyncService

        sync_service = StatusSyncService(db)
        result = sync_service.get_sync_mapping_info(config_id, repository_id, project_id)
        if result.get("success"):
            return jsonify(result)
        return jsonify(result), 500
    except Exception as exc:
        log_print(f"获取同步映射信息失败: {exc}", "ERROR", force=True)
        return jsonify({"success": False, "message": str(exc)}), 500


def status_sync_management():
    """Global status-sync management page."""
    return render_template("status_sync_management.html")


def project_status_sync_management(project_code):
    """Project-specific status-sync management page."""
    (Project,) = get_runtime_models("Project")
    project = Project.query.filter_by(code=project_code).first()
    if not project:
        flash(f"项目 {project_code} 不存在", "error")
        return redirect(url_for("index"))
    return render_template("status_sync_management.html", project=project)


def status_sync_test():
    """Status-sync test page."""
    return render_template("status_sync_test.html")


def get_sync_configs():
    """Get status-sync config list with optional project filter."""
    WeeklyVersionConfig, log_print = get_runtime_models("WeeklyVersionConfig", "log_print")
    try:
        project_id = request.args.get("project_id", type=int)
        query = WeeklyVersionConfig.query
        if project_id:
            query = query.filter_by(project_id=project_id)

        configs = query.order_by(WeeklyVersionConfig.created_at.desc()).all()
        config_list = []
        for config in configs:
            config_list.append(
                {
                    "id": config.id,
                    "name": config.name,
                    "repository_name": config.repository.name if config.repository else "未知仓库",
                    "project_name": config.project.name if config.project else "未知项目",
                }
            )
        return jsonify({"success": True, "configs": config_list})
    except Exception as exc:
        log_print(f"获取同步配置失败: {exc}", "ERROR", force=True)
        return jsonify({"success": False, "message": str(exc)}), 500


def weekly_version_batch_confirm_api(config_id):
    """Batch confirm weekly-version pending files."""
    db, WeeklyVersionConfig, WeeklyVersionDiffCache, log_print = get_runtime_models(
        "db",
        "WeeklyVersionConfig",
        "WeeklyVersionDiffCache",
        "log_print",
    )
    try:
        WeeklyVersionConfig.query.get_or_404(config_id)

        data = request.get_json() or {}
        file_paths = data.get("file_paths", [])

        if file_paths:
            log_print(f"批量确认指定的 {len(file_paths)} 个文件", "WEEKLY")
            pending_caches = (
                WeeklyVersionDiffCache.query.filter(
                    WeeklyVersionDiffCache.config_id == config_id,
                    WeeklyVersionDiffCache.overall_status == "pending",
                    WeeklyVersionDiffCache.file_path.in_(file_paths),
                ).all()
            )
        else:
            log_print("批量确认所有待确认文件", "WEEKLY")
            pending_caches = WeeklyVersionDiffCache.query.filter_by(
                config_id=config_id,
                overall_status="pending",
            ).all()

        from services.status_sync_service import StatusSyncService
        from utils.request_security import _get_current_user

        sync_service = StatusSyncService(db)
        current_user = _get_current_user()
        operator_username = current_user.username if current_user else None
        updated_count = 0
        sync_results = []

        for cache in pending_caches:
            old_status = cache.overall_status
            confirmation_status = json.loads(cache.confirmation_status) if cache.confirmation_status else {}
            confirmation_status["dev"] = "confirmed"
            cache.confirmation_status = json.dumps(confirmation_status)
            cache.overall_status = "confirmed"
            cache.status_changed_by = operator_username
            cache.updated_at = datetime.now(timezone.utc)
            updated_count += 1
            log_print(f"确认文件: {cache.file_path}", "WEEKLY")

            if old_status != "confirmed":
                sync_result = sync_service.sync_weekly_to_commit(config_id, cache.file_path, "confirmed")
                sync_results.append(sync_result)

        db.session.commit()

        total_commits_updated = sum(
            result.get("updated_count", 0)
            for result in sync_results
            if result.get("success")
        )
        log_print(
            f"批量确认完成，共确认 {updated_count} 个文件，同步更新了 {total_commits_updated} 个提交记录",
            "WEEKLY",
        )

        return jsonify(
            {
                "success": True,
                "message": f"成功确认了 {updated_count} 个文件，同步更新了 {total_commits_updated} 个提交记录",
                "updated_count": updated_count,
            }
        )
    except Exception as exc:
        db.session.rollback()
        log_print(f"批量确认失败: {exc}", "ERROR", force=True)
        return jsonify({"success": False, "message": str(exc)}), 500
