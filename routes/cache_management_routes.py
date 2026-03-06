#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cache management routes extracted from app.py."""

from datetime import datetime, timezone

from flask import Blueprint, jsonify, render_template, request
from sqlalchemy import case, func, text

from services.model_loader import get_runtime_models
from utils.request_security import require_admin


cache_management_bp = Blueprint("cache_management", __name__)


@cache_management_bp.route("/admin/excel-cache/cleanup-expired", methods=["POST"])
@require_admin
def cleanup_expired_cache():
    """Manual cleanup for expired cache records."""
    try:
        db, AgentTempCache, excel_cache_service, excel_html_cache_service, weekly_excel_cache_service = get_runtime_models(
            "db",
            "AgentTempCache",
            "excel_cache_service",
            "excel_html_cache_service",
            "weekly_excel_cache_service",
        )

        expired_count = excel_cache_service.cleanup_expired_cache()
        old_count = excel_cache_service._cleanup_old_cache()
        html_expired_count = excel_html_cache_service.cleanup_expired_cache()
        weekly_excel_expired_count = weekly_excel_cache_service.cleanup_expired_cache()
        weekly_excel_old_count = weekly_excel_cache_service.cleanup_old_cache()
        now_utc = datetime.now(timezone.utc)
        agent_temp_expired_count = (
            AgentTempCache.query.filter(
                AgentTempCache.expire_at.isnot(None),
                AgentTempCache.expire_at < now_utc,
            ).delete(synchronize_session=False)
        )
        db.session.commit()

        return jsonify(
            {
                "success": True,
                "message": "清理完成",
                "expired_count": expired_count,
                "old_count": old_count,
                "html_expired_count": html_expired_count,
                "weekly_excel_expired_count": weekly_excel_expired_count,
                "weekly_excel_old_count": weekly_excel_old_count,
                "agent_temp_expired_count": int(agent_temp_expired_count or 0),
            }
        )
    except Exception as exc:
        return jsonify({"success": False, "message": f"清理失败: {str(exc)}"}), 500


@cache_management_bp.route("/admin/excel-cache/clear-all-diff-cache", methods=["POST"])
@require_admin
def clear_all_diff_cache():
    """Clear all Excel diff cache records."""
    db, DiffCache, BackgroundTask, AgentTempCache, log_print = get_runtime_models(
        "db",
        "DiffCache",
        "BackgroundTask",
        "AgentTempCache",
        "log_print",
    )
    try:
        log_print("🧹 开始清理Excel差异数据缓存...", "INFO")
        total_diff_cache_count = DiffCache.query.delete(synchronize_session=False)
        task_count = BackgroundTask.query.filter_by(task_type="excel_diff").delete(synchronize_session=False)
        agent_temp_count = AgentTempCache.query.delete(synchronize_session=False)
        db.session.commit()

        log_print(
            (
                f"🧹 清理完成：{total_diff_cache_count} 条Excel差异数据缓存，"
                f"{agent_temp_count} 条Agent临时缓存，{task_count} 条相关后台任务"
            ),
            "INFO",
        )

        return jsonify(
            {
                "success": True,
                "message": (
                    f"已清理 {total_diff_cache_count} 条Excel差异数据缓存、"
                    f"{agent_temp_count} 条Agent临时缓存和 {task_count} 条相关任务"
                ),
                "diff_cache_count": total_diff_cache_count,
                "agent_temp_count": int(agent_temp_count or 0),
                "task_count": task_count,
            }
        )
    except Exception as exc:
        db.session.rollback()
        log_print(f"清理Excel差异数据缓存失败: {exc}", "INFO", force=True)
        return jsonify({"success": False, "message": f"清理失败: {str(exc)}"}), 500


