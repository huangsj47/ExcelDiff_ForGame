#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Weekly-version route blueprint wrappers.

This module extracts weekly-version route registration from app.py while
delegating business logic to the original handlers to minimize regression risk.
"""

from flask import Blueprint, request

from services.model_loader import get_runtime_model
from utils.json_body import read_json_object

weekly_version_bp = Blueprint("weekly_version_routes", __name__)


def _dispatch(handler_name, *args, **kwargs):
    handler = get_runtime_model(handler_name)
    return handler(*args, **kwargs)


@weekly_version_bp.before_request
def _enforce_json_object_body():
    """本蓝图所有写接口的请求体形状在**这里**统一校验：根节点必须是对象。

    为什么在这个层做，而不是写进 handler：handler 所在的
    `services/weekly_version_logic.py` 已经顶在 1800 行的拆分预算上
    （`tests/test_todo_split_followup_round2.py` 断言 `< 1800`），三行守卫加进去会
    直接把那条棘轮测试弄红。

    为什么不会漏掉那两个入口：`weekly_version_config_api`（POST 创建）与
    `weekly_version_config_detail_api`（PUT 更新）都由**本蓝图**注册
    （见下面的 route 定义），蓝图级 `before_request` 对蓝图内每个请求都生效 ——
    比逐个 wrapper 加守卫更难漏。

    只对**真的带了 body 的 JSON 请求**生效。`silent=False` 与 handler 原先的
    `request.get_json()` 一致，Content-Type 是 JSON 但 body 不是合法 JSON 时照旧 400，
    不顺手改掉这个语义。

    ## 为什么不能只看 `request.is_json`

    这一条原先写的是「`request.is_json` 为假时（无 body 的 GET/DELETE、表单提交）
    直接放行」—— **那个前提是错的**：`is_json` 只看 `Content-Type`，**说明不了有没有
    body**。于是「带着 `Content-Type: application/json` 发一个没有 body 的请求」会被
    判成「body 不是合法 JSON」，得到 400。

    这不是理论边界，本项目的页面就是这么发的：`weekly_version_config.html` 的
    `editConfig`（GET 详情）与 `deleteConfig`（DELETE）都写着
    `headers: {'Content-Type': 'application/json'}` 而没有 body。现象是**编辑按钮点了
    报「获取配置信息失败，请重试」**、删除与重命名一起失效 —— 而且返回的是 Flask 默认的
    **HTML** 400，前端 `response.json()` 先抛异常，连服务端的 message 都读不到，
    排查方向会被带到「接口 500 了」上去。

    所以判据换成「有没有 body」：没有 body 就没有形状可校验，无论什么方法。

    顺带一提，无 body 的请求本来也不会出这条守卫要防的那个错：
    handler 里的 `request.get_json(silent=True) or {}` 对空 body 得到的就是 `{}`，
    不会像根节点是数组那样崩在 `.get()` 上。所以放行它不降低任何保护。
    """
    if not request.is_json:
        return None
    if not request.get_data():
        return None
    _payload, error = read_json_object(silent=False)
    return error


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
