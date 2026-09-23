"""Repository admin handlers extracted from app.py."""

from __future__ import annotations

from flask import flash, jsonify, redirect, request, url_for
from sqlalchemy import or_

from services.deployment_mode import is_agent_dispatch_mode
from services.model_loader import get_runtime_model, get_runtime_models
from services.repository_cleanup_helpers import delete_local_repository_directory
from utils.json_body import read_json_object
from utils.path_security import build_repository_local_path
from utils.request_security import require_admin
from utils.security_utils import sanitize_text

# 短于该长度的「凭据」不做文本替换：几个字符的子串会误伤正常文本
# （例如 token 恰为 "abc" 时把 "abclog" 里的子串也换掉）。
_MIN_REPLACEABLE_SECRET_LEN = 6


def _redact_repository_secrets(text, repository) -> str:
    """把仓库 URL / token 从「要落日志或回显给浏览器」的文本里抹掉。

    仓库 URL 常以 `https://oauth2:<PAT>@git.example.com/x.git` 的形式粘贴保存，
    于是 URL 本身就是凭据载体。本模块测试连接时会把 URL、以及底层 git 返回的
    错误文本（其中常带整条 remote URL）写进日志并回显，等于把 token 明文落到
    logs/runlog.log 与浏览器页面上 —— 同仓库的 `services/git_service.py` 早已
    统一过 `sanitize_url()`，此处是漏网的一处。

    只替换**本仓库自己的** URL 与 token，而不是对整个文本跑通用脱敏：
    git 的错误文本形态不可枚举，但我们要防的是自己这条链路把凭据带出去。
    """
    redacted = "" if text is None else str(text)
    candidates = (
        getattr(repository, "url", None),
        getattr(repository, "token", None),
    )
    for secret in candidates:
        if not secret or not isinstance(secret, str):
            continue
        if len(secret) < _MIN_REPLACEABLE_SECRET_LEN:
            continue
        redacted = redacted.replace(secret, sanitize_text(secret))
    return redacted


def _runtime(*names):
    return get_runtime_models(*names)


def _optional_runtime(name):
    try:
        return get_runtime_model(name)
    except Exception:
        return None


@require_admin
def update_repository_order():
    db, Repository = _runtime("db", "Repository")
    try:
        data, error = read_json_object()
        if error is not None:
            return error
        repo_id = data.get("repo_id")
        new_order = data.get("new_order")
        project_id = data.get("project_id")

        if not repo_id or new_order is None or not project_id:
            return jsonify({"status": "error", "message": "缺少必要参数"}), 400

        repositories = (
            Repository.query.filter_by(project_id=project_id)
            .order_by(Repository.display_order.asc())
            .all()
        )
        target_repo = None
        for repo in repositories:
            if repo.id == repo_id:
                target_repo = repo
                break

        if not target_repo:
            return jsonify({"status": "error", "message": "仓库不存在"}), 404

        repositories.remove(target_repo)
        repositories.insert(new_order, target_repo)

        for index, repo in enumerate(repositories):
            repo.display_order = index

        db.session.commit()
        return jsonify({"status": "success", "message": "仓库排序更新成功"})
    except Exception as exc:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(exc)}), 500


@require_admin
def swap_repository_order():
    db, Repository = _runtime("db", "Repository")
    try:
        data, error = read_json_object()
        if error is not None:
            return error
        first_repo_id = data.get("first_repo_id")
        second_repo_id = data.get("second_repo_id")
        project_id = data.get("project_id")

        if not first_repo_id or not second_repo_id or not project_id:
            return jsonify({"status": "error", "message": "缺少必要参数"}), 400

        if first_repo_id == second_repo_id:
            return jsonify({"status": "error", "message": "不能选择相同仓库"}), 400

        first_repo = Repository.query.filter_by(id=first_repo_id, project_id=project_id).first()
        second_repo = Repository.query.filter_by(id=second_repo_id, project_id=project_id).first()
        if not first_repo or not second_repo:
            return jsonify({"status": "error", "message": "仓库不存在或不属于当前项目"}), 404

        first_order = first_repo.display_order
        second_order = second_repo.display_order
        first_repo.display_order = second_order
        second_repo.display_order = first_order

        db.session.commit()
        return jsonify(
            {
                "status": "success",
                "message": f"成功交换仓库 {first_repo.name} 和 {second_repo.name} 的顺序",
            }
        )
    except Exception as exc:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(exc)}), 500