@cache_management_bp.route("/admin/excel-cache/clear-project-cache", methods=["POST"])
@require_admin
def clear_project_cache():
    """清理指定项目的所有缓存数据"""
    db, DiffCache, ExcelHtmlCache, AgentTempCache, Repository, BackgroundTask, log_print = get_runtime_models(
        "db", "DiffCache", "ExcelHtmlCache", "AgentTempCache", "Repository", "BackgroundTask", "log_print",
    )
    try:
        data = request.get_json() or {}
        project_id = data.get("project_id")
        if not project_id:
            return jsonify({"success": False, "message": "缺少 project_id 参数"}), 400

        # 获取该项目下所有仓库 ID
        repo_ids = [r.id for r in Repository.query.filter_by(project_id=project_id).all()]
        if not repo_ids:
            return jsonify({"success": True, "message": "该项目下没有仓库，无需清理", "diff_count": 0, "html_count": 0, "task_count": 0})

        # 批量删除 DiffCache
        diff_count = DiffCache.query.filter(DiffCache.repository_id.in_(repo_ids)).delete(synchronize_session=False)
        # 批量删除 ExcelHtmlCache
        html_count = ExcelHtmlCache.query.filter(ExcelHtmlCache.repository_id.in_(repo_ids)).delete(synchronize_session=False)
        # 批量删除相关后台任务
        task_count = BackgroundTask.query.filter(
            BackgroundTask.repository_id.in_(repo_ids),
            BackgroundTask.task_type.in_(["excel_diff", "auto_sync"])
        ).delete(synchronize_session=False)
        # 清理平台侧 Agent 临时缓存（按项目或仓库维度）
        agent_temp_count = AgentTempCache.query.filter(
            (AgentTempCache.project_id == project_id)
            | (AgentTempCache.repository_id.in_(repo_ids))
        ).delete(synchronize_session=False)

        # 尝试清理周版本Excel缓存
        weekly_count = 0
        try:
            WeeklyVersionExcelCache = get_runtime_models("WeeklyVersionExcelCache")[0]
            from models.weekly_version import WeeklyVersionConfig
            config_ids = [c.id for c in WeeklyVersionConfig.query.filter_by(project_id=project_id).all()]
            if config_ids:
                weekly_count = WeeklyVersionExcelCache.query.filter(
                    WeeklyVersionExcelCache.config_id.in_(config_ids)
                ).delete(synchronize_session=False)
        except Exception:
            pass

        db.session.commit()

        log_print(
            (
                f"🧹 项目 {project_id} 缓存清理完成: {diff_count} 条Diff缓存, "
                f"{html_count} 条HTML缓存, {weekly_count} 条周版本缓存, "
                f"{agent_temp_count} 条Agent临时缓存, {task_count} 条任务"
            ),
            "INFO",
        )
        return jsonify({
            "success": True,
            "message": (
                f"已清理项目缓存: {diff_count} 条Diff + {html_count} 条HTML + "
                f"{weekly_count} 条周版本 + {agent_temp_count} 条Agent临时缓存 + {task_count} 条任务"
            ),
            "diff_count": diff_count,
            "html_count": html_count,
            "weekly_count": weekly_count,
            "agent_temp_count": int(agent_temp_count or 0),
            "task_count": task_count,
        })
    except Exception as exc:
        db.session.rollback()
        log_print(f"清理项目缓存失败: {exc}", "INFO", force=True)
        return jsonify({"success": False, "message": f"清理失败: {str(exc)}"}), 500


@cache_management_bp.route("/admin/excel-cache/strategy-info", methods=["GET"])
def get_cache_strategy_info():
    """Get cache strategy summary."""
    try:
        excel_cache_service, weekly_excel_cache_service, DIFF_LOGIC_VERSION = get_runtime_models(
            "excel_cache_service",
            "weekly_excel_cache_service",
            "DIFF_LOGIC_VERSION",
        )
        strategy_info = {
            "max_cache_count": excel_cache_service.max_cache_count,
            "long_processing_threshold": excel_cache_service.long_processing_threshold,
            "long_processing_expire_days": excel_cache_service.long_processing_expire_days,
            "html_cache_expire_days": 30,
            "weekly_excel_max_cache_count": weekly_excel_cache_service.max_cache_count,
            "weekly_excel_expire_days": weekly_excel_cache_service.expire_days,
            "current_diff_version": DIFF_LOGIC_VERSION,
        }

        return jsonify({"success": True, "strategy": strategy_info})
    except Exception as exc:
        return jsonify({"success": False, "message": f"获取策略信息失败: {str(exc)}"}), 500


