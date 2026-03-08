"""Repository update form handlers extracted from app.py."""

from __future__ import annotations

from sqlalchemy.exc import SQLAlchemyError


REPOSITORY_UPDATE_FORM_FORCE_SYNC_ERRORS = (
    ImportError,
    RuntimeError,
    ValueError,
    TypeError,
    AttributeError,
    OSError,
)
REPOSITORY_UPDATE_FORM_ASYNC_REFILTER_ERRORS = (
    SQLAlchemyError,
    RuntimeError,
    ValueError,
    TypeError,
    AttributeError,
    KeyError,
    OSError,
)
REPOSITORY_UPDATE_FORM_SUBMIT_ERRORS = (
    SQLAlchemyError,
    RuntimeError,
    ValueError,
    TypeError,
    AttributeError,
    KeyError,
)


def clear_repository_state_for_switch(
    *,
    repository,
    switch_type,
    old_value,
    new_value,
    WeeklyVersionConfig,
    Commit,
    DiffCache,
    ExcelHtmlCache,
    MergedDiffCache,
    WeeklyVersionDiffCache,
    WeeklyVersionExcelCache,
    BackgroundTask,
    or_,
    log_print,
):
    """Conservative cleanup flow after branch/version switch."""
    repository_id = repository.id
    pending_statuses = ["pending", "processing"]

    weekly_configs = WeeklyVersionConfig.query.filter_by(repository_id=repository_id).all()
    config_ids = [config.id for config in weekly_configs]
    config_id_strs = [str(config_id) for config_id in config_ids]

    commits_deleted = Commit.query.filter_by(repository_id=repository_id).delete(synchronize_session=False)
    diff_deleted = DiffCache.query.filter_by(repository_id=repository_id).delete(synchronize_session=False)
    html_deleted = ExcelHtmlCache.query.filter_by(repository_id=repository_id).delete(synchronize_session=False)
    merged_deleted = MergedDiffCache.query.filter_by(repository_id=repository_id).delete(synchronize_session=False)

    weekly_diff_query = WeeklyVersionDiffCache.query.filter_by(repository_id=repository_id)
    weekly_excel_query = WeeklyVersionExcelCache.query.filter_by(repository_id=repository_id)
    if config_ids:
        weekly_diff_query = WeeklyVersionDiffCache.query.filter(
            or_(
                WeeklyVersionDiffCache.repository_id == repository_id,
                WeeklyVersionDiffCache.config_id.in_(config_ids),
            )
        )
        weekly_excel_query = WeeklyVersionExcelCache.query.filter(
            or_(
                WeeklyVersionExcelCache.repository_id == repository_id,
                WeeklyVersionExcelCache.config_id.in_(config_ids),
            )
        )

    weekly_diff_deleted = weekly_diff_query.delete(synchronize_session=False)
    weekly_excel_deleted = weekly_excel_query.delete(synchronize_session=False)

    repository_task_deleted = BackgroundTask.query.filter(
        BackgroundTask.task_type.in_(["auto_sync", "excel_diff"]),
        BackgroundTask.repository_id == repository_id,
        BackgroundTask.status.in_(pending_statuses),
    ).delete(synchronize_session=False)

    weekly_sync_task_deleted = 0
    weekly_excel_task_deleted = 0
    if config_ids:
        weekly_sync_task_deleted = BackgroundTask.query.filter(
            BackgroundTask.task_type == "weekly_sync",
            BackgroundTask.commit_id.in_(config_id_strs),
            BackgroundTask.status.in_(pending_statuses),
        ).delete(synchronize_session=False)
        weekly_excel_task_deleted = BackgroundTask.query.filter(
            BackgroundTask.task_type == "weekly_excel_cache",
            BackgroundTask.repository_id.in_(config_ids),
            BackgroundTask.status.in_(pending_statuses),
        ).delete(synchronize_session=False)

    repository.last_sync_commit_id = None
    repository.last_sync_time = None
    repository.cache_version = None
    repository.sync_mode = "full"
    repository.last_sync_error = None
    repository.last_sync_error_time = None

    log_print(
        (
            f"检测到仓库切换: repo_id={repository_id}, type={switch_type}, "
            f"from='{old_value}' to='{new_value}'. 清理结果: "
            f"commits={commits_deleted}, diff={diff_deleted}, html={html_deleted}, merged={merged_deleted}, "
            f"weekly_diff={weekly_diff_deleted}, weekly_excel={weekly_excel_deleted}, "
            f"repo_tasks={repository_task_deleted}, weekly_sync_tasks={weekly_sync_task_deleted}, "
            f"weekly_excel_tasks={weekly_excel_task_deleted}, weekly_configs={len(config_ids)}"
        ),
        "APP",
        force=True,
    )

    return {
        "weekly_config_count": len(config_ids),
        "commits_deleted": commits_deleted,
        "diff_deleted": diff_deleted,
        "html_deleted": html_deleted,
        "merged_deleted": merged_deleted,
        "weekly_diff_deleted": weekly_diff_deleted,
        "weekly_excel_deleted": weekly_excel_deleted,
        "repository_task_deleted": repository_task_deleted,
        "weekly_sync_task_deleted": weekly_sync_task_deleted,
        "weekly_excel_task_deleted": weekly_excel_task_deleted,
    }


