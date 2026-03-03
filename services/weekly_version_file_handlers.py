"""Weekly-version file handlers extracted from app.py."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from flask import jsonify, redirect, render_template, request, url_for

from services.model_loader import get_runtime_models


def weekly_version_file_previous_version(config_id):
    """View previous version file content for weekly config."""
    WeeklyVersionConfig, Commit, log_print = get_runtime_models("WeeklyVersionConfig", "Commit", "log_print")
    try:
        config = WeeklyVersionConfig.query.get_or_404(config_id)
        file_path = request.args.get("file_path")
        commit_id = request.args.get("commit_id")
        if not file_path or not commit_id:
            return "缺少文件路径或提交ID参数", 400

        from services.threaded_git_service import ThreadedGitService

        git_service = ThreadedGitService(
            config.repository.url,
            config.repository.root_directory,
            config.repository.username,
            config.repository.token,
            config.repository,
        )
        file_content = git_service.get_file_content(commit_id, file_path)
        if file_content is None:
            # 仓库本地状态可能滞后，先尝试更新后再取一次
            try:
                sync_ok, _ = git_service.clone_or_update_repository()
                if sync_ok:
                    file_content = git_service.get_file_content(commit_id, file_path)
            except Exception:
                pass

        if file_content is None:
            lower_path = file_path.lower()
            is_excel_file = lower_path.endswith((".xlsx", ".xls", ".xlsm", ".xlsb"))
            if is_excel_file:
                # Excel 删除场景优先跳转到提交Diff页面，避免文本预览接口无法解码二进制内容
                commit_query = Commit.query.filter(
                    Commit.repository_id == config.repository_id,
                    Commit.path == file_path,
                )
                if len(commit_id) >= 40:
                    previous_commit = commit_query.filter(Commit.commit_id == commit_id).first()
                else:
                    previous_commit = commit_query.filter(Commit.commit_id.like(f"{commit_id}%")).first()

                if previous_commit:
                    try:
                        diff_url = url_for(
                            "commit_diff_with_path",
                            project_code=config.project.code,
                            repository_name=config.repository.name,
                            commit_id=previous_commit.id,
                        )
                    except Exception:
                        diff_url = url_for("commit_diff", commit_id=previous_commit.id)
                    return redirect(diff_url)

            return render_template(
                "error.html",
                error_message="无法获取文件内容，文件可能不存在",
                back_url=url_for(
                    "weekly_version_file_full_diff",
                    config_id=config_id,
                    file_path=file_path,
                ),
            )

        commit_info = git_service.get_commit_info(commit_id)
        return render_template(
            "weekly_version_previous_file.html",
            config=config,
            file_path=file_path,
            commit_id=commit_id,
            commit_info=commit_info,
            file_content=file_content,
        )
    except Exception as exc:
        log_print(f"查看上一版本文件失败: {exc}", "ERROR", force=True)
        return render_template(
            "error.html",
            error_message=f"加载失败: {str(exc)}",
            back_url=url_for("weekly_version_diff", config_id=config_id),
        )


def weekly_version_file_complete_diff(config_id):
    """Weekly-version file complete diff page."""
    WeeklyVersionConfig, WeeklyVersionDiffCache, log_print = get_runtime_models(
        "WeeklyVersionConfig",
        "WeeklyVersionDiffCache",
        "log_print",
    )
    from services.diff_render_helpers import generate_side_by_side_diff

    try:
        config = WeeklyVersionConfig.query.get_or_404(config_id)
        file_path = request.args.get("file_path")
        if not file_path:
            return "缺少文件路径参数", 400

        diff_cache = WeeklyVersionDiffCache.query.filter_by(
            config_id=config_id,
            file_path=file_path,
        ).first()
        if not diff_cache:
            return render_template(
                "error.html",
                error_message="未找到该文件的diff数据",
                back_url=url_for("weekly_version_diff", config_id=config_id),
            )

        commit_authors = json.loads(diff_cache.commit_authors) if diff_cache.commit_authors else []
        commit_messages = json.loads(diff_cache.commit_messages) if diff_cache.commit_messages else []
        commit_times = json.loads(diff_cache.commit_times) if diff_cache.commit_times else []

        repository = config.repository
        previous_file_content = ""
        if diff_cache.base_commit_id:
            try:
                previous_file_content = get_file_content_at_commit(
                    repository,
                    diff_cache.base_commit_id,
                    file_path,
                )
            except Exception as exc:
                log_print(f"获取基准版本文件内容失败: {exc}", "ERROR")
                previous_file_content = ""

        current_file_content = ""
        if diff_cache.latest_commit_id:
            try:
                current_file_content = get_file_content_at_commit(
                    repository,
                    diff_cache.latest_commit_id,
                    file_path,
                )
            except Exception as exc:
                log_print(f"获取当前版本文件内容失败: {exc}", "ERROR")
                current_file_content = ""

        base_commit_info = None
        if diff_cache.base_commit_id:
            base_commit_info = {
                "short_id": diff_cache.base_commit_id[:8],
                "author": "基准版本",
                "commit_time": config.start_time.strftime("%Y-%m-%d %H:%M"),
                "message": "周版本基准",
            }

        side_by_side_diff = generate_side_by_side_diff(current_file_content, previous_file_content)
        return render_template(
            "weekly_version_complete_diff.html",
            config=config,
            diff_cache=diff_cache,
            file_path=file_path,
            commit_authors=commit_authors,
            commit_messages=commit_messages,
            commit_times=commit_times,
            base_commit_info=base_commit_info,
            base_commit_id=diff_cache.base_commit_id,
            latest_commit_id=diff_cache.latest_commit_id,
            previous_file_content=previous_file_content,
            current_file_content=current_file_content,
            side_by_side_diff=side_by_side_diff,
        )
    except Exception as exc:
        log_print(f"获取周版本完整文件对比失败: {exc}", "ERROR", force=True)
        return render_template(
            "error.html",
            error_message=f"加载完整文件对比失败: {str(exc)}",
            back_url=url_for("weekly_version_diff", config_id=config_id),
        )


def weekly_version_file_status_api(config_id):
    """Update weekly-version file confirmation status."""
    db, WeeklyVersionConfig, WeeklyVersionDiffCache, log_print = get_runtime_models(
        "db",
        "WeeklyVersionConfig",
        "WeeklyVersionDiffCache",
        "log_print",
    )
    try:
        WeeklyVersionConfig.query.get_or_404(config_id)
        data = request.get_json() or {}
        file_path = data.get("file_path")
        status = data.get("status")
        if not file_path or not status:
            return jsonify({"success": False, "message": "缺少必需参数"}), 400

        diff_cache = WeeklyVersionDiffCache.query.filter_by(
            config_id=config_id,
            file_path=file_path,
        ).first()
        if not diff_cache:
            return jsonify({"success": False, "message": "未找到文件记录"}), 404

        old_status = diff_cache.overall_status
        confirmation_status = json.loads(diff_cache.confirmation_status) if diff_cache.confirmation_status else {}
        confirmation_status["dev"] = status
        diff_cache.confirmation_status = json.dumps(confirmation_status)
        diff_cache.overall_status = status
        diff_cache.updated_at = datetime.now(timezone.utc)
        # 记录操作者用户名
        from utils.request_security import _get_current_user
        current_user = _get_current_user()
        if status in ('confirmed', 'rejected'):
            diff_cache.status_changed_by = current_user.username if current_user else None
        elif status == 'pending':
            diff_cache.status_changed_by = None
        db.session.commit()

        if old_status != status:
            from services.status_sync_service import StatusSyncService

            sync_service = StatusSyncService(db)
            sync_result = sync_service.sync_weekly_to_commit(config_id, file_path, status)
            log_print(f"周版本状态同步结果: {sync_result}", "SYNC")

        return jsonify({
            "success": True,
            "message": "状态更新成功",
            "status_changed_by": diff_cache.status_changed_by
        })
    except Exception as exc:
        db.session.rollback()
        log_print(f"更新文件状态失败: {exc}", "ERROR", force=True)
        return jsonify({"success": False, "message": str(exc)}), 500


def weekly_version_file_status_info_api(config_id):
    """Get weekly-version file confirmation status info."""
    WeeklyVersionConfig, WeeklyVersionDiffCache, log_print = get_runtime_models(
        "WeeklyVersionConfig",
        "WeeklyVersionDiffCache",
        "log_print",
    )
    try:
        WeeklyVersionConfig.query.get_or_404(config_id)
        file_path = request.args.get("file_path")
        if not file_path:
            return jsonify({"success": False, "message": "缺少文件路径参数"}), 400

        diff_cache = WeeklyVersionDiffCache.query.filter_by(
            config_id=config_id,
            file_path=file_path,
        ).first()
        if not diff_cache:
            return jsonify({"success": False, "message": "未找到文件记录"}), 404

        return jsonify(
            {
                "success": True,
                "status": diff_cache.overall_status or "pending",
                "file_path": file_path,
            }
        )
    except Exception as exc:
        log_print(f"获取周版本文件状态失败: {exc}", "ERROR", force=True)
        return jsonify({"success": False, "message": f"获取失败: {str(exc)}"}), 500


def weekly_version_stats_api(config_id):
    """Get weekly-version config status stats."""
    WeeklyVersionConfig, WeeklyVersionDiffCache, log_print = get_runtime_models(
        "WeeklyVersionConfig",
        "WeeklyVersionDiffCache",
        "log_print",
    )
    try:
        WeeklyVersionConfig.query.get_or_404(config_id)
        total_files = WeeklyVersionDiffCache.query.filter_by(config_id=config_id).count()
        pending_count = WeeklyVersionDiffCache.query.filter_by(config_id=config_id, overall_status="pending").count()
        confirmed_count = WeeklyVersionDiffCache.query.filter_by(
            config_id=config_id,
            overall_status="confirmed",
        ).count()
        rejected_count = WeeklyVersionDiffCache.query.filter_by(config_id=config_id, overall_status="rejected").count()

        return jsonify(
            {
                "success": True,
                "stats": {
                    "total_files": total_files,
                    "pending_count": pending_count,
                    "confirmed_count": confirmed_count,
                    "rejected_count": rejected_count,
                },
            }
        )
    except Exception as exc:
        log_print(f"获取周版本统计信息失败: {exc}", "ERROR", force=True)
        return jsonify({"success": False, "message": str(exc)}), 500


def get_file_content_at_commit(repository, commit_id, file_path):
    """Get file content at specific commit."""
    log_print = get_runtime_models("log_print")[0]
    try:
        from services.git_service import GitService

        git_service = GitService(
            repo_url=repository.url,
            root_directory=repository.root_directory,
            username=repository.username,
            token=repository.token,
            repository=repository,
        )
        return git_service.get_file_content(commit_id, file_path)
    except Exception as exc:
        log_print(f"获取文件内容失败: {exc}", "ERROR")
        return ""