@cache_management_bp.route("/api/excel-cache/logs")
def get_excel_cache_logs():
    """获取Excel缓存操作日志"""
    try:
        OperationLog, Repository, Project, db = get_runtime_models(
            "OperationLog",
            "Repository",
            "Project",
            "db",
        )

        page = max(1, request.args.get("page", 1, type=int) or 1)
        per_page = request.args.get("per_page", 10, type=int) or 10

        per_page = min(max(per_page, 1), 50)
        max_total = 200

        logs_query = OperationLog.query.filter_by(source="excel_cache")
        total_logs_raw = logs_query.count()
        total_logs = min(total_logs_raw, max_total)
        offset = (page - 1) * per_page

        if offset >= total_logs:
            paginated_logs_db = []
        else:
            fetch_size = min(per_page, total_logs - offset)
            paginated_logs_db = (
                logs_query
                .order_by(OperationLog.created_at.desc())
                .offset(offset)
                .limit(fetch_size)
                .all()
            )

        from utils.timezone_utils import format_beijing_time

        repository_ids = {log.repository_id for log in paginated_logs_db if getattr(log, "repository_id", None)}
        repository_project_code_map = {}
        if repository_ids:
            rows = (
                db.session.query(Repository.id, Project.code)
                .join(Project, Project.id == Repository.project_id)
                .filter(Repository.id.in_(list(repository_ids)))
                .all()
            )
            repository_project_code_map = {repo_id: code for repo_id, code in rows}

        logs = []
        for log in paginated_logs_db:
            message = log.message or ""
            if not str(message).startswith("【"):
                project_code = repository_project_code_map.get(getattr(log, "repository_id", None)) or "UNKNOWN"
                message = f"【{project_code}】{message}"
            logs.append(
                {
                    "time": format_beijing_time(log.created_at, "%Y/%m/%d %H:%M:%S"),
                    "message": message,
                    "type": log.log_type,
                }
            )

        total_pages = (total_logs + per_page - 1) // per_page

        return jsonify(
            {
                "success": True,
                "logs": logs,
                "pagination": {
                    "page": page,
                    "per_page": per_page,
                    "total": total_logs,
                    "total_pages": total_pages,
                    "has_next": page < total_pages,
                    "has_prev": page > 1,
                },
            }
        )
    except Exception as exc:
        return jsonify({"success": False, "message": f"获取日志失败: {str(exc)}"}), 500


@cache_management_bp.route("/api/excel-html-cache/clear")
def clear_excel_html_cache():
    """清理Excel HTML缓存（版本检查）"""
    db, excel_html_cache_service, log_print = get_runtime_models(
        "db",
        "excel_html_cache_service",
        "log_print",
    )
    try:
        _repository_id = request.args.get("repository_id", type=int)
        force_all = request.args.get("force_all", "false").lower() == "true"

        if force_all:
            result = db.session.execute(text("DELETE FROM excel_html_cache"))
            count = int(result.rowcount or 0)
            db.session.commit()
            log_print(f"🧹 强制清理了所有 {count} 个HTML缓存", "INFO")
        else:
            count = excel_html_cache_service.cleanup_old_version_cache()

        return jsonify(
            {
                "success": True,
                "message": f"清理了 {count} 个{'所有' if force_all else '旧版本'}HTML缓存",
                "cleared_count": count,
            }
        )
    except Exception as exc:
        log_print(f"❌ 清理HTML缓存失败: {exc}", "INFO")
        return jsonify({"success": False, "message": f"清理失败: {str(exc)}"})


@cache_management_bp.route("/api/excel-html-cache/regenerate")
def regenerate_excel_html_cache():
    """重新生成Excel HTML缓存"""
    db, Commit, ExcelHtmlCache, get_excel_diff_data, log_print = get_runtime_models(
        "db",
        "Commit",
        "ExcelHtmlCache",
        "get_excel_diff_data",
        "log_print",
    )
    try:
        repository_id = request.args.get("repository_id", type=int)
        commit_id = request.args.get("commit_id")
        file_path = request.args.get("file_path")

        if not all([repository_id, commit_id, file_path]):
            return jsonify({"success": False, "message": "缺少必要参数"})

        existing_cache = ExcelHtmlCache.query.filter_by(
            repository_id=repository_id,
            commit_id=commit_id,
            file_path=file_path,
        ).first()

        if existing_cache:
            db.session.delete(existing_cache)
            db.session.commit()
            log_print(f"🗑️ 删除现有HTML缓存: {file_path}", "INFO")

        commit = Commit.query.filter_by(
            repository_id=repository_id,
            commit_id=commit_id,
            path=file_path,
        ).first()

        if commit:
            get_excel_diff_data(commit.id)
            return jsonify(
                {
                    "success": True,
                    "message": f"HTML缓存重新生成完成: {file_path}",
                    "regenerated": True,
                }
            )

        return jsonify({"success": False, "message": "找不到对应的提交记录"})
    except Exception as exc:
        log_print(f"❌ 重新生成HTML缓存失败: {exc}", "INFO")
        return jsonify({"success": False, "message": f"重新生成失败: {str(exc)}"})