@require_admin
def delete_repository(repository_id):
    (
        db,
        Repository,
        BackgroundTask,
        DiffCache,
        ExcelHtmlCache,
        MergedDiffCache,
        WeeklyVersionDiffCache,
        WeeklyVersionExcelCache,
        WeeklyVersionConfig,
        Commit,
        log_print,
    ) = _runtime(
        "db",
        "Repository",
        "BackgroundTask",
        "DiffCache",
        "ExcelHtmlCache",
        "MergedDiffCache",
        "WeeklyVersionDiffCache",
        "WeeklyVersionExcelCache",
        "WeeklyVersionConfig",
        "Commit",
        "log_print",
    )
    AgentTask = _optional_runtime("AgentTask")
    repository = Repository.query.get_or_404(repository_id)
    project_id = repository.project_id
    repo_name = repository.name
    local_path = build_repository_local_path(
        repository.project.code,
        repository.name,
        repository.id,
        strict=False,
    )

    log_print(f"开始删除仓库 {repo_name} (ID: {repository_id})", "DELETE")
    try:
        background_task_ids = [
            row[0]
            for row in db.session.query(BackgroundTask.id).filter(
                BackgroundTask.repository_id == repository_id
            ).all()
        ]

        if AgentTask is not None:
            agent_task_filters = [AgentTask.repository_id == repository_id]
            if background_task_ids:
                agent_task_filters.append(AgentTask.source_task_id.in_(background_task_ids))
            agent_tasks_deleted = AgentTask.query.filter(or_(*agent_task_filters)).delete(synchronize_session=False)
            log_print(f"删除了 {agent_tasks_deleted} 个AgentTask记录", "DELETE")

        background_tasks_deleted = BackgroundTask.query.filter_by(repository_id=repository_id).delete(synchronize_session=False)
        log_print(f"删除了 {background_tasks_deleted} 个BackgroundTask记录", "DELETE")

        diff_cache_deleted = DiffCache.query.filter_by(repository_id=repository_id).delete()
        log_print(f"删除了 {diff_cache_deleted} 个DiffCache记录", "DELETE")

        excel_cache_deleted = ExcelHtmlCache.query.filter_by(repository_id=repository_id).delete()
        log_print(f"删除了 {excel_cache_deleted} 个ExcelHtmlCache记录", "DELETE")

        try:
            merged_cache_deleted = MergedDiffCache.query.filter_by(repository_id=repository_id).delete()
            log_print(f"删除了 {merged_cache_deleted} 个MergedDiffCache记录", "DELETE")
        except Exception as exc:
            log_print(f"删除MergedDiffCache记录时出错（可能是表结构问题）: {exc}", "DELETE")
            try:
                db.session.execute(
                    "DELETE FROM merged_diff_cache WHERE repository_id = :repo_id",
                    {"repo_id": repository_id},
                )
                log_print("通过SQL成功删除MergedDiffCache记录", "DELETE")
            except Exception as sql_exc:
                log_print(f"SQL删除MergedDiffCache记录也失败: {sql_exc}", "DELETE")

        try:
            weekly_cache_deleted = WeeklyVersionDiffCache.query.filter_by(repository_id=repository_id).delete()
            log_print(f"删除了 {weekly_cache_deleted} 个WeeklyVersionDiffCache记录", "DELETE")
        except Exception as exc:
            log_print(f"删除WeeklyVersionDiffCache记录时出错: {exc}", "DELETE")

        try:
            weekly_excel_cache_deleted = WeeklyVersionExcelCache.query.filter_by(repository_id=repository_id).delete()
            log_print(f"删除了 {weekly_excel_cache_deleted} 个WeeklyVersionExcelCache记录", "DELETE")
        except Exception as exc:
            log_print(f"删除WeeklyVersionExcelCache记录时出错: {exc}", "DELETE")

        commit_deleted = Commit.query.filter_by(repository_id=repository_id).delete()
        log_print(f"删除了 {commit_deleted} 个Commit记录", "DELETE")

        # **周版本配置必须一起删掉。** `WeeklyVersionConfig.repository_id` 是
        # `nullable=False`，而 ORM 删父行时默认是把子行的外键置 NULL → IntegrityError →
        # 整个事务回滚。表现是：**只要这个仓库被任何一个周版本配置引用着，它就永远删不掉**，
        # 而界面上只 flash 一句「删除仓库失败: NOT NULL constraint failed:
        # weekly_version_config.repository_id」（`delete_project` 是显式删了这一张表的）。
        weekly_config_ids = [
            row[0]
            for row in db.session.query(WeeklyVersionConfig.id).filter(
                WeeklyVersionConfig.repository_id == repository_id
            ).all()
        ]
        if weekly_config_ids:
            # 两张缓存表按 `repository_id` **或** `config_id` 匹配 —— 与
            # `services/repository_diff_cache_reset.py` 的口径一致（老数据里可能有
            # 只带 config_id 的行）。上面按 repository_id 删过一遍了，这里补的是
            # 「config 属于这个仓库、但缓存行上的 repository_id 记的是别人」那种。
            for cache_model in (WeeklyVersionDiffCache, WeeklyVersionExcelCache):
                cache_model.query.filter(
                    or_(
                        cache_model.repository_id == repository_id,
                        cache_model.config_id.in_(weekly_config_ids),
                    )
                ).delete(synchronize_session=False)
        weekly_configs_deleted = WeeklyVersionConfig.query.filter_by(
            repository_id=repository_id
        ).delete(synchronize_session=False)
        log_print(f"删除了 {weekly_configs_deleted} 个WeeklyVersionConfig记录", "DELETE")

        repository.last_sync_commit_id = None
        repository.last_sync_time = None
        repository.cache_version = None
        repository.sync_mode = "full"
        log_print(f"清空了仓库 {repo_name} 的增量缓存同步字段", "DELETE")

        db.session.delete(repository)
        db.session.commit()
        log_print(f"成功删除仓库 {repo_name} 的所有数据库记录", "DELETE")
        flash(f"仓库 {repo_name} 及其所有关联数据已成功删除", "success")
    except Exception as exc:
        db.session.rollback()
        log_print(f"删除仓库失败: {str(exc)}", "ERROR")
        flash(f"删除仓库失败: {str(exc)}", "error")
        return redirect(url_for("repository_config", project_id=project_id))

    delete_local_repository_directory(local_path, repo_name)
    return redirect(url_for("repository_config", project_id=project_id))


