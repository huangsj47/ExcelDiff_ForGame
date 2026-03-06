#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent 管理相关路由。"""

from flask import Blueprint

from services.model_loader import get_runtime_model


agent_management_bp = Blueprint("agent_management_routes", __name__)


def _dispatch(handler_name, *args, **kwargs):
    handler = get_runtime_model(handler_name)
    return handler(*args, **kwargs)


@agent_management_bp.route("/api/agents/register", methods=["POST"], endpoint="register_agent_node")
def register_agent_node_route():
    return _dispatch("register_agent_node")


@agent_management_bp.route("/api/agents/heartbeat", methods=["POST"], endpoint="agent_heartbeat")
def agent_heartbeat_route():
    return _dispatch("agent_heartbeat")


@agent_management_bp.route("/api/agents/incidents/report", methods=["POST"], endpoint="agent_report_incident")
def agent_report_incident_route():
    return _dispatch("agent_report_incident")


@agent_management_bp.route("/api/agents", methods=["GET"], endpoint="list_agent_nodes")
def list_agent_nodes_route():
    return _dispatch("list_agent_nodes")


@agent_management_bp.route(
    "/api/agents/abnormal-summary",
    methods=["GET"],
    endpoint="agent_abnormal_summary",
)
def agent_abnormal_summary_route():
    return _dispatch("get_agent_abnormal_summary")


@agent_management_bp.route(
    "/api/agents/<string:agent_code>/incidents",
    methods=["GET"],
    endpoint="list_agent_incidents",
)
def list_agent_incidents_route(agent_code):
    return _dispatch("list_agent_incidents", agent_code)


@agent_management_bp.route(
    "/api/agents/incidents/<int:incident_id>/ignore",
    methods=["POST"],
    endpoint="ignore_agent_incident",
)
def ignore_agent_incident_route(incident_id):
    return _dispatch("ignore_agent_incident", incident_id)


@agent_management_bp.route("/admin/agents", methods=["GET"], endpoint="agent_overview_page")
def agent_overview_page_route():
    return _dispatch("agent_overview_page")


@agent_management_bp.route("/api/agents/tasks", methods=["GET"], endpoint="list_agent_tasks")
def list_agent_tasks_route():
    return _dispatch("list_agent_tasks")


@agent_management_bp.route("/api/agents/cache/upsert", methods=["POST"], endpoint="agent_upsert_temp_cache")
def agent_upsert_temp_cache_route():
    return _dispatch("agent_upsert_temp_cache")


@agent_management_bp.route("/api/agents/cache/<string:cache_key>", methods=["GET"], endpoint="get_agent_temp_cache")
def get_agent_temp_cache_route(cache_key):
    return _dispatch("get_agent_temp_cache", cache_key)


@agent_management_bp.route(
    "/api/agents/cache/<string:cache_key>/resolve",
    methods=["GET"],
    endpoint="resolve_agent_temp_cache",
)
def resolve_agent_temp_cache_route(cache_key):
    return _dispatch("resolve_agent_temp_cache", cache_key)


@agent_management_bp.route("/api/agents/tasks/claim", methods=["POST"], endpoint="agent_claim_task")
def agent_claim_task_route():
    return _dispatch("agent_claim_task")


@agent_management_bp.route("/api/agents/releases/latest", methods=["POST"], endpoint="agent_get_latest_release")
def agent_get_latest_release_route():
    return _dispatch("agent_get_latest_release")


@agent_management_bp.route("/api/agents/releases/admin/list", methods=["GET"], endpoint="list_agent_releases")
def list_agent_releases_route():
    return _dispatch("list_agent_releases")


@agent_management_bp.route("/api/agents/releases/admin/rollback", methods=["POST"], endpoint="rollback_agent_release")
def rollback_agent_release_route():
    return _dispatch("rollback_agent_release")


@agent_management_bp.route(
    "/api/agents/releases/<string:version>/package",
    methods=["GET"],
    endpoint="agent_download_release_package",
)
def agent_download_release_package_route(version):
    return _dispatch("agent_download_release_package", version)


@agent_management_bp.route(
    "/api/agents/tasks/<int:task_id>/result",
    methods=["POST"],
    endpoint="agent_report_task_result",
)
def agent_report_task_result_route(task_id):
    return _dispatch("agent_report_task_result", task_id)
