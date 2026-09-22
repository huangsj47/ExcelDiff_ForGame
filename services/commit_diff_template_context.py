"""Template context builders for commit diff pages."""

from __future__ import annotations

from flask import url_for

from services.weekly_version_files_api_helpers import describe_file_header_profile


def build_commit_diff_template_context(
    *,
    commit,
    repository,
    project,
    file_commits,
    previous_commit,
    is_excel: bool,
    diff_data=None,
    is_deleted: bool = False,
    mode_strategy=None,
):
    """Build `commit_diff.html` context with unified async-agent flag derivation."""
    async_mode_enabled = bool(getattr(mode_strategy, "async_agent_diff", False))
    async_agent_diff = bool(async_mode_enabled and not is_deleted and diff_data is None)
    return {
        "commit": commit,
        "repository": repository,
        "project": project,
        "diff_data": diff_data,
        "file_commits": file_commits,
        "previous_commit": previous_commit,
        "is_excel": bool(is_excel),
        "is_deleted": bool(is_deleted),
        "async_agent_diff": async_agent_diff,
        "agent_diff_endpoint": url_for("get_commit_diff_data", commit_id=commit.id),
        # 这张表命中的表头方案（含用户写的说明文本）；走兜底坐标时为 None，
        # 页面据此决定挂不挂那条说明。两条 render 共用这一处，不会漏。
        "header_profile": describe_file_header_profile(
            repository,
            getattr(commit, "path", None),
            getattr(commit, "commit_id", None),
        ),
    }
