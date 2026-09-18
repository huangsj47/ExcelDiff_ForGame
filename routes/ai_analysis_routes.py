#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI analysis routes.
"""

from flask import Blueprint, Response, jsonify, render_template, request, stream_with_context

from models import Commit, Project, Repository, WeeklyVersionConfig, db
from models.ai_analysis import AiAnalysisRun
from services.ai import project_pack_service, run_progress
from services.ai.analysis_budget import budget_status, platform_budget_status, with_live_usage
from services.ai.endpoint_service import ConfigValidationError, probe_connection, probe_models
from services.ai.platform_budget import platform_budget_public, set_platform_budget
from services.ai.pricing import estimate_cost
from services.ai_analysis_service import (
    build_endpoint_client,
    get_latest_commit_result,
    get_latest_weekly_result,
    get_project_analysis_config,
    get_project_api_key_status,
    project_price_table,
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
    require_admin,
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


@ai_analysis_bp.route("/ai-analysis/runs/<int:run_id>/progress", methods=["GET"])
def ai_run_progress(run_id):
    """**跑的过程中的一眼**：第几轮、已用多少 token、现在有没有超预算。

    为什么要轮询而不是 SSE 推：分析入口是「生成器里跑一次阻塞调用」，
    `_execute_analysis` 返回之前一个事件都 yield 不出去（见 services/ai/run_progress.py
    的模块 docstring）。引擎每跑完一轮把累计用量写进进程内的快照，这里读它。

    三件事必须说清：

    * `progress` 为 `null` 表示**读不到**（没在跑 / 跑在别的进程 / 已过期），
      界面要显示「进度不可用」，**不是显示 0**；
    * `budget` 是把**本次运行尚未落库**的用量算进去之后的判定（`with_live_usage`），
      所以它可能比 `/config` 返回的那份更早显示「已超」—— 这正是这个端点的用途；
    * 权限按**这条运行自己的 project_id** 判，不接受调用方指定项目（换个号就能读别人的
      消耗与预算）。只读库 + 读进程内快照，**不发起任何计费调用**。
    """
    run = db.session.get(AiAnalysisRun, run_id)
    if run is None:
        return jsonify({"success": False, "message": "Not found."}), 404
    if not _has_project_access(run.project_id):
        return jsonify({"success": False, "message": "Access denied."}), 403

    snap = run_progress.snapshot(run_id)
    status = budget_status(run.project_id)
    payload = {
        "success": True,
        "run_id": run_id,
        "project_id": run.project_id,
        "status": run.effective_status,
        "progress": snap.to_dict() if snap else None,
        "budget": status,
    }
    if snap is None:
        return jsonify(payload), 200

    # 本次运行**还没落库**的那部分用量：token 直接取快照，费用按这条运行的模型与项目
    # 价格表现算（算不出就是 `None`，`with_live_usage` 会据此**不判**费用那一档）。
    table, _errors = project_price_table(run.project_id)
    live_cost = None
    currency = ""
    if table is not None:
        estimate = estimate_cost(
            str(run.model or ""),
            tokens_input=snap.prompt_tokens,
            tokens_output=snap.completion_tokens,
            cache_read=snap.cache_read_tokens,
            table=table,
        )
        live_cost = estimate.amount
        currency = str(estimate.currency or "")
    payload["budget"] = with_live_usage(
        status,
        tokens=snap.live_tokens,
        cost=live_cost,
        currency=currency,
    )
    return jsonify(payload), 200


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

    **这个页面只需要登录**（`enforce_admin_access` 对它的要求就是登录，它不是
    `/admin/` 下的路径，也不在 `SENSITIVE_ENDPOINTS` 里），**没有**第二个装饰器 ——
    与 `/ai-analysis/*` 其余接口「有项目权限就能看」是同一条口径。

    页面本身**不含任何数据**：数据由下面的三个端点按**当前用户可访问的项目**回，
    所以「谁能打开这一页」与「他能看到什么」是两件事 —— 后者才是权限边界，
    前者只是一个空壳。（这里原先的注释写成「闸门由全局 enforce_admin_access 把守，
    与缓存管理页一致」，那是错的：缓存管理页在 `/admin/` 下，会被按路径要求管理员。）
    """
    return render_template("ai_usage_dashboard.html")