@cache_management_bp.route("/api/excel-diff-status/<cache_key>")
def excel_diff_status(cache_key):
    """旧版状态接口（已废弃）"""
    try:
        return (
            jsonify(
                {
                    "status": "deprecated",
                    "message": "该接口已废弃，请改用 /commits/<commit_id>/diff-data 与缓存管理接口查询状态。",
                    "cache_key": cache_key,
                }
            ),
            410,
        )
    except Exception as exc:
        (log_print,) = get_runtime_models("log_print")
        log_print(f"检查Excel diff状态失败: {str(exc)}")
        return jsonify({"status": "error", "error": str(exc)}), 500


@cache_management_bp.route("/api/excel-html-cache/stats", methods=["GET"])
def get_excel_html_cache_stats():
    """获取Excel HTML缓存统计信息"""
    (
        db,
        AgentTempCache,
        excel_cache_service,
        excel_html_cache_service,
        DIFF_LOGIC_VERSION,
        log_print,
    ) = get_runtime_models(
        "db",
        "AgentTempCache",
        "excel_cache_service",
        "excel_html_cache_service",
        "DIFF_LOGIC_VERSION",
        "log_print",
    )
    try:
        data_cache_stats = excel_cache_service.get_cache_statistics()
        html_cache_stats = excel_html_cache_service.get_cache_statistics()
        now_utc = datetime.now(timezone.utc)
        agent_row = (
            db.session.query(
                func.count(AgentTempCache.id).label("total_count"),
                func.sum(func.coalesce(AgentTempCache.payload_size, 0)).label("total_size"),
                func.sum(
                    case(
                        (
                            (AgentTempCache.expire_at.isnot(None))
                            & (AgentTempCache.expire_at < now_utc),
                            1,
                        ),
                        else_=0,
                    )
                ).label("expired_count"),
            )
            .first()
        )
        agent_total = int((agent_row.total_count if agent_row else 0) or 0)
        agent_total_size = int((agent_row.total_size if agent_row else 0) or 0)
        agent_expired = int((agent_row.expired_count if agent_row else 0) or 0)

        return jsonify(
            {
                "success": True,
                "data_cache": data_cache_stats,
                "html_cache": html_cache_stats,
                "agent_temp_cache": {
                    "total_count": agent_total,
                    "expired_count": agent_expired,
                    "active_count": max(agent_total - agent_expired, 0),
                    "total_size_bytes": agent_total_size,
                    "total_size_mb": round(agent_total_size / (1024 * 1024), 2),
                },
                "strategy": {
                    "current_version": DIFF_LOGIC_VERSION,
                    "cache_limit": 1000,
                    "long_processing_threshold": 10.0,
                    "long_processing_expire_days": 90,
                    "html_cache_expire_days": 30,
                },
            }
        )
    except Exception as exc:
        log_print(f"❌ 获取HTML缓存统计失败: {exc}", "CACHE", force=True)
        return jsonify({"success": False, "message": f"获取统计失败: {str(exc)}"}), 500


