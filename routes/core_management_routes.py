#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Core app route blueprint wrappers.

This module extracts the remaining non-weekly/non-cache/non-commit route
registration from app.py. Handlers stay in app.py for low-risk behavior parity.
"""

from flask import Blueprint, jsonify

from services.model_loader import get_runtime_model


core_management_bp = Blueprint("core_management_routes", __name__)


def _dispatch(handler_name, *args, **kwargs):
    handler = get_runtime_model(handler_name)
    return handler(*args, **kwargs)


def _probe_database_ok():
    """做一次 `SELECT 1`，确认连接池与数据库真的可用。返回 `(ok, error)`。

    单独抽成函数是为了可测：探针最有价值的性质是「数据库挂了返回 503」，
    而直接往路由里塞一个坏引擎需要 monkeypatch Flask-SQLAlchemy 的 `engine`
    属性（那是需要应用上下文的 property），既别扭又会影响其他 fixture 的收尾。
    """
    try:
        from flask import current_app

        from sqlalchemy import text as sa_text

        db = current_app.extensions["sqlalchemy"]
        with db.engine.connect() as conn:
            conn.execute(sa_text("SELECT 1"))
        return True, None
    except Exception as exc:  # noqa: BLE001 - 探针必须把所有失败都归类为「不健康」
        return False, exc


@core_management_bp.route("/healthz", methods=["GET"], endpoint="healthz")
def healthz_route():
    """存活/就绪探针（供负载均衡、systemd watchdog、k8s probe 使用）。

    为什么需要它：此前唯一的存活判断手段是 `/test`（恒返回 200，不碰数据库）
    或 `/`（要渲染项目列表）。两者都不适合做探针：

    * `/test` 在数据库已挂、磁盘写满、迁移失败时**照样返回 200** ——
      探针会把一个已经不可用的实例一直留在负载均衡后面；
    * `/` 要渲染模板并查询项目列表，把它当探针等于每次探测都做一次业务查询。

    本接口的三个刻意选择：

    1. **免鉴权**（`/healthz` 已注册进 `AUTH_EXEMPT_PATHS`）——
       探针拿不到会话 Cookie，需要鉴权的探针根本没法用。
    2. 只做一次 `SELECT 1`，数据库不可用时返回 **503**（不是 200）
       —— 这是探针能起作用的唯一前提。
    3. **不泄露任何敏感信息**：响应体只有固定的 `status` / `database` 两个字段，
       不含版本号、路径、连接串、表名；失败详情只写服务端日志。

    ⚠️ 本路由注册在 `core_management_routes` 上。仓库里曾有一套**从未被注册**的
    遗留路由包（`routes/main_routes.py` 等 5 个模块定义了同名蓝图，但
    `register_blueprints()` 全仓无调用方），写在那里等于没写（实测加完仍是 404，
    且不报错）。那 5 个模块已删除，`routes/__init__.py` 里写明了原因。
    新增路由请一律确认对应蓝图**真的被 app.py 注册**了。
    """
    ok, exc = _probe_database_ok()
    if ok:
        return jsonify({"status": "ok", "database": "ok"}), 200

    try:
        from utils.safe_print import log_print
        from utils.security_utils import sanitize_text

        # 详细原因只进服务端日志，不出现在响应体里。
        #
        # 必须过 sanitize_text：数据库连接类异常的消息里常常带着完整连接串
        # （`mysql+pymysql://user:pw@host/db`），直接写日志等于把口令落到日志文件。
        # 实测未脱敏时日志里会出现 `secret-dsn-user:pw@10.1.2.3/diff_platform`。
        log_print(
            f"健康检查失败: {type(exc).__name__}: {sanitize_text(str(exc))}",
            "APP",
            force=True,
        )
    except Exception:  # pragma: no cover - 日志不可用不影响探针语义
        pass

    return jsonify({"status": "degraded", "database": "unavailable"}), 503


@core_management_bp.route("/auth/login", methods=["GET", "POST"], endpoint="admin_login")
def admin_login_route():
    return _dispatch("admin_login")


@core_management_bp.route("/auth/logout", methods=["POST"], endpoint="admin_logout")
def admin_logout_route():
    return _dispatch("admin_logout")


@core_management_bp.route("/test", endpoint="test")
def test_route():
    return _dispatch("test")


@core_management_bp.route("/help", endpoint="help_page")
def help_page_route():
    return _dispatch("help_page")


@core_management_bp.route("/", endpoint="index")
def index_route():
    return _dispatch("index")


@core_management_bp.route("/projects", methods=["GET", "POST"], endpoint="projects")
def projects_route():
    return _dispatch("projects")


@core_management_bp.route("/projects/<int:project_id>/update", methods=["POST"], endpoint="update_project")
def update_project_route(project_id):
    return _dispatch("update_project", project_id)


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


@core_management_bp.route("/repositories/<int:repository_id>/clone-status", endpoint="get_clone_status")
def get_clone_status_route(repository_id):
    return _dispatch("get_clone_status", repository_id)


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
