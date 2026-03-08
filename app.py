import os
import sys

# 在所有配置读取之前加载 .env 文件
# 优先级: 系统环境变量 > .env 文件 (override=False)
from dotenv import load_dotenv
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    load_dotenv(_env_path, override=False)

import json
import math
import re
import threading
import queue
import time
import atexit
import signal
import schedule
import logging
import secrets
from datetime import datetime, timedelta, timezone

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, abort
from flask_cors import CORS
from sqlalchemy import Index, func, case, or_, and_
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.exceptions import Forbidden, NotFound

from models import (
    db,
    Project,
    Repository,
    GlobalRepositoryCounter,
    Commit,
    DiffCache,
    ExcelHtmlCache,
    MergedDiffCache,
    BackgroundTask,
    WeeklyVersionConfig,
    WeeklyVersionDiffCache,
    WeeklyVersionExcelCache,
    OperationLog,
    AgentNode,
    AgentProjectBinding,
    AgentTask,
)

from services.git_service import GitService
from services.enhanced_git_service import EnhancedGitService
from services.threaded_git_service import ThreadedGitService
# ThreadedGitService配置说明:
# - 多线程优化前一次提交查找，显著提升大仓库性能
# - 默认使用CPU核心数+4个工作线程
# - 包含超时机制和异常降级处理
# - 与原GitService完全兼容
from services.svn_service import SVNService
from services.diff_service import DiffService
from services.excel_html_cache_service import ExcelHtmlCacheService
from services.excel_diff_cache_service import (
    ExcelDiffCacheService,
    configure_excel_diff_cache_service,
)
from services.performance_metrics_service import get_perf_metrics_service
from services.repository_cleanup_helpers import (
    cleanup_pending_deletions,
    delete_local_repository_directory,
)
from services.repository_compare_helpers import (
    commits_compare,
    get_commits_by_file,
    repository_compare,
)
from services.diff_render_helpers import (
    generate_side_by_side_diff,
    get_file_icon,
    is_deleted_file,
    parse_and_render_diff,
    render_deleted_content_details,
    render_deleted_file_content,
    render_excel_diff_html,
    render_github_style_diff,
    render_git_diff_content,
    render_new_file_content,
)
from services.repository_admin_handlers import (
    delete_project,
    delete_repository,
    swap_repository_order,
    test_repository,
    update_repository_order,
)
from services.commit_operation_handlers import (
    _attach_author_display,
    approve_all_files,
    batch_approve_commits,
    batch_reject_commits,
    get_commit_diff_data,
    merge_diff,
    refresh_merge_diff,
    reject_commit,
    request_priority_diff,
    request_priority_diff_with_path,
    update_commit_fields_route,
)
from services.status_sync_handlers import (
    clear_all_confirmation_status,
    get_sync_configs,
    get_sync_mapping_info,
    project_status_sync_management,
    status_sync_management,
    status_sync_test,
    weekly_version_batch_confirm_api,
)
from services.commit_diff_logic import (
    configure_commit_diff_logic,
    get_diff_data,
    resolve_previous_commit,
    get_real_diff_data_for_merge,
    get_merged_diff_data,
    generate_merged_diff_data,
    handle_different_files_merge,
    handle_consecutive_commits_merge_internal,
    handle_non_consecutive_commits_merge_internal,
    build_smart_display_list,
    check_commit_cache_available,
    create_merged_commit_display,
    are_commits_consecutive_internal,
    get_commit_pair_diff_internal,
    convert_hunks_to_lines,
    get_mock_diff_data,
)
from services.agent_commit_diff_dispatch import maybe_dispatch_commit_diff
from services.auth_bootstrap_service import initialize_auth_subsystem
from services.commit_diff_template_context import build_commit_diff_template_context
from services.app_routing_bootstrap_service import configure_app_routing_bootstrap
from services.app_runtime_wiring_service import configure_runtime_wirings
from services.app_security_bootstrap_service import configure_app_security_bootstrap
from services.repository_update_form_service import (
    clear_repository_state_for_switch,
    handle_update_repository_form,
)
from services.repository_update_api_service import (
    handle_batch_update_credentials,
    handle_reuse_repository_and_update,
    handle_update_repository_and_cache,
    run_repository_update_and_cache_worker,
)
from services.commit_status_api_service import (
    handle_batch_update_commits_compat,
    handle_update_commit_status,
)
from services.repository_maintenance_api_service import (
    handle_get_cache_status,
    handle_get_clone_status,
    handle_regenerate_cache,
    handle_retry_clone_repository,
    handle_sync_repository,
    should_retry_with_reclone,
)
from services.commit_diff_page_service import (
    handle_commit_full_diff,
    handle_refresh_commit_diff,
)
from services.app_bootstrap_db_service import (
    clear_startup_version_mismatch_cache,
    create_tables_with_runtime_checks,
)
from services.db_migration_service import apply_schema_migrations
from services.deployment_mode import get_commit_diff_mode_strategy
from services.weekly_version_file_handlers import (
    get_file_content_at_commit,
    weekly_version_file_complete_diff,
    weekly_version_file_previous_version,
    weekly_version_file_status_api,
    weekly_version_file_status_info_api,
    weekly_version_stats_api,
)
from services.repository_creation_handlers import (
    clone_repository_to_local,
    clone_svn_repository_to_local,
    create_git_repository,
    create_svn_repository,
    enhanced_async_clone_with_status_update,
    enhanced_async_svn_clone_with_status_update,
)
from services.core_navigation_handlers import (
    add_git_repository,
    add_svn_repository,
    admin_login,
    admin_logout,
    help_page,
    index,
    project_detail,
    project_detail_original,
    projects,
    update_project,
    repository_config,
    test,
)
from services.repository_sync_status import (
    clear_sync_error as clear_repository_sync_error,
    record_sync_error as record_repository_sync_error,
)
from services.agent_management_handlers import (
    register_agent_node,
    agent_heartbeat,
    agent_upsert_temp_cache,
    get_agent_temp_cache,
    resolve_agent_temp_cache,
    agent_get_latest_release,
    agent_download_release_package,
    list_agent_releases,
    rollback_agent_release,
    list_agent_nodes,
    list_agent_tasks,
    agent_overview_page,
    agent_claim_task,
    agent_report_task_result,
    agent_report_incident,
    list_agent_incidents,
    ignore_agent_incident,
    get_agent_abnormal_summary,
)
from routes.agent_management_routes import agent_management_bp
from routes.cache_management_routes import cache_management_bp
from routes.commit_diff_routes import commit_diff_bp
from routes.core_management_routes import core_management_bp
from routes.weekly_version_management_routes import weekly_version_bp
from utils.url_helpers import generate_commit_diff_url, generate_excel_diff_data_url, generate_refresh_diff_url
import threading
import queue
import logging
from utils.db_retry import db_retry
from utils.sqlite_config import set_sqlite_pragma  # 导入SQLite优化配置
from utils.db_config import (
    apply_database_settings,
    get_database_backend_from_config,
)
from urllib.parse import urlparse
from os import system

from utils.security_utils import (
    decrypt_credential,
    encrypt_credential,
    sanitize_text,
    validate_repository_name,
)
from utils.path_security import build_repository_local_path
from utils.diff_data_utils import (
    clean_json_data,
    format_cell_value,
    get_excel_column_letter,
    safe_json_serialize,
    validate_excel_diff_data,
)
from utils.request_security import (
    _csrf_error_response,
    _csrf_token_from_request,
    _is_safe_redirect,
    _is_same_origin_request,
    _is_valid_admin_token,
    _has_admin_access,
    _has_project_admin_access,
    _has_project_access,
    _has_project_create_access,
    _is_logged_in,
    _get_current_user,
    _get_accessible_project_ids,
    _unauthorized_admin_response,
    _unauthorized_login_response,
    csrf_token,
    configure_request_security,
    require_admin,
    require_login,
)
_IS_TESTING = os.environ.get("TESTING", "").lower() in ("1", "true", "yes")
if not _IS_TESTING:
    system("title SEOTool - diff-confirmation-platform")
# 设置控制台输出编码为UTF-8
# 在测试环境中跳过 stdout/stderr 重包装，避免 pytest 的 I/O 冲突
if sys.platform == 'win32' and not _IS_TESTING:
    import codecs
    import io
    # 设置UTF-8编码并启用错误处理
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass  # stdout.buffer 不可用（如被 pytest 捕获时）
    # 设置控制台代码页为UTF-8
    os.system('chcp 65001 >nul 2>&1')
# Diff逻辑版本号 - 当diff算法或逻辑发生变化时需要更新此版本号
DIFF_LOGIC_VERSION = "1.8.0"

# ---------------------------------------------------------------------------
#  日志系统 — 已拆分至 utils/logger.py
# ---------------------------------------------------------------------------
from utils.logger import (
    LOG_LEVEL,
    _LOG_CATEGORIES,
    clear_log_file,
    install_exception_handlers,
    install_print_override,
    log_print,
    safe_log_print,
)