@cache_management_bp.route("/api/excel-cache/stats-by-project", methods=["GET"])
def get_excel_cache_stats_by_project():
    """获取按项目分组的Excel缓存统计信息"""
    (
        db,
        Project,
        Repository,
        DiffCache,
        ExcelHtmlCache,
        WeeklyVersionExcelCache,
        AgentTempCache,
        excel_html_cache_service,
        weekly_excel_cache_service,
        DIFF_LOGIC_VERSION,
        log_print,
    ) = get_runtime_models(
        "db",
        "Project",
        "Repository",
        "DiffCache",
        "ExcelHtmlCache",
        "WeeklyVersionExcelCache",
        "AgentTempCache",
        "excel_html_cache_service",
        "weekly_excel_cache_service",
        "DIFF_LOGIC_VERSION",
        "log_print",
    )
    try:
        projects = Project.query.order_by(Project.id.asc()).all()
        project_stats = []

        repo_counts = (
            db.session.query(
                Repository.project_id,
                func.count(Repository.id).label("repository_count"),
            )
            .group_by(Repository.project_id)
            .all()
        )
        repo_count_map = {pid: int(cnt or 0) for pid, cnt in repo_counts}

        diff_rows = (
            db.session.query(
                Repository.project_id.label("project_id"),
                func.count(DiffCache.id).label("total_count"),
                func.sum(case((DiffCache.cache_status == "completed", 1), else_=0)).label("completed_count"),
                func.sum(case((DiffCache.cache_status == "processing", 1), else_=0)).label("processing_count"),
                func.sum(case((DiffCache.cache_status == "failed", 1), else_=0)).label("failed_count"),
                func.sum(case((DiffCache.cache_status == "outdated", 1), else_=0)).label("outdated_count"),
                func.sum(
                    case(
                        (
                            (DiffCache.cache_status == "completed")
                            & (DiffCache.diff_version == DIFF_LOGIC_VERSION),
                            1,
                        ),
                        else_=0,
                    )
                ).label("current_version_count"),
                func.sum(
                    case(
                        ((DiffCache.cache_status == "completed") & (DiffCache.is_long_processing.is_(True)), 1),
                        else_=0,
                    )
                ).label("long_processing_count"),
            )
            .join(Repository, Repository.id == DiffCache.repository_id)
            .group_by(Repository.project_id)
            .all()
        )
        diff_map = {}
        for row in diff_rows:
            total_count = int(row.total_count or 0)
            completed_count = int(row.completed_count or 0)
            long_processing_count = int(row.long_processing_count or 0)
            diff_map[row.project_id] = {
                "total_count": total_count,
                "completed_count": completed_count,
                "processing_count": int(row.processing_count or 0),
                "failed_count": int(row.failed_count or 0),
                "outdated_count": int(row.outdated_count or 0),
                "current_version_count": int(row.current_version_count or 0),
                "long_processing_count": long_processing_count,
                "normal_processing_count": max(completed_count - long_processing_count, 0),
                "version": DIFF_LOGIC_VERSION,
            }

        html_rows = (
            db.session.query(
                Repository.project_id.label("project_id"),
                func.count(ExcelHtmlCache.id).label("total_count"),
                func.sum(case((ExcelHtmlCache.cache_status == "completed", 1), else_=0)).label("completed_count"),
                func.sum(case((ExcelHtmlCache.diff_version == excel_html_cache_service.current_version, 1), else_=0)).label(
                    "current_version_count"
                ),
                func.sum(
                    func.length(func.coalesce(ExcelHtmlCache.html_content, ""))
                    + func.length(func.coalesce(ExcelHtmlCache.css_content, ""))
                    + func.length(func.coalesce(ExcelHtmlCache.js_content, ""))
                ).label("total_size_bytes"),
            )
            .join(Repository, Repository.id == ExcelHtmlCache.repository_id)
            .group_by(Repository.project_id)
            .all()
        )
        html_map = {}
        for row in html_rows:
            total_count = int(row.total_count or 0)
            current_version_count = int(row.current_version_count or 0)
            total_size_bytes = int(row.total_size_bytes or 0)
            html_map[row.project_id] = {
                "total_count": total_count,
                "completed_count": int(row.completed_count or 0),
                "current_version_count": current_version_count,
                "old_version_count": max(total_count - current_version_count, 0),
                "total_size_mb": round(total_size_bytes / (1024 * 1024), 2),
                "current_version": excel_html_cache_service.current_version,
            }

        weekly_rows = (
            db.session.query(
                Repository.project_id.label("project_id"),
                func.count(WeeklyVersionExcelCache.id).label("total_count"),
                func.sum(case((WeeklyVersionExcelCache.cache_status == "completed", 1), else_=0)).label(
                    "completed_count"
                ),
                func.sum(case((WeeklyVersionExcelCache.cache_status == "processing", 1), else_=0)).label(
                    "processing_count"
                ),
                func.sum(case((WeeklyVersionExcelCache.cache_status == "failed", 1), else_=0)).label("failed_count"),
                func.sum(func.length(func.coalesce(WeeklyVersionExcelCache.html_content, ""))).label("total_size"),
            )
            .join(Repository, Repository.id == WeeklyVersionExcelCache.repository_id)
            .group_by(Repository.project_id)
            .all()
        )
        weekly_map = {}
        for row in weekly_rows:
            weekly_map[row.project_id] = {
                "total_count": int(row.total_count or 0),
                "completed_count": int(row.completed_count or 0),
                "processing_count": int(row.processing_count or 0),
                "failed_count": int(row.failed_count or 0),
                "total_size": int(row.total_size or 0),
                "max_cache_count": weekly_excel_cache_service.max_cache_count,
                "expire_days": weekly_excel_cache_service.expire_days,
            }

        now_utc = datetime.now(timezone.utc)
        agent_rows = (
            db.session.query(
                AgentTempCache.project_id.label("project_id"),
                func.count(AgentTempCache.id).label("total_count"),
                func.sum(func.coalesce(AgentTempCache.payload_size, 0)).label("total_size"),
                func.sum(
                    case(
                        (
                            (AgentTempCache.expire_at.isnot(None))
                            & (AgentTempCache.expire_at < now_utc),
                            1,
                        ),
                        else_=0,
                    )
                ).label("expired_count"),
            )
            .group_by(AgentTempCache.project_id)
            .all()
        )
        agent_map = {}
        for row in agent_rows:
            project_id = int(row.project_id) if row.project_id else None
            if not project_id:
                continue
            total_count = int(row.total_count or 0)
            expired_count = int(row.expired_count or 0)
            total_size_bytes = int(row.total_size or 0)
            agent_map[project_id] = {
                "total_count": total_count,
                "expired_count": expired_count,
                "active_count": max(total_count - expired_count, 0),
                "total_size_bytes": total_size_bytes,
                "total_size_mb": round(total_size_bytes / (1024 * 1024), 2),
            }

        for project in projects:
            pid = project.id
            data_cache_stats = diff_map.get(
                pid,
                {
                    "total_count": 0,
                    "completed_count": 0,
                    "processing_count": 0,
                    "failed_count": 0,
                    "outdated_count": 0,
                    "current_version_count": 0,
                    "long_processing_count": 0,
                    "normal_processing_count": 0,
                    "version": DIFF_LOGIC_VERSION,
                },
            )
            html_cache_stats = html_map.get(
                pid,
                {
                    "total_count": 0,
                    "completed_count": 0,
                    "current_version_count": 0,
                    "old_version_count": 0,
                    "total_size_mb": 0.0,
                    "current_version": excel_html_cache_service.current_version,
                },
            )
            weekly_excel_cache_stats = weekly_map.get(
                pid,
                {
                    "total_count": 0,
                    "completed_count": 0,
                    "processing_count": 0,
                    "failed_count": 0,
                    "total_size": 0,
                    "max_cache_count": weekly_excel_cache_service.max_cache_count,
                    "expire_days": weekly_excel_cache_service.expire_days,
                },
            )
            agent_temp_cache_stats = agent_map.get(
                pid,
                {
                    "total_count": 0,
                    "expired_count": 0,
                    "active_count": 0,
                    "total_size_bytes": 0,
                    "total_size_mb": 0.0,
                },
            )

            project_stats.append(
                {
                    "project": {
                        "id": pid,
                        "code": project.code,
                        "name": project.name,
                        "repository_count": repo_count_map.get(pid, 0),
                    },
                    "data_cache": data_cache_stats,
                    "html_cache": html_cache_stats,
                    "weekly_excel_cache": weekly_excel_cache_stats,
                    "agent_temp_cache": agent_temp_cache_stats,
                }
            )

        return jsonify({"success": True, "projects": project_stats, "total_projects": len(projects)})
    except Exception as exc:
        log_print(f"❌ 获取项目缓存统计失败: {exc}", "CACHE", force=True)
        return jsonify({"success": False, "message": f"获取项目统计失败: {str(exc)}"}), 500


