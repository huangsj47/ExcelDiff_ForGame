"""Commit list page handler extracted from app.py."""

from __future__ import annotations

import re

from sqlalchemy import func, or_


def handle_commit_list_page(
    *,
    repository_id,
    Repository,
    Commit,
    request,
    abort,
    render_template,
    log_print,
    has_project_access,
    queue_missing_git_branch_refresh,
    attach_author_display,
):
    """Render commit list page with filters, pagination and author mapping."""
    log_print("=== 访问提交列表页面 ===", "APP")
    log_print(f"Repository ID: {repository_id}", "APP")
    repository = Repository.query.get_or_404(repository_id)
    project = repository.project
    if not has_project_access(project.id):
        abort(403)

    all_repositories = project.repositories
    repository_groups = {}
    missing_git_branch_repo_ids = []
    for repo in all_repositories:
        if not repo.branch and repo.type == "git":
            missing_git_branch_repo_ids.append(repo.id)
        base_name = repo.name
        if base_name.endswith("_git") or base_name.endswith("_svn"):
            base_name = base_name.rsplit("_", 1)[0]
        if base_name not in repository_groups:
            repository_groups[base_name] = {
                "name": base_name,
                "repositories": [],
                "earliest_repo": repo,
            }
        repository_groups[base_name]["repositories"].append(repo)
        if repo.id < repository_groups[base_name]["earliest_repo"].id:
            repository_groups[base_name]["earliest_repo"] = repo
    if missing_git_branch_repo_ids:
        queued = queue_missing_git_branch_refresh(project.id, missing_git_branch_repo_ids)
        if queued:
            log_print(
                f"检测到 {len(missing_git_branch_repo_ids)} 个缺失分支的Git仓库，已异步刷新",
                "APP",
            )

    grouped_repositories = []
    for group_name, group_data in repository_groups.items():
        grouped_repositories.append(
            {
                "name": group_name,
                "repositories": group_data["repositories"],
                "current_repo": (
                    repository if repository in group_data["repositories"] else group_data["earliest_repo"]
                ),
            }
        )
    repositories = all_repositories

    raw_status_params = [s for s in request.args.getlist("status") if s]
    normalized_status_list = []
    for raw_status in raw_status_params:
        for status_item in re.split(r"[,，]", str(raw_status)):
            normalized = status_item.strip()
            if normalized and normalized not in normalized_status_list:
                normalized_status_list.append(normalized)

    if not normalized_status_list:
        fallback_status_param = request.args.get("status", "")
        if fallback_status_param:
            for status_item in re.split(r"[,，]", str(fallback_status_param)):
                normalized = status_item.strip()
                if normalized and normalized not in normalized_status_list:
                    normalized_status_list.append(normalized)

    filters = {
        "author": request.args.get("author", ""),
        "path": request.args.get("path", ""),
        "version": request.args.get("version", ""),
        "operation": request.args.get("operation", ""),
        "status": ",".join(normalized_status_list) if normalized_status_list else request.args.get("status", ""),
        "status_list": normalized_status_list,
        "start_date": request.args.get("start_date", ""),
        "end_date": request.args.get("end_date", ""),
    }
    page = max(1, request.args.get("page", 1, type=int) or 1)
    requested_per_page = request.args.get("per_page", 50, type=int) or 50
    per_page = min(max(requested_per_page, 1), 200)

    def _parse_confirm_usernames(raw_value):
        if not raw_value:
            return []
        usernames = [item.strip() for item in re.split(r"[,，;；|\n\r]+", str(raw_value)) if item and item.strip()]
        unique_usernames = []
        for username in usernames:
            if username not in unique_usernames:
                unique_usernames.append(username)
        return unique_usernames

    def _extract_author_lookup_keys(raw_author):
        text = str(raw_author or "").strip()
        if not text:
            return []
        keys = []
        lower_text = text.lower()
        if all(symbol not in lower_text for symbol in ("@", "<", ">", " ")):
            keys.append(lower_text)
        if "@" in lower_text and "<" not in lower_text and ">" not in lower_text:
            email_prefix = lower_text.split("@", 1)[0].strip()
            if email_prefix and email_prefix not in keys:
                keys.append(email_prefix)
        for email in re.findall(r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})", text):
            email_prefix = email.lower().split("@", 1)[0].strip()
            if email_prefix and email_prefix not in keys:
                keys.append(email_prefix)
        return keys

    def _get_auth_user_model():
        try:
            from auth import get_auth_backend

            if get_auth_backend() == "qkit":
                from qkit_auth.models import QkitAuthUser as _UserModel
            else:
                from auth.models import AuthUser as _UserModel
            return _UserModel
        except Exception as model_error:
            log_print(f"加载账号模型失败，回退原始作者显示: {model_error}", "APP")
            return None

    _UserModel = _get_auth_user_model()
    query = Commit.query.filter_by(repository_id=repository_id)
    if repository.start_date:
        query = query.filter(Commit.commit_time >= repository.start_date)
        log_print(f"应用仓库起始日期过滤: {repository.start_date}", "APP")

    repository_status = {
        "clone_status": repository.clone_status,
        "clone_error": repository.clone_error,
        "is_data_ready": False,
        "status_message": "",
    }
    base_count = query.count()
    log_print(f"基础查询结果数量: {base_count}", "APP")
    if repository.clone_status == "cloning":
        repository_status["status_message"] = "仓库正在克隆中，请稍后刷新页面查看数据..."
    elif repository.clone_status == "failed":
        repository_status["status_message"] = f"仓库克隆失败：{repository.clone_error or '未知错误'}"
    elif repository.clone_status == "completed" and base_count == 0:
        repository_status["status_message"] = '仓库克隆完成，正在分析提交数据，请稍后刷新页面或点击"手动获取数据"按钮...'
    elif base_count > 0:
        repository_status["is_data_ready"] = True

    if filters["author"]:
        author_keyword = str(filters["author"]).strip()
        author_keyword_lower = author_keyword.lower()
        author_conditions = [func.lower(Commit.author).like(f"%{author_keyword_lower}%")]
        if _UserModel is not None and author_keyword:
            matched_author_tokens = set()
            try:
                user_like = f"%{author_keyword}%"
                matched_users = _UserModel.query.filter(
                    or_(
                        _UserModel.username.ilike(user_like),
                        _UserModel.display_name.ilike(user_like),
                        _UserModel.email.ilike(user_like),
                    )
                ).all()

                for user in matched_users:
                    username = (getattr(user, "username", "") or "").strip().lower()
                    if username:
                        matched_author_tokens.add(username)
                    email = (getattr(user, "email", "") or "").strip().lower()
                    if email and "@" in email:
                        matched_author_tokens.add(email.split("@", 1)[0])

                for token in matched_author_tokens:
                    author_conditions.append(func.lower(Commit.author).like(f"%{token}%"))
            except Exception as filter_error:
                log_print(f"按姓名筛选作者失败，回退原始筛选: {filter_error}", "APP")
        query = query.filter(or_(*author_conditions))
    if filters["path"]:
        query = query.filter(Commit.path.contains(filters["path"]))
    if filters["version"]:
        query = query.filter(Commit.version.contains(filters["version"]))
    if filters["operation"]:
        query = query.filter_by(operation=filters["operation"])
    if filters["status_list"]:
        query = query.filter(Commit.status.in_(filters["status_list"]))
    elif filters["status"]:
        query = query.filter_by(status=filters["status"])

    pagination = query.order_by(Commit.commit_time.desc()).paginate(page=page, per_page=per_page, error_out=False)
    commits = pagination.items

    all_confirm_usernames = set()
    all_author_keys = set()
    for commit in commits:
        all_confirm_usernames.update(_parse_confirm_usernames(commit.status_changed_by))
        all_author_keys.update(_extract_author_lookup_keys(commit.author))

    username_to_display_name = {}
    username_to_display_name_lower = {}
    email_prefix_to_display_name = {}
    if _UserModel is not None and (all_confirm_usernames or all_author_keys):
        try:
            username_conditions = []
            if all_confirm_usernames:
                username_conditions.append(func.lower(_UserModel.username).in_([u.lower() for u in all_confirm_usernames]))
            if all_author_keys:
                username_conditions.append(func.lower(_UserModel.username).in_(list(all_author_keys)))
                username_conditions.extend(
                    func.lower(_UserModel.email).like(f"{author_key}@%")
                    for author_key in all_author_keys
                    if author_key
                )

            users = _UserModel.query.filter(or_(*username_conditions)).all() if username_conditions else []
            for user in users:
                username = (getattr(user, "username", "") or "").strip()
                if not username:
                    continue
                display_name = (getattr(user, "display_name", "") or "").strip() or username
                username_to_display_name[username] = display_name
                username_to_display_name_lower[username.lower()] = display_name

                email = (getattr(user, "email", "") or "").strip().lower()
                if email and "@" in email:
                    email_prefix_to_display_name[email.split("@", 1)[0]] = display_name
        except Exception as exc:
            log_print(f"加载作者/确认用户姓名映射失败，回退为原始显示: {exc}", "APP")

    def _resolve_author_display(raw_author):
        text = str(raw_author or "").strip()
        if not text:
            return ""

        for author_key in _extract_author_lookup_keys(text):
            mapped_name = username_to_display_name_lower.get(author_key) or email_prefix_to_display_name.get(author_key)
            if mapped_name:
                return mapped_name
        return text

    for commit in commits:
        commit_confirm_users = _parse_confirm_usernames(commit.status_changed_by)
        commit_confirm_display_names = [
            username_to_display_name.get(username) or username_to_display_name_lower.get(username.lower(), username)
            for username in commit_confirm_users
        ]

        confirm_users_display = ""
        confirm_users_title = ""
        if commit.status in ("confirmed", "rejected") and commit_confirm_users:
            confirm_users_display = ", ".join(commit_confirm_display_names)
            confirm_users_title = ", ".join(commit_confirm_users)

        commit.confirm_users_display = confirm_users_display
        commit.confirm_users_title = confirm_users_title
        commit.author_display = _resolve_author_display(commit.author)

    try:
        attach_author_display(commits)
    except Exception as author_map_error:
        log_print(f"补齐提交列表作者映射失败，继续使用原始作者显示: {author_map_error}", "APP")

    log_print("=== 分页调试信息 ===", "APP")
    log_print(f"Repository ID: {repository_id}", "APP")
    log_print(f"Page: {page}, Per page: {per_page}", "APP")
    log_print(f"Pagination total: {pagination.total}", "APP")
    log_print(f"Pagination pages: {pagination.pages}", "APP")
    log_print(f"Current page items: {len(commits)}", "APP")
    log_print(f"Filters: {filters}", "APP")
    log_print("=====================", "APP")
    return render_template(
        "commit_list.html",
        commits=commits,
        pagination=pagination,
        repository=repository,
        project=project,
        repositories=repositories,
        grouped_repositories=grouped_repositories,
        filters=filters,
        repository_status=repository_status,
    )
