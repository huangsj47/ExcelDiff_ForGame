#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Weekly-version route blueprint wrappers.

This module extracts weekly-version route registration from app.py while
delegating business logic to the original handlers to minimize regression risk.
"""

from flask import Blueprint

from services.model_loader import get_runtime_model


weekly_version_bp = Blueprint("weekly_version_routes", __name__)


def _dispatch(handler_name, *args, **kwargs):
    handler = get_runtime_model(handler_name)
    return handler(*args, **kwargs)


@weekly_version_bp.route(
    "/projects/<int:project_id>/weekly-version-config",
    endpoint="weekly_version_config",
)
def weekly_version_config_route(project_id):
    return _dispatch("weekly_version_config", project_id)


@weekly_version_bp.route(
    "/projects/<int:project_id>/weekly-version-config/api",
    methods=["GET", "POST"],
    endpoint="weekly_version_config_api",
)
def weekly_version_config_api_route(project_id):
    return _dispatch("weekly_version_config_api", project_id)


@weekly_version_bp.route(
    "/projects/<int:project_id>/weekly-version-config/api/<int:config_id>",
    methods=["GET", "PUT", "DELETE"],
    endpoint="weekly_version_config_detail_api",
)
def weekly_version_config_detail_api_route(project_id, config_id):
    return _dispatch("weekly_version_config_detail_api", project_id, config_id)


@weekly_version_bp.route(
    "/projects/<int:project_id>/weekly-version",
    endpoint="weekly_version_list",
)
def weekly_version_list_route(project_id):
    return _dispatch("weekly_version_list", project_id)


@weekly_version_bp.route(
    "/weekly-version-config/<int:config_id>/diff",
    endpoint="weekly_version_diff",
)
def weekly_version_diff_route(config_id):
    return _dispatch("weekly_version_diff", config_id)


@weekly_version_bp.route(
    "/weekly-version-config/<int:config_id>/info",
    endpoint="weekly_version_config_info_api",
)
def weekly_version_config_info_api_route(config_id):
    return _dispatch("weekly_version_config_info_api", config_id)


@weekly_version_bp.route(
    "/weekly-version-config/<int:config_id>/files",
    endpoint="weekly_version_files_api",
)
def weekly_version_files_api_route(config_id):
    return _dispatch("weekly_version_files_api", config_id)


@weekly_version_bp.route(
    "/weekly-version-config/<int:config_id>/file-diff",
    endpoint="weekly_version_file_diff_api",
)
def weekly_version_file_diff_api_route(config_id):
    return _dispatch("weekly_version_file_diff_api", config_id)


@weekly_version_bp.route(
    "/weekly-version-config/<int:config_id>/file-full-diff",
    endpoint="weekly_version_file_full_diff",
)
def weekly_version_file_full_diff_route(config_id):
    return _dispatch("weekly_version_file_full_diff", config_id)


@weekly_version_bp.route(
    "/weekly-version-config/<int:config_id>/file-full-diff-data",
    endpoint="weekly_version_file_full_diff_data",
)
def weekly_version_file_full_diff_data_route(config_id):
    return _dispatch("weekly_version_file_full_diff_data", config_id)


@weekly_version_bp.route(
    "/weekly-version-config/<int:config_id>/file-previous-version",
    endpoint="weekly_version_file_previous_version",
)
def weekly_version_file_previous_version_route(config_id):
    return _dispatch("weekly_version_file_previous_version", config_id)


@weekly_version_bp.route(
    "/weekly-version-config/<int:config_id>/file-complete-diff",
    endpoint="weekly_version_file_complete_diff",
)
def weekly_version_file_complete_diff_route(config_id):
    return _dispatch("weekly_version_file_complete_diff", config_id)


@weekly_version_bp.route(
    "/weekly-version-config/<int:config_id>/file-status",
    methods=["POST"],
    endpoint="weekly_version_file_status_api",
)
def weekly_version_file_status_api_route(config_id):
    return _dispatch("weekly_version_file_status_api", config_id)


@weekly_version_bp.route(
    "/weekly-version-config/<int:config_id>/file-status-info",
    endpoint="weekly_version_file_status_info_api",
)
def weekly_version_file_status_info_api_route(config_id):
    return _dispatch("weekly_version_file_status_info_api", config_id)


@weekly_version_bp.route(
    "/weekly-version-config/<int:config_id>/batch-confirm",
    methods=["POST"],
    endpoint="weekly_version_batch_confirm_api",
)
def weekly_version_batch_confirm_api_route(config_id):
    return _dispatch("weekly_version_batch_confirm_api", config_id)


@weekly_version_bp.route(
    "/weekly-version-config/<int:config_id>/stats",
    endpoint="weekly_version_stats_api",
)
def weekly_version_stats_api_route(config_id):
    return _dispatch("weekly_version_stats_api", config_id)