@cache_management_bp.route("/admin/weekly-excel-cache/stats", methods=["GET"])
def get_weekly_excel_cache_stats():
    """获取周版本Excel缓存统计信息"""
    weekly_excel_cache_service, log_print = get_runtime_models(
        "weekly_excel_cache_service",
        "log_print",
    )
    try:
        stats = weekly_excel_cache_service.get_cache_stats()
        return jsonify({"success": True, "stats": stats})
    except Exception as exc:
        log_print(f"❌ 获取统计失败: {exc}", "CACHE", force=True)
        import traceback

        traceback.print_exc()
        return jsonify({"success": False, "message": f"获取统计信息失败: {str(exc)}"}), 500


@cache_management_bp.route("/admin/weekly-excel-cache/cleanup", methods=["POST"])
@require_admin
def cleanup_weekly_excel_cache():
    """清理周版本Excel缓存"""
    (weekly_excel_cache_service,) = get_runtime_models("weekly_excel_cache_service")
    try:
        expired_count = weekly_excel_cache_service.cleanup_expired_cache()
        old_count = weekly_excel_cache_service.cleanup_old_cache()

        return jsonify(
            {
                "success": True,
                "message": "清理完成",
                "expired_count": expired_count,
                "old_count": old_count,
            }
        )
    except Exception as exc:
        return jsonify({"success": False, "message": f"清理失败: {str(exc)}"}), 500