# ---------------------------------------------------------------------------
#  平台总预算（全平台合计的上限）
# ---------------------------------------------------------------------------
# **刻意不挂在 `/ai-analysis/usage/` 下面**：那一族路径被一条测试钉着「只能是 GET、
# 不能有 POST」（`test_the_three_endpoints_are_read_only`），守的是「别在一个只读页面上
# 顺手放一个会真花钱的『重新分析』按钮」。预算保存不是计费动作，但它确实是个写接口 ——
# 与其放宽那条护栏，不如把写接口放在它该在的地方：**配置**。
# 这也与单价表一致（单价表的写侧走 `/ai-analysis/projects/<id>/config`，同样不在 /usage 下）。
#
# ## 读：与这一页的其它端点同一档权限（登录 + 项目权限）；写：平台管理员
#
# 读侧原来也挂了 `@require_admin`，理由是「它回的是全平台合计的消耗，能反推别人的花费」。
# **那个理由是错的**，而且错得有害：`usage_overview`（这一页的主数据）本来就把
# `platform_budget` 与 `platform_status`（含全平台 used/limits）下发给任何有项目权限的
# 用户 —— 于是这道闸门一个字节都没挡住，只让非管理员看到一张「你没有权限查看」的卡片，
# 而同样的数字就在他刚拿到的总览响应里。**假装挡住比不挡更糟**：用户会以为自己看错了，
# 或者以为平台有 bug。
#
# 所以读侧与页面上其它端点一致（登录即可，与 `usage_overview` 同一档）；写侧仍然只有
# 平台管理员能动（`@require_admin` 在下面的 POST 上）。若日后确实要让「平台合计」只对
# 管理员可见，正确的做法是**在服务端把 overview 里那几个字段按 is_admin() 置空**，
# 而不是只堵这一个端点。

@ai_analysis_bp.route("/ai-analysis/platform-budget", methods=["GET"])
def ai_platform_budget():
    """平台总预算：配置 + 当前状态 + **谁能改**。

    与 `usage_overview` 同权限（见上面的说明）：这个数已经在总览响应里了，单独给它加
    管理员闸门只会让界面显示一张假的「无权限」卡片。

    所以「非管理员只读」这件事必须由**响应自己说清**（`can_edit`），而不是靠一个 403：
    读得到但改不了的人，看到的应该是「数字 + 一句‘只有平台管理员能改’」，而不是
    「你没有权限查看」——后者是错的（他刚在总览里看到过这些数），而且会让他以为页面坏了。
    """
    return jsonify(
        {
            "success": True,
            "budget": platform_budget_public(),
            "status": platform_budget_status(),
            "can_edit": _can_edit_platform_budget(),
        }
    ), 200


def _can_edit_platform_budget() -> bool:
    """能不能改平台总预算。**与写接口用的是同一个判据**（`require_admin` 内部那一个）。

    界面据此决定编辑器是否可用；判定本身仍由写接口再做一次 —— 前端禁用只是提示，
    不是权限（那一条是这个仓库一贯的口径：权限判定永远在服务端）。

    `ENABLE_ADMIN_SECURITY` 关掉时整条安全链都是放行的（见 `require_admin`），这里跟着
    放行 —— 否则本地/内网部署下界面会显示成只读，而写接口其实是通的，两边自相矛盾。

    **两个属性都在调用时从模块上取**（函数内 import + 属性访问），不是 `from ... import`：
    这条链上的判定函数在本仓库的测试里是靠 monkeypatch 换掉的，导入时绑定就拿不到替换值
    —— 这个坑在 `_has_project_access` 上已经踩过一次。
    """
    from utils import request_security

    if not getattr(request_security, "ENABLE_ADMIN_SECURITY", True):
        return True
    try:
        return bool(request_security._has_admin_access())
    except Exception:  # noqa: BLE001 —— 查不出管理员身份时按「不能改」处理
        return False


@ai_analysis_bp.route("/ai-analysis/platform-budget", methods=["POST"])
@require_admin
def ai_platform_budget_update():
    """保存平台总预算。**部分更新**：只写提交里出现的字段。

    校验复用项目档那一套（`services/ai/platform_budget.py`），所以「填 0」这类会在
    两处得到同一句错误，而不是一处允许、另一处拒绝。
    """
    payload, error = read_json_object()
    if error is not None:
        return error
    user = _get_current_user()
    username = getattr(user, "username", "") if user else ""
    ok, message, errors = set_platform_budget(payload, updated_by=username)
    body = {"success": ok, "message": message}
    if errors:
        body["errors"] = errors
    if ok:
        # 保存后立刻回一份新状态：界面要马上显示「现在限制到多少、已用多少」，
        # 让它再发一次 GET 会把「保存成功」与「状态刷新」变成两次可能不一致的往返。
        body["budget"] = platform_budget_public()
        body["status"] = platform_budget_status()
    return jsonify(body), 200 if ok else 400


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


