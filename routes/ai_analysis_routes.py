#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI analysis routes.
"""

from flask import Blueprint, Response, jsonify, render_template, request, stream_with_context

from models import Commit, Repository, WeeklyVersionConfig, db
from models.ai_analysis import AiAnalysisRun
from services.ai.analysis_budget import budget_status
from services.ai.endpoint_service import probe_connection, probe_models
from services.ai_analysis_service import (
    build_endpoint_client,
    get_latest_commit_result,
    get_latest_weekly_result,
    get_project_analysis_config,
    get_project_api_key_status,
    set_project_api_key,
    stream_commit_analysis,
    stream_weekly_analysis,
    update_project_analysis_config,
)
from services.ai_usage_service import (
    parse_usage_filters,
    project_usage,
    run_usage,
    usage_overview,
)
from utils.json_body import read_json_object
from utils.request_security import (
    _get_accessible_project_ids,
    _get_current_user,
    _has_project_access,
    _has_project_admin_access,
)

ai_analysis_bp = Blueprint("ai_analysis_routes", __name__)


@ai_analysis_bp.route("/ai-analysis/projects/<int:project_id>/api-key/status", methods=["GET"])
def ai_project_key_status(project_id):
    if not _has_project_access(project_id):
        return jsonify({"success": False, "message": "Access denied."}), 403
    status = get_project_api_key_status(project_id)
    return jsonify({"success": True, **status})


@ai_analysis_bp.route("/ai-analysis/projects/<int:project_id>/api-key", methods=["POST"])
def ai_project_key_update(project_id):
    if not _has_project_admin_access(project_id):
        return jsonify({"success": False, "message": "Admin permission required."}), 403
    payload, error = read_json_object()
    if error is not None:
        return error
    api_key = payload.get("api_key", "")
    if not isinstance(api_key, str):
        # Token 只有字符串这一种合法形态。**不 str() 兜底**：那会把 123 静默存成
        # "123"、把 [1,2] 存成 "[1, 2]"，用户以为配上的东西根本不是他填的。
        return jsonify({"success": False, "message": "API Token 必须是字符串。"}), 400
    user = _get_current_user()
    username = getattr(user, "username", "") if user else ""
    ok, message = set_project_api_key(project_id, api_key, updated_by=username)
    status_code = 200 if ok else 400
    return jsonify({"success": ok, "message": message}), status_code


@ai_analysis_bp.route("/ai-analysis/projects/<int:project_id>/config", methods=["GET"])
def ai_project_config(project_id):
    if not _has_project_access(project_id):
        return jsonify({"success": False, "message": "Access denied."}), 403
    config = get_project_analysis_config(project_id)
    # 预算状态挂在这里下发，而不是塞进 `get_project_analysis_config`：
    # 后者被 `project_price_table` 调用，而预算判定自己要读价格表 —— 塞进去就是
    # 一条 `project_price_table → get_project_analysis_config → budget_status →
    # project_price_table` 的无限递归。这一层只读、只多查一次，没有这个环。
    return jsonify({"success": True, **config, "budget": budget_status(project_id)})


@ai_analysis_bp.route("/ai-analysis/projects/<int:project_id>/budget", methods=["GET"])
def ai_project_budget(project_id):
    """只读的预算状态。

    单独一个端点是为了给「分析被拦下之后」用：抽屉拿到一条预算拦截的报错，需要
    说清楚是哪个周期、已用多少、上限多少，而这些数在那个时刻未必是最新的。
    """
    if not _has_project_access(project_id):
        return jsonify({"success": False, "message": "Access denied."}), 403
    return jsonify({"success": True, "budget": budget_status(project_id)}), 200


@ai_analysis_bp.route("/ai-analysis/projects/<int:project_id>/config", methods=["POST"])
def ai_project_config_update(project_id):
    if not _has_project_admin_access(project_id):
        return jsonify({"success": False, "message": "Admin permission required."}), 403
    payload, error = read_json_object()
    if error is not None:
        return error
    user = _get_current_user()
    username = getattr(user, "username", "") if user else ""
    ok, message, errors = update_project_analysis_config(
        project_id, payload, updated_by=username
    )
    body = {"success": ok, "message": message}
    if errors:
        # 字段级明细：界面据此把错误标到具体那一栏，而不是只在顶部说一句「保存失败」。
        body["errors"] = errors
    return jsonify(body), 200 if ok else 400


def _probe_guard(project_id):
    """两个探测接口共用的权限判定。

    用**管理员**权限而不是访问权限：这两个接口会带着项目密钥去请求外部地址，
    普通成员不该有能力触发它（也就没人能拿它当探测内网的跳板）。
    """
    if not _has_project_admin_access(project_id):
        return jsonify({"success": False, "message": "Admin permission required."}), 403
    return None


@ai_analysis_bp.route("/ai-analysis/projects/<int:project_id>/models", methods=["POST"])
def ai_project_models(project_id):
    """取可用模型列表。

    **由服务端带着密钥去请求**，只把模型 id 字符串回给浏览器 —— 密钥绝不能下发到前端。
    取不到列表时返回 `success=True` + `supported=False`（**不是错误**）：实测存在
    「能正常对话但不在列表里」的模型，所以这不阻断任何操作，界面引导用户手填即可。
    """
    denied = _probe_guard(project_id)
    if denied is not None:
        return denied

    # body 形状必须在**建客户端之前**判定：这个接口会带着项目密钥去请求用户填的地址，
    # 解析失败就不该有任何出网动作。
    payload, error = read_json_object()
    if error is not None:
        return error

    client, errors = build_endpoint_client(project_id, payload)
    if client is None:
        return jsonify({"success": False, "message": "配置不完整", "errors": errors}), 400

    result = probe_models(client)
    return jsonify({"success": True, **result.as_dict()}), 200


@ai_analysis_bp.route(
    "/ai-analysis/projects/<int:project_id>/test-connection", methods=["POST"]
)
def ai_project_test_connection(project_id):
    """用**请求体里当前填的**地址/Token/模型发一次最小对话。

    未保存也能测，用户不必先把一个可能错的配置存下来。输入框留空表示沿用已保存的值。
    """
    denied = _probe_guard(project_id)
    if denied is not None:
        return denied

    payload, error = read_json_object()
    if error is not None:
        return error

    client, errors = build_endpoint_client(project_id, payload)
    if client is None:
        return jsonify({"success": False, "message": "配置不完整", "errors": errors}), 400

    result = probe_connection(client)
    return jsonify({"success": True, **result.as_dict()}), 200


@ai_analysis_bp.route("/ai-analysis/commit/<int:commit_id>/stream", methods=["GET"])
def ai_commit_stream(commit_id):
    user = _get_current_user()
    username = getattr(user, "username", "") if user else ""
    commit = Commit.query.get_or_404(commit_id)
    repo = db.session.get(Repository, commit.repository_id)
    project_id = repo.project_id if repo else None
    if project_id and not _has_project_access(project_id):
        return jsonify({"success": False, "message": "Access denied."}), 403

    def _generate():
        yield from stream_commit_analysis(commit_id, user_label=username)

    return Response(stream_with_context(_generate()), mimetype="text/event-stream")


@ai_analysis_bp.route("/ai-analysis/weekly/<int:config_id>/stream", methods=["GET"])
def ai_weekly_stream(config_id):
    trigger_source = request.args.get("source", "manual")
    # 分析范围：all（默认）/ table / code / <repository_id>。认不出来的值不会把分析变成
    # 空跑 —— `_filter_delta_files_by_focus` 一律退回「不筛」。
    focus = request.args.get("focus", "all")
    config = WeeklyVersionConfig.query.get_or_404(config_id)
    if not _has_project_access(config.project_id):
        return jsonify({"success": False, "message": "Access denied."}), 403

    def _generate():
        yield from stream_weekly_analysis(config_id, trigger_source=trigger_source, focus=focus)

    return Response(stream_with_context(_generate()), mimetype="text/event-stream")


@ai_analysis_bp.route("/ai-analysis/weekly/<int:config_id>/latest", methods=["GET"])
def ai_weekly_latest(config_id):
    config = WeeklyVersionConfig.query.get_or_404(config_id)
    if not _has_project_access(config.project_id):
        return jsonify({"success": False, "message": "Access denied."}), 403
    result = get_latest_weekly_result(config_id)
    if not result:
        return jsonify({"success": True, "result": None})
    return jsonify({"success": True, "result": result})


@ai_analysis_bp.route("/ai-analysis/commit/<int:commit_id>/latest", methods=["GET"])
def ai_commit_latest(commit_id):
    commit = Commit.query.get_or_404(commit_id)
    repo = db.session.get(Repository, commit.repository_id)
    project_id = repo.project_id if repo else None
    if project_id and not _has_project_access(project_id):
        return jsonify({"success": False, "message": "Access denied."}), 403
    result = get_latest_commit_result(commit_id)
    if not result:
        return jsonify({"success": True, "result": None})
    return jsonify({"success": True, "result": result})


# ---------------------------------------------------------------------------
#  AI 消耗面板
# ---------------------------------------------------------------------------
# 页面与数据分属两条路由：同一个路径不能既渲染页面又回 JSON。
#   /ai-analysis/usage                 → 页面（导航入口指向它）
#   /ai-analysis/usage/overview        → 跨项目总览数据
#   /ai-analysis/usage/project/<id>    → 单项目下钻数据
#   /ai-analysis/runs/<run_id>/usage   → 单次运行明细（抽屉里点开的那张表）
#
# 权限与 AI 分析一致：**有项目权限就能看**，不用 `@require_admin`
# （费用是运营数据，但它与「谁能看这个项目的分析结论」是同一批人）。
# 三个数据端点都是**只读库**，不触发任何会计费的分析调用。


@ai_analysis_bp.route("/ai-analysis/usage", methods=["GET"])
def ai_usage_dashboard():
    """AI 消耗面板页面。

    页面的登录闸门由全局 `enforce_admin_access` 统一把守（与缓存管理页一致），
    这里不再挂第二个装饰器 —— 重复的鉴权只会让人以为两处规则不同。
    页面本身不含数据，数据由下面的三个端点按**当前用户可访问的项目**回。
    """
    return render_template("ai_usage_dashboard.html")


@ai_analysis_bp.route("/ai-analysis/usage/overview", methods=["GET"])
def ai_usage_overview():
    """跨项目总览：每个项目的运行次数、token、命中率、费用、预算，外加一行平台合计。

    筛选条件从 query 来（项目 / 时间范围 / 触发来源 / 状态），**在服务端聚合**。
    非法值由 `parse_usage_filters` 回落默认并在 `filters.notes` 里说明，不会 500。
    """
    filters = parse_usage_filters(request.args)
    payload = usage_overview(_get_accessible_project_ids(), filters)
    return jsonify(payload), 200


@ai_analysis_bp.route("/ai-analysis/usage/project/<int:project_id>", methods=["GET"])
def ai_usage_project(project_id):
    """单项目下钻：周版本维度 + 逐次运行 + 按工具类型。"""
    if not _has_project_access(project_id):
        return jsonify({"success": False, "message": "Access denied."}), 403
    filters = parse_usage_filters(request.args)
    return jsonify(project_usage(project_id, filters)), 200


@ai_analysis_bp.route("/ai-analysis/runs/<int:run_id>/usage", methods=["GET"])
def ai_run_usage(run_id):
    """单次运行的用量明细。

    权限按**这条运行自己的 project_id** 判，不接受调用方指定项目 —— 只信 URL 里的
    项目号的话，换个号就能读到别人的运行明细。
    """
    run = db.session.get(AiAnalysisRun, run_id)
    if run is None:
        return jsonify({"success": False, "message": "Not found."}), 404
    if not _has_project_access(run.project_id):
        return jsonify({"success": False, "message": "Access denied."}), 403
    payload = run_usage(run_id)
    if payload is None:
        return jsonify({"success": False, "message": "Not found."}), 404
    return jsonify(payload), 200
