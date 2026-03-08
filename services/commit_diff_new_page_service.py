"""commit_diff_new page handler extracted from app.py."""

from __future__ import annotations


def handle_commit_diff_new_page(
    *,
    commit_id,
    Commit,
    resolve_previous_commit,
    attach_author_display,
    get_unified_diff_data,
    ensure_commit_access_or_403,
    render_template,
    log_print,
):
    """Render the new diff page for one commit."""
    commit = Commit.query.get_or_404(commit_id)
    repository, project = ensure_commit_access_or_403(commit)
    file_commits = Commit.query.filter(
        Commit.repository_id == repository.id,
        Commit.path == commit.path,
    ).order_by(Commit.commit_time.desc()).all()
    previous_commit = resolve_previous_commit(commit, file_commits=file_commits)
    try:
        commits_for_author_mapping = [commit]
        if previous_commit:
            commits_for_author_mapping.append(previous_commit)
        attach_author_display(commits_for_author_mapping)
    except Exception as author_map_error:
        log_print(f"commit_diff_new 作者姓名映射失败，回退原始作者: {author_map_error}", "DIFF")
    diff_data = get_unified_diff_data(commit, previous_commit)
    return render_template(
        "commit_diff_new.html",
        commit=commit,
        repository=repository,
        project=project,
        diff_data=diff_data,
        file_commits=file_commits,
        previous_commit=previous_commit,
    )