# ---------------------------------------------------------------------------
#  项目专属知识包（`skills/projects/<项目代号>/`）
# ---------------------------------------------------------------------------
# 知识包原本只能靠人往服务器上放文件，界面上完全没有入口。这一组接口把那条写路径
# 补上，并且**写入前一律过 `skill_contract` 的校验闸门**（见 project_pack_service）。
#
# 权限照抄 `/config` 那条既有口径，不另立一套：
#   * 读（列表 / 读某个文件）→ `_has_project_access`，与配置的 GET 一致；
#   * 写（新建 / 覆盖 / 删除）→ `_has_project_admin_access`，与配置的 POST 一致。
# 知识包会被注入提示词，改它等于改所有项目的评审上下文，所以写侧必须是项目管理员。
#
# **路径形状本身就是一道闸门**：`<name>` 是 Flask 的单段转换器，不含分隔符；
# 类型段只有 manifest / references / skills 三种，各自落在唯一的相对路径上
# （见 `project_pack_service._rel_path_for`）。调用方**没有**办法提交一个自由路径。


def _knowledge_project_code(project_id):
    """取项目代号，顺带把「代号为空 / 项目不存在」这两种情况回成可读的错误。"""
    project = db.session.get(Project, project_id)
    if project is None:
        return None, (jsonify({"success": False, "message": "项目不存在。", "errors": []}), 404)
    return getattr(project, "code", "") or "", None


def _knowledge_read(project_id, action):
    """读侧的统一入口：判权限 → 取代号 → 执行 → 错误回字段级明细。"""
    if not _has_project_access(project_id):
        return jsonify({"success": False, "message": "Access denied.", "errors": []}), 403
    code, error = _knowledge_project_code(project_id)
    if error is not None:
        return error
    try:
        payload = action(code)
    except ConfigValidationError as exc:
        return _knowledge_error_response(exc, http_status=400)
    except OSError as exc:
        # 磁盘层面的失败（权限、被占用）不是用户的输入问题，回 500 但要说清是哪一步。
        return jsonify(
            {
                "success": False,
                "message": f"读写知识包目录失败：{exc}",
                "errors": [],
            }
        ), 500
    return jsonify({"success": True, **payload}), 200


def _knowledge_write(project_id, action):
    """写侧的统一入口。与读侧的差别只有权限判定与成功文案。"""
    if not _has_project_admin_access(project_id):
        return jsonify({"success": False, "message": "Admin permission required.", "errors": []}), 403
    code, error = _knowledge_project_code(project_id)
    if error is not None:
        return error
    try:
        payload = action(code)
    except ConfigValidationError as exc:
        return _knowledge_error_response(exc, http_status=400)
    except OSError as exc:
        return jsonify(
            {
                "success": False,
                "message": f"写入知识包目录失败：{exc}",
                "errors": [],
            }
        ), 500
    return jsonify({"success": True, **payload}), 200


def _knowledge_error_response(exc, *, http_status):
    """把 `ConfigValidationError` 变成 `{success, message, errors[]}`。

    `errors` 一定存在（哪怕是空列表）：前端读 `data.errors` 时不必先判存在，
    「字段级明细」这条契约就不会因为某一条分支没带而静默降级成「只有一个 toast」。
    """
    errors = [item.as_dict() for item in exc.errors]
    return (
        jsonify(
            {
                "success": False,
                "message": "；".join(f"{item['label']}：{item['message']}" for item in errors)
                or "保存失败。",
                "errors": errors,
            }
        ),
        http_status,
    )


@ai_analysis_bp.route("/ai-analysis/projects/<int:project_id>/knowledge", methods=["GET"])
def ai_project_knowledge(project_id):
    """列出知识包内容。**目录不存在不是错误**（`exists=False` + 空列表）。"""
    return _knowledge_read(project_id, lambda code: project_pack_service.describe_pack(code))


@ai_analysis_bp.route("/ai-analysis/projects/<int:project_id>/knowledge/scaffold", methods=["POST"])
def ai_project_knowledge_scaffold(project_id):
    """从模板创建知识包：清单 + 起始文档，一次写完。

    **不能拆成「先建清单、再加文档」两次调用**：建包模板的正文引用了那两份文档，
    而 `skill_contract` 的 references 校验是双向的 —— 中间那一步必然不合法。
    这条注释是给未来的自己看的：把这里拆开就会得到一个「怎么点都失败」的按钮。
    """
    return _knowledge_write(
        project_id,
        lambda code: project_pack_service.create_pack_from_template(code),
    )


