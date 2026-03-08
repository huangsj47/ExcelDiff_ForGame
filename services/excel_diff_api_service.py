"""Excel diff API handler extracted from app.py."""

from __future__ import annotations

import json
import traceback

from sqlalchemy import and_, or_
from sqlalchemy.exc import SQLAlchemyError

from services.api_response_service import json_error, json_success


def handle_get_excel_diff_data(
    *,
    commit_id,
    request,
    jsonify,
    time_module,
    Commit,
    db,
    excel_cache_service,
    excel_html_cache_service,
    performance_metrics_service,
    maybe_dispatch_commit_diff,
    get_unified_diff_data,
    add_excel_diff_task,
    ensure_commit_access_or_403,
    log_print,
):
    """Handle API: fetch excel diff payload with HTML/data cache strategy."""
    request_start = time_module.time()
    commit = Commit.query.get_or_404(commit_id)
    repository, project = ensure_commit_access_or_403(commit)
    perf_project_tags = {
        "project_id": getattr(project, "id", "") if project else "",
        "project_code": getattr(project, "code", "") if project else "",
    }

    if not excel_cache_service.is_excel_file(commit.path):
        return json_error(
            jsonify=jsonify,
            message="不是Excel文件",
            error_type="invalid_file_type",
            http_status=400,
            error=True,
        )

    force_retry = str(request.args.get("force_retry") or "").strip().lower() in {"1", "true", "yes"}
    dispatch_result = maybe_dispatch_commit_diff(commit, force_retry=force_retry)
    if dispatch_result is not None:
        status = str(dispatch_result.get("status") or "")
        if status == "ready":
            payload = dispatch_result.get("payload") or {}
            diff_data = payload.get("diff_data")
            if not isinstance(diff_data, dict):
                return json_error(
                    jsonify=jsonify,
                    message="Agent diff 返回格式异常",
                    error_type="invalid_agent_payload",
                    http_status=500,
                )
            try:
                html_content, css_content, js_content = excel_html_cache_service.generate_excel_html(diff_data)
            except Exception as render_exc:
                return jsonify(
                    {
                        "success": True,
                        "from_agent": True,
                        "from_data_cache": False,
                        "html_render_failed": True,
                        "message": f"Excel HTML 渲染失败: {render_exc}",
                        "diff_data": diff_data,
                    }
                )

            metadata = {
                "file_path": commit.path,
                "commit_id": commit.commit_id,
                "repository_name": repository.name,
                "source": "agent_commit_diff",
            }
            return jsonify(
                {
                    "success": True,
                    "from_agent": True,
                    "from_html_cache": False,
                    "from_data_cache": False,
                    "html_content": html_content,
                    "css_content": css_content,
                    "js_content": js_content,
                    "metadata": metadata,
                }
            )

        if status == "unbound":
            return json_error(
                jsonify=jsonify,
                message=dispatch_result.get("message") or "项目未绑定 Agent",
                error_type="agent_unbound",
                status="unbound",
                http_status=409,
            )

        if status in {"pending", "pending_offline"}:
            return json_success(
                jsonify=jsonify,
                message=dispatch_result.get("message") or "Agent 正在处理diff",
                status=status,
                http_status=202,
                pending=True,
                retry_after_seconds=dispatch_result.get("retry_after_seconds") or 60,
                task_id=dispatch_result.get("task_id"),
            )

        return json_error(
            jsonify=jsonify,
            message=dispatch_result.get("message") or "Agent diff 获取失败",
            error_type="agent_dispatch_failed",
            http_status=500,
        )

    try:
        html_lookup_start = time_module.time()
        cached_html = excel_html_cache_service.get_cached_html(repository.id, commit.commit_id, commit.path)
        if cached_html:
            html_lookup_time = time_module.time() - html_lookup_start
            total_time = time_module.time() - request_start
            log_print(f"✅ 从HTML缓存获取Excel差异: {commit.path}", "EXCEL")
            log_print(
                f"📊 Excel接口耗时: html_lookup={html_lookup_time:.2f}s, total={total_time:.2f}s | "
                f"html_bytes={len(cached_html.get('html_content') or '')}",
                "EXCEL",
            )
            performance_metrics_service.record(
                "api_excel_diff",
                success=True,
                metrics={
                    "total_ms": total_time * 1000,
                    "html_lookup_ms": html_lookup_time * 1000,
                    "html_bytes": len(cached_html.get("html_content") or ""),
                },
                tags={
                    "source": "html_cache",
                    "repository_id": repository.id,
                    "project_id": perf_project_tags["project_id"],
                    "project_code": perf_project_tags["project_code"],
                    "file_path": commit.path,
                },
            )
            created_at_value = cached_html.get("created_at")
            if created_at_value and hasattr(created_at_value, "isoformat"):
                created_at_iso = created_at_value.isoformat()
            elif created_at_value:
                created_at_iso = str(created_at_value)
            else:
                created_at_iso = None
            return jsonify(
                {
                    "success": True,
                    "html_content": cached_html["html_content"],
                    "css_content": cached_html["css_content"],
                    "js_content": cached_html["js_content"],
                    "metadata": cached_html["metadata"],
                    "from_html_cache": True,
                    "created_at": created_at_iso,
                }
            )

        data_lookup_start = time_module.time()
        cached_diff = excel_cache_service.get_cached_diff(repository.id, commit.commit_id, commit.path)
        data_lookup_time = time_module.time() - data_lookup_start
        if cached_diff:
            log_print(f"📊 从数据缓存获取Excel差异，生成HTML: {commit.path}", "EXCEL")
            try:
                render_start = time_module.time()
                diff_data = json.loads(cached_diff.diff_data)
                html_content, css_content, js_content = excel_html_cache_service.generate_excel_html(diff_data)
                render_time = time_module.time() - render_start
                metadata = {
                    "file_path": commit.path,
                    "commit_id": commit.commit_id,
                    "repository_name": repository.name,
                    "processing_time": cached_diff.processing_time,
                }
                excel_html_cache_service.save_html_cache(
                    repository.id,
                    commit.commit_id,
                    commit.path,
                    html_content,
                    css_content,
                    js_content,
                    metadata,
                )
                total_time = time_module.time() - request_start
                log_print(
                    f"📊 Excel接口耗时: data_lookup={data_lookup_time:.2f}s, render={render_time:.2f}s, total={total_time:.2f}s | "
                    f"diff_bytes={len(cached_diff.diff_data.encode('utf-8')) / 1024:.1f}KB",
                    "EXCEL",
                )
                performance_metrics_service.record(
                    "api_excel_diff",
                    success=True,
                    metrics={
                        "total_ms": total_time * 1000,
                        "data_lookup_ms": data_lookup_time * 1000,
                        "render_ms": render_time * 1000,
                        "diff_bytes": len(cached_diff.diff_data.encode("utf-8")),
                    },
                    tags={
                        "source": "data_cache",
                        "repository_id": repository.id,
                        "project_id": perf_project_tags["project_id"],
                        "project_code": perf_project_tags["project_code"],
                        "file_path": commit.path,
                    },
                )
                return jsonify(
                    {
                        "success": True,
                        "html_content": html_content,
                        "css_content": css_content,
                        "js_content": js_content,
                        "metadata": metadata,
                        "from_html_cache": False,
                        "from_data_cache": True,
                    }
                )
            except Exception as exc:
                log_print(f"⚠️ HTML生成失败，返回原始数据: {exc}", "INFO")
                performance_metrics_service.record(
                    "api_excel_diff",
                    success=False,
                    metrics={
                        "total_ms": (time_module.time() - request_start) * 1000,
                        "data_lookup_ms": data_lookup_time * 1000,
                    },
                    tags={
                        "source": "data_cache_html_render_failed",
                        "repository_id": repository.id,
                        "project_id": perf_project_tags["project_id"],
                        "project_code": perf_project_tags["project_code"],
                        "file_path": commit.path,
                    },
                )
                return jsonify({"success": True, "diff_data": json.loads(cached_diff.diff_data), "from_cache": True})

        log_print(f"🔄 缓存未命中，开始实时处理Excel文件: {commit.path}", "INFO")
        previous_lookup_start = time_module.time()
        previous_commit = Commit.query.filter(
            Commit.repository_id == repository.id,
            Commit.path == commit.path,
            or_(
                Commit.commit_time < commit.commit_time,
                and_(Commit.commit_time == commit.commit_time, Commit.id < commit.id),
            ),
        ).order_by(Commit.commit_time.desc(), Commit.id.desc()).first()
        if not previous_commit:
            previous_commit = Commit.query.filter(
                Commit.repository_id == repository.id,
                Commit.path == commit.path,
                Commit.id < commit.id,
            ).order_by(Commit.id.desc()).first()
        previous_lookup_time = time_module.time() - previous_lookup_start
        diff_start = time_module.time()
        diff_data = get_unified_diff_data(commit, previous_commit)
        diff_time = time_module.time() - diff_start
        if diff_data and diff_data.get("type") == "excel":
            try:
                render_start = time_module.time()
                html_content, css_content, js_content = excel_html_cache_service.generate_excel_html(diff_data)
                render_time = time_module.time() - render_start
                metadata = {
                    "file_path": commit.path,
                    "commit_id": commit.commit_id,
                    "repository_name": repository.name,
                    "real_time_processing": True,
                }
                excel_html_cache_service.save_html_cache(
                    repository.id,
                    commit.commit_id,
                    commit.path,
                    html_content,
                    css_content,
                    js_content,
                    metadata,
                )
                add_excel_diff_task(repository.id, commit.commit_id, commit.path, priority=1)
                total_time = time_module.time() - request_start
                log_print(f"✅ Excel差异实时处理完成，HTML缓存已保存: {commit.path}", "EXCEL")
                log_print(
                    f"📊 Excel接口耗时: data_lookup={data_lookup_time:.2f}s, prev_lookup={previous_lookup_time:.2f}s, "
                    f"diff={diff_time:.2f}s, render={render_time:.2f}s, total={total_time:.2f}s",
                    "EXCEL",
                )
                performance_metrics_service.record(
                    "api_excel_diff",
                    success=True,
                    metrics={
                        "total_ms": total_time * 1000,
                        "data_lookup_ms": data_lookup_time * 1000,
                        "prev_lookup_ms": previous_lookup_time * 1000,
                        "diff_ms": diff_time * 1000,
                        "render_ms": render_time * 1000,
                    },
                    tags={
                        "source": "realtime",
                        "repository_id": repository.id,
                        "project_id": perf_project_tags["project_id"],
                        "project_code": perf_project_tags["project_code"],
                        "file_path": commit.path,
                    },
                )
                return jsonify(
                    {
                        "success": True,
                        "html_content": html_content,
                        "css_content": css_content,
                        "js_content": js_content,
                        "metadata": metadata,
                        "from_html_cache": False,
                        "real_time": True,
                    }
                )
            except Exception as exc:
                log_print(f"⚠️ HTML生成失败，返回原始数据: {exc}", "INFO")
                add_excel_diff_task(repository.id, commit.commit_id, commit.path, priority=1)
                performance_metrics_service.record(
                    "api_excel_diff",
                    success=False,
                    metrics={
                        "total_ms": (time_module.time() - request_start) * 1000,
                        "data_lookup_ms": data_lookup_time * 1000,
                        "prev_lookup_ms": previous_lookup_time * 1000,
                        "diff_ms": diff_time * 1000,
                    },
                    tags={
                        "source": "realtime_html_render_failed",
                        "repository_id": repository.id,
                        "project_id": perf_project_tags["project_id"],
                        "project_code": perf_project_tags["project_code"],
                        "file_path": commit.path,
                    },
                )
                return jsonify({"success": True, "diff_data": diff_data, "from_cache": False})

        error_msg = diff_data.get("error", "处理失败") if diff_data else "Excel文件处理返回空结果"
        performance_metrics_service.record(
            "api_excel_diff",
            success=False,
            metrics={
                "total_ms": (time_module.time() - request_start) * 1000,
                "data_lookup_ms": data_lookup_time * 1000,
                "prev_lookup_ms": previous_lookup_time * 1000,
                "diff_ms": diff_time * 1000,
            },
            tags={
                "source": "realtime_diff_failed",
                "repository_id": repository.id,
                "project_id": perf_project_tags["project_id"],
                "project_code": perf_project_tags["project_code"],
                "file_path": commit.path,
            },
        )
        return json_error(
            jsonify=jsonify,
            message=error_msg,
            error_type="excel_diff_failed",
            http_status=500,
            error=True,
        )
    except SQLAlchemyError as exc:
        log_print(f"❌ Excel diff处理数据库失败: {str(exc)}")
        traceback.print_exc()
        performance_metrics_service.record(
            "api_excel_diff",
            success=False,
            metrics={"total_ms": (time_module.time() - request_start) * 1000},
            tags={
                "source": "sqlalchemy_exception",
                "repository_id": repository.id if repository else "",
                "project_id": perf_project_tags["project_id"],
                "project_code": perf_project_tags["project_code"],
                "file_path": commit.path if commit else "",
            },
        )
        return json_error(
            jsonify=jsonify,
            message="Excel文件处理失败: 数据库访问异常",
            error_type="database_error",
            http_status=500,
            error=True,
        )
    except (ValueError, TypeError, RuntimeError) as exc:
        log_print(f"❌ Excel diff处理失败: {str(exc)}")
        traceback.print_exc()
        performance_metrics_service.record(
            "api_excel_diff",
            success=False,
            metrics={"total_ms": (time_module.time() - request_start) * 1000},
            tags={
                "source": "runtime_exception",
                "repository_id": repository.id if repository else "",
                "project_id": perf_project_tags["project_id"],
                "project_code": perf_project_tags["project_code"],
                "file_path": commit.path if commit else "",
            },
        )
        return json_error(
            jsonify=jsonify,
            message=f"Excel文件处理失败: {str(exc)}",
            error_type="runtime_error",
            http_status=500,
            error=True,
        )
    except Exception as exc:
        log_print(f"❌ Excel diff处理失败: {str(exc)}")
        traceback.print_exc()
        performance_metrics_service.record(
            "api_excel_diff",
            success=False,
            metrics={"total_ms": (time_module.time() - request_start) * 1000},
            tags={
                "source": "exception",
                "repository_id": repository.id if repository else "",
                "project_id": perf_project_tags["project_id"],
                "project_code": perf_project_tags["project_code"],
                "file_path": commit.path if commit else "",
            },
        )
        return json_error(
            jsonify=jsonify,
            message=f"Excel文件处理失败: {str(exc)}",
            error_type="unexpected_error",
            http_status=500,
            error=True,
        )
