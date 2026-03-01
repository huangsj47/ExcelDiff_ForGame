#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Core app route blueprint wrappers.

This module extracts the remaining non-weekly/non-cache/non-commit route
registration from app.py. Handlers stay in app.py for low-risk behavior parity.
"""

from flask import Blueprint

from services.model_loader import get_runtime_model


core_management_bp = Blueprint("core_management_routes", __name__)


def _dispatch(handler_name, *args, **kwargs):
    handler = get_runtime_model(handler_name)
    return handler(*args, **kwargs)


@core_management_bp.route("/auth/login", methods=["GET", "POST"], endpoint="admin_login")
def admin_login_route():
    return _dispatch("admin_login")


@core_management_bp.route("/auth/logout", methods=["POST"], endpoint="admin_logout")
def admin_logout_route():
    return _dispatch("admin_logout")


@core_management_bp.route("/test", endpoint="test")
def test_route():
    return _dispatch("test")


@core_management_bp.route("/", endpoint="index")
def index_route():
    return _dispatch("index")


@core_management_bp.route("/projects", methods=["GET", "POST"], endpoint="projects")
def projects_route():
    return _dispatch("projects")


@core_management_bp.route("/projects/<int:project_id>", endpoint="project_detail")
def project_detail_route(project_id):
    return _dispatch("project_detail", project_id)


@core_management_bp.route("/projects/<int:project_id>/detail", endpoint="project_detail_original")
def project_detail_original_route(project_id):
    return _dispatch("project_detail_original", project_id)


@core_management_bp.route("/projects/<int:project_id>/merged-view", endpoint="merged_project_view")
def merged_project_view_route(project_id):
    return _dispatch("merged_project_view", project_id)


@core_management_bp.route("/status-sync/clear-all", methods=["POST"], endpoint="clear_all_confirmation_status")
def clear_all_confirmation_status_route():
    return _dispatch("clear_all_confirmation_status")


@core_management_bp.route("/status-sync/mapping-info", endpoint="get_sync_mapping_info")
def get_sync_mapping_info_route():
    return _dispatch("get_sync_mapping_info")


@core_management_bp.route("/status-sync/management", endpoint="status_sync_management")
def status_sync_management_route():
    return _dispatch("status_sync_management")


@core_management_bp.route("/<project_code>/status-sync/management", endpoint="project_status_sync_management")
def project_status_sync_management_route(project_code):
    return _dispatch("project_status_sync_management", project_code)


@core_management_bp.route("/status-sync/test", endpoint="status_sync_test")
def status_sync_test_route():
    return _dispatch("status_sync_test")


@core_management_bp.route("/status-sync/configs", endpoint="get_sync_configs")
def get_sync_configs_route():
    return _dispatch("get_sync_configs")


@core_management_bp.route("/projects/<int:project_id>/repositories", endpoint="repository_config")
def repository_config_route(project_id):
    return _dispatch("repository_config", project_id)


@core_management_bp.route("/projects/<int:project_id>/repositories/add-git", endpoint="add_git_repository")
def add_git_repository_route(project_id):
    return _dispatch("add_git_repository", project_id)


@core_management_bp.route("/projects/<int:project_id>/repositories/add-svn", endpoint="add_svn_repository")
def add_svn_repository_route(project_id):
    return _dispatch("add_svn_repository", project_id)


@core_management_bp.route("/repositories/git", methods=["POST"], endpoint="create_git_repository")
def create_git_repository_route():
    return _dispatch("create_git_repository")


@core_management_bp.route("/repositories/svn", methods=["POST"], endpoint="create_svn_repository")
def create_svn_repository_route():
    return _dispatch("create_svn_repository")


@core_management_bp.route(
    "/repositories/<int:repository_id>/regenerate-cache",
    methods=["POST"],
    endpoint="regenerate_cache",
)
def regenerate_cache_route(repository_id):
    return _dispatch("regenerate_cache", repository_id)


@core_management_bp.route("/repositories/<int:repository_id>/cache-status", endpoint="get_cache_status")
def get_cache_status_route(repository_id):
    return _dispatch("get_cache_status", repository_id)


@core_management_bp.route(
    "/repositories/<int:repository_id>/retry-clone",
    methods=["POST"],
    endpoint="retry_clone_repository",
)
def retry_clone_repository_route(repository_id):
    return _dispatch("retry_clone_repository", repository_id)


@core_management_bp.route("/repositories/<int:repository_id>/sync", methods=["POST"], endpoint="sync_repository")
def sync_repository_route(repository_id):
    return _dispatch("sync_repository", repository_id)


@core_management_bp.route(
    "/api/repositories/<int:repository_id>/reuse-and-update",
    methods=["POST"],
    endpoint="reuse_repository_and_update",
)
def reuse_repository_and_update_route(repository_id):
    return _dispatch("reuse_repository_and_update", repository_id)


@core_management_bp.route("/repositories/<int:repository_id>/edit", endpoint="edit_repository")
def edit_repository_route(repository_id):
    return _dispatch("edit_repository", repository_id)


@core_management_bp.route("/repositories/<int:repository_id>/update", methods=["POST"], endpoint="update_repository")
def update_repository_route(repository_id):
    return _dispatch("update_repository", repository_id)


@core_management_bp.route(
    "/repositories/<int:repository_id>/update-api",
    methods=["POST"],
    endpoint="update_repository_and_cache",
)
def update_repository_and_cache_route(repository_id):
    return _dispatch("update_repository_and_cache", repository_id)


@core_management_bp.route(
    "/repositories/batch-update-credentials",
    methods=["POST"],
    endpoint="batch_update_credentials",
)
def batch_update_credentials_route():
    return _dispatch("batch_update_credentials")


@core_management_bp.route("/repositories/update-order", methods=["POST"], endpoint="update_repository_order")
def update_repository_order_route():
    return _dispatch("update_repository_order")


@core_management_bp.route("/repositories/swap-order", methods=["POST"], endpoint="swap_repository_order")
def swap_repository_order_route():
    return _dispatch("swap_repository_order")


@core_management_bp.route("/repositories/<int:repository_id>/delete", methods=["POST"], endpoint="delete_repository")
def delete_repository_route(repository_id):
    return _dispatch("delete_repository", repository_id)


@core_management_bp.route("/repositories/<int:repository_id>/test", methods=["POST"], endpoint="test_repository")
def test_repository_route(repository_id):
    return _dispatch("test_repository", repository_id)


@core_management_bp.route("/projects/<int:project_id>/delete", methods=["POST"], endpoint="delete_project")
def delete_project_route(project_id):
    return _dispatch("delete_project", project_id)


@core_management_bp.route("/repositories/compare", endpoint="repository_compare")
def repository_compare_route():
    return _dispatch("repository_compare")