@require_admin
def test_repository(repository_id):
    """测试仓库连通性 / 触发同步。

    权限：本文件其他四个维护接口（update_repository_order / swap_repository_order /
    delete_repository / delete_project）都带 `@require_admin`，唯独这个没有，
    也不在 `SENSITIVE_ENDPOINTS` 里。于是任意已登录用户可以对**任意**
    repository_id 触发真实同步任务（agent 模式下会给别的项目的仓库派发任务）
    或本地 `clone_or_update_repository()`，并把 git 错误文本回显出来。
    现已补 `@require_admin`，并把它一并加入 `SENSITIVE_ENDPOINTS` 作第二道防线
    （该表原先写的是裸 endpoint 名，整体失效，已修为全限定名）。
    """
    Repository, log_print = _runtime("Repository", "log_print")
    get_git_service = get_runtime_model("get_git_service")
    create_auto_sync_task = get_runtime_model("create_auto_sync_task")

    repository = Repository.query.get_or_404(repository_id)

    def _is_ajax_request():
        accept = request.headers.get("Accept", "")
        return (
            request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or request.is_json
            or "application/json" in accept
        )

    def _respond(success: bool, message: str, *, category: str, status_code: int):
        if _is_ajax_request():
            return (
                jsonify(
                    {
                        "success": bool(success),
                        "status": "success" if success else "error",
                        "message": message,
                        "scope": "platform_local",
                    }
                ),
                status_code,
            )
        flash(message, category)
        return redirect(url_for("repository_config", project_id=repository.project_id))

    if is_agent_dispatch_mode():
        task_id = create_auto_sync_task(repository.id)
        if task_id:
            return _respond(
                True,
                f"已派发到 Agent 执行连通性检查与同步 (task_id={task_id})，平台不再本地 clone 仓库",
                category="success",
                status_code=200,
            )
        return _respond(
            False,
            "平台+Agent 模式下未能派发测试任务，请检查项目与Agent绑定状态",
            category="error",
            status_code=409,
        )

    try:
        log_print(f"测试仓库连接: {repository.name}", "TEST")
        log_print(f"仓库类型: {repository.type}", "TEST")
        log_print(f"仓库URL: {_redact_repository_secrets(repository.url, repository)}", "TEST")
        log_print(f"分支: {repository.branch}", "TEST")
        log_print(f"Token: {'已设置' if repository.token else '未设置'}", "TEST")

        if repository.type == "git":
            service = get_git_service(repository)
            log_print(f"本地路径: {service.local_path}", "TEST")
            ssh_test_result = service.test_ssh_connection()
            log_print(f"SSH连接测试结果: {ssh_test_result}", "TEST")
            if not ssh_test_result:
                return _respond(
                    False,
                    "SSH连接测试失败，请检查网络连接和SSH配置",
                    category="error",
                    status_code=400,
                )
            else:
                success, message = service.clone_or_update_repository()
                if success:
                    return _respond(
                        True,
                        f"仓库连接测试成功: {_redact_repository_secrets(message, repository)}",
                        category="success",
                        status_code=200,
                    )
                else:
                    return _respond(
                        False,
                        f"仓库连接测试失败: {_redact_repository_secrets(message, repository)}",
                        category="error",
                        status_code=400,
                    )
        else:
            return _respond(
                False,
                "暂时只支持Git仓库测试",
                category="warning",
                status_code=400,
            )
    except Exception as exc:
        # git 的异常文本同样会带 remote URL（含凭据），落日志与回显前都要过一遍。
        safe_exc = _redact_repository_secrets(str(exc), repository)
        log_print(f"测试过程中发生错误: {safe_exc}", "TEST", force=True)
        import traceback

        # 栈里同样有那条 URL：先取文本、过脱敏、再输出，别用 print_exc() 直接打原文
        # （stderr 常被启动脚本重定向进日志文件）。
        safe_traceback = _redact_repository_secrets(traceback.format_exc(), repository)
        log_print(safe_traceback, "TEST", force=True)
        return _respond(
            False,
            f"测试失败: {safe_exc}",
            category="error",
            status_code=500,
        )


