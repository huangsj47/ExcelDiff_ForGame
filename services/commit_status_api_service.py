"""Commit status API handlers extracted from app.py."""

from __future__ import annotations


def handle_update_commit_status(
    *,
    commit_id,
    request,
    jsonify,
    db,
    Commit,
    NotFound,
    SQLAlchemyError,
    app_logger,
    ensure_commit_access_or_403,
    can_operate_project_confirmation,
    get_current_user,
    status_sync_service_cls,
    log_print,
):
    """Handle API: update single commit status."""
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
        status = data.get("status")
        if not status:
            action = (data.get("action") or request.form.get("action") or request.form.get("status") or "").strip()
            action_to_status = {
                "confirm": "confirmed",
                "confirmed": "confirmed",
                "approve": "confirmed",
                "reject": "rejected",
                "rejected": "rejected",
                "pending": "pending",
                "reviewed": "reviewed",
            }
            status = action_to_status.get(action, action)
        if status not in ["pending", "reviewed", "confirmed", "rejected"]:
            return jsonify({"status": "error", "message": "无效的状态值"}), 400

        commit = Commit.query.get_or_404(commit_id)
        ensure_commit_access_or_403(commit)
        if status in ("confirmed", "rejected"):
            project_id = commit.repository.project_id if commit.repository else None
            action = "confirm" if status == "confirmed" else "reject"
            allowed, permission_message = can_operate_project_confirmation(project_id, action)
            if not allowed:
                return jsonify({"status": "error", "message": permission_message}), 403

        old_status = commit.status
        commit.status = status
        current_user = get_current_user()
        if status in ("confirmed", "rejected"):
            commit.status_changed_by = current_user.username if current_user else None
        elif status == "pending":
            commit.status_changed_by = None
        db.session.commit()

        if old_status != status:
            sync_service = status_sync_service_cls(db)
            sync_result = sync_service.sync_commit_to_weekly(commit_id, status)
            log_print(f"提交状态同步结果: {sync_result}", "SYNC")
        return jsonify(
            {
                "success": True,
                "message": "状态更新成功",
                "status_changed_by": commit.status_changed_by,
            }
        )
    except NotFound:
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "提交记录不存在",
                    "error_type": "commit_not_found",
                }
            ),
            404,
        )
    except SQLAlchemyError as exc:
        db.session.rollback()
        app_logger.error(f"更新提交状态数据库失败: {str(exc)}")
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "数据库操作失败，请稍后重试",
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
    except RuntimeError as exc:
        app_logger.error(f"更新提交状态运行时异常: {str(exc)}")
        return (
            jsonify(
                {
                    "status": "error",
                    "message": str(exc),
                    "error_type": "runtime_error",
                }
            ),
            500,
        )


def handle_batch_update_commits_compat(
    *,
    request,
    jsonify,
    db,
    Commit,
    SQLAlchemyError,
    log_print,
    status_sync_service_cls,
    get_current_user,
    can_operate_project_confirmation,
):
    """Handle API: batch commit status update (compat endpoint)."""
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
        commit_ids = data.get("commit_ids") or data.get("ids") or request.form.getlist("ids")
        action = (data.get("action") or request.form.get("action") or "").strip().lower()
        if not commit_ids:
            return jsonify({"status": "error", "message": "未选择任何提交"}), 400

        normalized_ids = []
        for raw_id in commit_ids:
            try:
                normalized_ids.append(int(raw_id))
            except (TypeError, ValueError):
                continue

        if not normalized_ids:
            return jsonify({"status": "error", "message": "提交ID无效"}), 400

        if action in {"confirm", "confirmed", "approve"}:
            target_status = "confirmed"
        elif action in {"reject", "rejected"}:
            target_status = "rejected"
        else:
            return jsonify({"status": "error", "message": "不支持的批量操作"}), 400

        sync_service = status_sync_service_cls(db)
        current_user = get_current_user()
        updated_count = 0
        sync_results = []
        permission_cache = {}
        for commit_id in normalized_ids:
            commit = db.session.get(Commit, commit_id)
            if commit and commit.status != target_status:
                project_id = commit.repository.project_id if commit.repository else None
                action_name = "confirm" if target_status == "confirmed" else "reject"
                if project_id not in permission_cache:
                    permission_cache[project_id] = can_operate_project_confirmation(project_id, action_name)
                allowed, permission_message = permission_cache[project_id]
                if not allowed:
                    return jsonify({"status": "error", "message": permission_message}), 403
                commit.status = target_status
                commit.status_changed_by = current_user.username if current_user else None
                updated_count += 1
                sync_results.append(sync_service.sync_commit_to_weekly(commit_id, target_status))
        db.session.commit()
        total_weekly_updated = sum(r.get("updated_count", 0) for r in sync_results if r.get("success"))
        return jsonify(
            {
                "status": "success",
                "message": f"已更新 {updated_count} 个提交，同步更新 {total_weekly_updated} 个周版本记录",
                "updated_count": updated_count,
            }
        )
    except SQLAlchemyError as exc:
        db.session.rollback()
        log_print(f"批量更新提交失败: {str(exc)}", "APP", force=True)
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "数据库操作失败，请稍后重试",
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