@ai_analysis_bp.route("/ai-analysis/projects/<int:project_id>/knowledge/manifest", methods=["GET"])
def ai_project_knowledge_manifest(project_id):
    return _knowledge_read(
        project_id,
        lambda code: project_pack_service.read_entry(
            code, project_pack_service.KIND_MANIFEST, ""
        ),
    )


@ai_analysis_bp.route(
    "/ai-analysis/projects/<int:project_id>/knowledge/references/<name>", methods=["GET"]
)
def ai_project_knowledge_reference(project_id, name):
    return _knowledge_read(
        project_id,
        lambda code: project_pack_service.read_entry(
            code, project_pack_service.KIND_REFERENCE, name
        ),
    )


@ai_analysis_bp.route(
    "/ai-analysis/projects/<int:project_id>/knowledge/skills/<name>", methods=["GET"]
)
def ai_project_knowledge_skill(project_id, name):
    return _knowledge_read(
        project_id,
        lambda code: project_pack_service.read_entry(
            code, project_pack_service.KIND_SKILL, name
        ),
    )


@ai_analysis_bp.route("/ai-analysis/projects/<int:project_id>/knowledge/manifest", methods=["PUT"])
def ai_project_knowledge_manifest_save(project_id):
    """新建 / 覆盖知识包清单。"""
    payload, error = read_json_object()
    if error is not None:
        return error
    return _knowledge_write(
        project_id,
        lambda code: project_pack_service.write_manifest(code, payload.get("content", "")),
    )


@ai_analysis_bp.route("/ai-analysis/projects/<int:project_id>/knowledge/manifest", methods=["DELETE"])
def ai_project_knowledge_manifest_delete(project_id):
    """清单**不允许删除** —— 这里刻意返回一条能读懂的拒绝，而不是 405。

    删掉清单之后，包里的文档一份也读不到（模型只知道清单里列出的东西），
    而 `validate_project_pack` 也会把整个包判成不合法。让用户看到一句
    「为什么不能删」，比让他的请求掉进一个没有路由的 404/405 有用。
    """
    if not _has_project_admin_access(project_id):
        return jsonify({"success": False, "message": "Admin permission required.", "errors": []}), 403
    return jsonify(
        {
            "success": False,
            "message": "知识包清单（KNOWLEDGE.md）不能删除。它是知识包的入口，"
            "删掉之后包里的文档一份也读不到。如果整包不再需要，请直接删除其中的文档。",
            "errors": [
                {
                    "field": "__pack__",
                    "label": "知识包清单",
                    "message": "清单是知识包的入口，不支持删除。",
                }
            ],
        }
    ), 400


@ai_analysis_bp.route(
    "/ai-analysis/projects/<int:project_id>/knowledge/references/<name>", methods=["PUT"]
)
def ai_project_knowledge_reference_save(project_id, name):
    payload, error = read_json_object()
    if error is not None:
        return error
    return _knowledge_write(
        project_id,
        lambda code: project_pack_service.write_reference(
            code, name, payload.get("content", ""), description=payload.get("description", "")
        ),
    )


@ai_analysis_bp.route(
    "/ai-analysis/projects/<int:project_id>/knowledge/references/<name>", methods=["DELETE"]
)
def ai_project_knowledge_reference_delete(project_id, name):
    return _knowledge_write(
        project_id,
        lambda code: project_pack_service.delete_reference(code, name),
    )


@ai_analysis_bp.route(
    "/ai-analysis/projects/<int:project_id>/knowledge/skills/<name>", methods=["PUT"]
)
def ai_project_knowledge_skill_save(project_id, name):
    """新建 / 整体覆盖一个子 skill。

    frontmatter 由服务端按「目录名 + 一句话说明」拼出来，**不接受整份 SKILL.md 文本**：
    让用户手写 frontmatter 是必然出错的（值里一个 `": "` 就会被 YAML 截断，
    而本平台刻意不引 PyYAML 依赖，所以那被直接判非法）。
    """
    payload, error = read_json_object()
    if error is not None:
        return error
    return _knowledge_write(
        project_id,
        lambda code: project_pack_service.write_skill(
            code,
            name,
            description=payload.get("description", ""),
            body=payload.get("body", ""),
        ),
    )


@ai_analysis_bp.route(
    "/ai-analysis/projects/<int:project_id>/knowledge/skills/<name>", methods=["DELETE"]
)
def ai_project_knowledge_skill_delete(project_id, name):
    return _knowledge_write(
        project_id,
        lambda code: project_pack_service.delete_skill(code, name),
    )
