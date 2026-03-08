"""Commit diff page handlers extracted from app.py."""

from __future__ import annotations

import os
import subprocess
import time

from services.api_response_service import json_error, json_success

COMMIT_FULL_DIFF_FILE_READ_ERRORS = (subprocess.SubprocessError, OSError, ValueError, TypeError, RuntimeError)
COMMIT_FULL_DIFF_PIPELINE_ERRORS = (
    subprocess.SubprocessError,
    OSError,
    ValueError,
    TypeError,
    RuntimeError,
    AttributeError,
    KeyError,
)
COMMIT_REFRESH_CACHE_SAVE_ERRORS = (
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    AttributeError,
    KeyError,
)
COMMIT_REFRESH_UNEXPECTED_ERRORS = (AttributeError, KeyError, OSError, subprocess.SubprocessError)


def handle_commit_full_diff(
    *,
    commit_id,
    Commit,
    get_svn_service,
    threaded_git_service_cls,
    get_commit_diff_mode_strategy,
    ensure_commit_access_or_403,
    resolve_previous_commit,
    generate_side_by_side_diff,
    render_template,
    log_print,
):
    """Render full file diff page."""
    commit = Commit.query.get_or_404(commit_id)
    repository, project = ensure_commit_access_or_403(commit)
    mode_strategy = get_commit_diff_mode_strategy()

    file_commits = Commit.query.filter(
        Commit.repository_id == repository.id,
        Commit.path == commit.path,
    ).order_by(Commit.commit_time.desc()).all()
    previous_commit = resolve_previous_commit(commit, file_commits=file_commits)

    try:
        if repository.type == "git":
            git_service = threaded_git_service_cls(
                repository.url,
                repository.root_directory,
                repository.username,
                repository.token,
                repository,
                set(),
            )
            local_path = git_service.local_path
            if not os.path.exists(local_path):
                if not mode_strategy.allow_platform_local_git_clone:
                    message = mode_strategy.local_clone_block_message
                    return render_template(
                        "full_file_diff.html",
                        commit=commit,
                        repository=repository,
                        project=project,
                        previous_commit=previous_commit,
                        current_file_content=message,
                        previous_file_content=message,
                    )
                success, message = git_service.clone_or_update_repository()
                if not success:
                    failure = f"仓库克隆失败: {message}"
                    return render_template(
                        "full_file_diff.html",
                        commit=commit,
                        repository=repository,
                        project=project,
                        previous_commit=previous_commit,
                        current_file_content=failure,
                        previous_file_content=failure,
                    )
        else:
            svn_service = get_svn_service(repository)
            local_path = svn_service.local_path

        try:
            if repository.type == "git":
                result = subprocess.run(
                    ["git", "show", f"{commit.commit_id}:{commit.path}"],
                    cwd=local_path,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                )
            else:
                result = subprocess.run(
                    ["svn", "cat", f"{repository.url}/{commit.path}@{commit.commit_id}"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                )
            if result.returncode == 0:
                current_file_content = result.stdout
                log_print(f"Current file content length: {len(current_file_content)}")
            else:
                current_file_content = f"无法获取文件内容: {result.stderr}"
                log_print(f"Git show error: {result.stderr}", "INFO")
        except COMMIT_FULL_DIFF_FILE_READ_ERRORS as exc:
            current_file_content = f"获取当前版本失败: {str(exc)}"

        previous_file_content = ""
        if previous_commit:
            try:
                if repository.type == "git":
                    result = subprocess.run(
                        ["git", "show", f"{previous_commit.commit_id}:{commit.path}"],
                        cwd=local_path,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                    )
                else:
                    result = subprocess.run(
                        ["svn", "cat", f"{repository.url}/{commit.path}@{previous_commit.commit_id}"],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                    )
                if result.returncode == 0:
                    previous_file_content = result.stdout
                else:
                    previous_file_content = f"无法获取文件内容: {result.stderr}"
            except COMMIT_FULL_DIFF_FILE_READ_ERRORS as exc:
                previous_file_content = f"获取前一版本失败: {str(exc)}"
        else:
            previous_file_content = ""
    except COMMIT_FULL_DIFF_PIPELINE_ERRORS as exc:
        log_print(f"获取文件内容失败: {exc}", "INFO")
        current_file_content = "无法获取文件内容"
        previous_file_content = "无法获取文件内容"

    side_by_side_diff = generate_side_by_side_diff(current_file_content, previous_file_content)
    return render_template(
        "git_style_diff.html",
        commit=commit,
        repository=repository,
        project=project,
        previous_commit=previous_commit,
        current_file_content=current_file_content,
        previous_file_content=previous_file_content,
        side_by_side_diff=side_by_side_diff,
    )


def handle_refresh_commit_diff(
    *,
    commit_id,
    Commit,
    DiffCache,
    ExcelHtmlCache,
    db,
    SQLAlchemyError,
    excel_cache_service,
    maybe_dispatch_commit_diff,
    get_unified_diff_data,
    safe_json_serialize,
    ensure_commit_access_or_403,
    jsonify,
    log_print,
):
    """Refresh commit diff and bypass cache when needed."""
    start_time = None
    try:
        commit = Commit.query.get_or_404(commit_id)
        repository, _project = ensure_commit_access_or_403(commit)
        dispatch_result = maybe_dispatch_commit_diff(commit, force_retry=True)
        if dispatch_result is not None:
            status = str(dispatch_result.get("status") or "")
            if status == "ready":
                payload = dispatch_result.get("payload") or {}
                return json_success(
                    jsonify=jsonify,
                    status="ready",
                    message="Agent diff 已就绪",
                    diff_data=payload.get("diff_data"),
                    previous_commit=payload.get("previous_commit"),
                )
            if status == "unbound":
                return json_error(
                    jsonify=jsonify,
                    status="unbound",
                    message=dispatch_result.get("message") or "项目未绑定 Agent",
                    error_type="agent_unbound",
                    http_status=409,
                )
            if status in {"pending", "pending_offline"}:
                return json_success(
                    jsonify=jsonify,
                    status=status,
                    message=dispatch_result.get("message") or "Agent 正在处理diff",
                    pending=True,
                    retry_after_seconds=dispatch_result.get("retry_after_seconds") or 60,
                    task_id=dispatch_result.get("task_id"),
                    http_status=202,
                )
            return json_error(
                jsonify=jsonify,
                status="error",
                message=dispatch_result.get("message") or "派发 Agent diff 失败",
                error_type="agent_dispatch_failed",
                http_status=500,
            )

        log_print(f"🔄 开始重新计算差异: commit={commit_id}, file={commit.path}", "APP")
        start_time = time.time()
        if excel_cache_service.is_excel_file(commit.path):
            log_print(f"🗑️ 清除Excel缓存: {commit.path}", "EXCEL")
            try:
                cache_delete_start = time.time()
                deleted_count = DiffCache.query.filter_by(
                    repository_id=repository.id,
                    commit_id=commit.commit_id,
                    file_path=commit.path,
                ).delete()
                html_deleted_count = ExcelHtmlCache.query.filter_by(
                    repository_id=repository.id,
                    commit_id=commit.commit_id,
                    file_path=commit.path,
                ).delete()
                if deleted_count > 0 or html_deleted_count > 0:
                    db.session.commit()
                    cache_delete_time = time.time() - cache_delete_start
                    log_print(
                        f"✅ 已删除缓存记录: diff={deleted_count}, html={html_deleted_count} | 耗时: {cache_delete_time:.2f}秒",
                        "EXCEL",
                    )
                else:
                    log_print("ℹ️ 没有找到需要删除的缓存记录", "EXCEL")
            except SQLAlchemyError as cache_error:
                log_print(f"⚠️ 清除缓存时出错: {cache_error}", "EXCEL", force=True)
                db.session.rollback()

        file_commits = Commit.query.filter(
            Commit.repository_id == repository.id,
            Commit.path == commit.path,
            Commit.commit_time < commit.commit_time,
        ).order_by(Commit.commit_time.desc()).first()

        diff_calculation_start = time.time()
        diff_data = get_unified_diff_data(commit, file_commits)
        diff_calculation_time = time.time() - diff_calculation_start
        if diff_data:
            if excel_cache_service.is_excel_file(commit.path) and diff_data.get("type") == "excel":
                cache_start = time.time()
                log_print("💾 重新缓存Excel差异数据", "EXCEL")
                try:
                    excel_cache_service.save_cached_diff(
                        repository_id=repository.id,
                        commit_id=commit.commit_id,
                        file_path=commit.path,
                        diff_data=diff_data,
                        previous_commit_id=file_commits.commit_id if file_commits else None,
                        processing_time=diff_calculation_time,
                        commit_time=commit.commit_time,
                    )
                    cache_time = time.time() - cache_start
                    log_print(f"💾 缓存保存完成，耗时: {cache_time:.2f}秒", "EXCEL")
                except COMMIT_REFRESH_CACHE_SAVE_ERRORS as cache_error:
                    log_print(f"⚠️ 保存缓存时出错: {cache_error}", "EXCEL")
            total_time = time.time() - start_time
            log_print(
                f"✅ 差异重新计算完成: {commit.path} | 计算耗时: {diff_calculation_time:.2f}秒 | 总耗时: {total_time:.2f}秒",
                "APP",
            )
            safe_diff_data = safe_json_serialize(diff_data)
            return json_success(
                jsonify=jsonify,
                status="ready",
                message=f"差异重新计算完成，计算耗时 {diff_calculation_time:.2f} 秒",
                processing_time=diff_calculation_time,
                total_time=total_time,
                diff_data=safe_diff_data,
            )
        total_time = time.time() - start_time
        log_print(f"❌ 差异重新计算失败: {commit.path} | 耗时: {total_time:.2f}秒", "APP", force=True)
        return json_error(
            jsonify=jsonify,
            message="差异重新计算失败，请检查文件内容",
            error_type="diff_recompute_failed",
            http_status=500,
            total_time=total_time,
        )
    except SQLAlchemyError as exc:
        total_time = time.time() - start_time if start_time is not None else 0
        db.session.rollback()
        log_print(f"❌ 重新计算差异数据库异常: {exc} | 耗时: {total_time:.2f}秒", "APP", force=True)
        return json_error(
            jsonify=jsonify,
            message="重新计算差异失败: 数据库操作异常",
            error_type="database_error",
            http_status=500,
            total_time=total_time,
        )
    except (ValueError, TypeError, RuntimeError) as exc:
        total_time = time.time() - start_time if start_time is not None else 0
        log_print(f"❌ 重新计算差异异常: {exc} | 耗时: {total_time:.2f}秒", "APP", force=True)
        import traceback

        traceback.print_exc()
        return json_error(
            jsonify=jsonify,
            message=f"重新计算差异失败: {str(exc)}",
            error_type="runtime_error",
            http_status=500,
            total_time=total_time,
        )
    except COMMIT_REFRESH_UNEXPECTED_ERRORS as exc:
        total_time = time.time() - start_time if start_time is not None else 0
        log_print(f"❌ 重新计算差异未知异常: {exc} | 耗时: {total_time:.2f}秒", "APP", force=True)
        import traceback

        traceback.print_exc()
        return json_error(
            jsonify=jsonify,
            message=f"重新计算差异失败: {str(exc)}",
            error_type="unexpected_error",
            http_status=500,
            total_time=total_time,
        )