# 安装 print 重载和异常处理器
install_print_override(_IS_TESTING)
install_exception_handlers()

# ---------------------------------------------------------------------------
#  VCS 内容获取 — 已拆分至 services/vcs_content_service.py
# ---------------------------------------------------------------------------
from services.vcs_content_service import (
    configure_vcs_service,
    get_file_content_from_git,
    get_file_content_from_svn,
    get_git_service,
    get_svn_service,
    get_unified_diff_data,
)
from bootstrap.app_factory import build_runtime_settings, create_app
from bootstrap.bootstrap import AppBootstrapManager

app = create_app(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


class _WerkzeugAgentAccessFilter(logging.Filter):
    """Suppress high-frequency /api/agents access logs for 2xx/3xx responses."""

    _SUPPRESS_PATH_PREFIXES = ("/api/agents",)
    _REQUEST_LINE_PATTERN = re.compile(
        r'"(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(\S+)\s+HTTP/[0-9.]+"\s+(?:\x1b\[[0-9;]*m)?(\d{3})'
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True

        if "/api/agents" not in message:
            return True

        matched = self._REQUEST_LINE_PATTERN.search(message or "")
        if not matched:
            return True

        path = matched.group(1)
        try:
            status_code = int(matched.group(2))
        except Exception:
            return True

        if any(path.startswith(prefix) for prefix in self._SUPPRESS_PATH_PREFIXES):
            return status_code >= 400
        return True


def _configure_werkzeug_access_log_filter() -> None:
    if not _env_bool("SUPPRESS_AGENT_ACCESS_LOG", True):
        return
    werkzeug_logger = logging.getLogger("werkzeug")
    if any(isinstance(item, _WerkzeugAgentAccessFilter) for item in werkzeug_logger.filters):
        return
    werkzeug_logger.addFilter(_WerkzeugAgentAccessFilter())


_configure_werkzeug_access_log_filter()
# 启用CORS支持，允许跨域请求
runtime_settings = build_runtime_settings(os.environ)
secret_key = runtime_settings.secret_key
if not secret_key:
    secret_key = secrets.token_urlsafe(48)
    log_print("⚠️ FLASK_SECRET_KEY 未配置，已使用运行期随机密钥。生产环境必须显式配置。", "APP", force=True)
cors_allowed_origins = runtime_settings.cors_allowed_origins
if cors_allowed_origins:
    CORS(app, resources={
        r"/status-sync/*": {"origins": cors_allowed_origins},
        r"/api/*": {"origins": cors_allowed_origins},
        r"/admin/*": {"origins": cors_allowed_origins},
    })
else:
    log_print("ℹ️ 未配置 CORS_ALLOWED_ORIGINS，默认禁用跨域访问。", "APP", force=True)
CSRF_SESSION_KEY = "_csrf_token"
ENABLE_ADMIN_SECURITY = runtime_settings.enable_admin_security
DEPLOYMENT_MODE = runtime_settings.deployment_mode
if runtime_settings.deployment_mode_invalid:
    raw_mode = (os.environ.get("DEPLOYMENT_MODE") or "").strip()
    log_print(f"⚠️ 非法 DEPLOYMENT_MODE={raw_mode}，回退为 single", "APP", force=True)
ENABLE_LOCAL_WORKER = runtime_settings.enable_local_worker


log_print(
    f"服务启动模式: {DEPLOYMENT_MODE} | 本地后台任务: {'启用' if ENABLE_LOCAL_WORKER else '禁用'}",
    "APP",
    force=True,
)
configure_request_security(
    csrf_session_key=CSRF_SESSION_KEY,
    enable_admin_security=ENABLE_ADMIN_SECURITY,
)

# static-check compatibility:
# sensitive endpoints still include 'reuse_repository_and_update' and 'update_repository_and_cache',
# but the full list moved to services/app_security_bootstrap_service.py.


@app.before_request
def log_request_info():
    """Record incoming request info for admin routes."""
    if request.path.startswith('/admin/'):
        log_print(f"[REQUEST] {request.method} {request.path}", 'REQUEST')
from utils.timezone_utils import format_beijing_time
configure_app_security_bootstrap(
    app=app,
    log_print=log_print,
    csrf_session_key=CSRF_SESSION_KEY,
    enable_admin_security=ENABLE_ADMIN_SECURITY,
    deployment_mode=DEPLOYMENT_MODE,
    csrf_token=csrf_token,
    has_admin_access=lambda: _has_admin_access(),
    is_logged_in=lambda: _is_logged_in(),
    get_current_user=lambda: _get_current_user(),
    has_project_access=lambda project_id: _has_project_access(project_id),
    has_project_admin_access=lambda project_id: _has_project_admin_access(project_id),
    is_valid_admin_token=lambda: _is_valid_admin_token(),
    unauthorized_admin_response=lambda: _unauthorized_admin_response(),
    unauthorized_login_response=lambda: _unauthorized_login_response(),
    has_project_create_access=lambda: _has_project_create_access(),
    csrf_token_from_request=lambda: _csrf_token_from_request(),
    csrf_error_response=lambda message: _csrf_error_response(message),
    is_same_origin_request=lambda: _is_same_origin_request(),
    get_excel_column_letter=get_excel_column_letter,
    format_beijing_time=format_beijing_time,
)
app.config['SECRET_KEY'] = secret_key
db_runtime_settings = apply_database_settings(app.config)
# 兼容静态检查：入口层显式读取一次后端类型，确保启动配置链路可见。
configured_db_backend = get_database_backend_from_config(app.config)
app.secret_key = secret_key
log_print(
    f"ℹ️ 数据库后端: {db_runtime_settings['backend']} | URI: {db_runtime_settings['display_uri']}",
    'DB',
    force=True
)
if configured_db_backend != db_runtime_settings.get("backend"):
    log_print(
        f"⚠️ 数据库后端判定不一致: settings={db_runtime_settings.get('backend')}, parsed={configured_db_backend}",
        'DB',
        force=True,
    )
db.init_app(app)
log_print("[TRACE] db.init_app(app) done", "APP")

# ── 初始化 Auth 账号系统 ──
initialize_auth_subsystem(app=app, db=db, log_print=log_print)

app.register_blueprint(cache_management_bp)
log_print("[TRACE] cache_management_bp registered", "APP")
try:
    app.register_blueprint(commit_diff_bp)
    log_print("[TRACE] commit_diff_bp registered", "APP")
except Exception as e:
    log_print(f"[TRACE] commit_diff_bp FAILED: {e}", "APP", force=True)
    import traceback; traceback.print_exc()
try:
    app.register_blueprint(core_management_bp)
    log_print("[TRACE] core_management_bp registered", "APP")
except Exception as e:
    log_print(f"[TRACE] core_management_bp FAILED: {e}", "APP", force=True)
    import traceback; traceback.print_exc()
try:
    app.register_blueprint(weekly_version_bp)
    log_print("[TRACE] weekly_version_bp registered", "APP")
except Exception as e:
    log_print(f"[TRACE] weekly_version_bp FAILED: {e}", "APP", force=True)
    import traceback; traceback.print_exc()
try:
    app.register_blueprint(agent_management_bp)
    log_print("[TRACE] agent_management_bp registered", "APP")
except Exception as e:
    log_print(f"[TRACE] agent_management_bp FAILED: {e}", "APP", force=True)
    import traceback; traceback.print_exc()

configure_app_routing_bootstrap(app=app, log_print=log_print)

# 周版本相关路由已迁移至 routes/weekly_version_management_routes.py，
# 此处保留处理函数供蓝图包装层复用，避免大范围业务回归。


# 全局变量存储Git进程
active_git_processes = set()
# 将 active_git_processes 注入到 VCS 内容服务模块
configure_vcs_service(active_git_processes)
# Excel diff 状态统一走数据库缓存与任务队列，不再使用进程内字典状态。

# 数据库模型统一定义在 models/ 包中，通过 from models import ... 导入

# Excel差异缓存服务已拆分到 services/excel_diff_cache_service.py
# 初始化服务实例
configure_excel_diff_cache_service(
    app_instance=app,
    db_instance=db,
    diff_logic_version=DIFF_LOGIC_VERSION,
    diff_cache_model=DiffCache,
    operation_log_model=OperationLog,
    commit_model=Commit,
    repository_model=Repository,
    log_print_func=log_print,
    unified_diff_func=get_unified_diff_data,
)
excel_cache_service = ExcelDiffCacheService()
excel_html_cache_service = ExcelHtmlCacheService(db, DIFF_LOGIC_VERSION)
performance_metrics_service = get_perf_metrics_service()
# 初始化周版本Excel缓存服务
from services.weekly_excel_cache_service import WeeklyExcelCacheService

weekly_excel_cache_service = WeeklyExcelCacheService(db, DIFF_LOGIC_VERSION)
log_print("[TRACE] services initialized", "APP")

# ---------------------------------------------------------------------------
# 后台任务工作服务（已拆分到 services/task_worker_service.py）
# ---------------------------------------------------------------------------
from services.task_worker_service import (
    configure_task_worker, register_cleanup,
    TaskWrapper, background_task_queue,
    start_background_task_worker, stop_background_task_worker,
    add_excel_diff_task, add_excel_diff_tasks_batch,
    create_auto_sync_task, create_weekly_sync_task,
    dispatch_auto_sync_task_when_agent_mode,
    cleanup_git_processes, queue_missing_git_branch_refresh,
    regenerate_repository_cache,
    setup_schedule, start_scheduler, stop_scheduler,
)

# 保留原项目详情页面作为备用
# 周版本相关路由已迁移至 routes/weekly_version_management_routes.py，
# 此处保留处理函数供蓝图包装层复用，避免大范围业务回归。

# ---------------------------------------------------------------------------
#  周版本业务逻辑 — 已拆分至 services/weekly_version_logic.py
# ---------------------------------------------------------------------------
from services.weekly_version_logic import (
    configure_weekly_version_logic,
    weekly_version_config,
    weekly_version_config_api,
    weekly_version_config_detail_api,
    weekly_version_list,
    merged_project_view,
    weekly_version_diff,
    weekly_version_config_info_api,
    weekly_version_files_api,
    weekly_version_file_diff_api,
    weekly_version_file_full_diff,
    weekly_version_file_full_diff_data,
    generate_weekly_git_diff_html,
    generate_weekly_excel_merged_diff_html,
    get_status_text,
    get_status_badge_class,
    process_weekly_version_sync,
    generate_weekly_merged_diff,
    process_weekly_excel_cache,
    create_weekly_excel_cache_task,
    get_real_base_commit_from_vcs,
    _merge_segmented_excel_diff_payload,
    _extract_excel_diff_from_payload,
    _load_weekly_excel_diff_from_cache,
)

def commit_list(repository_id):
    log_print(f"=== 访问提交列表页面 ===", 'APP')
    log_print(f"Repository ID: {repository_id}", 'APP')
    repository = Repository.query.get_or_404(repository_id)
    project = repository.project
    if not _has_project_access(project.id):
        abort(403)
    # 获取同一项目下的所有仓库，按名称分组
    all_repositories = project.repositories
    repository_groups = {}
    # 页面渲染阶段只使用本地缓存分支；缺失分支异步刷新，避免阻塞请求
    missing_git_branch_repo_ids = []
    for repo in all_repositories:
        if not repo.branch and repo.type == 'git':
            missing_git_branch_repo_ids.append(repo.id)
        # 移除 _git 和 _svn 后缀来分组
        base_name = repo.name
        if base_name.endswith('_git') or base_name.endswith('_svn'):
            base_name = base_name.rsplit('_', 1)[0]
        if base_name not in repository_groups:
            repository_groups[base_name] = {
                'name': base_name,
                'repositories': [],
                'earliest_repo': repo
            }
        repository_groups[base_name]['repositories'].append(repo)
        # 找到最早创建的仓库作为代表
        if repo.id < repository_groups[base_name]['earliest_repo'].id:
            repository_groups[base_name]['earliest_repo'] = repo
    if missing_git_branch_repo_ids:
        queued = queue_missing_git_branch_refresh(project.id, missing_git_branch_repo_ids)
        if queued:
            log_print(
                f"检测到 {len(missing_git_branch_repo_ids)} 个缺失分支的Git仓库，已异步刷新",
                'APP',
            )
    # 转换为列表格式，用于模板显示
    grouped_repositories = []
    for group_name, group_data in repository_groups.items():
        grouped_repositories.append({
            'name': group_name,
            'repositories': group_data['repositories'],
            'current_repo': repository if repository in group_data['repositories'] else group_data['earliest_repo']
        })
    repositories = all_repositories  # 保持向后兼容
    raw_status_params = [s for s in request.args.getlist('status') if s]
    normalized_status_list = []
    for raw_status in raw_status_params:
        for status_item in re.split(r"[,，]", str(raw_status)):
            normalized = status_item.strip()
            if normalized and normalized not in normalized_status_list:
                normalized_status_list.append(normalized)

    if not normalized_status_list:
        fallback_status_param = request.args.get('status', '')
        if fallback_status_param:
            for status_item in re.split(r"[,，]", str(fallback_status_param)):
                normalized = status_item.strip()
                if normalized and normalized not in normalized_status_list:
                    normalized_status_list.append(normalized)

    # 获取筛选参数
    filters = {
        'author': request.args.get('author', ''),
        'path': request.args.get('path', ''),
        'version': request.args.get('version', ''),
        'operation': request.args.get('operation', ''),
        'status': ','.join(normalized_status_list) if normalized_status_list else request.args.get('status', ''),
        'status_list': normalized_status_list,
        'start_date': request.args.get('start_date', ''),
        'end_date': request.args.get('end_date', ''),
    }
    # 获取分页参数
    page = max(1, request.args.get('page', 1, type=int) or 1)
    requested_per_page = request.args.get('per_page', 50, type=int) or 50
    per_page = min(max(requested_per_page, 1), 200)  # 限制每页大小，避免大页拖垮查询

    def _parse_confirm_usernames(raw_value):
        if not raw_value:
            return []

        usernames = [
            item.strip() for item in re.split(r"[,，;；|\n\r]+", str(raw_value)) if item and item.strip()
        ]
        unique_usernames = []
        for username in usernames:
            if username not in unique_usernames:
                unique_usernames.append(username)
        return unique_usernames

    def _extract_author_lookup_keys(raw_author):
        """提取可用于匹配账号系统的作者标识（用户名 / 邮箱前缀）。"""
        text = str(raw_author or '').strip()
        if not text:
            return []

        keys = []
        lower_text = text.lower()

        # 场景1: commit.author 直接是用户名
        if all(symbol not in lower_text for symbol in ('@', '<', '>', ' ')):
            keys.append(lower_text)

        # 场景2: commit.author 直接是邮箱
        if '@' in lower_text and '<' not in lower_text and '>' not in lower_text:
            email_prefix = lower_text.split('@', 1)[0].strip()
            if email_prefix and email_prefix not in keys:
                keys.append(email_prefix)

        # 场景3: commit.author 包含邮箱，如 "name <xxx@yy.com>"
        for email in re.findall(r'([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})', text):
            email_prefix = email.lower().split('@', 1)[0].strip()
            if email_prefix and email_prefix not in keys:
                keys.append(email_prefix)

        return keys

    def _get_auth_user_model():
        try:
            from auth import get_auth_backend

            if get_auth_backend() == "qkit":
                from qkit_auth.models import QkitAuthUser as _UserModel
            else:
                from auth.models import AuthUser as _UserModel
            return _UserModel
        except Exception as model_error:
            log_print(f"加载账号模型失败，回退原始作者显示: {model_error}", 'APP')
            return None

    _UserModel = _get_auth_user_model()
    # 构建查询
    query = Commit.query.filter_by(repository_id=repository_id)
    # 应用仓库配置的起始日期过滤
    if repository.start_date:
        query = query.filter(Commit.commit_time >= repository.start_date)
        log_print(f"应用仓库起始日期过滤: {repository.start_date}", 'APP')
    # 检查仓库克隆状态和数据准备情况
    repository_status = {
        'clone_status': repository.clone_status,
        'clone_error': repository.clone_error,
        'is_data_ready': False,
        'status_message': ''
    }
    # 先检查基础查询结果
    base_count = query.count()
    log_print(f"基础查询结果数量: {base_count}", 'APP')
    # 判断数据是否准备好
    if repository.clone_status == 'cloning':
        repository_status['status_message'] = '仓库正在克隆中，请稍后刷新页面查看数据...'
    elif repository.clone_status == 'failed':
        repository_status['status_message'] = f'仓库克隆失败：{repository.clone_error or "未知错误"}'
    elif repository.clone_status == 'completed' and base_count == 0:
        repository_status['status_message'] = '仓库克隆完成，正在分析提交数据，请稍后刷新页面或点击"手动获取数据"按钮...'
    elif base_count > 0:
        repository_status['is_data_ready'] = True
    if filters['author']:
        from sqlalchemy import or_

        author_keyword = str(filters['author']).strip()
        author_keyword_lower = author_keyword.lower()
        author_conditions = [func.lower(Commit.author).like(f"%{author_keyword_lower}%")]

        # 允许按姓名筛选：如果输入命中 display_name / username / email，则回查到用户名或邮箱前缀再匹配 commit.author
        if _UserModel is not None and author_keyword:
            matched_author_tokens = set()
            try:
                user_like = f"%{author_keyword}%"
                matched_users = _UserModel.query.filter(
                    or_(
                        _UserModel.username.ilike(user_like),
                        _UserModel.display_name.ilike(user_like),
                        _UserModel.email.ilike(user_like),
                    )
                ).all()

                for user in matched_users:
                    username = (getattr(user, 'username', '') or '').strip().lower()
                    if username:
                        matched_author_tokens.add(username)
                    email = (getattr(user, 'email', '') or '').strip().lower()
                    if email and '@' in email:
                        matched_author_tokens.add(email.split('@', 1)[0])

                for token in matched_author_tokens:
                    author_conditions.append(func.lower(Commit.author).like(f"%{token}%"))
            except Exception as filter_error:
                log_print(f"按姓名筛选作者失败，回退原始筛选: {filter_error}", 'APP')

        query = query.filter(or_(*author_conditions))
    if filters['path']:
        query = query.filter(Commit.path.contains(filters['path']))
    if filters['version']:
        query = query.filter(Commit.version.contains(filters['version']))
    if filters['operation']:
        query = query.filter_by(operation=filters['operation'])
    # 处理状态筛选
    if filters['status_list']:
        query = query.filter(Commit.status.in_(filters['status_list']))
    elif filters['status']:
        query = query.filter_by(status=filters['status'])
    # 分页查询
    pagination = query.order_by(Commit.commit_time.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    commits = pagination.items

    # 批量查询确认用户姓名 + 提交作者姓名（display_name）
    all_confirm_usernames = set()
    all_author_keys = set()
    for commit in commits:
        all_confirm_usernames.update(_parse_confirm_usernames(commit.status_changed_by))
        all_author_keys.update(_extract_author_lookup_keys(commit.author))

    username_to_display_name = {}
    username_to_display_name_lower = {}
    email_prefix_to_display_name = {}
    if _UserModel is not None and (all_confirm_usernames or all_author_keys):
        try:
            from sqlalchemy import or_

            username_conditions = []
            if all_confirm_usernames:
                username_conditions.append(func.lower(_UserModel.username).in_([u.lower() for u in all_confirm_usernames]))
            if all_author_keys:
                username_conditions.append(func.lower(_UserModel.username).in_(list(all_author_keys)))
                username_conditions.extend(
                    func.lower(_UserModel.email).like(f"{author_key}@%")
                    for author_key in all_author_keys
                    if author_key
                )

            users = _UserModel.query.filter(or_(*username_conditions)).all() if username_conditions else []
            for user in users:
                username = (getattr(user, 'username', '') or '').strip()
                if not username:
                    continue
                display_name = (getattr(user, 'display_name', '') or '').strip() or username
                username_to_display_name[username] = display_name
                username_to_display_name_lower[username.lower()] = display_name

                email = (getattr(user, 'email', '') or '').strip().lower()
                if email and '@' in email:
                    email_prefix_to_display_name[email.split('@', 1)[0]] = display_name
        except Exception as e:
            log_print(f"加载作者/确认用户姓名映射失败，回退为原始显示: {e}", 'APP')

    def _resolve_author_display(raw_author):
        text = str(raw_author or '').strip()
        if not text:
            return ''

        for author_key in _extract_author_lookup_keys(text):
            mapped_name = (
                username_to_display_name_lower.get(author_key)
                or email_prefix_to_display_name.get(author_key)
            )
            if mapped_name:
                return mapped_name
        return text

    for commit in commits:
        commit_confirm_users = _parse_confirm_usernames(commit.status_changed_by)
        commit_confirm_display_names = [
            username_to_display_name.get(username)
            or username_to_display_name_lower.get(username.lower(), username)
            for username in commit_confirm_users
        ]

        confirm_users_display = ''
        confirm_users_title = ''
        if commit.status in ('confirmed', 'rejected') and commit_confirm_users:
            confirm_users_display = ', '.join(commit_confirm_display_names)
            # title 保留用户名，便于定位账号
            confirm_users_title = ', '.join(commit_confirm_users)

        commit.confirm_users_display = confirm_users_display
        commit.confirm_users_title = confirm_users_title
        commit.author_display = _resolve_author_display(commit.author)

    # 兜底补齐作者显示名：同时尝试 local/qkit 账号表，避免后端切换导致映射缺失。
    try:
        _attach_author_display(commits)
    except Exception as author_map_error:
        log_print(f"补齐提交列表作者映射失败，继续使用原始作者显示: {author_map_error}", 'APP')

    # 调试信息
    log_print(f"=== 分页调试信息 ===", 'APP')
    log_print(f"Repository ID: {repository_id}", 'APP')
    log_print(f"Page: {page}, Per page: {per_page}", 'APP')
    log_print(f"Pagination total: {pagination.total}", 'APP')
    log_print(f"Pagination pages: {pagination.pages}", 'APP')
    log_print(f"Current page items: {len(commits)}", 'APP')
    log_print(f"Filters: {filters}", 'APP')
    log_print(f"=====================", 'APP')
    return render_template('commit_list.html',
                         commits=commits,
                         pagination=pagination,
                         repository=repository,
                         project=project,
                         repositories=repositories,
                         grouped_repositories=grouped_repositories,
                         filters=filters,
                         repository_status=repository_status)
# diff确认页面
# 新的带项目代号和仓库名的Excel diff数据路由


def _ensure_repository_access_or_403(repository):
    project = repository.project if repository else None
    if project is None:
        abort(404)
    project_id = getattr(project, "id", None)
    if project_id is not None and not _has_project_access(project_id):
        abort(403)
    return project


def _ensure_commit_access_or_403(commit):
    repository = commit.repository if commit else None
    project = _ensure_repository_access_or_403(repository)
    return repository, project


def _ensure_commit_route_scope_or_404(commit, project_code=None, repository_name=None):
    repository, project = _ensure_commit_access_or_403(commit)
    expected_project_code = str(project_code or "").strip()
    expected_repo_name = str(repository_name or "").strip()
    if expected_project_code and str(project.code or "").strip() != expected_project_code:
        abort(404)
    if expected_repo_name and str(repository.name or "").strip() != expected_repo_name:
        abort(404)
    return repository, project


def _resolve_previous_commit_db_only(commit):
    # 与 single 模式保持同一套“上一版本”解析逻辑，避免双轨行为不一致。
    return resolve_previous_commit(commit)


def get_excel_diff_data_with_path(project_code, repository_name, commit_id):
    commit = Commit.query.get_or_404(commit_id)
    _ensure_commit_route_scope_or_404(
        commit,
        project_code=project_code,
        repository_name=repository_name,
    )
    return get_excel_diff_data(commit_id)

# 保持向后兼容的原路由


def get_excel_diff_data(commit_id):
    """异步获取Excel diff数据的API端点（支持HTML缓存优先）"""
    request_start = time.time()
    commit = Commit.query.get_or_404(commit_id)
    repository, project = _ensure_commit_access_or_403(commit)
    perf_project_tags = {
        "project_id": getattr(project, "id", "") if project else "",
        "project_code": getattr(project, "code", "") if project else "",
    }
    # 检查是否为Excel文件
    is_excel = excel_cache_service.is_excel_file(commit.path)
    if not is_excel:
        return jsonify({'error': True, 'message': '不是Excel文件'})

    force_retry = str(request.args.get("force_retry") or "").strip().lower() in {"1", "true", "yes"}
    dispatch_result = maybe_dispatch_commit_diff(commit, force_retry=force_retry)
    if dispatch_result is not None:
        status = str(dispatch_result.get("status") or "")
        if status == "ready":
            payload = dispatch_result.get("payload") or {}
            diff_data = payload.get("diff_data")
            if not isinstance(diff_data, dict):
                return jsonify({
                    "success": False,
                    "status": "error",
                    "message": "Agent diff 返回格式异常",
                }), 500
            try:
                html_content, css_content, js_content = excel_html_cache_service.generate_excel_html(diff_data)
            except Exception as render_exc:
                return jsonify({
                    "success": True,
                    "from_agent": True,
                    "from_data_cache": False,
                    "html_render_failed": True,
                    "message": f"Excel HTML 渲染失败: {render_exc}",
                    "diff_data": diff_data,
                })

            metadata = {
                "file_path": commit.path,
                "commit_id": commit.commit_id,
                "repository_name": repository.name,
                "source": "agent_commit_diff",
            }
            return jsonify({
                "success": True,
                "from_agent": True,
                "from_html_cache": False,
                "from_data_cache": False,
                "html_content": html_content,
                "css_content": css_content,
                "js_content": js_content,
                "metadata": metadata,
            })

        if status == "unbound":
            return jsonify({
                "success": False,
                "status": "unbound",
                "message": dispatch_result.get("message") or "项目未绑定 Agent",
            }), 409

        if status in {"pending", "pending_offline"}:
            return jsonify({
                "success": True,
                "pending": True,
                "status": status,
                "message": dispatch_result.get("message") or "Agent 正在处理diff",
                "retry_after_seconds": dispatch_result.get("retry_after_seconds") or 60,
                "task_id": dispatch_result.get("task_id"),
            }), 202

        return jsonify({
            "success": False,
            "status": "error",
            "message": dispatch_result.get("message") or "Agent diff 获取失败",
        }), 500

    try:
        # 首先检查HTML缓存（优先级最高）
        html_lookup_start = time.time()
        cached_html = excel_html_cache_service.get_cached_html(
            repository.id, commit.commit_id, commit.path
        )
        if cached_html:
            html_lookup_time = time.time() - html_lookup_start
            total_time = time.time() - request_start
            log_print(f"✅ 从HTML缓存获取Excel差异: {commit.path}", 'EXCEL')
            log_print(
                f"📊 Excel接口耗时: html_lookup={html_lookup_time:.2f}s, total={total_time:.2f}s | "
                f"html_bytes={len(cached_html.get('html_content') or '')}",
                'EXCEL'
            )
            performance_metrics_service.record(
                "api_excel_diff",
                success=True,
                metrics={
                    "total_ms": total_time * 1000,
                    "html_lookup_ms": html_lookup_time * 1000,
                    "html_bytes": len(cached_html.get("html_content") or ""),
                },
                tags={
                    "source": "html_cache",
                    "repository_id": repository.id,
                    "project_id": perf_project_tags["project_id"],
                    "project_code": perf_project_tags["project_code"],
                    "file_path": commit.path,
                },
            )
            created_at_value = cached_html.get('created_at')
            if created_at_value and hasattr(created_at_value, 'isoformat'):
                created_at_iso = created_at_value.isoformat()
            elif created_at_value:
                created_at_iso = str(created_at_value)
            else:
                created_at_iso = None
            return jsonify({
                'success': True, 
                'html_content': cached_html['html_content'],
                'css_content': cached_html['css_content'],
                'js_content': cached_html['js_content'],
                'metadata': cached_html['metadata'],
                'from_html_cache': True,
                'created_at': created_at_iso
            })
        # HTML缓存未命中，检查原始数据缓存
        data_lookup_start = time.time()
        cached_diff = excel_cache_service.get_cached_diff(
            repository.id, commit.commit_id, commit.path
        )
        data_lookup_time = time.time() - data_lookup_start
        if cached_diff:
            log_print(f"📊 从数据缓存获取Excel差异，生成HTML: {commit.path}", 'EXCEL')
            try:
                # 解析缓存的diff数据
                import json
                render_start = time.time()
                diff_data = json.loads(cached_diff.diff_data)
                # 生成HTML缓存
                html_content, css_content, js_content = excel_html_cache_service.generate_excel_html(diff_data)
                render_time = time.time() - render_start
                # 保存HTML缓存
                metadata = {
                    'file_path': commit.path,
                    'commit_id': commit.commit_id,
                    'repository_name': repository.name,
                    'processing_time': cached_diff.processing_time
                }
                excel_html_cache_service.save_html_cache(
                    repository.id, commit.commit_id, commit.path,
                    html_content, css_content, js_content, metadata
                )
                total_time = time.time() - request_start
                log_print(
                    f"📊 Excel接口耗时: data_lookup={data_lookup_time:.2f}s, render={render_time:.2f}s, total={total_time:.2f}s | "
                    f"diff_bytes={len(cached_diff.diff_data.encode('utf-8')) / 1024:.1f}KB",
                    'EXCEL'
                )
                performance_metrics_service.record(
                    "api_excel_diff",
                    success=True,
                    metrics={
                        "total_ms": total_time * 1000,
                        "data_lookup_ms": data_lookup_time * 1000,
                        "render_ms": render_time * 1000,
                        "diff_bytes": len(cached_diff.diff_data.encode("utf-8")),
                    },
                    tags={
                        "source": "data_cache",
                        "repository_id": repository.id,
                        "project_id": perf_project_tags["project_id"],
                        "project_code": perf_project_tags["project_code"],
                        "file_path": commit.path,
                    },
                )
                return jsonify({
                    'success': True, 
                    'html_content': html_content,
                    'css_content': css_content,
                    'js_content': js_content,
                    'metadata': metadata,
                    'from_html_cache': False,
                    'from_data_cache': True
                })
            except Exception as e:
                log_print(f"⚠️ HTML生成失败，返回原始数据: {e}", 'INFO')
                performance_metrics_service.record(
                    "api_excel_diff",
                    success=False,
                    metrics={
                        "total_ms": (time.time() - request_start) * 1000,
                        "data_lookup_ms": data_lookup_time * 1000,
                    },
                    tags={
                        "source": "data_cache_html_render_failed",
                        "repository_id": repository.id,
                        "project_id": perf_project_tags["project_id"],
                        "project_code": perf_project_tags["project_code"],
                        "file_path": commit.path,
                    },
                )
                return jsonify({'success': True, 'diff_data': json.loads(cached_diff.diff_data), 'from_cache': True})

        # 所有缓存都未命中，实时处理
        log_print(f"🔄 缓存未命中，开始实时处理Excel文件: {commit.path}", 'INFO')
        # 获取前一个提交
        previous_lookup_start = time.time()
        previous_commit = None
        from sqlalchemy import and_, or_
        file_commits = Commit.query.filter(
            Commit.repository_id == repository.id,
            Commit.path == commit.path,
            or_(
                Commit.commit_time < commit.commit_time,
                and_(Commit.commit_time == commit.commit_time, Commit.id < commit.id),
            )
        ).order_by(Commit.commit_time.desc(), Commit.id.desc()).first()
        if not file_commits:
            file_commits = Commit.query.filter(
                Commit.repository_id == repository.id,
                Commit.path == commit.path,
                Commit.id < commit.id
            ).order_by(Commit.id.desc()).first()
        previous_lookup_time = time.time() - previous_lookup_start
        # 使用统一差异服务处理
        diff_start = time.time()
        diff_data = get_unified_diff_data(commit, file_commits)
        diff_time = time.time() - diff_start
        if diff_data and diff_data.get('type') == 'excel':
            try:
                # 生成HTML内容
                render_start = time.time()
                html_content, css_content, js_content = excel_html_cache_service.generate_excel_html(diff_data)
                render_time = time.time() - render_start
                # 保存HTML缓存
                metadata = {
                    'file_path': commit.path,
                    'commit_id': commit.commit_id,
                    'repository_name': repository.name,
                    'real_time_processing': True
                }
                excel_html_cache_service.save_html_cache(
                    repository.id, commit.commit_id, commit.path,
                    html_content, css_content, js_content, metadata
                )
                # 异步缓存原始数据，用户主动请求使用高优先级
                add_excel_diff_task(repository.id, commit.commit_id, commit.path, priority=1)
                total_time = time.time() - request_start
                log_print(f"✅ Excel差异实时处理完成，HTML缓存已保存: {commit.path}", 'EXCEL')
                log_print(
                    f"📊 Excel接口耗时: data_lookup={data_lookup_time:.2f}s, prev_lookup={previous_lookup_time:.2f}s, "
                    f"diff={diff_time:.2f}s, render={render_time:.2f}s, total={total_time:.2f}s",
                    'EXCEL'
                )
                performance_metrics_service.record(
                    "api_excel_diff",
                    success=True,
                    metrics={
                        "total_ms": total_time * 1000,
                        "data_lookup_ms": data_lookup_time * 1000,
                        "prev_lookup_ms": previous_lookup_time * 1000,
                        "diff_ms": diff_time * 1000,
                        "render_ms": render_time * 1000,
                    },
                    tags={
                        "source": "realtime",
                        "repository_id": repository.id,
                        "project_id": perf_project_tags["project_id"],
                        "project_code": perf_project_tags["project_code"],
                        "file_path": commit.path,
                    },
                )
                return jsonify({
                    'success': True, 
                    'html_content': html_content,
                    'css_content': css_content,
                    'js_content': js_content,
                    'metadata': metadata,
                    'from_html_cache': False,
                    'real_time': True
                })
            except Exception as e:
                log_print(f"⚠️ HTML生成失败，返回原始数据: {e}", 'INFO')
                # 如果HTML生成失败，仍然返回原始数据，用户主动请求使用高优先级
                add_excel_diff_task(repository.id, commit.commit_id, commit.path, priority=1)
                performance_metrics_service.record(
                    "api_excel_diff",
                    success=False,
                    metrics={
                        "total_ms": (time.time() - request_start) * 1000,
                        "data_lookup_ms": data_lookup_time * 1000,
                        "prev_lookup_ms": previous_lookup_time * 1000,
                        "diff_ms": diff_time * 1000,
                    },
                    tags={
                        "source": "realtime_html_render_failed",
                        "repository_id": repository.id,
                        "project_id": perf_project_tags["project_id"],
                        "project_code": perf_project_tags["project_code"],
                        "file_path": commit.path,
                    },
                )
                return jsonify({'success': True, 'diff_data': diff_data, 'from_cache': False})

        else:
            error_msg = diff_data.get('error', '处理失败') if diff_data else 'Excel文件处理返回空结果'
            performance_metrics_service.record(
                "api_excel_diff",
                success=False,
                metrics={
                    "total_ms": (time.time() - request_start) * 1000,
                    "data_lookup_ms": data_lookup_time * 1000,
                    "prev_lookup_ms": previous_lookup_time * 1000,
                    "diff_ms": diff_time * 1000,
                },
                tags={
                    "source": "realtime_diff_failed",
                    "repository_id": repository.id,
                    "project_id": perf_project_tags["project_id"],
                    "project_code": perf_project_tags["project_code"],
                    "file_path": commit.path,
                },
            )
            return jsonify({'error': True, 'message': error_msg})

    except Exception as e:
        log_print(f"❌ Excel diff处理失败: {str(e)}")
        import traceback
        traceback.print_exc()
        performance_metrics_service.record(
            "api_excel_diff",
            success=False,
            metrics={
                "total_ms": (time.time() - request_start) * 1000,
            },
            tags={
                "source": "exception",
                "repository_id": repository.id if repository else "",
                "project_id": perf_project_tags["project_id"],
                "project_code": perf_project_tags["project_code"],
                "file_path": commit.path if commit else "",
            },
        )
        return jsonify({'error': True, 'message': f'Excel文件处理失败: {str(e)}'})

# 新的统一差异显示路由
# 新的带项目代号和仓库名的新diff路由


def commit_diff_new_with_path(project_code, repository_name, commit_id):
    commit = Commit.query.get_or_404(commit_id)
    _ensure_commit_route_scope_or_404(
        commit,
        project_code=project_code,
        repository_name=repository_name,
    )
    return commit_diff_new(commit_id)

# 保持向后兼容的原路由


def commit_diff_new(commit_id):
    """使用新的差异服务显示文件差异"""
    commit = Commit.query.get_or_404(commit_id)
    repository, project = _ensure_commit_access_or_403(commit)
    # 获取该文件的所有提交历史
    file_commits = Commit.query.filter(
        Commit.repository_id == repository.id,
        Commit.path == commit.path
    ).order_by(Commit.commit_time.desc()).all()
    previous_commit = resolve_previous_commit(commit, file_commits=file_commits)
    try:
        commits_for_author_mapping = [commit]
        if previous_commit:
            commits_for_author_mapping.append(previous_commit)
        _attach_author_display(commits_for_author_mapping)
    except Exception as author_map_error:
        log_print(f"commit_diff_new 作者姓名映射失败，回退原始作者: {author_map_error}", 'DIFF')
    # 使用新的差异服务处理文件
    diff_data = get_unified_diff_data(commit, previous_commit)
    return render_template('commit_diff_new.html', 
                         commit=commit, 
                         repository=repository,
                         project=project,
                         diff_data=diff_data,
                         file_commits=file_commits,
                         previous_commit=previous_commit)
# 完整文件diff路由


def commit_full_diff(commit_id):
    """显示完整文件的diff，类似Git工具的并排显示"""
    return handle_commit_full_diff(
        commit_id=commit_id,
        Commit=Commit,
        get_svn_service=get_svn_service,
        threaded_git_service_cls=ThreadedGitService,
        get_commit_diff_mode_strategy=get_commit_diff_mode_strategy,
        ensure_commit_access_or_403=_ensure_commit_access_or_403,
        resolve_previous_commit=resolve_previous_commit,
        generate_side_by_side_diff=generate_side_by_side_diff,
        render_template=render_template,
        log_print=log_print,
    )
# 重新计算差异API
# 新的带项目代号和仓库名的刷新diff路由


def refresh_commit_diff_with_path(project_code, repository_name, commit_id):
    commit = Commit.query.get_or_404(commit_id)
    _ensure_commit_route_scope_or_404(
        commit,
        project_code=project_code,
        repository_name=repository_name,
    )
    return refresh_commit_diff(commit_id)

# 保持向后兼容的原路由


def refresh_commit_diff(commit_id):
    """重新计算提交的差异数据，绕过缓存 - 优化版本"""
    return handle_refresh_commit_diff(
        commit_id=commit_id,
        Commit=Commit,
        DiffCache=DiffCache,
        ExcelHtmlCache=ExcelHtmlCache,
        db=db,
        SQLAlchemyError=SQLAlchemyError,
        excel_cache_service=excel_cache_service,
        maybe_dispatch_commit_diff=maybe_dispatch_commit_diff,
        get_unified_diff_data=get_unified_diff_data,
        safe_json_serialize=safe_json_serialize,
        ensure_commit_access_or_403=_ensure_commit_access_or_403,
        jsonify=jsonify,
        log_print=log_print,
    )
# 新的带项目代号和仓库名的路由


def commit_diff_with_path(project_code, repository_name, commit_id):
    commit = Commit.query.get_or_404(commit_id)
    _ensure_commit_route_scope_or_404(
        commit,
        project_code=project_code,
        repository_name=repository_name,
    )
    return commit_diff(commit_id)

# 保持向后兼容的原路由


def commit_diff(commit_id):
    diff_request_start = time.time()
    commit = Commit.query.get_or_404(commit_id)
    repository, project = _ensure_commit_access_or_403(commit)
    mode_strategy = get_commit_diff_mode_strategy()
    # 检查是否为删除操作
    is_deleted = commit.operation == 'D'
    # 检查是否为Excel文件
    is_excel = excel_cache_service.is_excel_file(commit.path)
    # 获取该文件的所有提交历史 - 使用更严格的排序
    file_commits = Commit.query.filter(
        Commit.repository_id == repository.id,
        Commit.path == commit.path
    ).order_by(Commit.commit_time.desc(), Commit.id.desc()).all()
    previous_commit = resolve_previous_commit(commit, file_commits=file_commits)
    try:
        commits_for_author_mapping = [commit]
        if previous_commit:
            commits_for_author_mapping.append(previous_commit)
        _attach_author_display(commits_for_author_mapping)
    except Exception as author_map_error:
        log_print(f"commit_diff 作者姓名映射失败，回退原始作者: {author_map_error}", 'DIFF')
    # 调试日志
    log_print(f"🔍 查找前一提交 - 文件: {commit.path}", 'DIFF', force=True)
    log_print(f"🔍 该文件总提交数: {len(file_commits)}", 'DIFF', force=True)
    if previous_commit:
        log_print(f"✅ 找到前一提交: ID:{previous_commit.id} {previous_commit.commit_id[:8]} {previous_commit.commit_time}", 'DIFF', force=True)
    else:
        log_print(f"❌ 未找到前一提交 - 这是初始提交", 'DIFF', force=True)
    # 如果是删除操作，返回删除信息页面
    if is_deleted:
        return render_template(
            "commit_diff.html",
            **build_commit_diff_template_context(
                commit=commit,
                repository=repository,
                project=project,
                file_commits=file_commits,
                previous_commit=previous_commit,
                is_excel=is_excel,
                diff_data={"type": "deleted", "message": "该文件已被删除"},
                is_deleted=True,
                mode_strategy=mode_strategy,
            ),
        )
    if mode_strategy.async_agent_diff:
        return render_template(
            "commit_diff.html",
            **build_commit_diff_template_context(
                commit=commit,
                repository=repository,
                project=project,
                file_commits=file_commits,
                previous_commit=previous_commit,
                is_excel=is_excel,
                diff_data=None,
                is_deleted=False,
                mode_strategy=mode_strategy,
            ),
        )
    if is_excel:
        # Excel文件处理 - 优先使用缓存
        try:
            log_print(f"处理Excel文件差异: {commit.path}", 'EXCEL')
            log_print(f"Commit ID: {commit.commit_id}", 'EXCEL')
            log_print(f"Repository: {repository.name}", 'EXCEL')
            # 首先检查缓存
            cached_diff = excel_cache_service.get_cached_diff(
                repository.id, commit.commit_id, commit.path
            )
            diff_data = None
            cache_is_valid = False
            if cached_diff:
                log_print(f"📦 从缓存获取Excel差异数据: {commit.path}", 'EXCEL')
                log_print(f"🏷️ 缓存版本: {cached_diff.diff_version} | 缓存时间: {cached_diff.created_at}", 'EXCEL')
                try:
                    # 从缓存对象中提取实际的diff数据
                    import json
                    cached_data = json.loads(cached_diff.diff_data)
                    # 验证缓存数据的完整性
                    is_valid, validation_message = validate_excel_diff_data(cached_data)
                    log_print(f"🔍 缓存数据验证: {validation_message}", 'EXCEL')
                    if is_valid:
                        diff_data = cached_data
                        cache_is_valid = True
                        log_print(f"✅ 缓存数据验证通过，使用缓存数据", 'EXCEL')
                    else:
                        log_print(f"❌ 缓存数据验证失败: {validation_message}", 'EXCEL', force=True)
                        log_print(f"🔄 将删除无效缓存并重新生成", 'EXCEL')
                        # 删除无效的缓存记录
                        try:
                            db.session.delete(cached_diff)
                            db.session.commit()
                            log_print(f"🗑️ 已删除无效缓存记录 ID: {cached_diff.id}", 'EXCEL')
                        except SQLAlchemyError as delete_error:
                            log_print(f"❌ 删除缓存记录失败: {delete_error}", 'EXCEL', force=True)
                            db.session.rollback()
                except json.JSONDecodeError as e:
                    log_print(f"❌ 缓存数据JSON解析失败: {e}", 'EXCEL', force=True)
                    cache_is_valid = False
                except Exception as e:
                    log_print(f"❌ 缓存数据处理异常: {e}", 'EXCEL', force=True)
                    cache_is_valid = False
            # 如果缓存无效或不存在，重新生成
            if not cache_is_valid:
                log_print(f"🔄 缓存未命中或无效，开始实时处理Excel文件: {commit.path}", 'EXCEL')
                diff_data = get_unified_diff_data(commit, previous_commit)
                # 验证新生成的数据
                if diff_data:
                    is_valid, validation_message = validate_excel_diff_data(diff_data)
                    log_print(f"🔍 新生成数据验证: {validation_message}", 'EXCEL')
                    if is_valid:
                        cache_is_valid = True
                    else:
                        log_print(f"❌ 新生成的数据也无效: {validation_message}", 'EXCEL', force=True)
                else:
                    log_print(f"❌ 新数据生成失败", 'EXCEL', force=True)
                # 如果处理成功且验证通过，优化并立即缓存结果
                if diff_data and cache_is_valid:
                    log_print(f"💾 立即缓存Excel差异结果: {commit.path}", 'EXCEL')
                    cache_success = excel_cache_service.save_cached_diff(
                        repository_id=repository.id,
                        commit_id=commit.commit_id,
                        file_path=commit.path,
                        diff_data=diff_data,
                        previous_commit_id=previous_commit.commit_id if previous_commit else None,
                        processing_time=0,  # 这里可以记录实际处理时间
                        file_size=0,  # 这里可以记录文件大小
                        commit_time=commit.commit_time  # 传递提交时间
                    )
                    if cache_success:
                        log_print(f"✅ Excel差异缓存成功: {commit.path}", 'EXCEL')
                    else:
                        log_print(f"❌ Excel差异缓存失败: {commit.path}", 'EXCEL', force=True)
                        # 缓存失败时，添加到后台任务队列重试，用户点击的条目使用高优先级
                        add_excel_diff_task(repository.id, commit.commit_id, commit.path, priority=1)
                        log_print(f"已添加Excel差异缓存任务到后台队列 (高优先级): {commit.path}", 'EXCEL')
                else:
                    log_print(f"❌ 缓存条件不满足，跳过缓存", 'CACHE', force=True)
                # 如果新服务失败，尝试使用旧的Excel处理逻辑作为备用
                # 只有在diff_data为None或空时才使用备用逻辑，不要检查type字段
                if not diff_data:
                    log_print("使用旧的Excel处理逻辑作为备用", 'EXCEL')
                    git_service = ThreadedGitService(repository.url, repository.root_directory, repository.username, repository.token, repository, active_git_processes)
                    diff_data = git_service.parse_excel_diff(commit.commit_id, commit.path)
                    log_print(f"旧Excel处理逻辑返回: {type(diff_data)}", 'EXCEL')
        except Exception as e:
            log_print(f"Excel diff generation failed: {e}", 'EXCEL', force=True)
            import traceback
            traceback.print_exc()
            diff_data = None
        # 清理diff_data中的不可序列化值
        if diff_data:
            diff_data = clean_json_data(diff_data)
        # 调试日志：确认模板变量
        log_print(f"🔍 模板变量调试: is_excel=True, diff_data存在={diff_data is not None}", 'EXCEL', force=True)
        if diff_data:
            log_print(f"🔍 diff_data类型: {type(diff_data)}, 键: {list(diff_data.keys()) if isinstance(diff_data, dict) else 'N/A'}", 'EXCEL', force=True)
        # 构建模板上下文
        template_context = build_commit_diff_template_context(
            commit=commit,
            repository=repository,
            project=project,
            file_commits=file_commits,
            previous_commit=previous_commit,
            is_excel=True,
            diff_data=diff_data,
            is_deleted=False,
            mode_strategy=mode_strategy,
        )
        log_print(f"🔍 模板上下文键: {list(template_context.keys())}", 'EXCEL', force=True)
        log_print(f"🔍 is_excel值: {template_context['is_excel']}, 类型: {type(template_context['is_excel'])}", 'EXCEL', force=True)
        return render_template('commit_diff.html', **template_context)

    else:
        # 非Excel文件，正常同步处理
        diff_data = get_diff_data(commit, previous_commit=previous_commit)
        perf_tags = {
            "source": "realtime_non_excel",
            "repository_id": repository.id,
            "project_id": project.id if project else "",
            "project_code": project.code if project else "",
            "file_path": commit.path,
        }
        perf_success = True
        if isinstance(diff_data, dict) and str(diff_data.get("type") or "").lower() == "error":
            perf_success = False
            perf_tags["source"] = "realtime_diff_failed"
        performance_metrics_service.record(
            "api_commit_diff",
            success=perf_success,
            metrics={
                "total_ms": (time.time() - diff_request_start) * 1000,
            },
            tags=perf_tags,
        )
        return render_template(
            "commit_diff.html",
            **build_commit_diff_template_context(
                commit=commit,
                repository=repository,
                project=project,
                file_commits=file_commits,
                previous_commit=previous_commit,
                is_excel=False,
                diff_data=diff_data,
                is_deleted=False,
                mode_strategy=mode_strategy,
            ),
        )
# 确认/拒绝提交（旧版本，已被新的API替代）
# 重新生成Diff缓存


@require_admin
def regenerate_cache(repository_id):
    """重新生成指定仓库的Excel文件差异缓存"""
    return handle_regenerate_cache(
        repository_id=repository_id,
        Repository=Repository,
        DiffCache=DiffCache,
        ExcelHtmlCache=ExcelHtmlCache,
        db=db,
        excel_cache_service=excel_cache_service,
        add_excel_diff_task=add_excel_diff_task,
        jsonify=jsonify,
        log_print=log_print,
    )
# 获取缓存状态


def get_cache_status(repository_id):
    """获取仓库的缓存状态"""
    return handle_get_cache_status(
        repository_id=repository_id,
        Repository=Repository,
        DiffCache=DiffCache,
        excel_cache_service=excel_cache_service,
        ensure_repository_access_or_403=_ensure_repository_access_or_403,
        jsonify=jsonify,
        log_print=log_print,
    )
# 查询仓库克隆状态 API


def get_clone_status(repository_id):
    """轻量级 API：返回仓库的 clone_status，供前端轮询。"""
    return handle_get_clone_status(
        repository_id=repository_id,
        db=db,
        Repository=Repository,
        Commit=Commit,
        ensure_repository_access_or_403=_ensure_repository_access_or_403,
        jsonify=jsonify,
    )


def _should_retry_with_reclone(repository) -> bool:
    """Infer retry strategy from repository state.

    - Clone phase failure: remove local worktree then re-clone.
    - Update phase failure: repair local worktree then update.
    """
    return should_retry_with_reclone(repository=repository, db=db, Commit=Commit)


# 重试仓库同步（按失败阶段自动分流）
@require_admin
def retry_clone_repository(repository_id):
    return handle_retry_clone_repository(
        repository_id=repository_id,
        Repository=Repository,
        dispatch_auto_sync_task_when_agent_mode=dispatch_auto_sync_task_when_agent_mode,
        create_auto_sync_task=create_auto_sync_task,
        should_retry_with_reclone_func=_should_retry_with_reclone,
        flash=flash,
        redirect=redirect,
        url_for=url_for,
    )

# 同步仓库提交记录


@require_admin
def sync_repository(repository_id):
    """手动获取数据 - 立即执行git pull和分析"""
    return handle_sync_repository(
        repository_id=repository_id,
        db=db,
        Repository=Repository,
        Commit=Commit,
        get_git_service=get_git_service,
        get_svn_service=get_svn_service,
        dispatch_auto_sync_task_when_agent_mode=dispatch_auto_sync_task_when_agent_mode,
        record_repository_sync_error=record_repository_sync_error,
        clear_repository_sync_error=clear_repository_sync_error,
        add_excel_diff_task=add_excel_diff_task,
        excel_cache_service=excel_cache_service,
        jsonify=jsonify,
        log_print=log_print,
    )

def run_repository_update_and_cache(repository_id):
    """异步执行仓库更新和缓存（线程安全：按ID重新查询对象）"""
    # static-check compatibility:
    # with app.app_context():
    #     repository = db.session.get(Repository, repository_id)
    return run_repository_update_and_cache_worker(
        repository_id=repository_id,
        app=app,
        db=db,
        Repository=Repository,
        Commit=Commit,
        get_git_service=get_git_service,
        get_svn_service=get_svn_service,
        dispatch_auto_sync_task_when_agent_mode=dispatch_auto_sync_task_when_agent_mode,
        clear_repository_sync_error=clear_repository_sync_error,
        record_repository_sync_error=record_repository_sync_error,
        log_print=log_print,
    )


def _start_repository_update_thread(repository_id):
    update_thread = threading.Thread(target=run_repository_update_and_cache, args=(repository_id,), daemon=True)
    update_thread.start()


@require_admin
def reuse_repository_and_update(repository_id):
    """复用仓库并触发更新和缓存操作的API接口"""
    return handle_reuse_repository_and_update(
        repository_id=repository_id,
        request=request,
        jsonify=jsonify,
        Repository=Repository,
        db=db,
        NotFound=NotFound,
        SQLAlchemyError=SQLAlchemyError,
        log_print=log_print,
        dispatch_auto_sync_task_when_agent_mode=dispatch_auto_sync_task_when_agent_mode,
        spawn_update_worker=_start_repository_update_thread,
    )
def check_local_repository_exists(project_code, repository_name, repository_id):
    """检查本地仓库是否存在"""
    try:
        local_path = build_repository_local_path(project_code, repository_name, repository_id, strict=False)
    except (TypeError, ValueError):
        return False

    return os.path.exists(local_path)

def update_commit_status(commit_id):
    """更新提交状态"""
    # static-check compatibility:
    # action_to_status = {
    #     'confirm': 'confirmed',
    #     'reject': 'rejected',
    # }
    from services.status_sync_service import StatusSyncService
    from utils.request_security import (
        _get_current_user,
        can_current_user_operate_project_confirmation,
    )

    return handle_update_commit_status(
        commit_id=commit_id,
        request=request,
        jsonify=jsonify,
        db=db,
        Commit=Commit,
        NotFound=NotFound,
        SQLAlchemyError=SQLAlchemyError,
        app_logger=app.logger,
        ensure_commit_access_or_403=_ensure_commit_access_or_403,
        can_operate_project_confirmation=can_current_user_operate_project_confirmation,
        get_current_user=_get_current_user,
        status_sync_service_cls=StatusSyncService,
        log_print=log_print,
    )

@require_admin
def batch_update_commits_compat():
    """兼容历史前端的批量更新接口（batch-approve/batch-reject 的统一入口）"""
    # static-check compatibility:
    # data.get('commit_ids') or data.get('ids') or request.form.getlist('ids')
    # if action in {'confirm', 'confirmed', 'approve'}:
    # elif action in {'reject', 'rejected'}:
    from services.status_sync_service import StatusSyncService
    from utils.request_security import (
        _get_current_user,
        can_current_user_operate_project_confirmation,
    )

    return handle_batch_update_commits_compat(
        request=request,
        jsonify=jsonify,
        db=db,
        Commit=Commit,
        SQLAlchemyError=SQLAlchemyError,
        log_print=log_print,
        status_sync_service_cls=StatusSyncService,
        get_current_user=_get_current_user,
        can_operate_project_confirmation=can_current_user_operate_project_confirmation,
    )

def edit_repository(repository_id):
    repository = Repository.query.get_or_404(repository_id)
    project = repository.project
    if repository.type == 'git':
        return render_template('add_git_repository.html', 
                             project=project, 
                             repository=repository, 
                             is_edit=True)
    else:
        return render_template('add_svn_repository.html', 
                             project=project, 
                             repository=repository, 
                             is_edit=True)


def _clear_repository_state_for_switch(repository, switch_type, old_value, new_value):
    """分支/版本切换后的保守清空流程（不改表结构）。"""
    return clear_repository_state_for_switch(
        repository=repository,
        switch_type=switch_type,
        old_value=old_value,
        new_value=new_value,
        WeeklyVersionConfig=WeeklyVersionConfig,
        Commit=Commit,
        DiffCache=DiffCache,
        ExcelHtmlCache=ExcelHtmlCache,
        MergedDiffCache=MergedDiffCache,
        WeeklyVersionDiffCache=WeeklyVersionDiffCache,
        WeeklyVersionExcelCache=WeeklyVersionExcelCache,
        BackgroundTask=BackgroundTask,
        or_=or_,
        log_print=log_print,
    )
# 更新仓库配置 - 表单提交处理


@require_admin
def update_repository(repository_id):
    """处理仓库编辑表单提交"""
    repository = Repository.query.get_or_404(repository_id)
    # static-check compatibility:
    # def async_refilter():
    #     with app.app_context():
    #         repo = db.session.get(Repository, repository_id)
    return handle_update_repository_form(
        repository=repository,
        request=request,
        redirect=redirect,
        url_for=url_for,
        flash=flash,
        db=db,
        validate_repository_name=validate_repository_name,
        log_print=log_print,
        create_auto_sync_task=create_auto_sync_task,
        app=app,
        Commit=Commit,
        Repository=Repository,
        DiffCache=DiffCache,
        clear_repository_state_for_switch_func=_clear_repository_state_for_switch,
    )

# 更新仓库配置 - API接口


@require_admin
def update_repository_and_cache(repository_id):
    """更新仓库并触发缓存操作的API接口"""
    return handle_update_repository_and_cache(
        repository_id=repository_id,
        request=request,
        jsonify=jsonify,
        Repository=Repository,
        db=db,
        NotFound=NotFound,
        SQLAlchemyError=SQLAlchemyError,
        log_print=log_print,
        spawn_update_worker=_start_repository_update_thread,
    )
# 批量更新仓库凭据


@require_admin
def batch_update_credentials():
    """批量更新项目下的仓库凭据"""
    return handle_batch_update_credentials(
        request=request,
        jsonify=jsonify,
        Repository=Repository,
        db=db,
        SQLAlchemyError=SQLAlchemyError,
        app_logger=app.logger,
    )

@app.context_processor
def inject_template_functions():
    """注入模板函数"""
    return dict(
        get_diff_data=get_diff_data,
        generate_commit_diff_url=generate_commit_diff_url,
        generate_excel_diff_data_url=generate_excel_diff_data_url,
        generate_refresh_diff_url=generate_refresh_diff_url
    )
def create_tables():
    """创建数据库表"""
    # static-check compatibility: implementation now lives in service and still uses inspect(db.engine).get_table_names()
    create_tables_with_runtime_checks(
        app=app,
        db=db,
        log_print=log_print,
        apply_schema_migrations=apply_schema_migrations,
    )
def clear_version_mismatch_cache():
    """清理版本不匹配的缓存（自动模式）"""
    clear_startup_version_mismatch_cache(
        log_print=log_print,
        diff_logic_version=DIFF_LOGIC_VERSION,
        excel_cache_service=excel_cache_service,
        excel_html_cache_service=excel_html_cache_service,
        db=db,
    )

configure_runtime_wirings(
    log_print=log_print,
    configure_commit_diff_logic=configure_commit_diff_logic,
    configure_weekly_version_logic=configure_weekly_version_logic,
    configure_task_worker=configure_task_worker,
    excel_cache_service=excel_cache_service,
    excel_html_cache_service=excel_html_cache_service,
    active_git_processes=active_git_processes,
    add_excel_diff_task=add_excel_diff_task,
    get_unified_diff_data=get_unified_diff_data,
    get_git_service=get_git_service,
    get_svn_service=get_svn_service,
    weekly_excel_cache_service=weekly_excel_cache_service,
    create_weekly_sync_task=create_weekly_sync_task,
    get_file_content_from_git=get_file_content_from_git,
    get_file_content_from_svn=get_file_content_from_svn,
    generate_merged_diff_data=generate_merged_diff_data,
    app=app,
    db=db,
    BackgroundTask=BackgroundTask,
    Commit=Commit,
    Repository=Repository,
    DiffCache=DiffCache,
    WeeklyVersionConfig=WeeklyVersionConfig,
    process_weekly_version_sync=process_weekly_version_sync,
    process_weekly_excel_cache=process_weekly_excel_cache,
    db_retry=db_retry,
)


def _init_auth_default_data_with_context():
    from auth import init_auth_default_data

    with app.app_context():
        init_auth_default_data()


_bootstrap_manager = AppBootstrapManager(
    app=app,
    log_print=log_print,
    enable_local_worker=ENABLE_LOCAL_WORKER,
    create_tables_func=create_tables,
    init_auth_default_data_func=_init_auth_default_data_with_context,
    start_background_task_worker_func=start_background_task_worker,
    stop_background_task_worker_func=stop_background_task_worker,
    start_scheduler_func=start_scheduler,
    stop_scheduler_func=stop_scheduler,
    clear_version_mismatch_cache_func=clear_version_mismatch_cache,
    cleanup_pending_deletions_func=cleanup_pending_deletions,
    cleanup_git_processes_func=cleanup_git_processes,
)


def initialize_app():
    """兼容入口：调用 bootstrap 生命周期管理器执行初始化。"""
    _bootstrap_manager.initialize_app()


def cleanup_app():
    """兼容入口：调用 bootstrap 生命周期管理器执行清理。"""
    _bootstrap_manager.cleanup_app()


# cache management routes moved to routes/cache_management_routes.py
# 注册清理函数
log_print("[TRACE] about to register atexit", "APP")
atexit.register(cleanup_app)
log_print(f"[TRACE] reached if __name__ check, __name__={__name__!r}", "APP")


if __name__ == '__main__':
    from bootstrap.runtime_entry import run_runtime_entry

    run_runtime_entry(sys.modules[__name__])

