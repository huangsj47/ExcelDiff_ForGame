#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Commit/diff route blueprint wrappers.

This module extracts commit/diff route registration from app.py while keeping
original handler implementations in app.py for low-risk migration.
"""

from flask import Blueprint

from services.model_loader import get_runtime_model


commit_diff_bp = Blueprint("commit_diff_routes", __name__)


def _dispatch(handler_name, *args, **kwargs):
    handler = get_runtime_model(handler_name)
    return handler(*args, **kwargs)


@commit_diff_bp.route(
    "/repositories/<int:repository_id>/commits",
    endpoint="commit_list",
)
def commit_list_route(repository_id):
    return _dispatch("commit_list", repository_id)


@commit_diff_bp.route(
    "/<project_code>/<repository_name>/commits/<int:commit_id>/excel-diff-data",
    endpoint="get_excel_diff_data_with_path",
)
def get_excel_diff_data_with_path_route(project_code, repository_name, commit_id):
    return _dispatch("get_excel_diff_data_with_path", project_code, repository_name, commit_id)


@commit_diff_bp.route(
    "/commits/<int:commit_id>/excel-diff-data",
    endpoint="get_excel_diff_data",
)
def get_excel_diff_data_route(commit_id):
    return _dispatch("get_excel_diff_data", commit_id)


@commit_diff_bp.route(
    "/<project_code>/<repository_name>/commits/<int:commit_id>/diff/new",
    endpoint="commit_diff_new_with_path",
)
def commit_diff_new_with_path_route(project_code, repository_name, commit_id):
    return _dispatch("commit_diff_new_with_path", project_code, repository_name, commit_id)


@commit_diff_bp.route(
    "/commits/<int:commit_id>/diff/new",
    endpoint="commit_diff_new",
)
def commit_diff_new_route(commit_id):
    return _dispatch("commit_diff_new", commit_id)


@commit_diff_bp.route(
    "/commits/<int:commit_id>/full-diff",
    endpoint="commit_full_diff",
)
def commit_full_diff_route(commit_id):
    return _dispatch("commit_full_diff", commit_id)


@commit_diff_bp.route(
    "/<project_code>/<repository_name>/commits/<int:commit_id>/refresh-diff",
    methods=["POST"],
    endpoint="refresh_commit_diff_with_path",
)
def refresh_commit_diff_with_path_route(project_code, repository_name, commit_id):
    return _dispatch("refresh_commit_diff_with_path", project_code, repository_name, commit_id)


@commit_diff_bp.route(
    "/commits/<int:commit_id>/refresh-diff",
    methods=["POST"],
    endpoint="refresh_commit_diff",
)
def refresh_commit_diff_route(commit_id):
    return _dispatch("refresh_commit_diff", commit_id)


@commit_diff_bp.route(
    "/<project_code>/<repository_name>/commits/<int:commit_id>/diff",
    endpoint="commit_diff_with_path",
)
def commit_diff_with_path_route(project_code, repository_name, commit_id):
    return _dispatch("commit_diff_with_path", project_code, repository_name, commit_id)


@commit_diff_bp.route(
    "/commits/<int:commit_id>/diff",
    endpoint="commit_diff",
)
def commit_diff_route(commit_id):
    return _dispatch("commit_diff", commit_id)


@commit_diff_bp.route(
    "/commits/<int:commit_id>/status",
    methods=["POST"],
    endpoint="update_commit_status",
)
def update_commit_status_route(commit_id):
    return _dispatch("update_commit_status", commit_id)


@commit_diff_bp.route(
    "/commits/batch-update",
    methods=["POST"],
    endpoint="batch_update_commits_compat",
)
def batch_update_commits_compat_route():
    return _dispatch("batch_update_commits_compat")


@commit_diff_bp.route(
    "/commits/<int:commit_id>/approve-all",
    methods=["POST"],
    endpoint="approve_all_files",
)
def approve_all_files_route(commit_id):
    return _dispatch("approve_all_files", commit_id)


@commit_diff_bp.route(
    "/commits/batch-approve",
    methods=["POST"],
    endpoint="batch_approve_commits",
)
def batch_approve_commits_route():
    return _dispatch("batch_approve_commits")


@commit_diff_bp.route(
    "/commits/batch-reject",
    methods=["POST"],
    endpoint="batch_reject_commits",
)
def batch_reject_commits_route():
    return _dispatch("batch_reject_commits")


@commit_diff_bp.route(
    "/commits/reject",
    methods=["POST"],
    endpoint="reject_commit",
)
def reject_commit_route():
    return _dispatch("reject_commit")


@commit_diff_bp.route(
    "/commits/<int:commit_id>/priority-diff",
    methods=["POST"],
    endpoint="request_priority_diff",
)
def request_priority_diff_route(commit_id):
    return _dispatch("request_priority_diff", commit_id)


@commit_diff_bp.route(
    "/<project_code>/<repository_name>/commits/<int:commit_id>/priority-diff",
    methods=["POST"],
    endpoint="request_priority_diff_with_path",
)
def request_priority_diff_with_path_route(project_code, repository_name, commit_id):
    return _dispatch("request_priority_diff_with_path", project_code, repository_name, commit_id)


@commit_diff_bp.route(
    "/commits/<int:commit_id>/diff-data",
    methods=["GET"],
    endpoint="get_commit_diff_data",
)
def get_commit_diff_data_route(commit_id):
    return _dispatch("get_commit_diff_data", commit_id)


@commit_diff_bp.route(
    "/commits/merge-diff/refresh",
    methods=["POST"],
    endpoint="refresh_merge_diff",
)
def refresh_merge_diff_route():
    return _dispatch("refresh_merge_diff")


@commit_diff_bp.route(
    "/commits/merge-diff",
    endpoint="merge_diff",
)
def merge_diff_route():
    return _dispatch("merge_diff")


@commit_diff_bp.route(
    "/update_commit_fields",
    endpoint="update_commit_fields_route",
)
def update_commit_fields_route_wrapper():
    return _dispatch("update_commit_fields_route")


@commit_diff_bp.route(
    "/repositories/<int:repository_id>/commits/by-file",
    endpoint="get_commits_by_file",
)
def get_commits_by_file_route(repository_id):
    return _dispatch("get_commits_by_file", repository_id)


@commit_diff_bp.route(
    "/commits/compare",
    endpoint="commits_compare",
)
def commits_compare_route():
    return _dispatch("commits_compare")
