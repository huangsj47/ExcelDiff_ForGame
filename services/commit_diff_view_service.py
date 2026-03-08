"""Commit diff page view handler extracted from app.py."""

from __future__ import annotations

import json
import traceback

from sqlalchemy.exc import SQLAlchemyError

COMMIT_DIFF_VIEW_AUTHOR_MAP_ERRORS = (RuntimeError, ValueError, TypeError, AttributeError)
COMMIT_DIFF_VIEW_CACHE_PROCESS_ERRORS = (RuntimeError, ValueError, TypeError, AttributeError, KeyError)
COMMIT_DIFF_VIEW_EXCEL_PIPELINE_ERRORS = (
    SQLAlchemyError,
    RuntimeError,
    ValueError,
    TypeError,
    AttributeError,
    KeyError,
    OSError,
)


def handle_commit_diff_view(
    *,
    commit_id,
    time_module,
    Commit,
    db,
    excel_cache_service,
    add_excel_diff_task,
    threaded_git_service_cls,
    active_git_processes,
    get_commit_diff_mode_strategy,
    resolve_previous_commit,
    attach_author_display,
    get_unified_diff_data,
    get_diff_data,
    validate_excel_diff_data,
    clean_json_data,
    build_commit_diff_template_context,
    performance_metrics_service,
    ensure_commit_access_or_403,
    render_template,
    log_print,
):
    """Render commit diff page with excel/non-excel branching."""
    diff_request_start = time_module.time()
    commit = Commit.query.get_or_404(commit_id)
    repository, project = ensure_commit_access_or_403(commit)
    mode_strategy = get_commit_diff_mode_strategy()
    is_deleted = commit.operation == "D"
    is_excel = excel_cache_service.is_excel_file(commit.path)
    file_commits = Commit.query.filter(
        Commit.repository_id == repository.id,
        Commit.path == commit.path,
    ).order_by(Commit.commit_time.desc(), Commit.id.desc()).all()
    previous_commit = resolve_previous_commit(commit, file_commits=file_commits)
    try:
        commits_for_author_mapping = [commit]
        if previous_commit:
            commits_for_author_mapping.append(previous_commit)
        attach_author_display(commits_for_author_mapping)
    except COMMIT_DIFF_VIEW_AUTHOR_MAP_ERRORS as author_map_error:
        log_print(f"commit_diff 作者姓名映射失败，回退原始作者: {author_map_error}", "DIFF")
    log_print(f"🔍 查找前一提交 - 文件: {commit.path}", "DIFF", force=True)
    log_print(f"🔍 该文件总提交数: {len(file_commits)}", "DIFF", force=True)
    if previous_commit:
        log_print(
            f"✅ 找到前一提交: ID:{previous_commit.id} {previous_commit.commit_id[:8]} {previous_commit.commit_time}",
            "DIFF",
            force=True,
        )
    else:
        log_print("❌ 未找到前一提交 - 这是初始提交", "DIFF", force=True)
    if is_deleted:
        return render_template(
            "commit_diff.html",
            **build_commit_diff_template_context(
                commit=commit,
                repository=repository,
                project=project,
                file_commits=file_commits,
                previous_commit=previous_commit,
                is_excel=is_excel,
                diff_data={"type": "deleted", "message": "该文件已被删除"},
                is_deleted=True,
                mode_strategy=mode_strategy,
            ),
        )
    if mode_strategy.async_agent_diff:
        return render_template(
            "commit_diff.html",
            **build_commit_diff_template_context(
                commit=commit,
                repository=repository,
                project=project,
                file_commits=file_commits,
                previous_commit=previous_commit,
                is_excel=is_excel,
                diff_data=None,
                is_deleted=False,
                mode_strategy=mode_strategy,
            ),
        )
    if is_excel:
        try:
            log_print(f"处理Excel文件差异: {commit.path}", "EXCEL")
            log_print(f"Commit ID: {commit.commit_id}", "EXCEL")
            log_print(f"Repository: {repository.name}", "EXCEL")
            cached_diff = excel_cache_service.get_cached_diff(repository.id, commit.commit_id, commit.path)
            diff_data = None
            cache_is_valid = False
            if cached_diff:
                log_print(f"📦 从缓存获取Excel差异数据: {commit.path}", "EXCEL")
                log_print(f"🏷️ 缓存版本: {cached_diff.diff_version} | 缓存时间: {cached_diff.created_at}", "EXCEL")
                try:
                    cached_data = json.loads(cached_diff.diff_data)
                    is_valid, validation_message = validate_excel_diff_data(cached_data)
                    log_print(f"🔍 缓存数据验证: {validation_message}", "EXCEL")
                    if is_valid:
                        diff_data = cached_data
                        cache_is_valid = True
                        log_print("✅ 缓存数据验证通过，使用缓存数据", "EXCEL")
                    else:
                        log_print(f"❌ 缓存数据验证失败: {validation_message}", "EXCEL", force=True)
                        log_print("🔄 将删除无效缓存并重新生成", "EXCEL")
                        try:
                            db.session.delete(cached_diff)
                            db.session.commit()
                            log_print(f"🗑️ 已删除无效缓存记录 ID: {cached_diff.id}", "EXCEL")
                        except SQLAlchemyError as delete_error:
                            log_print(f"❌ 删除缓存记录失败: {delete_error}", "EXCEL", force=True)
                            db.session.rollback()
                except json.JSONDecodeError as exc:
                    log_print(f"❌ 缓存数据JSON解析失败: {exc}", "EXCEL", force=True)
                    cache_is_valid = False
                except COMMIT_DIFF_VIEW_CACHE_PROCESS_ERRORS as exc:
                    log_print(f"❌ 缓存数据处理异常: {exc}", "EXCEL", force=True)
                    cache_is_valid = False
            if not cache_is_valid:
                log_print(f"🔄 缓存未命中或无效，开始实时处理Excel文件: {commit.path}", "EXCEL")
                diff_data = get_unified_diff_data(commit, previous_commit)
                if diff_data:
                    is_valid, validation_message = validate_excel_diff_data(diff_data)
                    log_print(f"🔍 新生成数据验证: {validation_message}", "EXCEL")
                    if is_valid:
                        cache_is_valid = True
                    else:
                        log_print(f"❌ 新生成的数据也无效: {validation_message}", "EXCEL", force=True)
                else:
                    log_print("❌ 新数据生成失败", "EXCEL", force=True)
                if diff_data and cache_is_valid:
                    log_print(f"💾 立即缓存Excel差异结果: {commit.path}", "EXCEL")
                    cache_success = excel_cache_service.save_cached_diff(
                        repository_id=repository.id,
                        commit_id=commit.commit_id,
                        file_path=commit.path,
                        diff_data=diff_data,
                        previous_commit_id=previous_commit.commit_id if previous_commit else None,
                        processing_time=0,
                        file_size=0,
                        commit_time=commit.commit_time,
                    )
                    if cache_success:
                        log_print(f"✅ Excel差异缓存成功: {commit.path}", "EXCEL")
                    else:
                        log_print(f"❌ Excel差异缓存失败: {commit.path}", "EXCEL", force=True)
                        add_excel_diff_task(repository.id, commit.commit_id, commit.path, priority=1)
                        log_print(f"已添加Excel差异缓存任务到后台队列 (高优先级): {commit.path}", "EXCEL")
                else:
                    log_print("❌ 缓存条件不满足，跳过缓存", "CACHE", force=True)
                if not diff_data:
                    log_print("使用旧的Excel处理逻辑作为备用", "EXCEL")
                    git_service = threaded_git_service_cls(
                        repository.url,
                        repository.root_directory,
                        repository.username,
                        repository.token,
                        repository,
                        active_git_processes,
                    )
                    diff_data = git_service.parse_excel_diff(commit.commit_id, commit.path)
                    log_print(f"旧Excel处理逻辑返回: {type(diff_data)}", "EXCEL")
        except COMMIT_DIFF_VIEW_EXCEL_PIPELINE_ERRORS as exc:
            log_print(f"Excel diff generation failed: {exc}", "EXCEL", force=True)
            traceback.print_exc()
            diff_data = None
        if diff_data:
            diff_data = clean_json_data(diff_data)
        log_print(f"🔍 模板变量调试: is_excel=True, diff_data存在={diff_data is not None}", "EXCEL", force=True)
        if diff_data:
            log_print(
                f"🔍 diff_data类型: {type(diff_data)}, 键: {list(diff_data.keys()) if isinstance(diff_data, dict) else 'N/A'}",
                "EXCEL",
                force=True,
            )
        template_context = build_commit_diff_template_context(
            commit=commit,
            repository=repository,
            project=project,
            file_commits=file_commits,
            previous_commit=previous_commit,
            is_excel=True,
            diff_data=diff_data,
            is_deleted=False,
            mode_strategy=mode_strategy,
        )
        log_print(f"🔍 模板上下文键: {list(template_context.keys())}", "EXCEL", force=True)
        log_print(f"🔍 is_excel值: {template_context['is_excel']}, 类型: {type(template_context['is_excel'])}", "EXCEL", force=True)
        return render_template("commit_diff.html", **template_context)

    diff_data = get_diff_data(commit, previous_commit=previous_commit)
    perf_tags = {
        "source": "realtime_non_excel",
        "repository_id": repository.id,
        "project_id": project.id if project else "",
        "project_code": project.code if project else "",
        "file_path": commit.path,
    }
    perf_success = True
    if isinstance(diff_data, dict) and str(diff_data.get("type") or "").lower() == "error":
        perf_success = False
        perf_tags["source"] = "realtime_diff_failed"
    performance_metrics_service.record(
        "api_commit_diff",
        success=perf_success,
        metrics={"total_ms": (time_module.time() - diff_request_start) * 1000},
        tags=perf_tags,
    )
    return render_template(
        "commit_diff.html",
        **build_commit_diff_template_context(
            commit=commit,
            repository=repository,
            project=project,
            file_commits=file_commits,
            previous_commit=previous_commit,
            is_excel=False,
            diff_data=diff_data,
            is_deleted=False,
            mode_strategy=mode_strategy,
        ),
    )
