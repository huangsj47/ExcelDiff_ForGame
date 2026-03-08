"""Helpers for deleted-file handling in weekly Excel diff views."""

from __future__ import annotations

import json
from html import escape
from typing import Any
from urllib.parse import quote


def is_deleted_operation(operation: Any) -> bool:
    op = str(operation or "").strip().upper()
    return op in {"D", "DEL", "DELETE", "DELETED", "REMOVE", "REMOVED"}


def resolve_weekly_deleted_excel_state(
    *,
    commit_model,
    config,
    diff_cache,
    file_path: str,
) -> tuple[bool, str | None]:
    """Judge whether the weekly Excel result ended in deleted state."""
    latest_commit = None
    repository_id = getattr(config, "repository_id", None)
    if not repository_id:
        repository_id = getattr(getattr(config, "repository", None), "id", None)

    if repository_id and getattr(diff_cache, "latest_commit_id", None):
        try:
            latest_commit = (
                commit_model.query.filter(
                    commit_model.repository_id == repository_id,
                    commit_model.path == file_path,
                    commit_model.commit_id == diff_cache.latest_commit_id,
                )
                .order_by(commit_model.commit_time.desc(), commit_model.id.desc())
                .first()
            )
        except Exception:
            latest_commit = None

    if latest_commit and is_deleted_operation(getattr(latest_commit, "operation", None)):
        previous_commit = None
        try:
            previous_commit = (
                commit_model.query.filter(
                    commit_model.repository_id == repository_id,
                    commit_model.path == file_path,
                    commit_model.commit_time < latest_commit.commit_time,
                )
                .order_by(commit_model.commit_time.desc(), commit_model.id.desc())
                .first()
            )
            if previous_commit is None:
                previous_commit = (
                    commit_model.query.filter(
                        commit_model.repository_id == repository_id,
                        commit_model.path == file_path,
                        commit_model.commit_time == latest_commit.commit_time,
                        commit_model.id < latest_commit.id,
                    )
                    .order_by(commit_model.id.desc())
                    .first()
                )
        except Exception:
            previous_commit = None

        previous_commit_id = (
            previous_commit.commit_id if previous_commit and previous_commit.commit_id else diff_cache.base_commit_id
        )
        return True, previous_commit_id

    if getattr(diff_cache, "merged_diff_data", None):
        try:
            merged_payload = json.loads(diff_cache.merged_diff_data)
            if isinstance(merged_payload, dict):
                operations = merged_payload.get("operations")
                commit_ids = merged_payload.get("commit_ids")
                if isinstance(operations, list) and operations and is_deleted_operation(operations[-1]):
                    previous_commit_id = None
                    if isinstance(commit_ids, list) and len(commit_ids) >= 2:
                        previous_commit_id = commit_ids[-2]
                    if not previous_commit_id:
                        previous_commit_id = diff_cache.base_commit_id
                    return True, previous_commit_id
        except Exception:
            pass

    return False, None


def render_weekly_deleted_excel_notice(
    *,
    commit_model,
    url_for,
    config,
    file_path: str,
    previous_commit_id: str | None,
) -> str:
    """Render deleted-file notice HTML with optional previous version shortcut."""
    safe_file_name = escape((file_path or "").split("/")[-1] or file_path or "该文件")
    previous_html = ""
    if previous_commit_id:
        previous_commit_str = str(previous_commit_id).strip()
        previous_url = None
        commit_query = commit_model.query.filter(
            commit_model.repository_id == config.repository_id,
            commit_model.path == file_path,
        )
        if len(previous_commit_str) >= 40:
            previous_commit = commit_query.filter(commit_model.commit_id == previous_commit_str).first()
        else:
            previous_commit = None
            try:
                if hasattr(commit_model.commit_id, "like"):
                    previous_commit = commit_query.filter(
                        commit_model.commit_id.like(f"{previous_commit_str}%")
                    ).first()
            except Exception:
                previous_commit = None
            if previous_commit is None:
                previous_commit = commit_query.filter(commit_model.commit_id == previous_commit_str).first()

        if previous_commit:
            try:
                previous_url = url_for(
                    "commit_diff_with_path",
                    project_code=config.project.code,
                    repository_name=config.repository.name,
                    commit_id=previous_commit.id,
                )
            except Exception:
                try:
                    previous_url = url_for("commit_diff", commit_id=previous_commit.id)
                except Exception:
                    previous_url = None

        if not previous_url:
            encoded_file_path = quote(file_path or "", safe="")
            previous_url = (
                f"/weekly-version-config/{config.id}/file-previous-version"
                f"?file_path={encoded_file_path}&commit_id={quote(previous_commit_str, safe='')}"
            )

        previous_html = (
            "<hr>"
            "<p class='mb-0'>"
            "<small class='text-muted'>"
            f"可以查看 <a href='{previous_url}' class='alert-link' target='_blank'>上一个版本 ({escape(previous_commit_str[:8])})</a> "
            "来查看删除前的Excel内容。"
            "</small>"
            "</p>"
        )

    return (
        "<div class='p-4 text-center'>"
        "<div class='alert alert-warning mb-4'>"
        "<i class='bi bi-trash fs-1 mb-3 d-block text-warning'></i>"
        "<h5 class='alert-heading'>Excel文件已删除</h5>"
        f"<p class='mb-0'>{safe_file_name} 在该周版本中已被删除。</p>"
        f"{previous_html}"
        "</div>"
        "</div>"
    )
