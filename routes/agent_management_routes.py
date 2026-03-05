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


@agent_management_bp.route("/api/agents", methods=["GET"], endpoint="list_agent_nodes")
def list_agent_nodes_route():
    return _dispatch("list_agent_nodes")


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


@agent_management_bp.route(
    "/api/agents/tasks/<int:task_id>/result",
    methods=["POST"],
    endpoint="agent_report_task_result",
)
def agent_report_task_result_route(task_id):
    return _dispatch("agent_report_task_result", task_id)


@agent_management_bp.route(
    "/api/agents/tasks/<int:task_id>/execute-proxy",
    methods=["POST"],
    endpoint="agent_execute_task_proxy",
)
def agent_execute_task_proxy_route(task_id):
    return _dispatch("agent_execute_task_proxy", task_id)
