#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI analysis routes.
"""

import json
from io import BytesIO

from flask import (
    Blueprint,
    Response,
    jsonify,
    render_template,
    request,
    send_file,
    stream_with_context,
)

from models import Commit, Project, Repository, WeeklyVersionConfig, db
from models.ai_analysis import AiAnalysisAnomaly, AiAnalysisRun
from models.ai_analysis import job as job_model
from models.ai_analysis.anomaly import DEFAULT_DISPOSITION, DISPOSITION_LABELS, DISPOSITIONS
from services import ai_report_history_service as report_history_service
from services.ai import job_service, project_pack_service, report_document, run_progress, verdict
from services.ai.analysis_budget import (
    budget_status,
    platform_budget_status,
    visible_budget,
    with_live_usage,
)
from services.ai.anomaly_disposition import (
    MAX_BATCH_ITEMS,
    DispositionError,
    anomalies_of_run,
    normalize_disposition,
    note_was_truncated,
    set_disposition,
    set_many,
)
from services.ai.endpoint_service import ConfigValidationError, probe_connection, probe_models
from services.ai.platform_budget import platform_budget_public, set_platform_budget
from services.ai.pricing import estimate_cost
from services.ai.usage_statistics import (
    RESET_CONFIRM_WORD,
    parse_baseline_input,
    purge_usage_statistics,
    set_usage_baseline,
    statistics_public,
)
from services.ai_analysis_service import (
    build_endpoint_client,
    build_weekly_group_key,
    end_with_a_terminal_event,
    get_latest_commit_result,
    get_latest_weekly_result,
    get_project_analysis_config,
    get_project_api_key_status,
    project_price_table,
    set_project_api_key,
    stream_commit_analysis,
    update_project_analysis_config,
)

# 注意：**这里不再 import `stream_weekly_analysis`**。P0-01 之后周版本那条流式入口
# 没有生产调用方了（创建在 POST /jobs，订阅在 GET /jobs/<id>/events）—— 它留在
# `services/ai_analysis_service.py` 里由 Owner 处理（那是别人的文件，本轮只报告不删）。
from services.ai_usage_service import (
    analysis_estimate,
    parse_estimate_args,
    parse_usage_filters,
    project_usage,
    run_usage,
    usage_overview,
)
from utils.json_body import read_json_object
from utils.request_security import (
    _get_accessible_project_ids,
    _has_project_access,
    _has_project_admin_access,
    _resolve_current_username,
    platform_scope_visible,
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
    username = _actor_name()
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
    return jsonify({"success": True, **config, "budget": visible_budget(budget_status(project_id))})


@ai_analysis_bp.route("/ai-analysis/projects/<int:project_id>/budget", methods=["GET"])
def ai_project_budget(project_id):
    """只读的预算状态。

    单独一个端点是为了给「分析被拦下之后」用：抽屉拿到一条预算拦截的报错，需要
    说清楚是哪个周期、已用多少、上限多少，而这些数在那个时刻未必是最新的。
    """
    if not _has_project_access(project_id):
        return jsonify({"success": False, "message": "Access denied."}), 403
    return jsonify(
        {"success": True, "budget": visible_budget(budget_status(project_id))}
    ), 200


@ai_analysis_bp.route("/ai-analysis/projects/<int:project_id>/config", methods=["POST"])
def ai_project_config_update(project_id):
    if not _has_project_admin_access(project_id):
        return jsonify({"success": False, "message": "Admin permission required."}), 403
    payload, error = read_json_object()
    if error is not None:
        return error
    username = _actor_name()
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
    username = _actor_name()
    commit = Commit.query.get_or_404(commit_id)
    repo = db.session.get(Repository, commit.repository_id)
    project_id = repo.project_id if repo else None
    # **取不到 project_id 也拒绝**（原来这里是 `if project_id and not ...`，取不到就放行）。
    # `commit.repository_id` / `repository.project_id` 都是 nullable=False，删仓库又连带删
    # commit，所以今天构造不出「仓库丢了但 commit 还在」的请求 —— 但这条链上少一个
    # `project_id` 就等于**跳过权限判定**，而权限判定不该有一条 fail-open 的支路。
    if not project_id or not _has_project_access(project_id):
        return jsonify({"success": False, "message": "Access denied."}), 403

    def _generate():
        # 包一层「一定以 result / error 收尾」：生成器中途抛异常时，客户端只会看到
        # 连接断掉 —— 那句话在界面上就是含糊的「AI 分析失败或连接中断」，原因只留在
        # 服务端日志里（见 services/ai_analysis_service.py::end_with_a_terminal_event）。
        yield from end_with_a_terminal_event(
            stream_commit_analysis(commit_id, user_label=username)
        )

    return Response(stream_with_context(_generate()), mimetype="text/event-stream")


@ai_analysis_bp.route("/ai-analysis/weekly/<int:config_id>/stream", methods=["GET"])
def ai_weekly_stream(config_id):
    """**只订阅**（P0-01 之前它一边创建任务一边流式传输）。

    这条路径不再创建任何东西：不建 job、不建 task、不建 run、不发模型请求。
    它要求一个 `?job_id=`（POST `/ai-analysis/weekly/<id>/jobs` 拿到的那个）。
    没有 job_id 时回 400 并**说明去哪儿创建** —— 回 404 会让「页面是旧的」看起来
    像「服务端坏了」。

    选「改成只订阅」而不是「删掉路由」的理由：`/jobs/<id>/events` 是给新页面用的，
    而这条老路径按分组身份寻址（`config_id`），保留它能让「手里只有一个 config_id」
    的调用方（老书签、别的页面、排查时手敲的 URL）拿到一条**说得清**的回答，
    而不是一个没有路由的 404。它自己不产生任何副作用，所以留着不增加付费面。
    """
    config = WeeklyVersionConfig.query.get_or_404(config_id)
    if not _has_project_access(config.project_id):
        return jsonify({"success": False, "message": "Access denied."}), 403

    raw_job_id = (request.args.get("job_id") or "").strip()
    if not raw_job_id:
        return (
            jsonify(
                {
                    "success": False,
                    "message": (
                        "这条路径已经不创建分析了（创建与订阅是两件事）。"
                        f"请先 POST /ai-analysis/weekly/{config_id}/jobs 拿到 job_id，"
                        "再用 GET /ai-analysis/jobs/<job_id>/events 订阅，"
                        "或者直接带上 ?job_id=<job_id>。"
                    ),
                }
            ),
            400,
        )
    job = job_service.get_job(raw_job_id)
    if job is None or job.target_id != config_id:
        # 不属于这个分组的 job 一律按「没有这个 job」回答：换个 config_id 就能读到
        # 别人那次分析的进度与结论是不行的（判权在上面对 config 的 project_id 做过，
        # 这里挡的是**跨目标**读）。
        return jsonify({"success": False, "message": "Not found."}), 404
    return _job_events_response(job)


# ---------------------------------------------------------------------------
#  Job 协议（AI-P0-01）：创建身份 / 读身份 / **只订阅**身份的事件流
# ---------------------------------------------------------------------------
# 这一组把「发起任务」与「SSE 订阅」拆开：
#   POST /ai-analysis/weekly/<config_id>/jobs   → 建身份（不跑分析）
#   GET  /ai-analysis/jobs/<job_id>             → 读身份（刷新 / 关抽屉再打开时恢复）
#   GET  /ai-analysis/jobs/<job_id>/events      → 只订阅（重连不会创建任何东西）
#
# **创建任务一定不是 GET**：GET 既不可幂等（浏览器、代理、预取都会重放）又落在 CSRF
# 保护之外（`enforce_csrf` 对 GET 直接放行），而它要做的正是「可能花钱的那次发起」。

#: 心跳间隔。SSE 会被中间设备按「多久没有字节流动」掐断，而一次分析可能几分钟没有新
#: 状态 —— 心跳是**保活**，不是进度（进度另有 `progress` 帧）。
JOB_EVENTS_HEARTBEAT_SECONDS = 15

#: 一条订阅流最长活多久。与 `models/ai_analysis/job.py` 的 `JOB_STALE_SECONDS`
#: （30 分钟）同量级：超过它还没到终态的 job 由恢复扫描判死，页面也**重连一次**
#: 重新问状态，比挂一条永远不会再有事件的长连接好。
JOB_EVENTS_MAX_SECONDS = 30 * 60


def _job_state_payload(job, *, last_event_id=None, stream_timeout=False) -> dict:
    """`state` 事件：这条 job 现在到哪一步了。**状态名就是 `STATE_*` 的字面值。**"""
    payload = {
        "job_id": job.id,
        "state": job.state,
        "requested_mode": job.requested_mode,
        "effective_mode": job.effective_mode,
        "upgrade_reason": job.upgrade_reason or "",
        "trigger_source": job.trigger_source,
        "run_id": job.run_id,
        "task_id": job.task_id,
        "reused_run_id": job.reused_run_id,
        "focus": job.focus or "",
        "terminal": bool(job.is_terminal),
        "last_event_id": last_event_id,
    }
    if stream_timeout:
        # 流自己到点了（不是分析失败、也不是同步卡住）。页面据此重连一次 ——
        # 那句话必须由服务端说，否则界面只能猜「怎么忽然连不上了」。
        payload["stream_timeout"] = True
    return payload


def _job_event_frame(event_id: int, name: str, payload: dict) -> str:
    """一帧带 `id:` 的 SSE。**`id` 是 `Last-Event-ID` 的凭据**（见下面的 docstring）。"""
    body = json.dumps(payload, ensure_ascii=False, default=str)
    return f"id: {event_id}\nevent: {name}\ndata: {body}\n\n"


def _job_event_frames(job_id, last_event_id=None):
    """订阅一条 job 的事件流。**只读**：全过程不写任何一行。

    ## 事件词汇（三份模板的既有处理器就认这几个）

    * `state`   —— 当前状态（状态名 = `AiAnalysisJob.STATE_*` 字面值）。首帧必发；
    * `waiting` —— 在等同步跑完（文案取 `describe_waiting_analysis`，服务端给）；
    * `progress`—— 跑的过程中那一眼（形状与 `/ai-analysis/runs/<id>/progress` 同源）；
    * `result`  —— 终态，载荷与流式入口那条 `result` **同形**（含 `run_id` / `status`）；
    * `heartbeat`、`error`（异常兜底）。

    ## `Last-Event-ID`

    浏览器自动重连时会带上最后收到的 `id`。这里的实现是：**从它继续编号**，并把
    收到的那个值回显在首帧的 `last_event_id` 上。不做历史回放 —— 每一帧都是
    「当前状态」的完整快照（幂等），重连时补发历史只会让页面把同一件事演两遍。

    ## 为什么不断言「这个端点不写库」

    不是靠自觉，是**结构上**没有写路径：本函数只调 `get_job` / `result_payload` /
    `progress_payload` / `notice_for_waiting`，四个都只读。`tests/test_ai_job_protocol.py`
    用行数断言钉住这一点（按 `job_id` / `target_key` 过滤，不数全表）。
    """
    import time

    heartbeat = max(int(JOB_EVENTS_HEARTBEAT_SECONDS), 1)
    try:
        event_id = int(str(last_event_id or "").strip() or 0)
    except (TypeError, ValueError):
        event_id = 0
    if event_id < 0:
        event_id = 0
    deadline = time.monotonic() + JOB_EVENTS_MAX_SECONDS
    seen_last_id = last_event_id

    job = job_service.get_job(job_id)
    if job is None:
        yield _job_event_frame(event_id + 1, "error", {"message": "这次分析不存在。"})
        return

    event_id += 1
    yield _job_event_frame(
        event_id, "state", _job_state_payload(job, last_event_id=seen_last_id)
    )
    if job.is_terminal:
        # 终态的 job：把结论直接发出去，然后收尾关闭（不再有心跳）。
        event_id += 1
        yield _job_event_frame(
            event_id,
            "result",
            job_service.result_payload(job) or {"run_id": job.run_id, "status": ""},
        )
        return

    # 运行号一到手就发一条 `run`：它与老流式入口那个 `run` 事件**同名同义**
    # （「运行号在这儿，接着看它的进度」），所以三份抽屉里既有的那个监听器
    # （`startAiBudgetWatch`）直接复用 —— 不为 Job 协议再写一套进度接线。
    sent_run_id = None
    if job.run_id:
        sent_run_id = job.run_id
        event_id += 1
        yield _job_event_frame(
            event_id, "run", {"job_id": job.id, "run_id": job.run_id, "state": job.state}
        )

    if job.state == job_model.STATE_WAITING_SNAPSHOT:
        event_id += 1
        notice = job_service.notice_for_waiting(job)
        yield _job_event_frame(
            event_id,
            "waiting",
            {
                "job_id": job.id,
                "state": job.state,
                "waiting": True,
                "run_id": job.run_id or notice.get("run_id"),
                "message": notice.get("message") or "",
            },
        )

    while True:
        if time.monotonic() >= deadline:
            event_id += 1
            yield _job_event_frame(
                event_id,
                "state",
                _job_state_payload(job, last_event_id=seen_last_id, stream_timeout=True),
            )
            return
        time.sleep(heartbeat)
        # 只读流：每一轮把事务收掉再读。挂一个几十分钟的读事务在 SQLite 上没有任何
        # 好处（下一次读本来就会开新事务），而它会让「这个连接占着库」成为一个
        # 要排查的现象。
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001 —— 收事务失败不影响读下一帧
            pass
        job = job_service.get_job(job_id)
        if job is None:
            event_id += 1
            yield _job_event_frame(event_id, "error", {"message": "这次分析不存在。"})
            return
        if job.is_terminal:
            event_id += 1
            yield _job_event_frame(
                event_id,
                "result",
                job_service.result_payload(job) or {"run_id": job.run_id, "status": ""},
            )
            return
        # 运行号在这一轮才出现（排队 → 在跑）：补一条 `run`，让页面接上进度轮询。
        if job.run_id and job.run_id != sent_run_id:
            sent_run_id = job.run_id
            event_id += 1
            yield _job_event_frame(
                event_id,
                "run",
                {"job_id": job.id, "run_id": job.run_id, "state": job.state},
            )
        event_id += 1
        yield _job_event_frame(
            event_id, "heartbeat", _job_state_payload(job, last_event_id=seen_last_id)
        )
        event_id += 1
        yield _job_event_frame(
            event_id,
            "progress",
            {
                "job_id": job.id,
                "run_id": job.run_id,
                "status": job.state,
                # 读不到就是 null（不是 0）：界面显示「进度不可用」。
                "progress": job_service.progress_payload(job),
            },
        )


def _job_events_response(job):
    """把订阅流包成 SSE 响应。**`end_with_a_terminal_event` 那一层不许省**：
    生成器中途抛异常时，客户端只会看到连接断掉，而那句话在界面上就是含糊的
    「连接中断」—— 原因要跟着最后一条事件发出去。"""

    def _generate():
        yield from end_with_a_terminal_event(
            _job_event_frames(job.id, request.headers.get("Last-Event-ID"))
        )

    return Response(stream_with_context(_generate()), mimetype="text/event-stream")


def _job_message(result) -> str:
    """给用户的一句话：这次是**新建**还是**附着**、以及现在在哪一步。"""
    job = result.job
    if result.attached:
        head = (
            f"这次点击附着到已有的分析（job #{job.id}，同一个目标同一份输入只跑一次）"
        )
    else:
        head = f"已收下这次分析（job #{job.id}）"
    if result.upgraded:
        head += "；这次要的是全量，已把模式升级为全量"
    if job.state == job_model.STATE_WAITING_SNAPSHOT:
        tail = "：正在等周版本同步写完缓存，同步一结束会自动开始（本次没有产生任何消耗）。"
    elif job.state == job_model.STATE_QUEUED:
        tail = f"：已排进后台队列（任务 #{job.task_id}），轮到它就开跑。"
    elif job.state == job_model.STATE_RUNNING:
        tail = f"：它正在跑（运行 #{job.run_id}）。"
    else:
        tail = "：它已经跑完了，结论见 /ai-analysis/weekly/<config_id>/latest。"
    return head + tail


@ai_analysis_bp.route("/ai-analysis/weekly/<int:config_id>/jobs", methods=["POST"])
def ai_weekly_job_create(config_id):
    """**创建这次分析的身份**（不跑分析）：返回稳定的 `job_id`。

    body（JSON，全部可选）：

    * `analysis_mode`：`incremental`（默认）/ `full`。**认不出来回 400** ——
      静默当成增量会让「我点了全量」变成一句谎话；
    * `focus`：分析范围（`all` / `table` / `code` / 仓库 id），缺省 `all`；
    * `source`：`manual`（默认）/ `scheduled`；
    * `idempotency_key`：**客户端**给的动作标识。同一次点击重试要复用同一个键 ——
      「关掉抽屉再打开又点一下」不该变成第二次付费调用。

    权限与其余 `/ai-analysis/*` 端点同一口径（`_has_project_access`，取不到也拒绝）。

    返回 `{success, job_id, state, run_id, requested_mode, effective_mode,
    upgrade_reason, reused_run_id, attached, job}`。**同步没跑完也照样返回 `job_id`**
    （状态 `waiting_snapshot`）—— 这就是「不再有『没有运行号所以无法确认』那一档」。
    """
    config = WeeklyVersionConfig.query.get_or_404(config_id)
    if not _has_project_access(config.project_id):
        return jsonify({"success": False, "message": "Access denied."}), 403

    payload, error = read_json_object()
    if error is not None:
        return error

    # 两个取值都在这里先落地成变量：一是为了让「读到了什么」与「传下去什么」在同一条
    # 语句上下文里看得见，二是 `analysis_mode` 的**非法值判定**发生在服务层的归一化里
    # （`JobRequestError` → 400），不在这里静默回落。
    analysis_mode = payload.get("analysis_mode", job_model.MODE_INCREMENTAL)
    focus = payload.get("focus", job_service.FOCUS_ALL)

    try:
        result = job_service.create_or_attach_job(
            config=config,
            requested_mode=analysis_mode,
            focus=focus,
            trigger_source=payload.get("source", job_model.SOURCE_MANUAL),
            idempotency_key=payload.get("idempotency_key"),
        )
    except job_service.JobRequestError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400

    # 提交在这里（本层的口径，见 `services/ai/job_service.py` 的事务说明）：
    # 上面那几步都只是 flush，谁调用谁提交。
    db.session.commit()
    body = {"success": True, **result.to_dict()}
    body["message"] = _job_message(result)
    return jsonify(body), 200


@ai_analysis_bp.route("/ai-analysis/jobs/<int:job_id>", methods=["GET"])
def ai_job_status(job_id):
    """读一条 job 的持久状态。**页面刷新 / 关抽屉再打开时的恢复入口。**

    按 `job.project_id` 判权（不接受调用方指定项目：换个号就能读到别人的分析进度与
    结论是不行的）；不存在 → 404。
    """
    job = job_service.get_job(job_id)
    if job is None:
        return jsonify({"success": False, "message": "Not found."}), 404
    if not _has_project_access(job.project_id):
        return jsonify({"success": False, "message": "Access denied."}), 403
    return jsonify({"success": True, "job": job.to_dict()}), 200


@ai_analysis_bp.route("/ai-analysis/jobs/<int:job_id>/events", methods=["GET"])
def ai_job_events(job_id):
    """**只订阅**这条 job 的事件流。创建任务一次都不在这里发生。

    * `waiting_snapshot` → `waiting`（文案由服务端给）+ 心跳保活；
    * `queued` / `running` → 心跳 + 进度帧；
    * 终态 → `result`（与既有抽屉消费的那一份同形）后关闭；
    * 支持 `Last-Event-ID`（断线重连从它继续编号）。

    这个端点在任何情况下都**不产生**新的 job / run / task 行 —— 这是「重连不会创建
    第二条 run，也不会再次调用模型」那条验收；`tests/test_ai_job_protocol.py` 用行数
    断言钉它。
    """
    job = job_service.get_job(job_id)
    if job is None:
        return jsonify({"success": False, "message": "Not found."}), 404
    if not _has_project_access(job.project_id):
        return jsonify({"success": False, "message": "Access denied."}), 403
    return _job_events_response(job)


@ai_analysis_bp.route("/ai-analysis/weekly/<int:config_id>/latest", methods=["GET"])
def ai_weekly_latest(config_id):
    config = WeeklyVersionConfig.query.get_or_404(config_id)
    if not _has_project_access(config.project_id):
        return jsonify({"success": False, "message": "Access denied."}), 403
    result = get_latest_weekly_result(config_id)
    if not result:
        return jsonify({"success": True, "result": None})
    # 顶层 `run_id` 与 SSE 的 `result` 事件同义：抽屉里那一行「本次消耗」的「明细」按钮
    # 要按**运行记录**取数（`/ai-analysis/runs/<id>/usage`）。原先只有 `result` 里那个
    # 嵌套的 `run_id`，而三份模板读的都是 `data.run_id` —— 于是「刷新页面看得见消耗、
    # 点明细没反应」，因为按钮在拿不到运行号时是隐藏的（那不是「没采集」，是按钮没了）。
    return jsonify({"success": True, "result": result, "run_id": result.get("run_id")})


@ai_analysis_bp.route("/ai-analysis/commit/<int:commit_id>/latest", methods=["GET"])
def ai_commit_latest(commit_id):
    commit = Commit.query.get_or_404(commit_id)
    repo = db.session.get(Repository, commit.repository_id)
    project_id = repo.project_id if repo else None
    # **取不到 project_id 也拒绝**（原来这里是 `if project_id and not ...`，取不到就放行）。
    # `commit.repository_id` / `repository.project_id` 都是 nullable=False，删仓库又连带删
    # commit，所以今天构造不出「仓库丢了但 commit 还在」的请求 —— 但这条链上少一个
    # `project_id` 就等于**跳过权限判定**，而权限判定不该有一条 fail-open 的支路。
    if not project_id or not _has_project_access(project_id):
        return jsonify({"success": False, "message": "Access denied."}), 403
    result = get_latest_commit_result(commit_id)
    if not result:
        return jsonify({"success": True, "result": None})
    # 同 `ai_weekly_latest`：顶层 `run_id` 是给抽屉那行「明细」按钮用的。
    return jsonify({"success": True, "result": result, "run_id": result.get("run_id")})


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
        "budget": visible_budget(status),
    }
    if snap is None:
        return jsonify(payload), 200

    # 本次运行**还没落库**的那部分用量：token 取快照里**跨成员累计**的那一份（`job_tokens`），
    # 费用按这条运行的模型与项目价格表现算（算不出就是 `None`，`with_live_usage` 会据此
    # **不判**费用那一档）。
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
    # **合并跑中用量在前、脱敏在后**：`with_live_usage` 会按 `over_limits` 重算
    # `over` / `blocks_analysis`，拿一份已经抹掉数字的判定进去喂它，平台档就会被算成
    # 「没超」（它的 `limits` 已经是空的）—— 那正是「面板说没超、闸门说超了」的成因。
    payload["budget"] = visible_budget(
        with_live_usage(
            status,
            # **job 级那一份**（跨成员累计、单调不减），不是 `live_tokens` ——
            # 后者是**当前成员**的局部量，子代理模式下换个成员就从 0 重新开始
            # （见 `services/ai/run_progress.py` 的「两个用量口径」），
            # 于是「这次其实已经超了」的提示会在换成员那一帧当场消失。
            # 两者都是下界（上游报多少算多少），`with_live_usage` 的口径不变；
            # 读不到时它是 `None`，那里按 0 加（与 `live_tokens` 为 `None` 时逐字一样）。
            tokens=snap.job_tokens,
            cost=live_cost,
            currency=currency,
        )
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
# ## 读与写都是平台管理员：**「平台合计」只有平台管理员能看**
#
# 读侧一度放开过（理由是「`usage_overview` 本来就把同一份数下发给任何人，只堵这里等于
# 假装挡住」）。那个观察是对的，但做法应该相反：**该收的是总览那份**，而不是把这个端点
# 也放开。平台合计是运营数字 —— 它把所有人的花费加起来，能反推出别的项目烧了多少钱，
# 所以只有平台管理员该看到它。
#
# 于是现在的分工是：
#   * 这个端点：`@require_admin`（读与写都是）；
#   * `usage_overview` / `usage/project/<id>`：按 `platform_scope_visible()` 传
#     `show_platform`，非管理员那份响应里连平台档的字段都没有（不是置 0，是**没有**）；
#   * 项目档自己的数照常下发（那是用户自己的花费）；
#   * 拦不拦**不受影响**：`over` / `blocks_analysis` 是平台策略，与谁在看无关
#     （见 `analysis_budget.redact_platform_scope` 的「只动显示，不动判定」）。

@ai_analysis_bp.route("/ai-analysis/platform-budget", methods=["GET"])
@require_admin
def ai_platform_budget():
    """平台总预算：配置 + 当前状态。**与写接口同权限（平台管理员）。**

    读侧一度放成「有项目权限就能看」，理由是「数字反正已经在 `usage_overview` 里了」。
    那句话说对了一半：当时确实是这样，所以这道闸门当时一个字节都没挡住。但**结论反了** ——
    该做的是把总览里那份也收掉，而不是把这个端点也放开。平台合计是运营数字：把所有人的
    花费加起来，能反推出别的项目烧了多少钱。

    现在两处口径一致：`usage_overview` 对非管理员不下发平台档
    （`show_platform=platform_scope_visible()`），这里也回到 `@require_admin`。
    """
    return jsonify(
        {
            "success": True,
            "budget": platform_budget_public(),
            "status": platform_budget_status(),
        }
    ), 200


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
    username = _actor_name()
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


# ---------------------------------------------------------------------------
#  消耗统计的口径：起点 + 全量重置
# ---------------------------------------------------------------------------
# **为什么不在 `/ai-analysis/usage/` 下面**：那一族路径被一条测试钉着「只能是 GET、
# 不能有 POST」（`test_the_three_endpoints_are_read_only`），守的是「别在一个只读页面上
# 顺手放一个会真花钱的『重新分析』按钮」。平台预算当初就是从这条护栏前退到
# `/ai-analysis/platform-budget` 的（见上面那段注释），这两个写接口照同一条口径放在
# `/ai-analysis/statistics/` 下 —— 路径里没有 `/usage`，因此也不会被那条测试的扫描网住。
#
# 权限与平台预算**同一条**：`@require_admin`。起点是平台级口径（它同时改变所有项目在
# 面板上的数字），全量重置删的是全平台的记录 —— 两者都不是项目管理员该碰的。
#
# 全量重置**另外进了 `SENSITIVE_ENDPOINTS`**（`services/app_security_bootstrap_service.py`）：
# 那是第二道防线（万一将来有人漏了装饰器，before_request 仍然拦得住），理由与
# 「delete_repository」那一类相同 —— 它是这个平台上唯一一个「一点就删一大片、且没有
# 撤销」的动作。起点**不进**那张表：它一条记录都不删、随时可恢复，按破坏性动作去加固
# 会让读代码的人以为它同样危险。


@ai_analysis_bp.route("/ai-analysis/statistics/baseline", methods=["POST"])
@require_admin
def ai_statistics_baseline_update():
    """设置（或清除）消耗统计的起点。**只改口径，不删任何数据。**

    body：`{"since": "2026-09-19T14:30"}`（北京墙钟，浏览器 `datetime-local` 的形状）；
    `{"since": null}` = 恢复成「统计全部历史」。

    `since` **必须出现**（哪怕是 null）：键缺失时静默清除起点的话，一个拼错的字段名就会
    让别人以为「只是没设成」，而起点其实已经被清掉了。
    """
    payload, error = read_json_object()
    if error is not None:
        return error
    if "since" not in payload:
        return (
            jsonify(
                {
                    "success": False,
                    "message": "缺少 since 字段（要恢复「统计全部历史」请显式传 null）。",
                    "errors": [{"field": "since", "message": "缺少该字段"}],
                }
            ),
            400,
        )

    since, problem = parse_baseline_input(payload.get("since"))
    if problem:
        return (
            jsonify({"success": False, "message": problem, "errors": [{"field": "since", "message": problem}]}),
            400,
        )

    username = _actor_name()
    ok, message, errors = set_usage_baseline(since, updated_by=username)
    body = {"success": ok, "message": message}
    if errors:
        body["errors"] = errors
    if ok:
        # 与平台预算同一条：保存后直接回新状态，界面不必再发一次 GET
        # （那会把「保存成功」与「状态刷新」变成两次可能不一致的往返）。
        body["statistics"] = statistics_public(can_manage=True)
    return jsonify(body), 200 if ok else 400


@ai_analysis_bp.route("/ai-analysis/statistics/reset", methods=["POST"])
@require_admin
def ai_statistics_reset():
    """**全量重置**：删掉全部 AI 运行记录（含报告正文、分轮轨迹、异常清单）。不可逆。

    body：`{"confirm": "重置"}` —— 确认词不对就不执行。它不是安全边界（权限才是），
    防的是误触：这个动作删掉的是分析历史，点错了没有撤销。

    在途运行存在时直接拒绝（原因见 `services/ai/usage_statistics.purge_usage_statistics`）。
    """
    payload, error = read_json_object()
    if error is not None:
        return error
    confirm = str(payload.get("confirm") or "").strip()
    if confirm != RESET_CONFIRM_WORD:
        return (
            jsonify(
                {
                    "success": False,
                    "message": f"请先在确认框里输入「{RESET_CONFIRM_WORD}」两个字再提交。",
                    "errors": [
                        {"field": "confirm", "message": f"确认词必须是「{RESET_CONFIRM_WORD}」"}
                    ],
                }
            ),
            400,
        )

    username = _actor_name()
    ok, message, deleted = purge_usage_statistics(updated_by=username)
    body = {"success": ok, "message": message}
    if deleted:
        body["deleted"] = deleted
    if ok:
        body["statistics"] = statistics_public(can_manage=True)
    return jsonify(body), 200 if ok else 400


@ai_analysis_bp.route("/ai-analysis/usage/overview", methods=["GET"])
def ai_usage_overview():
    """跨项目总览：每个项目的运行次数、token、命中率、费用、预算，外加一行平台合计。

    筛选条件从 query 来（项目 / 时间范围 / 触发来源 / 状态），**在服务端聚合**。
    非法值由 `parse_usage_filters` 回落默认并在 `filters.notes` 里说明，不会 500。
    """
    filters = parse_usage_filters(request.args)
    payload = usage_overview(
        _get_accessible_project_ids(), filters, show_platform=platform_scope_visible()
    )
    return jsonify(payload), 200


@ai_analysis_bp.route("/ai-analysis/usage/estimate", methods=["GET"])
def ai_usage_estimate():
    """**执行前**的代价区间（AI-P1-03）：预计 token / 预计时间 / 最近一次实际值 / 是否命中基线。

    只读、只回 JSON、不产生任何模型消耗 —— 与上面三个端点同一条口径（也因此被
    `test_the_three_endpoints_are_read_only` 的那张网照着：只许 GET、路径里不许有 stream）。

    参数（**全部是可选的**，缺了就降级，不 500）：

    * `project`：必填才有意义，缺失或不合法 → 400；
    * `mode`：`full` / `incremental`，认不出来按 `full`（与 Job 协议同一套字面量）；
    * `files`：目标文件数，非数字/负数按「不知道」处理（区间就不缩放，见估算函数）；
    * `baseline`：`1` / `0` / 缺省（缺省 = 还没判定）。

    解析全部在 `parse_estimate_args`（服务层，与 `parse_usage_filters` 同一套回落规则）——
    路由这一层**只做权限**，第二波接手这个文件时不必读一遍参数解析。

    权限与 `/usage/project/<id>` 逐字相同（`_has_project_access`）：估算依据的是这个项目
    的历史运行，能看那些数字的人才能看这个区间。
    """
    project_id, params = parse_estimate_args(request.args)
    if project_id is None:
        return jsonify({"success": False, "message": "缺少有效的项目编号。"}), 400
    if not _has_project_access(project_id):
        return jsonify({"success": False, "message": "Access denied."}), 403

    payload = analysis_estimate(
        project_id,
        mode=params["mode"],
        planned_files=params["planned_files"],
        baseline_reusable=params["baseline_reusable"],
    )
    if params["notes"]:
        payload["notes"] = [*payload["notes"], *params["notes"]]
    return jsonify({"success": True, "project_id": project_id, **payload}), 200


@ai_analysis_bp.route("/ai-analysis/usage/project/<int:project_id>", methods=["GET"])
def ai_usage_project(project_id):
    """单项目下钻：周版本维度 + 逐次运行 + 按工具类型。"""
    if not _has_project_access(project_id):
        return jsonify({"success": False, "message": "Access denied."}), 403
    filters = parse_usage_filters(request.args)
    return jsonify(
        project_usage(project_id, filters, show_platform=platform_scope_visible())
    ), 200


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
#  导出这次运行的结论（markdown）
# ---------------------------------------------------------------------------
# 路径里**刻意不含 `usage`**：`tests/test_ai_usage_capture.py` 会扫所有带 `/usage`
# 的路由并断言它们是「只回 JSON 的只读端点」，而这个端点回的是文件。
#
# 权限与 `/runs/<id>/usage` **同一条口径**（按这条运行自己的 project_id 判），不接受
# 调用方指定项目：导出的正文是模型看过的变更细节，换个号就能拿到别人的报告是不行的。
# 目标名与项目名只从库里取，**不接受调用方传参拼文件名**（那是一条自己给自己开的注入面）。


def _payload_of(raw):
    """`request_payload` / `response_payload` 这类 JSON 文本列 → dict（坏数据当没有）。"""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _focus_label_of(run) -> str:
    """这次分析用户选的「分析范围」名。空串 = 没选（全量）。

    标签原文由发起分析那条路径写在 `request_payload.focus.label` 里（
    `_filter_delta_files_by_focus` 算出来的「仅配表仓库」这类字），**这里只取不拼** ——
    再算一遍就等于多一份会漂移的实现。
    """
    focus = _payload_of(run.request_payload).get("focus")
    if isinstance(focus, dict):
        return str(focus.get("label") or "")
    return ""


def _report_target_label(run) -> str:
    """这次运行的目标名（给人看的）。目标行被删掉时退化成 id，不编一个名字。"""
    if run.target_type == "weekly":
        config = db.session.get(WeeklyVersionConfig, run.target_id) if run.target_id else None
        if config is None:
            return "周版本"
        return report_document.weekly_target_label(
            config.name, config.start_time, config.end_time
        )
    commit = db.session.get(Commit, run.target_id) if run.target_id else None
    if commit is None:
        return f"提交 #{run.target_id}" if run.target_id else "提交"
    return report_document.commit_target_label(commit.commit_id, commit.message)


def _no_report_reason(run) -> str:
    """没有可导出的结论时，如实说**为什么**（不给一个空文件）。

    「跑完了但没有正文」与「压根没跑成」要分开说：前者要重新分析，后者要看失败原因。
    """
    status = str(run.effective_status or "")
    if status == "running":
        return "这次分析还在跑，跑完之后才能导出。"
    if status == "pending":
        return "这次分析还在排队，跑完之后才能导出。"
    if status == "failed":
        reason = str(run.error_message or "").strip()
        return f"这次分析没有结论（分析失败{f'：{reason}' if reason else ''}），无法导出。"
    return "这次运行没有可导出的报告正文。"


@ai_analysis_bp.route("/ai-analysis/runs/<int:run_id>/report.md", methods=["GET"])
def ai_run_report_md(run_id):
    """把**这一次运行**的结论导出成一份 markdown：元信息 → 报告原文 → 异常清单附录。

    拼文档的是 `services/ai/report_document.py`（纯函数、不碰库），这里只做三件事：
    判权、从库里取那几项元信息、把结果当附件发出去。

    **失败一律回 JSON，不回半个文件**：一个「下载成功但内容是空的」文件比一句
    「这次没有结论」危险得多 —— 前者会被当成一份分析过、没有问题的报告存档。
    """
    run = db.session.get(AiAnalysisRun, run_id)
    if run is None:
        return jsonify({"success": False, "message": "Not found."}), 404
    if not _has_project_access(run.project_id):
        return jsonify({"success": False, "message": "Access denied."}), 403
    # **用 `effective_status`**（僵尸 running 显示为 failed），与 `/latest`、`/history`
    # 同一条口径：进程被杀留下的那条 running 永远不会跑完，这里若按 `run.status` 判，
    # 用户会一直看到「这次分析还在跑，跑完之后才能导出」——一句话的等待，等不到头。
    status = run.effective_status
    if not report_document.is_exportable(status=status, report_text=run.response_text):
        return jsonify({"success": False, "message": _no_report_reason(run)}), 409

    project = db.session.get(Project, run.project_id)
    payload = _payload_of(run.response_payload)
    target_label = _report_target_label(run)
    # 维度那一列按**这次分析当时生效的清单**翻中文名 —— 清单随结果落库
    # （`response_payload.dimension_specs`，见 `result_payload`），这里只读不算。
    # **不许现查项目当前声明**：项目改了声明之后，历史结论会被按新清单重新贴标签
    # （当时合法的维度会变成「未归类」），那是篡改历史而不是「显示得不准」。
    dimension_labels = report_document.dimension_labels_from_payload(payload)
    # 「处置」列按**导出这一刻**现查这一次运行的行 —— 不能用 `payload["anomalies"]`：
    # 那是分析当时的快照，里面只有 `fingerprint`，没有处置状态（它每轮都在被人改）。
    # 翻中文名的映射表只有 `models/ai_analysis/anomaly.py::DISPOSITION_LABELS` 那一份，
    # 这里不抄第二份。查不到的条目在文档里写 `-`，不回落成「待确认」。
    dispositions = {
        str(row.fingerprint): DISPOSITION_LABELS.get(row.disposition or "", "")
        for row in anomalies_of_run(run.id)
        if row.fingerprint
    }
    markdown = report_document.build_report_markdown(
        project_label=str(getattr(project, "name", "") or ""),
        target_label=target_label,
        run_id=run.id,
        created_at_display=report_document.beijing_display(run.created_at),
        risk_level=payload.get("risk_level"),
        # 定级依据**原文带上**：那句「该等级仅按变更规模估算，不是模型评估结果」就在里面，
        # 在这里另写一份警示语必然与它漂移（见 report_document 的 docstring）。
        risk_reasons=payload.get("risk_reasons") or [],
        scope=run.scope,
        trigger_source=run.trigger_source,
        model=str(run.model or ""),
        degradation_label=str(payload.get("degradation_label") or ""),
        focus_label=_focus_label_of(run),
        # 报告正文末尾可能带一行**机器可读的裁决块**（`<!-- ai-verify-ruling: {...} -->`，
        # 见 services/ai/verdict.py）。它是给平台读回去的（`result_payload.read_ruling`），
        # 网页渲染看不见它，但用户下载的是一份原始 markdown —— 那一行会原样出现在文件里。
        # 导出只留给人读的那几节。
        report_text=verdict.strip_ruling_block(run.response_text),
        anomalies=payload.get("anomalies") or [],
        suppressed_count=int(payload.get("suppressed_count") or 0),
        dimension_labels=dimension_labels,
        dispositions=dispositions,
        # 覆盖账本（哪些文件进了本次输入、哪些真的取到过证据、还缺什么）。**这一行是必需的**：
        # 不传就没有「覆盖与缺口」那几行，而「分析范围：全量」会被读成「整个版本都看过了」
        # （实测一次周版本分析只取到过 60/996 个文件的证据，见 services/ai/coverage_ledger.py）。
        # 它只读库里已有的输入账与逐轮明细，**不发起任何计费调用**。
        coverage=report_document.coverage_for_run(run),
    )
    filename = report_document.report_filename(
        project_name=getattr(project, "name", "") or "",
        target_label=target_label,
        when=run.created_at,
    )
    # `as_attachment` + `download_name`：文件名里的中文由 Werkzeug 按 RFC 5987 编码，
    # 浏览器拿到的就是那一份带项目名与目标名的中文文件名（前端**不要**再加 `download`
    # 属性 —— 空值会让浏览器按 URL 末段命名，把中文名盖掉）。
    return send_file(
        BytesIO(markdown.encode("utf-8")),
        mimetype="text/markdown; charset=utf-8",
        as_attachment=True,
        download_name=filename,
    )


# ---------------------------------------------------------------------------
#  历次结论（这个目标跑过的每一次）
# ---------------------------------------------------------------------------
# 用户报的是「AI 重新分析时无法预览旧的结论」：点「重新分析」之后屏幕上那句
# 「AI 分析进行中...」就把旧报告换掉了，而库里那条结论一直都在。
#
# 三条路径的分工：
#   /commit/<id>/history  → 这个提交跑过的每一次（一行一条）
#   /weekly/<id>/history  → 这个周版本跑过的每一次（按**分组键**取，见服务层说明）
#   /runs/<id>/report     → 其中**某一次**的结论，形状与 `/latest` 逐字相同
#
# 判权都按**目标自己所属的项目**（与 `/latest` 同一条口径）：历次结论里含有报告正文，
# 换个号就能读到别人的是不行的。


@ai_analysis_bp.route("/ai-analysis/commit/<int:commit_id>/history", methods=["GET"])
def ai_commit_history(commit_id):
    commit = Commit.query.get_or_404(commit_id)
    repo = db.session.get(Repository, commit.repository_id)
    project_id = repo.project_id if repo else None
    # **取不到 project_id 也拒绝**（原来这里是 `if project_id and not ...`，取不到就放行）。
    # `commit.repository_id` / `repository.project_id` 都是 nullable=False，删仓库又连带删
    # commit，所以今天构造不出「仓库丢了但 commit 还在」的请求 —— 但这条链上少一个
    # `project_id` 就等于**跳过权限判定**，而权限判定不该有一条 fail-open 的支路。
    if not project_id or not _has_project_access(project_id):
        return jsonify({"success": False, "message": "Access denied."}), 403
    payload = report_history_service.list_target_runs(
        kind="commit", target_id=commit_id, limit=_history_limit()
    )
    return jsonify(payload), 200


@ai_analysis_bp.route("/ai-analysis/weekly/<int:config_id>/waiting", methods=["GET"])
def ai_weekly_waiting(config_id):
    """「等同步」的那次登记现在到哪一步了（抽屉轮询它，好在分析自动开始时接上去）。

    ## 为什么需要这个端点

    被同步闸门拦下时**一个模型请求都没发出去**，那次分析由 worker 在同步收尾后自动开始
    （见 `task_worker_queue_service.register_waiting_analysis_intent`）。而开始的那一刻
    浏览器这头没有任何连接 —— 页面只有两个办法知道「它开跑了」：轮询，或者假装知道。
    这里给的是轮询那条路：**只读**，不建 run、不排队、不发请求。

    三种回答对应页面的三种动作（文案由服务端给，`describe_waiting_analysis`）：
    拿到 `run_id` 就附着到那次运行上、`waiting` 为真就继续等、为假就把按钮放回可点。
    """
    config = WeeklyVersionConfig.query.get_or_404(config_id)
    if not _has_project_access(config.project_id):
        return jsonify({"success": False, "message": "Access denied."}), 403
    # **函数内 import**：队列服务在模块级 `import services.task_worker_service`，而那个
    # 模块又从队列服务取名字 —— 在模块级 import 会成环（谁先被 import 谁就炸）。
    from services.task_worker_queue_service import describe_waiting_analysis

    status = describe_waiting_analysis(config_id, build_weekly_group_key(config))
    return jsonify({"success": True, **status})


@ai_analysis_bp.route("/ai-analysis/weekly/<int:config_id>/history", methods=["GET"])
def ai_weekly_history(config_id):
    config = WeeklyVersionConfig.query.get_or_404(config_id)
    if not _has_project_access(config.project_id):
        return jsonify({"success": False, "message": "Access denied."}), 403
    # **按分组键取，不是按 config id**：读侧（`get_latest_weekly_result`）就是按
    # `build_weekly_group_key` 查的。按 config id 查会与抽屉上那份结论对不上。
    payload = report_history_service.list_target_runs(
        kind="weekly",
        target_key=build_weekly_group_key(config),
        limit=_history_limit(),
    )
    return jsonify(payload), 200


def _history_limit() -> int:
    """`?limit=` 的取值：脏值回落默认（列表不是分页，给个上限就够）。"""
    try:
        value = int(request.args.get("limit") or 0)
    except (TypeError, ValueError):
        return report_history_service.DEFAULT_HISTORY_LIMIT
    if value <= 0:
        return report_history_service.DEFAULT_HISTORY_LIMIT
    return min(value, report_history_service.MAX_HISTORY_LIMIT)


@ai_analysis_bp.route("/ai-analysis/runs/<int:run_id>/report", methods=["GET"])
def ai_run_report(run_id):
    """**某一次**运行的结论，形状与 `/latest` 逐字相同（前端一个渲染器通吃）。

    与 `/runs/<id>/report.md` 是两件事：那条回**文件**（给人下载/存档），这条回 JSON
    （给界面渲染）。路径相近是刻意的 —— 它们说的是同一份东西的两种出场方式。
    """
    run = db.session.get(AiAnalysisRun, run_id)
    if run is None:
        return jsonify({"success": False, "message": "Not found."}), 404
    if not _has_project_access(run.project_id):
        return jsonify({"success": False, "message": "Access denied."}), 403
    payload = report_history_service.get_run_report(run_id)
    if payload is None:
        return jsonify({"success": False, "message": "Not found."}), 404
    return jsonify({"success": True, "result": payload, "run_id": payload.get("run_id")}), 200


# ---------------------------------------------------------------------------
#  结论的人工处置（确认 / 忽略 / 撤销）
# ---------------------------------------------------------------------------
# `AiAnalysisAnomaly.disposition` 这一列原先**整棵树都没有写路径**：读侧是通的
# （`baseline_source.baseline_findings` → `baseline.classify` → `suppressed` →
# `result_payload` 把「已忽略且文件没再变」的结论从下一轮清单里剔掉），但没有入口
# 能把它设成非默认值，于是那条链路一次都没生效过 —— 用户在界面上找不到地方说
# 「这条我确认过」，模型下一轮照样把同一条原样再报一遍。
#
# 权限取 `_has_project_access`（项目成员即可），**不是管理员**：处置是「看过这份报告的
# 人去分诊」，正是项目成员每天在做的事；把它抬到管理员，等于让这一步没人做。
# 谁做的会记在 `disposition_by` 上。
#
# 路径形状：单条按 `anomaly_id`，批量按 `run_id`。**不做「按指纹处置」**：指纹是
# 给「跨轮次继承」用的内部身份，暴露成写入口会让调用方以为处置能跨运行传播 ——
# 实际传播靠的是下一轮读上一轮的行，不是这一次的写入。


def _anomaly_payload(rows):
    """一次运行的全部结论行 + 处置口径。中文标签在服务端给，界面不自己映射。"""
    counts = {item: 0 for item in DISPOSITIONS}
    for row in rows:
        key = row.disposition or DEFAULT_DISPOSITION
        counts[key] = counts.get(key, 0) + 1
    return {
        "anomalies": [row.to_dict() for row in rows],
        "counts": counts,
        "total": len(rows),
        "dispositions": [{"value": item, "label": DISPOSITION_LABELS[item]} for item in DISPOSITIONS],
    }


def _read_disposition_filter():
    """`?disposition=` 的取值。认不出来就报错，**不悄悄当成「全部」**。

    静默退回「全部」的后果是：用户点了「只看已忽略」，界面把全部条目列出来，
    而他不会发现自己看的是另一份清单 —— 于是他会以为「我忽略的那几条不见了」。
    """
    raw = (request.args.get("disposition") or "").strip()
    if not raw:
        return None, None
    try:
        return normalize_disposition(raw), None
    except DispositionError as exc:
        return None, (jsonify({"success": False, "message": str(exc)}), 400)


@ai_analysis_bp.route("/ai-analysis/runs/<int:run_id>/anomalies", methods=["GET"])
def ai_run_anomalies(run_id):
    """某一次运行的**结构化结论清单**（含每一条的处置状态）。

    与 `/runs/<id>/report` 是两件事：那条回的是模型写的那份 markdown（`response_text`），
    这条回的是落库的**行**。处置状态只在行上 —— 报告正文里没有它，模型也写不出它。
    """
    run = db.session.get(AiAnalysisRun, run_id)
    if run is None:
        return jsonify({"success": False, "message": "Not found."}), 404
    if not _has_project_access(run.project_id):
        return jsonify({"success": False, "message": "Access denied."}), 403
    wanted, error = _read_disposition_filter()
    if error is not None:
        return error
    rows = anomalies_of_run(run_id, disposition=wanted)
    return jsonify({"success": True, "run_id": run_id, **_anomaly_payload(rows)}), 200


@ai_analysis_bp.route("/ai-analysis/anomalies/<int:anomaly_id>/disposition", methods=["POST"])
def ai_anomaly_disposition(anomaly_id):
    """改**一条**结论的处置状态。"""
    row = db.session.get(AiAnalysisAnomaly, anomaly_id)
    if row is None:
        return jsonify({"success": False, "message": "Not found."}), 404
    if not _has_project_access(row.project_id):
        return jsonify({"success": False, "message": "Access denied."}), 403
    payload, error = read_json_object()
    if error is not None:
        return error
    try:
        set_disposition(
            row,
            disposition=payload.get("disposition"),
            note=payload.get("note", ""),
            username=_actor_name(),
        )
    except DispositionError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    db.session.commit()
    return jsonify(
        {
            "success": True,
            "changed": 1,
            "note_truncated": note_was_truncated(payload.get("note", "")),
            "anomaly": row.to_dict(),
        }
    ), 200


@ai_analysis_bp.route(
    "/ai-analysis/runs/<int:run_id>/anomalies/disposition", methods=["POST"]
)
def ai_run_anomalies_disposition(run_id):
    """**批量**处置：`{"ids": [...], "disposition": "...", "note": "..."}`。

    `ids` 必填且非空。不给「不传 ids = 全部」这种默认：一次误点把整份报告的结论
    全标成「已忽略」，代价是下一轮这些条目集体消失，而用户没有任何地方能看出
    「它们是被我误标掉的」。
    """
    run = db.session.get(AiAnalysisRun, run_id)
    if run is None:
        return jsonify({"success": False, "message": "Not found."}), 404
    if not _has_project_access(run.project_id):
        return jsonify({"success": False, "message": "Access denied."}), 403
    payload, error = read_json_object()
    if error is not None:
        return error
    raw_ids = payload.get("ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        return jsonify({"success": False, "message": "ids 必须是非空数组。"}), 400
    if len(raw_ids) > MAX_BATCH_ITEMS:
        return jsonify(
            {"success": False, "message": f"一次最多处置 {MAX_BATCH_ITEMS} 条，收到 {len(raw_ids)} 条。"}
        ), 400
    ids = []
    for value in raw_ids:
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": f"ids 里出现了非整数：{value!r}"}), 400
    # **按 run_id 过滤**：跨运行传 id 会让「某一次运行的处置」改到另一次运行的行上，
    # 而返回的计数看起来完全正常。多一条 where 就挡住了这件事。
    rows = AiAnalysisAnomaly.query.filter(
        AiAnalysisAnomaly.run_id == run_id, AiAnalysisAnomaly.id.in_(ids)
    ).all()
    if len(rows) != len(set(ids)):
        found = {row.id for row in rows}
        missing = sorted(set(ids) - found)
        return jsonify(
            {"success": False, "message": f"这些结论不属于本次运行：{missing}"}
        ), 400
    try:
        changed = set_many(
            rows,
            disposition=payload.get("disposition"),
            note=payload.get("note", ""),
            username=_actor_name(),
        )
    except DispositionError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    return jsonify(
        {
            "success": True,
            "changed": changed,
            "note_truncated": note_was_truncated(payload.get("note", "")),
            **_anomaly_payload(anomalies_of_run(run_id)),
        }
    ), 200


def _actor_name() -> str:
    """当前操作人的用户名。

    **用 `_resolve_current_username`，不自己 `getattr(user, "username", "")`**：
    平台管理员（环境变量那种）没有数据库用户对象，`_get_current_user()` 直接返回
    None —— 自己取的话，「谁把这条标成已忽略的」在管理员操作时永远是空的，
    而那正是最需要留名的一种操作。那个 helper 会把会话里的 `auth_username` /
    `admin_user` 一起算进来，并做掉邮箱后缀归一化（本仓库的单一口径）。
    """
    return _resolve_current_username()


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