@cache_management_bp.route("/admin/weekly-excel-cache/clear-all", methods=["POST"])
@require_admin
def clear_all_weekly_excel_cache():
    """清理所有周版本Excel缓存"""
    (weekly_excel_cache_service,) = get_runtime_models("weekly_excel_cache_service")
    try:
        count = weekly_excel_cache_service.clear_all_cache()
        return jsonify(
            {
                "success": True,
                "message": f"已清理 {count} 条周版本Excel缓存",
                "count": count,
            }
        )
    except Exception as exc:
        return jsonify({"success": False, "message": f"清理失败: {str(exc)}"}), 500


@cache_management_bp.route("/admin/weekly-excel-cache/rebuild/<int:config_id>", methods=["POST"])
@require_admin
def rebuild_weekly_excel_cache(config_id):
    """重建指定周版本配置的Excel缓存"""
    (
        db,
        WeeklyVersionConfig,
        BackgroundTask,
        WeeklyVersionExcelCache,
        WeeklyVersionDiffCache,
        weekly_excel_cache_service,
        create_weekly_excel_cache_task,
        log_print,
    ) = get_runtime_models(
        "db",
        "WeeklyVersionConfig",
        "BackgroundTask",
        "WeeklyVersionExcelCache",
        "WeeklyVersionDiffCache",
        "weekly_excel_cache_service",
        "create_weekly_excel_cache_task",
        "log_print",
    )
    log_print(f"🔄 开始重建周版本Excel缓存，配置ID: {config_id}", "WEEKLY", force=True)

    try:
        config = db.session.get(WeeklyVersionConfig, config_id)
        if not config:
            log_print(f"❌ 周版本配置不存在: {config_id}", "WEEKLY", force=True)
            return jsonify({"success": False, "message": f"周版本配置不存在: {config_id}"}), 404

        log_print(f"✅ 找到周版本配置: {config.name} (仓库: {config.repository.name})", "WEEKLY", force=True)

        log_print("🔍 准备调用log_cache_operation", "DEBUG", force=True)
        try:
            weekly_excel_cache_service.log_cache_operation(
                f"🔄 开始重建周版本Excel缓存: {config.name} (仓库: {config.repository.name})",
                "info",
                repository_id=config.repository_id,
                config_id=config_id,
            )
            log_print("🔍 log_cache_operation调用成功", "DEBUG", force=True)
        except Exception as exc:
            log_print(f"🔍 log_cache_operation调用失败: {exc}", "ERROR", force=True)

        log_print(f"🧹 清理配置ID {config_id} 的现有队列任务", "WEEKLY", force=True)
        pending_tasks_deleted = (
            BackgroundTask.query.filter(
                BackgroundTask.repository_id == config_id,
                BackgroundTask.task_type == "weekly_excel_cache",
                BackgroundTask.status.in_(["pending", "processing"]),
            )
            .delete(synchronize_session=False)
        )

        log_print(f"✅ 删除了 {pending_tasks_deleted} 个现有队列任务", "WEEKLY", force=True)

        log_print(f"🧹 清理配置ID {config_id} 下的现有Excel缓存数据", "WEEKLY", force=True)
        cache_deleted = WeeklyVersionExcelCache.query.filter_by(config_id=config_id).delete()
        log_print(f"✅ 删除了 {cache_deleted} 个缓存记录", "WEEKLY", force=True)

        db.session.commit()

        log_print(f"🔍 查询配置ID {config_id} 下的diff缓存...", "WEEKLY", force=True)
        excel_diff_caches = WeeklyVersionDiffCache.query.filter_by(config_id=config_id).all()
        log_print(f"📊 找到 {len(excel_diff_caches)} 个diff缓存记录", "WEEKLY", force=True)

        excel_files = []
        for diff_cache in excel_diff_caches:
            if weekly_excel_cache_service.is_excel_file(diff_cache.file_path):
                excel_files.append(diff_cache.file_path)
                log_print(f"📋 添加Excel文件: {diff_cache.file_path}", "WEEKLY", force=True)
            else:
                log_print(f"⏭️ 跳过非Excel文件: {diff_cache.file_path}", "WEEKLY", force=True)

        log_print(f"📈 总计需要重建缓存的Excel文件: {len(excel_files)} 个", "WEEKLY", force=True)

        if not excel_files:
            log_print("ℹ️ 没有需要重建缓存的Excel文件", "WEEKLY", force=True)
            return jsonify(
                {
                    "success": True,
                    "message": f'周版本配置 "{config.name}" 中没有需要重建缓存的Excel文件',
                    "task_count": 0,
                    "deleted_count": cache_deleted,
                }
            )

        log_print("🚀 开始创建缓存重建任务...", "WEEKLY", force=True)
        task_count = 0
        for file_path in excel_files:
            try:
                log_print(f"📝 创建任务: {file_path}", "WEEKLY", force=True)
                created_task_id = create_weekly_excel_cache_task(config_id, file_path)
                if created_task_id is not None:
                    task_count += 1
                    log_print(f"✅ 任务创建成功: {file_path} (task_id={created_task_id})", "WEEKLY", force=True)
                else:
                    log_print(f"⏭️ 跳过重复任务: {file_path}", "WEEKLY", force=True)
            except Exception as task_exc:
                log_print(f"❌ 创建Excel缓存任务失败: {file_path}, 错误: {task_exc}", "WEEKLY", force=True)

        message = f"已清理 {cache_deleted} 条旧缓存，创建 {task_count} 个重建任务，正在后台处理中..."
        log_print(f"🎉 重建缓存请求处理完成: {message}", "WEEKLY", force=True)

        weekly_excel_cache_service.log_cache_operation(
            f"✅ 周版本Excel缓存重建完成: {config.name} - {message}",
            "success",
            repository_id=config.repository_id,
            config_id=config_id,
        )

        return jsonify(
            {
                "success": True,
                "message": message,
                "task_count": task_count,
                "deleted_count": cache_deleted,
                "excel_files": excel_files,
            }
        )
    except Exception as exc:
        log_print(f"❌ 重建周版本Excel缓存失败: {exc}", "WEEKLY", force=True)
        import traceback

        traceback.print_exc()
        return jsonify({"success": False, "message": f"重建缓存失败: {str(exc)}"}), 500