def handle_update_repository_form(
    *,
    repository,
    request,
    redirect,
    url_for,
    flash,
    db,
    validate_repository_name,
    log_print,
    create_auto_sync_task,
    app,
    Commit,
    Repository,
    DiffCache,
    clear_repository_state_for_switch_func,
):
    """Handle repository edit form submit."""
    repository_id = repository.id
    project_id = repository.project_id
    try:
        def _field_changed(field_name, current_value):
            submitted = request.form.get(field_name)
            if submitted is None:
                return False
            return str(submitted).strip() != str(current_value or "").strip()

        new_name = (request.form.get("name") or "").strip()
        if not validate_repository_name(new_name):
            flash("仓库名称仅允许字母、数字、点、下划线和短横线", "error")
            return redirect(url_for("edit_repository", repository_id=repository_id))

        old_file_type_filter = repository.path_regex if repository.type == "git" else None
        switch_changed = False
        switch_type = ""
        switch_old_value = ""
        switch_new_value = ""
        switch_cleanup_summary = None

        repository.name = new_name
        repository.category = request.form.get("category")
        repository.resource_type = request.form.get("resource_type")
        repository.display_order = int(request.form.get("display_order", 0))
        repository.path_regex = request.form.get("file_type_filter") or request.form.get("path_regex")
        repository.log_regex = request.form.get("log_regex")
        repository.log_filter_regex = request.form.get("log_filter_regex")
        repository.commit_filter = request.form.get("commit_filter")
        repository.important_tables = request.form.get("important_tables")
        repository.unconfirmed_history = bool(request.form.get("unconfirmed_history"))
        repository.delete_table_alert = bool(request.form.get("delete_table_alert"))
        repository.weekly_version_setting = request.form.get("weekly_version_setting")
        header_rows = request.form.get("header_rows")
        repository.header_rows = int(header_rows) if header_rows else None
        repository.key_columns = request.form.get("key_columns")
        repository.enable_id_confirmation = bool(request.form.get("enable_id_confirmation"))
        repository.show_duplicate_id_warning = bool(request.form.get("show_duplicate_id_warning"))
        repository.tag_selection = request.form.get("tag_selection")

        if repository.type == "git":
            if _field_changed("url", repository.url) or _field_changed("server_url", repository.server_url):
                flash("已锁定基础地址信息（GitLab SSH URL / 服务器 URL）。如需修改，请删除仓库后重新创建并执行 clone。", "error")
                return redirect(url_for("edit_repository", repository_id=repository_id))
            new_token = (request.form.get("token") or "").strip()
            if new_token:
                repository.token = new_token
            new_branch = (request.form.get("branch") or "").strip()
            if not new_branch:
                flash("Git 分支不能为空", "error")
                return redirect(url_for("edit_repository", repository_id=repository_id))
            old_branch = str(repository.branch or "").strip()
            if new_branch != old_branch:
                if request.form.get("confirm_branch_switch") != "1":
                    flash("检测到分支变更，请完成切换风险确认后再提交。", "error")
                    return redirect(url_for("edit_repository", repository_id=repository_id))
                switch_changed = True
                switch_type = "分支"
                switch_old_value = old_branch
                switch_new_value = new_branch
            repository.branch = new_branch
            repository.enable_webhook = "enable_webhook" in request.form
            repository.show_latest_id = "show_latest_id" in request.form
            repository.table_name_column = request.form.get("table_name_column")
            current_date = request.form.get("current_date")
            if current_date:
                try:
                    from datetime import datetime

                    repository.start_date = datetime.strptime(current_date, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    try:
                        repository.start_date = datetime.strptime(current_date, "%Y-%m-%d")
                    except ValueError:
                        flash("日期格式错误，请使用 YYYY-MM-DD HH:MM:SS 或 YYYY-MM-DD 格式", "error")
                        return redirect(url_for("edit_repository", repository_id=repository_id))
        elif repository.type == "svn":
            if _field_changed("url", repository.url) or _field_changed("root_directory", repository.root_directory):
                flash("已锁定基础地址信息（SVN URL / SVN 仓库根目录）。如需修改，请删除仓库后重新创建并执行 svn co。", "error")
                return redirect(url_for("edit_repository", repository_id=repository_id))
            repository.username = request.form.get("username")
            new_password = (request.form.get("password") or "").strip()
            if new_password:
                repository.password = new_password
            new_current_version = (request.form.get("current_version") or "").strip()
            if not new_current_version:
                flash("SVN 当前版本号不能为空", "error")
                return redirect(url_for("edit_repository", repository_id=repository_id))
            old_current_version = str(repository.current_version or "").strip()
            if new_current_version != old_current_version:
                if request.form.get("confirm_branch_switch") != "1":
                    flash("检测到 SVN 版本号变更，请完成切换风险确认后再提交。", "error")
                    return redirect(url_for("edit_repository", repository_id=repository_id))
                switch_changed = True
                switch_type = "SVN版本号"
                switch_old_value = old_current_version
                switch_new_value = new_current_version
            repository.current_version = new_current_version

        if switch_changed:
            switch_cleanup_summary = clear_repository_state_for_switch_func(
                repository=repository,
                switch_type=switch_type,
                old_value=switch_old_value,
                new_value=switch_new_value,
            )

        db.session.commit()
        need_refilter = (
            repository.type == "git"
            and old_file_type_filter != repository.path_regex
            and not switch_changed
        )

        if switch_changed:
            task_id = create_auto_sync_task(repository.id)
            old_display = switch_old_value or "空"
            new_display = switch_new_value or "空"
            if task_id:
                if (switch_cleanup_summary or {}).get("weekly_config_count", 0) > 0:
                    flash(
                        (
                            f"检测到仓库{switch_type}变更（{old_display} -> {new_display}），"
                            f"已清空旧数据并重建分析任务（任务ID: {task_id}）。"
                            "该仓库关联的周版本缓存与确认状态已重置。"
                        ),
                        "warning",
                    )
                else:
                    flash(
                        (
                            f"检测到仓库{switch_type}变更（{old_display} -> {new_display}），"
                            f"已清空旧数据并重建分析任务（任务ID: {task_id}）。"
                        ),
                        "warning",
                    )
            else:
                flash(
                    (
                        f"仓库{switch_type}已切换（{old_display} -> {new_display}），"
                        "旧数据已清空，但自动重建任务创建失败，请手动执行仓库同步。"
                    ),
                    "error",
                )
        elif need_refilter:
            log_print(f"文件类型过滤器已更新: '{old_file_type_filter}' -> '{repository.path_regex}'", "APP")
            target_repository_id = repository.id
            new_path_regex = repository.path_regex

            def async_refilter():
                try:
                    log_print("开始异步重新筛选仓库内容...", "APP")
                    with app.app_context():
                        repo = db.session.get(Repository, target_repository_id)
                        if not repo:
                            log_print(f"❌ 未找到仓库ID: {target_repository_id}", "APP", force=True)
                            return

                        if new_path_regex:
                            import re

                            try:
                                pattern = re.compile(new_path_regex)
                                log_print(f"开始清理不符合过滤规则的记录: {new_path_regex}", "APP")
                                all_commits = Commit.query.filter_by(repository_id=target_repository_id).all()
                                commits_to_delete = []
                                for commit in all_commits:
                                    if commit.path and not pattern.match(commit.path):
                                        commits_to_delete.append(commit)
                                if commits_to_delete:
                                    log_print(f"找到 {len(commits_to_delete)} 个不符合规则的提交记录，开始清理...", "APP")
                                    for commit in commits_to_delete:
                                        DiffCache.query.filter_by(
                                            repository_id=target_repository_id,
                                            commit_id=commit.commit_id,
                                            file_path=commit.path,
                                        ).delete()
                                    for commit in commits_to_delete:
                                        db.session.delete(commit)
                                    db.session.commit()
                                    log_print(f"已清理 {len(commits_to_delete)} 个不符合规则的记录", "APP")
                                else:
                                    log_print("没有找到需要清理的记录", "APP")
                            except re.error as exc:
                                log_print(f"正则表达式编译失败: {exc}", "APP", force=True)
                        try:
                            from incremental_cache_system import IncrementalCacheManager

                            cache_system = IncrementalCacheManager()
                            success, message = cache_system.force_full_sync(target_repository_id)
                            if not success:
                                log_print(f"❌ 全量同步失败: {message}", "APP", force=True)
                            else:
                                log_print("✅ 全量同步成功", "APP")
                        except REPOSITORY_UPDATE_FORM_FORCE_SYNC_ERRORS as sync_exc:
                            log_print(f"❌ 全量同步异常: {sync_exc}", "APP", force=True)
                    log_print("仓库内容重新筛选完成", "APP")
                except REPOSITORY_UPDATE_FORM_ASYNC_REFILTER_ERRORS as exc:
                    log_print(f"重新筛选仓库内容时出错: {exc}", "APP", force=True)
                    import traceback

                    log_print(f"详细错误信息: {traceback.format_exc()}", "APP", force=True)

            import threading

            thread = threading.Thread(target=async_refilter, daemon=True)
            thread.start()
            flash("仓库设置已保存，正在后台重新筛选文件，请稍后查看提交列表。", "info")
        else:
            flash(f'仓库 "{repository.name}" 更新成功', "success")
        return redirect(url_for("repository_config", project_id=project_id))
    except REPOSITORY_UPDATE_FORM_SUBMIT_ERRORS as exc:
        db.session.rollback()
        flash(f"更新仓库失败: {str(exc)}", "error")
        return redirect(url_for("edit_repository", repository_id=repository_id))
