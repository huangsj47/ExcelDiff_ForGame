"""Commit route scope helpers extracted from app.py."""

from __future__ import annotations

import unicodedata


def ensure_repository_access_or_403(*, repository, has_project_access, abort):
    """Ensure repository belongs to an accessible project."""
    project = repository.project if repository else None
    if project is None:
        abort(404)
    project_id = getattr(project, "id", None)
    if project_id is not None and not has_project_access(project_id):
        abort(403)
    return project


def ensure_commit_access_or_403(*, commit, ensure_repository_access_or_403_func):
    """Ensure commit's repository/project is accessible."""
    repository = commit.repository if commit else None
    project = ensure_repository_access_or_403_func(repository)
    return repository, project


def ensure_commit_route_scope_or_404(
    *,
    commit,
    project_code,
    repository_name,
    ensure_commit_access_or_403_func,
    abort,
):
    """Ensure commit route path params match commit's repository scope."""
    repository, project = ensure_commit_access_or_403_func(commit)
    expected_project_code = str(project_code or "").strip()
    # 仓库名要按 NFC 比较：同一个「é」/汉字，输入法或外部粘贴可能给出**去组合形态**，
    # 与库里存的**合成形态**是两个不同的字符串，不归一化就会让一部分深链 404。
    # 写入侧已经统一成 NFC（utils/security_utils.py:normalize_repository_name），
    # 这里再归一化一次是为了容忍手工输入/外部粘贴的链接。
    # **两侧都要归一化**：只归一化一边等于没归一化。
    expected_repo_name = unicodedata.normalize("NFC", str(repository_name or "").strip())
    if expected_project_code and str(project.code or "").strip() != expected_project_code:
        abort(404)
    actual_repo_name = unicodedata.normalize("NFC", str(repository.name or "").strip())
    if expected_repo_name and actual_repo_name != expected_repo_name:
        abort(404)
    return repository, project


def dispatch_commit_route_with_scope(
    *,
    commit_id,
    project_code,
    repository_name,
    Commit,
    ensure_commit_route_scope_or_404_func,
    target_handler,
):
    """Load commit, validate path scope, then dispatch to target handler."""
    commit = Commit.query.get_or_404(commit_id)
    ensure_commit_route_scope_or_404_func(
        commit,
        project_code=project_code,
        repository_name=repository_name,
    )
    return target_handler(commit_id)
