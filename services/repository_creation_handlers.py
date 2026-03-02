"""Repository creation and clone handlers extracted from app.py."""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from flask import flash, redirect, request, url_for

from models import GlobalRepositoryCounter, Repository, db
from services.enhanced_git_service import EnhancedGitService
from services.model_loader import get_runtime_model
from utils.request_security import require_admin
from utils.security_utils import validate_repository_name


@require_admin
def create_git_repository():
    project_id = request.form.get("project_id")
    name = (request.form.get("name") or "").strip()
    category = request.form.get("category")
    url = request.form.get("url")
    server_url = request.form.get("server_url")
    token = (request.form.get("token") or "").strip()
    branch = request.form.get("branch")
    resource_type = request.form.get("resource_type")
    path_regex = request.form.get("file_type_filter") or request.form.get("path_regex")
    log_regex = request.form.get("log_regex")
    log_filter_regex = request.form.get("log_filter_regex")
    commit_filter = request.form.get("commit_filter")
    important_tables = request.form.get("important_tables")
    unconfirmed_history = bool(request.form.get("unconfirmed_history"))
    delete_table_alert = bool(request.form.get("delete_table_alert"))
    weekly_version_setting = request.form.get("weekly_version_setting")
    header_rows = request.form.get("header_rows")
    key_columns = request.form.get("key_columns")
    enable_id_confirmation = bool(request.form.get("enable_id_confirmation"))
    show_duplicate_id_warning = bool(request.form.get("show_duplicate_id_warning"))
    tag_selection = request.form.get("tag_selection")
    current_date = request.form.get("current_date")
    start_date = None

    if current_date:
        try:
            start_date = datetime.strptime(current_date, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                start_date = datetime.strptime(current_date, "%Y-%m-%d")
            except ValueError:
                flash("日期格式错误，请使用 YYYY-MM-DD HH:MM:SS 或 YYYY-MM-DD", "error")
                return redirect(url_for("add_git_repository", project_id=project_id))

    if not validate_repository_name(name):
        flash("仓库名称仅允许字母、数字、点、下划线和短横线", "error")
        return redirect(url_for("add_git_repository", project_id=project_id))

    required_fields = [name, url, server_url, token, branch, resource_type]
    if resource_type == "table" and not header_rows:
        flash("选择table类型时，表头行数为必填项", "error")
        return redirect(url_for("add_git_repository", project_id=project_id))

    if not all(required_fields):
        flash("必填字段不能为空", "error")
        return redirect(url_for("add_git_repository", project_id=project_id))

    counter = GlobalRepositoryCounter.query.first()
    if not counter:
        max_existing_id = db.session.query(db.func.max(Repository.id)).scalar() or 0
        counter = GlobalRepositoryCounter(max_repository_id=max_existing_id)
        db.session.add(counter)
        db.session.flush()

    new_repository_id = counter.max_repository_id + 1
    counter.max_repository_id = new_repository_id
    counter.updated_at = datetime.now(timezone.utc)

    repository = Repository(
        id=new_repository_id,
        project_id=project_id,
        name=name,
        type="git",
        category=category,
        url=url,
        server_url=server_url,
        token=token,
        branch=branch,
        resource_type=resource_type,
        path_regex=path_regex,
        log_regex=log_regex,
        log_filter_regex=log_filter_regex,
        commit_filter=commit_filter,
        important_tables=important_tables,
        unconfirmed_history=unconfirmed_history,
        delete_table_alert=delete_table_alert,
        weekly_version_setting=weekly_version_setting,
        header_rows=int(header_rows) if header_rows else None,
        key_columns=key_columns,
        enable_id_confirmation=enable_id_confirmation,
        show_duplicate_id_warning=show_duplicate_id_warning,
        tag_selection=tag_selection,
        start_date=start_date,
    )
    db.session.add(repository)
    db.session.commit()

    repository_id = repository.id
    repository_name = repository.name

    def async_clone():
        enhanced_async_clone_with_status_update(repository_id, repository_name)

    clone_thread = threading.Thread(target=async_clone, daemon=True)
    clone_thread.start()

    flash("Git仓库创建成功，正在后台克隆仓库，请稍后查看仓库状态", "success")
    return redirect(url_for("repository_config", project_id=project_id))


def clone_repository_to_local(repository):
    """Clone repository to local with enhanced git service."""
    log_print = get_runtime_model("log_print")
    log_print(f"开始使用增强Git服务克隆仓库: {repository.name}", "GIT")
    try:
        enhanced_git_service = EnhancedGitService(
            repository_url=repository.url,
            root_directory=repository.root_directory,
            username=repository.username,
            token=repository.token,
            repository=repository,
        )
        success, message = enhanced_git_service.clone_or_update_repository_with_retry()
        stats = enhanced_git_service.get_repository_size_info() if success else None

        if success:
            log_print(f"增强Git克隆成功: {repository.name}", "GIT")
            if stats:
                log_print(f"仓库统计: {stats}", "GIT")
        else:
            log_print(f"增强Git克隆失败: {repository.name} | 错误: {message}", "GIT", force=True)
            raise Exception(f"增强Git克隆失败: {message}")
    except Exception as exc:
        error_msg = f"增强Git克隆过程出错: {str(exc)}"
        log_print(error_msg, "GIT", force=True)
        raise Exception(error_msg)


def enhanced_async_clone_with_status_update(repository_id, repository_name):
    """Enhanced async git clone with status update."""
    app = get_runtime_model("app")
    log_print = get_runtime_model("log_print")
    create_auto_sync_task = get_runtime_model("create_auto_sync_task")
    try:
        with app.app_context():
            repo = db.session.get(Repository, repository_id)
            if not repo:
                log_print(f"无法找到仓库ID: {repository_id}", "REPO", force=True)
                return

            repo.clone_status = "cloning"
            repo.clone_error = None
            db.session.commit()
            log_print(f"开始异步克隆仓库: {repository_name}", "GIT")

            clone_repository_to_local(repo)

            repo.clone_status = "completed"
            repo.clone_error = None
            db.session.commit()
            log_print(f"异步克隆完成: {repository_name}", "GIT")

            task_id = create_auto_sync_task(repository_id)
            if task_id:
                log_print(f"已为仓库 {repository_name} 自动创建数据分析任务", "INFO")
    except Exception as exc:
        error_msg = str(exc)
        log_print(f"增强异步克隆失败: {error_msg}", "INFO")
        try:
            with app.app_context():
                repo = db.session.get(Repository, repository_id)
                if repo:
                    repo.clone_status = "failed"
                    repo.clone_error = error_msg
                    db.session.commit()
        except Exception as db_error:
            log_print(f"更新克隆状态失败: {str(db_error)}")

    log_print(f"异步克隆任务结束: {repository_name} (ID: {repository_id})", "INFO")


def enhanced_retry_clone_repository(repository_id):
    """Enhanced clone retry helper."""
    app = get_runtime_model("app")
    log_print = get_runtime_model("log_print")
    repository_name = f"repo-{repository_id}"
    try:
        with app.app_context():
            repository = db.session.get(Repository, repository_id)
            if not repository:
                log_print(f"重试克隆失败：仓库不存在 {repository_id}", "GIT", force=True)
                return

            repository_name = repository.name
            log_print(f"开始增强重试克隆: {repository_name}", "INFO")
            repository.clone_status = "cloning"
            repository.clone_error = None
            db.session.commit()

            clone_repository_to_local(repository)

            repository.clone_status = "completed"
            repository.clone_error = None
            db.session.commit()
            log_print(f"增强重试克隆完成: {repository_name}", "INFO")
    except Exception as exc:
        error_msg = f"增强重试克隆失败: {str(exc)}"
        log_print(error_msg, "GIT", force=True)
        try:
            with app.app_context():
                repository = db.session.get(Repository, repository_id)
                if repository:
                    repository.clone_status = "failed"
                    repository.clone_error = error_msg
                    db.session.commit()
        except Exception as db_error:
            log_print(f"更新重试克隆状态失败: {db_error}", "GIT", force=True)


def enhanced_async_svn_clone_with_status_update(repository_id, repository_name):
    """Enhanced async SVN clone with status update."""
    app = get_runtime_model("app")
    log_print = get_runtime_model("log_print")
    create_auto_sync_task = get_runtime_model("create_auto_sync_task")
    try:
        log_print(f"开始异步SVN克隆任务: {repository_name} (ID: {repository_id})", "INFO")
        with app.app_context():
            repo = db.session.get(Repository, repository_id)
            if not repo:
                log_print(f"仓库不存在: {repository_id}", "SVN")
                return

            repo.clone_status = "cloning"
            repo.clone_error = None
            db.session.commit()
            log_print(f"开始异步克隆SVN仓库: {repository_name}", "SVN")

            clone_svn_repository_to_local(repo)

            repo.clone_status = "completed"
            repo.clone_error = None
            db.session.commit()
            log_print(f"异步SVN克隆完成: {repository_name}", "SVN")

            task_id = create_auto_sync_task(repository_id)
            if task_id:
                log_print(f"已为SVN仓库 {repository_name} 自动创建数据分析任务", "INFO")
    except Exception as exc:
        error_msg = str(exc)
        log_print(f"增强异步SVN克隆失败: {error_msg}", "INFO")
        try:
            with app.app_context():
                repo = db.session.get(Repository, repository_id)
                if repo:
                    repo.clone_status = "failed"
                    repo.clone_error = error_msg
                    db.session.commit()
        except Exception as db_error:
            log_print(f"更新SVN克隆状态失败: {str(db_error)}")

    log_print(f"异步SVN克隆任务结束: {repository_name} (ID: {repository_id})", "INFO")


def clone_svn_repository_to_local(repository):
    """Clone SVN repository to local."""
    get_svn_service = get_runtime_model("get_svn_service")
    log_print = get_runtime_model("log_print")
    log_print(f"开始使用SVN服务克隆仓库: {repository.name}", "SVN")
    try:
        svn_service = get_svn_service(repository)
        success, message = svn_service.checkout_or_update_repository()
        if success:
            log_print(f"SVN仓库克隆成功: {repository.name} - {message}", "SVN")
        else:
            log_print(f"SVN仓库克隆失败: {repository.name} - {message}", "SVN")
            raise Exception(message)
    except Exception as exc:
        error_msg = f"SVN仓库克隆失败: {str(exc)}"
        log_print(error_msg, "SVN", force=True)
        raise Exception(error_msg)


@require_admin
def create_svn_repository():
    project_id = request.form.get("project_id")
    name = (request.form.get("name") or "").strip()
    category = request.form.get("category")
    url = request.form.get("url")
    root_directory = request.form.get("root_directory")
    username = (request.form.get("username") or "").strip()
    password = (request.form.get("password") or "").strip()
    current_version = request.form.get("current_version")
    resource_type = request.form.get("resource_type")
    path_regex = request.form.get("path_regex")
    log_regex = request.form.get("log_regex")
    log_filter_regex = request.form.get("log_filter_regex")
    commit_filter = request.form.get("commit_filter")
    important_tables = request.form.get("important_tables")
    unconfirmed_history = bool(request.form.get("unconfirmed_history"))
    delete_table_alert = bool(request.form.get("delete_table_alert"))
    weekly_version_setting = request.form.get("weekly_version_setting")
    header_rows = request.form.get("header_rows")
    key_columns = request.form.get("key_columns")
    enable_id_confirmation = bool(request.form.get("enable_id_confirmation"))
    show_duplicate_id_warning = bool(request.form.get("show_duplicate_id_warning"))
    tag_selection = request.form.get("tag_selection")

    if not validate_repository_name(name):
        flash("仓库名称仅允许字母、数字、点、下划线和短横线", "error")
        return redirect(url_for("add_svn_repository", project_id=project_id))

    required = [name, url, root_directory, username, password, current_version, resource_type]
    if not all(required):
        flash("必填字段不能为空", "error")
        return redirect(url_for("add_svn_repository", project_id=project_id))

    counter = GlobalRepositoryCounter.query.first()
    if not counter:
        max_existing_id = db.session.query(db.func.max(Repository.id)).scalar() or 0
        counter = GlobalRepositoryCounter(max_repository_id=max_existing_id)
        db.session.add(counter)
        db.session.flush()

    new_repository_id = counter.max_repository_id + 1
    counter.max_repository_id = new_repository_id
    counter.updated_at = datetime.now(timezone.utc)

    repository = Repository(
        id=new_repository_id,
        project_id=project_id,
        name=name,
        type="svn",
        category=category,
        url=url,
        root_directory=root_directory,
        username=username,
        password=password,
        current_version=current_version,
        resource_type=resource_type,
        path_regex=path_regex,
        log_regex=log_regex,
        log_filter_regex=log_filter_regex,
        commit_filter=commit_filter,
        important_tables=important_tables,
        unconfirmed_history=unconfirmed_history,
        delete_table_alert=delete_table_alert,
        weekly_version_setting=weekly_version_setting,
        clone_status="pending",
        header_rows=int(header_rows) if header_rows else None,
        key_columns=key_columns,
        enable_id_confirmation=enable_id_confirmation,
        show_duplicate_id_warning=show_duplicate_id_warning,
        tag_selection=tag_selection,
    )
    db.session.add(repository)
    db.session.commit()

    repository_id = repository.id
    repository_name = repository.name

    def async_svn_clone():
        enhanced_async_svn_clone_with_status_update(repository_id, repository_name)

    clone_thread = threading.Thread(target=async_svn_clone, daemon=True)
    clone_thread.start()

    flash("SVN仓库创建成功，正在后台克隆仓库，请稍后查看仓库状态", "success")
    return redirect(url_for("repository_config", project_id=project_id))
