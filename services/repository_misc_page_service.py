"""Misc repository page/helpers extracted from app.py."""

from __future__ import annotations

import os


def render_edit_repository_page(*, repository_id, Repository, render_template):
    """Render repository edit page by repository type."""
    repository = Repository.query.get_or_404(repository_id)
    project = repository.project
    if repository.type == "git":
        return render_template(
            "add_git_repository.html",
            project=project,
            repository=repository,
            is_edit=True,
        )
    return render_template(
        "add_svn_repository.html",
        project=project,
        repository=repository,
        is_edit=True,
    )


def check_local_repository_exists(
    *,
    project_code,
    repository_name,
    repository_id,
    build_repository_local_path,
):
    """Check whether local repository directory exists."""
    try:
        local_path = build_repository_local_path(project_code, repository_name, repository_id, strict=False)
    except (TypeError, ValueError):
        return False
    return os.path.exists(local_path)
