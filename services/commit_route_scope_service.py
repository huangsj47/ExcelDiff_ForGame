"""Commit route scope helpers extracted from app.py."""

from __future__ import annotations


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
    expected_repo_name = str(repository_name or "").strip()
    if expected_project_code and str(project.code or "").strip() != expected_project_code:
        abort(404)
    if expected_repo_name and str(repository.name or "").strip() != expected_repo_name:
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