@cache_management_bp.route("/admin/excel-cache")
def excel_cache_management():
    """Excel缓存管理页面"""
    Project, DIFF_LOGIC_VERSION = get_runtime_models("Project", "DIFF_LOGIC_VERSION")
    projects = Project.query.all()
    return render_template(
        "excel_cache_management.html",
        current_version=DIFF_LOGIC_VERSION,
        projects=projects,
    )


@cache_management_bp.route("/admin/performance")
@require_admin
def admin_performance_dashboard():
    """性能观测面板页面。"""
    return render_template("admin_performance_dashboard.html")


@cache_management_bp.route("/admin/performance/stats", methods=["GET"])
@require_admin
def admin_performance_stats():
    """获取性能观测聚合数据。"""
    try:
        (performance_metrics_service,) = get_runtime_models("performance_metrics_service")
        window_minutes = request.args.get("window_minutes", default=60, type=int) or 60
        recent_limit = request.args.get("recent_limit", default=500, type=int) or 500
        diff_kind = (request.args.get("diff_kind") or "all").strip().lower()
        mode_kind = (request.args.get("mode_kind") or "all").strip().lower()
        project_filter = (request.args.get("project_filter") or "all").strip()
        data = performance_metrics_service.snapshot(
            window_minutes=window_minutes,
            recent_limit=recent_limit,
            diff_kind=diff_kind,
            mode_kind=mode_kind,
            project_filter=project_filter,
        )
        return jsonify({"success": True, "data": data})
    except Exception as exc:
        return jsonify({"success": False, "message": f"获取性能统计失败: {str(exc)}"}), 500


@cache_management_bp.route("/admin/performance/reset", methods=["POST"])
@require_admin
def admin_performance_reset():
    """清空性能观测缓存事件。"""
    try:
        (performance_metrics_service,) = get_runtime_models("performance_metrics_service")
        cleared = performance_metrics_service.clear()
        return jsonify({"success": True, "cleared": cleared, "message": f"已清空 {cleared} 条性能事件"})
    except Exception as exc:
        return jsonify({"success": False, "message": f"清空性能统计失败: {str(exc)}"}), 500