@require_admin
def delete_project(project_id):
    (
        db,
        Project,
        Repository,
        Commit,
        DiffCache,
        ExcelHtmlCache,
        MergedDiffCache,
        BackgroundTask,
        WeeklyVersionConfig,
        WeeklyVersionDiffCache,
        WeeklyVersionExcelCache,
        OperationLog,
        AgentProjectBinding,
        AgentTask,
        log_print,
    ) = _runtime(
        "db",
        "Project",
        "Repository",
        "Commit",
        "DiffCache",
        "ExcelHtmlCache",
        "MergedDiffCache",
        "BackgroundTask",
        "WeeklyVersionConfig",
        "WeeklyVersionDiffCache",
        "WeeklyVersionExcelCache",
        "OperationLog",
        "AgentProjectBinding",
        "AgentTask",
        "log_print",
    )

    project = Project.query.get_or_404(project_id)
    repo_rows = Repository.query.filter_by(project_id=project_id).all()
    repo_ids = [repo.id for repo in repo_rows]
    weekly_config_ids = [row.id for row in WeeklyVersionConfig.query.filter_by(project_id=project_id).all()]
    project_name = project.name

    repo_local_paths = [
        (
            build_repository_local_path(project.code, repo.name, repo.id, strict=False),
            repo.name,
        )
        for repo in repo_rows
    ]

    AuthUserFunction = _optional_runtime("AuthUserFunction")
    AuthUserProject = _optional_runtime("AuthUserProject")
    AuthProjectJoinRequest = _optional_runtime("AuthProjectJoinRequest")
    AuthProjectCreateRequest = _optional_runtime("AuthProjectCreateRequest")
    AuthProjectPreAssignment = _optional_runtime("AuthProjectPreAssignment")
    QkitAuthUserProject = _optional_runtime("QkitAuthUserProject")
    QkitAuthProjectJoinRequest = _optional_runtime("QkitAuthProjectJoinRequest")
    QkitAuthProjectCreateRequest = _optional_runtime("QkitAuthProjectCreateRequest")
    QkitAuthProjectPreAssignment = _optional_runtime("QkitAuthProjectPreAssignment")
    QkitAuthProjectImportConfig = _optional_runtime("QkitAuthProjectImportConfig")
    QkitAuthImportBlock = _optional_runtime("QkitAuthImportBlock")
    AuthProjectConfirmPermission = _optional_runtime("AuthProjectConfirmPermission")
    QkitProjectConfirmPermission = _optional_runtime("QkitProjectConfirmPermission")
    AiAnalysisRun = _optional_runtime("AiAnalysisRun")
    AiAnalysisTrace = _optional_runtime("AiAnalysisTrace")
    AiAnalysisAnomaly = _optional_runtime("AiAnalysisAnomaly")
    # 逐轮事件账（`models/ai_analysis/round_event.py`）：`project_id` 是**可空**列，
    # 那张表也**刻意不声明外键**（见它的模块抬头）—— 所以删项目时 SQLAlchemy 既不会
    # 碰它、也不会撞 NOT NULL。不清的代价不是「删不掉」，而是**留下孤儿行**；
    # 而那条「跟着 schema 走」的护栏扫的正是「带非空外键的表」，看不见它。
    # 清单是手写的，所以这里必须显式列出来。
    AiAnalysisRoundEvent = _optional_runtime("AiAnalysisRoundEvent")
    AiProjectAnalysisConfig = _optional_runtime("AiProjectAnalysisConfig")
    AiProjectApiKey = _optional_runtime("AiProjectApiKey")
    AiWeeklyAnalysisState = _optional_runtime("AiWeeklyAnalysisState")
    # Diff 快照（`models/ai_analysis/diff_snapshot.py`）：`project_id` 是**非空**外键，
    # 且 `Project` 上的 backref 默认 cascade 里没有 delete —— 不显式清，删除项目时
    # SQLAlchemy 会先把子行的 `project_id` 置 NULL，撞 NOT NULL，**整个项目删不掉**。
    AiDiffSnapshot = _optional_runtime("AiDiffSnapshot")
    AiDiffSnapshotItem = _optional_runtime("AiDiffSnapshotItem")
    # 同理：AI 分析的**任务身份**表（`models/ai_analysis/job.py`）。
    AiAnalysisJob = _optional_runtime("AiAnalysisJob")
    AgentTempCache = _optional_runtime("AgentTempCache")

    def _safe_delete(query, label):
        try:
            count = query.delete(synchronize_session=False)
            if count:
                log_print(f"删除项目关联数据: {label} -> {count} 条", "DELETE")
            return count
        except Exception as exc:
            log_print(f"⚠️ 删除项目关联数据失败({label}): {exc}", "DELETE", force=True)
            return 0

    def _safe_nullify_created_project(model, label):
        if model is None:
            return 0
        try:
            count = (
                model.query.filter_by(created_project_id=project_id).update(
                    {"created_project_id": None},
                    synchronize_session=False,
                )
            )
            if count:
                log_print(f"清理项目创建申请引用: {label} -> {count} 条", "DELETE")
            return count
        except Exception as exc:
            log_print(f"⚠️ 清理项目创建申请引用失败({label}): {exc}", "DELETE", force=True)
            return 0

    try:
        # 先清理 auth / qkit 对项目的直接引用，避免 Project 删除时外键冲突
        if AuthUserFunction is not None:
            _safe_delete(AuthUserFunction.query.filter(AuthUserFunction.project_id == project_id), "AuthUserFunction")
        if AuthUserProject is not None:
            _safe_delete(AuthUserProject.query.filter(AuthUserProject.project_id == project_id), "AuthUserProject")
        if AuthProjectJoinRequest is not None:
            _safe_delete(
                AuthProjectJoinRequest.query.filter(AuthProjectJoinRequest.project_id == project_id),
                "AuthProjectJoinRequest",
            )
        if AuthProjectPreAssignment is not None:
            _safe_delete(
                AuthProjectPreAssignment.query.filter(AuthProjectPreAssignment.project_id == project_id),
                "AuthProjectPreAssignment",
            )
        _safe_nullify_created_project(AuthProjectCreateRequest, "AuthProjectCreateRequest")

        if QkitAuthUserProject is not None:
            _safe_delete(
                QkitAuthUserProject.query.filter(QkitAuthUserProject.project_id == project_id),
                "QkitAuthUserProject",
            )
        if QkitAuthProjectJoinRequest is not None:
            _safe_delete(
                QkitAuthProjectJoinRequest.query.filter(QkitAuthProjectJoinRequest.project_id == project_id),
                "QkitAuthProjectJoinRequest",
            )
        if QkitAuthProjectPreAssignment is not None:
            _safe_delete(
                QkitAuthProjectPreAssignment.query.filter(QkitAuthProjectPreAssignment.project_id == project_id),
                "QkitAuthProjectPreAssignment",
            )
        if QkitAuthProjectImportConfig is not None:
            _safe_delete(
                QkitAuthProjectImportConfig.query.filter(QkitAuthProjectImportConfig.project_id == project_id),
                "QkitAuthProjectImportConfig",
            )
        if QkitAuthImportBlock is not None:
            _safe_delete(
                QkitAuthImportBlock.query.filter(QkitAuthImportBlock.project_id == project_id),
                "QkitAuthImportBlock",
            )
        _safe_nullify_created_project(QkitAuthProjectCreateRequest, "QkitAuthProjectCreateRequest")
        # 确认权限规则：两张表都是 `project_id` NOT NULL，漏一张就整个删除失败。
        if AuthProjectConfirmPermission is not None:
            _safe_delete(
                AuthProjectConfirmPermission.query.filter(
                    AuthProjectConfirmPermission.project_id == project_id
                ),
                "AuthProjectConfirmPermission",
            )
        if QkitProjectConfirmPermission is not None:
            _safe_delete(
                QkitProjectConfirmPermission.query.filter(
                    QkitProjectConfirmPermission.project_id == project_id
                ),
                "QkitProjectConfirmPermission",
            )

        # ---- AI 分析那一族（ai_*）与 Agent 临时缓存 ----
        #
        # 整族都不在「仓库 / 周版本」那条链上，所以它们的外键此前**一条都没被显式删过**。
        # 后果不是「留了点垃圾数据」，而是**项目永远删不掉**：`db.session.delete(project)`
        # 时 ORM 会先把所有子行的 `project_id` 置 NULL（默认 cascade 里没有 delete），
        # 而这些列全是 NOT NULL，于是抛
        # `NOT NULL constraint failed: ai_project_analysis_config.project_id` ——
        # 报错信息里只有**第一张**撞上的表，剩下的几张要删一次、看一次日志才能试出来。
        #
        # 顺序：`ai_analysis_trace` / `ai_analysis_anomaly` 的 `run_id` 是 NOT NULL 外键，
        # 必须**先于** `ai_analysis_run` 删，否则删 run 的那一步同样会撞 NOT NULL。
        ai_run_ids: list[int] = []
        if AiAnalysisRun is not None:
            ai_run_ids = [
                row[0]
                for row in db.session.query(AiAnalysisRun.id)
                .filter(AiAnalysisRun.project_id == project_id)
                .all()
            ]
        if AiAnalysisTrace is not None and ai_run_ids:
            _safe_delete(
                AiAnalysisTrace.query.filter(AiAnalysisTrace.run_id.in_(ai_run_ids)),
                "AiAnalysisTrace",
            )
        if AiAnalysisRoundEvent is not None:
            # 两个条件都要，理由与下面 `AiAnalysisAnomaly` 那段同款：`run_id` 那条覆盖
            # 「挂在本项目运行上的逐轮事件」，`project_id` 那条覆盖「运行记录已经没了、
            # 事件还在」的孤儿行。放在删 `ai_analysis_run` **之前**——事件行是运行的下级。
            event_filters = [AiAnalysisRoundEvent.project_id == project_id]
            if ai_run_ids:
                event_filters.append(AiAnalysisRoundEvent.run_id.in_(ai_run_ids))
            _safe_delete(
                AiAnalysisRoundEvent.query.filter(or_(*event_filters)),
                "AiAnalysisRoundEvent",
            )
        if AiAnalysisAnomaly is not None:
            # 两个条件都要：`run_id` 那条覆盖「挂在本项目运行上的异常」，`project_id`
            # 那条覆盖「运行记录已经被清理、异常还在」的孤儿行（异常是可以单独处置的，
            # 用户处置过的那条不该因为运行记录没了就留下）。
            anomaly_filters = [AiAnalysisAnomaly.project_id == project_id]
            if ai_run_ids:
                anomaly_filters.append(AiAnalysisAnomaly.run_id.in_(ai_run_ids))
            _safe_delete(
                AiAnalysisAnomaly.query.filter(or_(*anomaly_filters)),
                "AiAnalysisAnomaly",
            )
        if AiAnalysisRun is not None:
            _safe_delete(
                AiAnalysisRun.query.filter(AiAnalysisRun.project_id == project_id),
                "AiAnalysisRun",
            )
        if AiProjectAnalysisConfig is not None:
            _safe_delete(
                AiProjectAnalysisConfig.query.filter(
                    AiProjectAnalysisConfig.project_id == project_id
                ),
                "AiProjectAnalysisConfig",
            )
        if AiProjectApiKey is not None:
            _safe_delete(
                AiProjectApiKey.query.filter(AiProjectApiKey.project_id == project_id),
                "AiProjectApiKey",
            )
        if AiWeeklyAnalysisState is not None:
            _safe_delete(
                AiWeeklyAnalysisState.query.filter(
                    AiWeeklyAnalysisState.project_id == project_id
                ),
                "AiWeeklyAnalysisState",
            )
        if AiDiffSnapshot is not None:
            # **先子后父**：`ai_diff_snapshot_item.snapshot_id` 也是非空外键。两张表都要
            # 清掉，只删父表会留一堆谁也读不到的条目行（同 `ai_analysis_trace`
            # 那一条的取舍，见 `services/ai/run_cache_source.cleanup_expired_analysis_runs`）。
            snapshot_ids = [
                row[0]
                for row in db.session.query(AiDiffSnapshot.id).filter(
                    AiDiffSnapshot.project_id == project_id
                ).all()
            ]
            if snapshot_ids and AiDiffSnapshotItem is not None:
                _safe_delete(
                    AiDiffSnapshotItem.query.filter(
                        AiDiffSnapshotItem.snapshot_id.in_(snapshot_ids)
                    ),
                    "AiDiffSnapshotItem",
                )
            _safe_delete(
                AiDiffSnapshot.query.filter(AiDiffSnapshot.project_id == project_id),
                "AiDiffSnapshot",
            )
        if AiAnalysisJob is not None:
            # 同上（`models/ai_analysis/job.py` 的 `project_id` 同样是非空外键）。
            # 这一条是**删除能不能成功**的必要条件，不是「顺手清垃圾」：项目下只要有过
            # 一次 AI 分析动作，`backref` 的默认 cascade 就会去把 `project_id` 置 NULL，
            # 撞 NOT NULL 之后整个删除事务回滚 —— 用户看到的是「删不掉」。
            #
            # **没有子表**：指向 job 的只有 `BackgroundTask.job_id`，那是普通整数列、
            # 没有外键（见 `models/task.py` 里那段说明），所以这一条是单表删除，
            # 不需要像上面 `AiDiffSnapshot` 那样先子后父。
            _safe_delete(
                AiAnalysisJob.query.filter(AiAnalysisJob.project_id == project_id),
                "AiAnalysisJob",
            )
        if AgentTempCache is not None:
            # 这张表**没有外键**（`project_id` 只是个可空列），所以它既不会让删除失败，
            # 也不会在删完之后显现出来 —— 只能显式清。
            temp_filters = [AgentTempCache.project_id == project_id]
            if repo_ids:
                temp_filters.append(AgentTempCache.repository_id.in_(repo_ids))
            _safe_delete(AgentTempCache.query.filter(or_(*temp_filters)), "AgentTempCache")

        # 删除 Agent 相关引用
        background_task_ids = []
        if repo_ids:
            background_task_ids = [row[0] for row in db.session.query(BackgroundTask.id).filter(
                BackgroundTask.repository_id.in_(repo_ids)
            ).all()]

        agent_task_filters = [AgentTask.project_id == project_id]
        if repo_ids:
            agent_task_filters.append(AgentTask.repository_id.in_(repo_ids))
        if background_task_ids:
            agent_task_filters.append(AgentTask.source_task_id.in_(background_task_ids))
        _safe_delete(AgentTask.query.filter(or_(*agent_task_filters)), "AgentTask")
        _safe_delete(
            AgentProjectBinding.query.filter(AgentProjectBinding.project_id == project_id),
            "AgentProjectBinding",
        )

        # 删除周版本缓存和配置
        weekly_diff_filters = []
        weekly_excel_filters = []
        if weekly_config_ids:
            weekly_diff_filters.append(WeeklyVersionDiffCache.config_id.in_(weekly_config_ids))
            weekly_excel_filters.append(WeeklyVersionExcelCache.config_id.in_(weekly_config_ids))
        if repo_ids:
            weekly_diff_filters.append(WeeklyVersionDiffCache.repository_id.in_(repo_ids))
            weekly_excel_filters.append(WeeklyVersionExcelCache.repository_id.in_(repo_ids))
        if weekly_diff_filters:
            _safe_delete(
                WeeklyVersionDiffCache.query.filter(or_(*weekly_diff_filters)),
                "WeeklyVersionDiffCache",
            )
        if weekly_excel_filters:
            _safe_delete(
                WeeklyVersionExcelCache.query.filter(or_(*weekly_excel_filters)),
                "WeeklyVersionExcelCache",
            )
        _safe_delete(
            WeeklyVersionConfig.query.filter(WeeklyVersionConfig.project_id == project_id),
            "WeeklyVersionConfig",
        )

        # 删除仓库相关记录（按依赖顺序）
        if repo_ids:
            _safe_delete(OperationLog.query.filter(OperationLog.repository_id.in_(repo_ids)), "OperationLog")
            _safe_delete(DiffCache.query.filter(DiffCache.repository_id.in_(repo_ids)), "DiffCache")
            _safe_delete(ExcelHtmlCache.query.filter(ExcelHtmlCache.repository_id.in_(repo_ids)), "ExcelHtmlCache")
            _safe_delete(MergedDiffCache.query.filter(MergedDiffCache.repository_id.in_(repo_ids)), "MergedDiffCache")
            _safe_delete(Commit.query.filter(Commit.repository_id.in_(repo_ids)), "Commit")
            _safe_delete(
                BackgroundTask.query.filter(BackgroundTask.repository_id.in_(repo_ids)),
                "BackgroundTask",
            )

        _safe_delete(Repository.query.filter(Repository.project_id == project_id), "Repository")

        db.session.delete(project)
        db.session.commit()
        log_print(f"项目删除成功: {project_name} (ID: {project_id})", "DELETE")
    except Exception as exc:
        db.session.rollback()
        log_print(f"删除项目失败: {project_name} (ID: {project_id}) -> {exc}", "ERROR", force=True)
        flash(f"删除项目失败: {exc}", "error")
        return redirect(url_for("index"))

    for local_path, repo_name in repo_local_paths:
        delete_local_repository_directory(local_path, repo_name)

    flash("项目删除成功", "success")
    return redirect(url_for("index"))
