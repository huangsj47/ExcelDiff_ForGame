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
import hmac
from datetime import datetime, timedelta, timezone

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from flask_cors import CORS
from sqlalchemy import Index, func, case, inspect

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
    enhanced_retry_clone_repository,
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
    repository_config,
    test,
)
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
    get_sqlite_path_from_uri,
    sanitize_database_uri,
)
from utils.db_safety import collect_sqlite_runtime_diagnostics
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
    _original_print,
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

app = Flask(__name__)
# 启用模板自动重载（开发环境）
app.config['TEMPLATES_AUTO_RELOAD'] = True
# 启用CORS支持，允许跨域请求
secret_key = os.environ.get("FLASK_SECRET_KEY") or os.environ.get("SECRET_KEY")
if not secret_key:
    secret_key = secrets.token_urlsafe(48)
    log_print("⚠️ FLASK_SECRET_KEY 未配置，已使用运行期随机密钥。生产环境必须显式配置。", "APP", force=True)
cors_allowed_origins = [origin.strip() for origin in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",") if origin.strip()]
if cors_allowed_origins:
    CORS(app, resources={
        r"/status-sync/*": {"origins": cors_allowed_origins},
        r"/api/*": {"origins": cors_allowed_origins},
        r"/admin/*": {"origins": cors_allowed_origins},
    })
else:
    log_print("ℹ️ 未配置 CORS_ALLOWED_ORIGINS，默认禁用跨域访问。", "APP", force=True)
CSRF_SESSION_KEY = "_csrf_token"
ENABLE_ADMIN_SECURITY = os.environ.get("ENABLE_ADMIN_SECURITY", "true").lower() != "false"
# 始终需要管理员权限的端点（不论请求方法）
SENSITIVE_ENDPOINTS = {
    'delete_repository',
    'delete_project',
    'batch_update_credentials',
    'clear_all_confirmation_status',
    'update_repository_order',
    'swap_repository_order',
    'create_git_repository',
    'create_svn_repository',
    'update_repository',
    'retry_clone_repository',
    'sync_repository',
    'reuse_repository_and_update',
    'update_repository_and_cache',
    'regenerate_cache',
    'batch_update_commits_compat',
    'update_commit_fields',
    'edit_repository',
    'add_git_repository',
    'add_svn_repository',
}

# 仅写操作（POST/PUT/DELETE/PATCH）需要管理员权限的端点
# GET请求不受限（允许查看，但禁止修改）
WRITE_PROTECTED_ENDPOINTS = {
    'projects',            # GET 查看列表允许, POST 创建项目需要权限
    'repository_config',   # GET 查看仓库配置允许, 写操作需要权限
}
configure_request_security(
    csrf_session_key=CSRF_SESSION_KEY,
    enable_admin_security=ENABLE_ADMIN_SECURITY,
)


@app.before_request
def log_request_info():
    """Record incoming request info for admin routes."""
    if request.path.startswith('/admin/'):
        log_print(f"[REQUEST] {request.method} {request.path}", 'REQUEST', force=True)
# 不需要登录即可访问的端点（白名单）
AUTH_EXEMPT_ENDPOINTS = frozenset({
    'static',
    'admin_login',
    'admin_logout',
    'auth_bp.login',
    'auth_bp.register',
    'auth_bp.logout',
    'help_page',
    'core_management_routes.help_page',
    'test',
})

# 不需要登录即可访问的路径前缀
AUTH_EXEMPT_PATHS = (
    '/static/',
    '/auth/login',
    '/auth/register',
    '/auth/logout',
    '/help',
)

@app.before_request
def enforce_admin_access():
    if not ENABLE_ADMIN_SECURITY:
        return None

    # 白名单端点和路径无需认证
    if request.endpoint in AUTH_EXEMPT_ENDPOINTS:
        return None
    if any(request.path.startswith(p) for p in AUTH_EXEMPT_PATHS):
        return None
    if _is_valid_admin_token():
        return None

    # ── 全局登录检查 ──
    # 所有非白名单页面必须登录
    if not _is_logged_in():
        return _unauthorized_login_response()

    # ── 管理员权限端点检查 ──
    # 始终需要管理员权限的端点
    if request.path.startswith('/admin/') or request.endpoint in SENSITIVE_ENDPOINTS:
        if not _has_admin_access():
            return _unauthorized_admin_response()

    # 仅写操作需要管理员权限的端点（GET 放行，POST/PUT/DELETE 等拦截）
    if request.endpoint in WRITE_PROTECTED_ENDPOINTS:
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            if not _has_admin_access():
                return _unauthorized_admin_response()

    return None

@app.before_request
def enforce_csrf():
    if request.method in {"GET", "HEAD", "OPTIONS", "TRACE"}:
        return None

    if request.endpoint in {'static'}:
        return None

    if _is_valid_admin_token():
        return None

    expected = session.get(CSRF_SESSION_KEY)
    provided = _csrf_token_from_request()
    if not (expected and provided and hmac.compare_digest(str(expected), str(provided))):
        return _csrf_error_response("CSRF token invalid or missing.")

    if not _is_same_origin_request():
        return _csrf_error_response("Cross-site request blocked.")

    return None

# Add the function to Jinja2 template globals
app.jinja_env.globals['get_excel_column_letter'] = get_excel_column_letter
app.jinja_env.globals['csrf_token'] = csrf_token
app.jinja_env.globals['is_admin'] = _has_admin_access
app.jinja_env.globals['is_logged_in'] = _is_logged_in
app.jinja_env.globals['get_current_user'] = _get_current_user
app.jinja_env.globals['has_project_access'] = _has_project_access
app.jinja_env.globals['has_project_admin_access'] = _has_project_admin_access

from utils.timezone_utils import format_beijing_time
app.jinja_env.globals['format_beijing_time'] = format_beijing_time
app.config['SECRET_KEY'] = secret_key
db_runtime_settings = apply_database_settings(app.config)
app.secret_key = secret_key
log_print(
    f"ℹ️ 数据库后端: {db_runtime_settings['backend']} | URI: {db_runtime_settings['display_uri']}",
    'DB',
    force=True
)
db.init_app(app)
_original_print("[TRACE] db.init_app(app) done")

# ── 初始化 Auth 账号系统 ──
try:
    from auth import init_auth
    init_auth(app, db)
    _original_print("[TRACE] auth module initialized")

    # 注册 Auth Blueprint
    from auth.routes import auth_bp
    app.register_blueprint(auth_bp)
    _original_print("[TRACE] auth_bp registered")

    # 在数据库表创建完成后初始化默认数据
    with app.app_context():
        try:
            from auth.services import init_default_functions, migrate_env_admin_to_db
            func_count = init_default_functions()
            if func_count > 0:
                _original_print(f"[TRACE] auth: initialized {func_count} default functions")
            admin_user = migrate_env_admin_to_db()
            if admin_user:
                _original_print(f"[TRACE] auth: migrated env admin to db: {admin_user.username}")
        except Exception as e:
            _original_print(f"[TRACE] auth: default data init skipped: {e}")
except ImportError as e:
    _original_print(f"[TRACE] auth module not available: {e}")
except Exception as e:
    _original_print(f"[TRACE] auth module init failed: {e}")
    import traceback; traceback.print_exc()

app.register_blueprint(cache_management_bp)
_original_print("[TRACE] cache_management_bp registered")
try:
    app.register_blueprint(commit_diff_bp)
    _original_print("[TRACE] commit_diff_bp registered")
except Exception as e:
    _original_print(f"[TRACE] commit_diff_bp FAILED: {e}")
    import traceback; traceback.print_exc()
try:
    app.register_blueprint(core_management_bp)
    _original_print("[TRACE] core_management_bp registered")
except Exception as e:
    _original_print(f"[TRACE] core_management_bp FAILED: {e}")
    import traceback; traceback.print_exc()
try:
    app.register_blueprint(weekly_version_bp)
    _original_print("[TRACE] weekly_version_bp registered")
except Exception as e:
    _original_print(f"[TRACE] weekly_version_bp FAILED: {e}")
    import traceback; traceback.print_exc()

# ---------------------------------------------------------------------------
# 为所有 Blueprint 端点注册短名称别名，使 url_for('index') 等继续工作
# ---------------------------------------------------------------------------
_bp_prefixes = [
    "core_management_routes.",
    "commit_diff_routes.",
    "weekly_version_routes.",
    "cache_management.",
    "main.",
]

def _register_endpoint_aliases(app):
    """
    遍历 app.url_map 中由蓝图注册的所有规则，为它们创建不带蓝图前缀的
    短名称端点别名。这样模板中的 url_for('index') 会自动映射到
    core_management_routes.index 的视图函数。
    """
    from werkzeug.routing import Rule
    alias_rules = []
    for rule in app.url_map.iter_rules():
        for prefix in _bp_prefixes:
            if rule.endpoint.startswith(prefix):
                short_name = rule.endpoint[len(prefix):]
                # 跳过已存在同名全局端点（如 static）
                if short_name in app.view_functions:
                    break
                # 注册视图函数的短名称引用
                app.view_functions[short_name] = app.view_functions[rule.endpoint]
                # 创建一条新的路由规则，端点为短名称，路径和方法与蓝图规则一致
                new_rule = Rule(
                    rule.rule,
                    endpoint=short_name,
                    methods=rule.methods,
                    defaults=rule.defaults,
                    subdomain=rule.subdomain,
                    strict_slashes=rule.strict_slashes,
                    merge_slashes=rule.merge_slashes,
                    redirect_to=rule.redirect_to,
                )
                alias_rules.append(new_rule)
                break  # 已匹配到前缀，无需继续检查其他前缀

    for new_rule in alias_rules:
        app.url_map.add(new_rule)

    _original_print(f"[TRACE] Registered {len(alias_rules)} endpoint short-name aliases")

_register_endpoint_aliases(app)

# 添加Excel列字母转换过滤器


@app.template_filter('excel_column_letter')
def excel_column_letter(index):
    """将数字索引转换为Excel列字母 (0->A, 1->B, ..., 25->Z, 26->AA)"""
    result = ""
    while index >= 0:
        result = chr(65 + (index % 26)) + result
        index = index // 26 - 1
        if index < 0:
            break

    return result

# 周版本相关路由已迁移至 routes/weekly_version_management_routes.py，
# 此处保留处理函数供蓝图包装层复用，避免大范围业务回归。


@app.template_filter('format_cell_value')
def format_cell_value_filter(value):
    """格式化单元格值，处理null、NaN等特殊值"""
    return format_cell_value(value)

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
_original_print("[TRACE] services initialized")

# ---------------------------------------------------------------------------
# 后台任务工作服务（已拆分到 services/task_worker_service.py）
# ---------------------------------------------------------------------------
from services.task_worker_service import (
    configure_task_worker, register_cleanup,
    TaskWrapper, background_task_queue,
    start_background_task_worker, stop_background_task_worker,
    add_excel_diff_task, add_excel_diff_tasks_batch,
    create_auto_sync_task, create_weekly_sync_task,
    cleanup_git_processes, queue_missing_git_branch_refresh,
    regenerate_repository_cache,
    setup_schedule, start_scheduler,
)

# ---------------------------------------------------------------------------
# Commit Diff 逻辑服务 — 已拆分至 services/commit_diff_logic.py
# ---------------------------------------------------------------------------
configure_commit_diff_logic(
    excel_cache_service=excel_cache_service,
    excel_html_cache_service=excel_html_cache_service,
    active_git_processes=active_git_processes,
    add_excel_diff_task_func=add_excel_diff_task,
    get_unified_diff_data_func=get_unified_diff_data,
    get_git_service_func=get_git_service,
    get_svn_service_func=get_svn_service,
)



# 测试路由







# 主页路由



# 项目管理路由



# 项目详情页面 - 重定向到项目概览



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
        query = query.filter(Commit.author.contains(filters['author']))
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

    # 批量查询确认用户姓名（display_name），避免模板中逐条查询
    all_confirm_usernames = set()
    for commit in commits:
        all_confirm_usernames.update(_parse_confirm_usernames(commit.status_changed_by))

    username_to_display_name = {}
    if all_confirm_usernames:
        try:
            from auth.models import AuthUser
            users = AuthUser.query.filter(AuthUser.username.in_(list(all_confirm_usernames))).all()
            username_to_display_name = {
                user.username: (user.display_name or user.username) for user in users
            }
        except Exception as e:
            log_print(f"加载确认用户姓名映射失败，回退为用户名显示: {e}", 'APP')

    for commit in commits:
        commit_confirm_users = _parse_confirm_usernames(commit.status_changed_by)
        commit_confirm_display_names = [
            username_to_display_name.get(username, username) for username in commit_confirm_users
        ]

        confirm_users_display = ''
        confirm_users_title = ''
        if commit.status in ('confirmed', 'rejected') and commit_confirm_users:
            confirm_users_display = ', '.join(commit_confirm_users)
            confirm_users_title = ', '.join(commit_confirm_display_names)

        commit.confirm_users_display = confirm_users_display
        commit.confirm_users_title = confirm_users_title

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


def get_excel_diff_data_with_path(project_code, repository_name, commit_id):
    return get_excel_diff_data(commit_id)

# 保持向后兼容的原路由


def get_excel_diff_data(commit_id):
    """异步获取Excel diff数据的API端点（支持HTML缓存优先）"""
    request_start = time.time()
    commit = Commit.query.get_or_404(commit_id)
    repository = commit.repository
    project = repository.project
    # 检查是否为Excel文件
    is_excel = excel_cache_service.is_excel_file(commit.path)
    if not is_excel:
        return jsonify({'error': True, 'message': '不是Excel文件'})

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
                "file_path": commit.path if commit else "",
            },
        )
        return jsonify({'error': True, 'message': f'Excel文件处理失败: {str(e)}'})

# 新的统一差异显示路由
# 新的带项目代号和仓库名的新diff路由


def commit_diff_new_with_path(project_code, repository_name, commit_id):
    return commit_diff_new(commit_id)

# 保持向后兼容的原路由


def commit_diff_new(commit_id):
    """使用新的差异服务显示文件差异"""
    commit = Commit.query.get_or_404(commit_id)
    repository = commit.repository
    project = repository.project
    # 获取该文件的所有提交历史
    file_commits = Commit.query.filter(
        Commit.repository_id == repository.id,
        Commit.path == commit.path
    ).order_by(Commit.commit_time.desc()).all()
    # 获取上一个版本的提交信息
    previous_commit = None
    current_index = None
    for i, fc in enumerate(file_commits):
        if fc.id == commit.id:
            current_index = i
            break

    if current_index is not None and current_index + 1 < len(file_commits):
        previous_commit = file_commits[current_index + 1]
    # 如果按索引未找到，尝试按时间查找
    if previous_commit is None:
        previous_commit = Commit.query.filter(
            Commit.repository_id == repository.id,
            Commit.path == commit.path,
            Commit.commit_time < commit.commit_time
        ).order_by(Commit.commit_time.desc()).first()
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
    commit = Commit.query.get_or_404(commit_id)
    repository = commit.repository
    project = repository.project
    # 获取上一个版本的提交信息
    file_commits = Commit.query.filter(
        Commit.repository_id == repository.id,
        Commit.path == commit.path
    ).order_by(Commit.commit_time.desc()).all()
    previous_commit = None
    current_index = None
    for i, fc in enumerate(file_commits):
        if fc.id == commit.id:
            current_index = i
            break

    if current_index is not None and current_index + 1 < len(file_commits):
        previous_commit = file_commits[current_index + 1]
    # 如果按索引未找到，尝试按时间查找
    if previous_commit is None:
        previous_commit = Commit.query.filter(
            Commit.repository_id == repository.id,
            Commit.path == commit.path,
            Commit.commit_time < commit.commit_time
        ).order_by(Commit.commit_time.desc()).first()
    # 获取完整文件内容
    try:
        import subprocess
        import os
        # 获取仓库的本地路径
        if repository.type == 'git':
            from services.threaded_git_service import ThreadedGitService
            git_service = ThreadedGitService(repository.url, repository.root_directory, 
                                   repository.username, repository.token, repository, set())
            local_path = git_service.local_path
            # 如果本地路径不存在，尝试克隆
            if not os.path.exists(local_path):
                success, message = git_service.clone_or_update_repository()
                if not success:
                    current_file_content = f"仓库克隆失败: {message}"
                    previous_file_content = f"仓库克隆失败: {message}"
                    return render_template('full_file_diff.html',
                                         commit=commit,
                                         repository=repository,
                                         project=project,
                                         previous_commit=previous_commit,
                                         current_file_content=current_file_content,
                                         previous_file_content=previous_file_content)
        else:
            svn_service = get_svn_service(repository)
            local_path = svn_service.local_path
        # 获取当前版本文件内容
        try:
            if repository.type == 'git':
                result = subprocess.run([
                    'git', 'show', f'{commit.commit_id}:{commit.path}'
                ], cwd=local_path, capture_output=True, text=True, encoding='utf-8')
            else:
                # SVN处理
                result = subprocess.run([
                    'svn', 'cat', f'{repository.url}/{commit.path}@{commit.commit_id}'
                ], capture_output=True, text=True, encoding='utf-8')
            if result.returncode == 0:
                current_file_content = result.stdout
                log_print(f"Current file content length: {len(current_file_content)}")
            else:
                current_file_content = f"无法获取文件内容: {result.stderr}"
                log_print(f"Git show error: {result.stderr}", 'INFO')
        except Exception as e:
            current_file_content = f"获取当前版本失败: {str(e)}"
        # 获取前一版本文件内容
        previous_file_content = ""
        if previous_commit:
            try:
                if repository.type == 'git':
                    result = subprocess.run([
                        'git', 'show', f'{previous_commit.commit_id}:{commit.path}'
                    ], cwd=local_path, capture_output=True, text=True, encoding='utf-8')
                else:
                    # SVN处理
                    result = subprocess.run([
                        'svn', 'cat', f'{repository.url}/{commit.path}@{previous_commit.commit_id}'
                    ], capture_output=True, text=True, encoding='utf-8')
                if result.returncode == 0:
                    previous_file_content = result.stdout
                else:
                    previous_file_content = f"无法获取文件内容: {result.stderr}"
            except Exception as e:
                previous_file_content = f"获取前一版本失败: {str(e)}"
        else:
            previous_file_content = ""
    except Exception as e:
        log_print(f"获取文件内容失败: {e}", 'INFO')
        current_file_content = "无法获取文件内容"
        previous_file_content = "无法获取文件内容"
    # 生成Git风格的并排diff数据
    side_by_side_diff = generate_side_by_side_diff(current_file_content, previous_file_content)
    return render_template('git_style_diff.html',
                         commit=commit,
                         repository=repository,
                         project=project,
                         previous_commit=previous_commit,
                         current_file_content=current_file_content,
                         previous_file_content=previous_file_content,
                         side_by_side_diff=side_by_side_diff)
# 重新计算差异API
# 新的带项目代号和仓库名的刷新diff路由


def refresh_commit_diff_with_path(project_code, repository_name, commit_id):
    return refresh_commit_diff(commit_id)

# 保持向后兼容的原路由


def refresh_commit_diff(commit_id):
    """重新计算提交的差异数据，绕过缓存 - 优化版本"""
    try:
        commit = Commit.query.get_or_404(commit_id)
        repository = commit.repository
        log_print(f"🔄 开始重新计算差异: commit={commit_id}, file={commit.path}", 'APP')
        # 记录开始时间
        start_time = time.time()
        # 如果是Excel文件，先清除相关缓存
        if excel_cache_service.is_excel_file(commit.path):
            log_print(f"🗑️ 清除Excel缓存: {commit.path}", 'EXCEL')
            # 优化的批量删除缓存，减少数据库操作和锁定时间
            try:
                cache_delete_start = time.time()
                # 删除diff缓存
                deleted_count = DiffCache.query.filter_by(
                    repository_id=repository.id,
                    commit_id=commit.commit_id,
                    file_path=commit.path
                ).delete()
                # 删除HTML缓存 - 直接在这里执行，避免额外的函数调用开销
                html_deleted_count = ExcelHtmlCache.query.filter_by(
                    repository_id=repository.id,
                    commit_id=commit.commit_id,
                    file_path=commit.path
                ).delete()
                # 一次性提交所有删除操作
                if deleted_count > 0 or html_deleted_count > 0:
                    db.session.commit()
                    cache_delete_time = time.time() - cache_delete_start
                    log_print(f"✅ 已删除缓存记录: diff={deleted_count}, html={html_deleted_count} | 耗时: {cache_delete_time:.2f}秒", 'EXCEL')
                else:
                    log_print(f"ℹ️ 没有找到需要删除的缓存记录", 'EXCEL')
            except Exception as cache_error:
                log_print(f"⚠️ 清除缓存时出错: {cache_error}", 'EXCEL', force=True)
                db.session.rollback()
        # 获取上一个版本的提交信息 - 优化查询
        file_commits = Commit.query.filter(
            Commit.repository_id == repository.id,
            Commit.path == commit.path,
            Commit.commit_time < commit.commit_time
        ).order_by(Commit.commit_time.desc()).first()
        # 强制重新计算差异
        diff_calculation_start = time.time()
        diff_data = get_unified_diff_data(commit, file_commits)
        diff_calculation_time = time.time() - diff_calculation_start
        if diff_data:
            # 如果是Excel文件，重新缓存结果
            if excel_cache_service.is_excel_file(commit.path) and diff_data.get('type') == 'excel':
                cache_start = time.time()
                log_print(f"💾 重新缓存Excel差异数据", 'EXCEL')
                try:
                    excel_cache_service.save_cached_diff(
                        repository_id=repository.id,
                        commit_id=commit.commit_id,
                        file_path=commit.path,
                        diff_data=diff_data,
                        previous_commit_id=file_commits.commit_id if file_commits else None,
                        processing_time=diff_calculation_time,
                        commit_time=commit.commit_time
                    )
                    cache_time = time.time() - cache_start
                    log_print(f"💾 缓存保存完成，耗时: {cache_time:.2f}秒", 'EXCEL')
                except Exception as cache_error:
                    log_print(f"⚠️ 保存缓存时出错: {cache_error}", 'EXCEL')
            total_time = time.time() - start_time
            log_print(f"✅ 差异重新计算完成: {commit.path} | 计算耗时: {diff_calculation_time:.2f}秒 | 总耗时: {total_time:.2f}秒", 'APP')
            # 使用安全的JSON序列化处理diff_data中的NaN值
            safe_diff_data = safe_json_serialize(diff_data)
            return jsonify({
                'success': True,
                'message': f'差异重新计算完成，计算耗时 {diff_calculation_time:.2f} 秒',
                'processing_time': diff_calculation_time,
                'total_time': total_time,
                'diff_data': safe_diff_data  # 使用清理后的数据，避免NaN导致JSON解析错误
            })
        else:
            total_time = time.time() - start_time
            log_print(f"❌ 差异重新计算失败: {commit.path} | 耗时: {total_time:.2f}秒", 'APP', force=True)
            return jsonify({
                'success': False,
                'message': '差异重新计算失败，请检查文件内容',
                'total_time': total_time
            })
    except Exception as e:
        total_time = time.time() - start_time if 'start_time' in locals() else 0
        log_print(f"❌ 重新计算差异异常: {e} | 耗时: {total_time:.2f}秒", 'APP', force=True)
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'message': f'重新计算差异失败: {str(e)}',
            'total_time': total_time
        }), 500
# 新的带项目代号和仓库名的路由


def commit_diff_with_path(project_code, repository_name, commit_id):
    return commit_diff(commit_id)

# 保持向后兼容的原路由


def commit_diff(commit_id):
    commit = Commit.query.get_or_404(commit_id)
    repository = commit.repository
    project = repository.project
    # 检查是否为删除操作
    is_deleted = commit.operation == 'D'
    # 检查是否为Excel文件
    is_excel = excel_cache_service.is_excel_file(commit.path)
    # 获取该文件的所有提交历史 - 使用更严格的排序
    file_commits = Commit.query.filter(
        Commit.repository_id == repository.id,
        Commit.path == commit.path
    ).order_by(Commit.commit_time.desc(), Commit.id.desc()).all()
    # 获取上一个版本的提交信息 - 改进的查找逻辑
    previous_commit = None
    # 方法1: 直接按时间查找前一提交（最可靠的方法）
    previous_commit = Commit.query.filter(
        Commit.repository_id == repository.id,
        Commit.path == commit.path,
        Commit.commit_time < commit.commit_time
    ).order_by(Commit.commit_time.desc(), Commit.id.desc()).first()
    # 方法2: 如果按时间未找到，尝试按ID查找（处理时间相同的情况）
    if previous_commit is None:
        previous_commit = Commit.query.filter(
            Commit.repository_id == repository.id,
            Commit.path == commit.path,
            Commit.id < commit.id
        ).order_by(Commit.id.desc()).first()
    # 方法3: 如果还是未找到，使用索引方法作为最后备选
    if previous_commit is None:
        current_index = None
        for i, fc in enumerate(file_commits):
            if fc.id == commit.id:
                current_index = i
                break

        if current_index is not None and current_index + 1 < len(file_commits):
            previous_commit = file_commits[current_index + 1]
    # 调试日志
    log_print(f"🔍 查找前一提交 - 文件: {commit.path}", 'DIFF', force=True)
    log_print(f"🔍 该文件总提交数: {len(file_commits)}", 'DIFF', force=True)
    if previous_commit:
        log_print(f"✅ 找到前一提交: ID:{previous_commit.id} {previous_commit.commit_id[:8]} {previous_commit.commit_time}", 'DIFF', force=True)
    else:
        log_print(f"❌ 未找到前一提交 - 这是初始提交", 'DIFF', force=True)
    # 如果是删除操作，返回删除信息页面
    if is_deleted:
        return render_template('commit_diff.html', 
                             commit=commit, 
                             repository=repository,
                             project=project,
                             diff_data={'type': 'deleted', 'message': '该文件已被删除'},
                             file_commits=file_commits,
                             previous_commit=previous_commit,
                             is_excel=is_excel,
                             is_deleted=True)
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
                        except Exception as delete_error:
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
        template_context = {
            'commit': commit,
            'repository': repository,
            'project': project,
            'diff_data': diff_data,
            'file_commits': file_commits,
            'previous_commit': previous_commit,
            'is_excel': True,
            'is_deleted': False
        }
        log_print(f"🔍 模板上下文键: {list(template_context.keys())}", 'EXCEL', force=True)
        log_print(f"🔍 is_excel值: {template_context['is_excel']}, 类型: {type(template_context['is_excel'])}", 'EXCEL', force=True)
        return render_template('commit_diff.html', **template_context)

    else:
        # 非Excel文件，正常同步处理
        diff_data = get_diff_data(commit)
        return render_template('commit_diff.html', 
                             commit=commit, 
                             repository=repository,
                             project=project,
                             diff_data=diff_data,
                             file_commits=file_commits,
                             previous_commit=previous_commit,
                             is_excel=False,
                             is_deleted=False)
# 确认/拒绝提交（旧版本，已被新的API替代）
# 重新生成Diff缓存


@require_admin
def regenerate_cache(repository_id):
    """重新生成指定仓库的Excel文件差异缓存"""
    try:
        repository = Repository.query.get_or_404(repository_id)
        # 直接在这里获取Excel提交数量并添加任务
        recent_commits = excel_cache_service.get_recent_excel_commits(repository, limit=1000)
        task_count = len(recent_commits)
        if task_count > 0:
            # 删除现有的所有缓存数据
            DiffCache.query.filter_by(repository_id=repository_id).delete()
            # 删除HTML缓存数据
            ExcelHtmlCache.query.filter_by(repository_id=repository_id).delete()
            db.session.commit()
            log_print(f"已清理仓库 {repository_id} 的所有缓存数据", 'INFO')
            # 为每个提交添加处理任务
            for commit in recent_commits:
                add_excel_diff_task(repository_id, commit.commit_id, commit.path, priority=15)  # 批量重建使用低优先级
            message = f'已将 {task_count} 个Excel文件差异放入缓存队列，正在后台处理中...'
            # 记录重新生成缓存操作
            excel_cache_service.log_cache_operation(f"🔄 重新生成缓存: 仓库 {repository.name}, 任务数量 {task_count}", 'info')
        else:
            message = f'仓库 {repository.name} 最近2周内没有Excel文件提交，无需重新生成缓存。'
        return jsonify({
            'success': True, 
            'message': message,
            'task_count': task_count
        })
    except Exception as e:
        log_print(f"重新生成缓存失败: {e}", 'INFO')
        return jsonify({
            'success': False, 
            'message': f'重新生成缓存失败: {str(e)}'
        }), 500
# 获取缓存状态


def get_cache_status(repository_id):
    """获取仓库的缓存状态"""
    try:
        repository = Repository.query.get_or_404(repository_id)
        # 统计缓存数据
        total_cache = DiffCache.query.filter_by(repository_id=repository_id).count()
        completed_cache = DiffCache.query.filter_by(
            repository_id=repository_id, 
            cache_status='completed'
        ).count()
        failed_cache = DiffCache.query.filter_by(
            repository_id=repository_id, 
            cache_status='failed'
        ).count()
        processing_cache = DiffCache.query.filter_by(
            repository_id=repository_id, 
            cache_status='processing'
        ).count()
        # 获取最近1000条提交中的Excel文件数量
        recent_commits = excel_cache_service.get_recent_excel_commits(repository, limit=1000)
        total_excel_commits = len(recent_commits)
        return jsonify({
            'success': True,
            'repository_name': repository.name,
            'total_cache': total_cache,
            'completed_cache': completed_cache,
            'failed_cache': failed_cache,
            'processing_cache': processing_cache,
            'total_excel_commits': total_excel_commits,
            'cache_coverage': f"{completed_cache}/{total_excel_commits}" if total_excel_commits > 0 else "0/0"
        })
    except Exception as e:
        log_print(f"获取缓存状态失败: {e}", 'INFO')
        return jsonify({
            'success': False, 
            'message': f'获取缓存状态失败: {str(e)}'
        }), 500
# 查询仓库克隆状态 API


def get_clone_status(repository_id):
    """轻量级 API：返回仓库的 clone_status，供前端轮询。"""
    repo = db.session.get(Repository, repository_id)
    if not repo:
        return jsonify({"success": False, "message": "仓库不存在"}), 404
    # 查询该仓库的提交记录数量，用于判断数据是否真正就绪
    commit_count = Commit.query.filter_by(repository_id=repository_id).count()
    is_data_ready = (repo.clone_status == 'completed' and commit_count > 0)
    return jsonify({
        "success": True,
        "clone_status": repo.clone_status or "pending",
        "clone_error": getattr(repo, "clone_error", None) or "",
        "commit_count": commit_count,
        "is_data_ready": is_data_ready,
    })


# 重试克隆仓库


@require_admin
def retry_clone_repository(repository_id):
    repository = Repository.query.get_or_404(repository_id)
    if repository.type != 'git':
        flash('只支持Git仓库的克隆重试', 'error')
        return redirect(url_for('repository_config', project_id=repository.project_id))

    project_id = repository.project_id
    # 启动后台线程进行重试克隆
    retry_thread = threading.Thread(target=enhanced_retry_clone_repository, args=(repository_id,), daemon=True)
    retry_thread.start()
    flash('已启动仓库克隆重试，请稍后查看状态', 'success')
    return redirect(url_for('repository_config', project_id=project_id))

# 同步仓库提交记录


@require_admin
def sync_repository(repository_id):
    """手动获取数据 - 立即执行git pull和分析"""
    try:
        log_print(f"🚀 [MANUAL_SYNC] 手动同步开始 - 仓库ID: {repository_id}", 'INFO')
        # 获取仓库信息
        repository = db.session.get(Repository, repository_id)
        if not repository:
            return jsonify({'status': 'error', 'message': '仓库不存在'}), 404

        log_print(f"📂 [MANUAL_SYNC] 仓库信息: {repository.name} ({repository.type})")
        if repository.type == 'git':
            git_service = get_git_service(repository)
            # 立即执行git pull操作
            log_print(f"🔄 [MANUAL_SYNC] 开始git pull操作...", 'INFO')
            success, message = git_service.clone_or_update_repository()
            if not success:
                log_print(f"❌ [MANUAL_SYNC] Git操作失败: {message}", 'INFO')
                return jsonify({'status': 'error', 'message': f'Git操作失败: {message}'}), 500

            log_print(f"✅ [MANUAL_SYNC] Git操作成功: {message}", 'INFO')
            # 获取数据库中最新的提交时间，用于增量同步
            latest_commit = Commit.query.filter_by(repository_id=repository_id)\
                .order_by(Commit.commit_time.desc()).first()
            since_date = None
            if latest_commit and latest_commit.commit_time:
                since_date = latest_commit.commit_time
                log_print(f"🔍 [MANUAL_SYNC] 从最新提交时间开始增量同步: {since_date}", 'INFO')
            else:
                log_print(f"🔍 [MANUAL_SYNC] 首次同步，获取最近800个提交", 'INFO')
            # 检查仓库配置的起始日期限制
            repository = db.session.get(Repository, repository_id)
            if repository and repository.start_date:
                if since_date is None or since_date < repository.start_date:
                    since_date = repository.start_date
                    log_print(f"🔍 [MANUAL_SYNC] 应用仓库配置的起始日期限制: {since_date}", 'INFO')
            # 获取提交记录 - 增量或限制数量，使用多线程优化版本
            limit = 800 if not since_date else 1000  # 首次同步限制800个，增量同步最多1000个
            import time
            start_time = time.time()
            commits = git_service.get_commits_threaded(since_date=since_date, limit=limit)
            end_time = time.time()
            log_print(f"⚡ [THREADED_GIT] 多线程获取提交记录耗时: {(end_time - start_time):.2f}秒, 提交数: {len(commits)}", 'GIT')
            log_print(f"🔍 [MANUAL_SYNC] 获取到 {len(commits)} 个提交记录")
            commits_added = 0
            excel_tasks_added = 0
            for i, commit_data in enumerate(commits):
                # 检查提交是否已存在
                existing_commit = Commit.query.filter_by(
                    repository_id=repository_id,
                    commit_id=commit_data['commit_id']
                ).first()
                if not existing_commit:
                    # 创建新的提交记录
                    new_commit = Commit(
                        repository_id=repository_id,
                        commit_id=commit_data['commit_id'],
                        author=commit_data.get('author', ''),
                        message=commit_data.get('message', ''),
                        commit_time=commit_data.get('commit_time'),
                        path=commit_data.get('path', ''),
                        version=commit_data.get('version', commit_data['commit_id'][:8]),
                        operation=commit_data.get('operation', 'M'),
                        status='pending'
                    )
                    db.session.add(new_commit)
                    commits_added += 1
                    log_print(f"➕ [MANUAL_SYNC] 添加新提交 {i+1}/{len(commits)}: {commit_data['commit_id'][:8]}")
                    # 检查是否为Excel文件，如果是则添加到diff缓存任务队列
                    if excel_cache_service.is_excel_file(commit_data.get('path', '')):
                        add_excel_diff_task(
                            repository_id, 
                            commit_data['commit_id'], 
                            commit_data.get('path', ''), 
                            priority=10,
                            auto_commit=False  # 不自动提交，避免会话冲突
                        )
                        excel_tasks_added += 1
                        log_print(f"📊 [MANUAL_SYNC] 添加Excel缓存任务: {commit_data.get('path', '')}")
                else:
                    log_print(f"⏭️ [MANUAL_SYNC] 跳过已存在提交 {i+1}/{len(commits)}: {commit_data['commit_id'][:8]}")
            # 提交数据库更改
            db.session.commit()
            log_print(f"✅ [MANUAL_SYNC] 手动同步完成，添加了 {commits_added} 个新提交，{excel_tasks_added} 个Excel缓存任务", 'INFO')
            return jsonify({
                'status': 'success', 
                'message': f'同步成功，添加了 {commits_added} 个新提交',
                'commits_added': commits_added
            }), 200
        elif repository.type == 'svn':
            svn_service = get_svn_service(repository)
            # 传入数据库模块避免循环导入
            commits_added = svn_service.sync_repository_commits(db, Commit)
            return jsonify({
                'status': 'success',
                'message': f'同步成功，添加了 {commits_added} 个新提交',
                'commits_added': commits_added
            }), 200
        else:
            return jsonify({'status': 'error', 'message': f'不支持的仓库类型: {repository.type}'}), 400

    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        log_print(f"❌ [MANUAL_SYNC] 手动同步失败: {str(e)}")
        log_print(f"错误详情: {error_details}", 'INFO')
        return jsonify({'status': 'error', 'message': f'同步失败: {str(e)}'}), 500

def run_repository_update_and_cache(repository_id):
    """异步执行仓库更新和缓存（线程安全：按ID重新查询对象）"""
    try:
        with app.app_context():
            repository = db.session.get(Repository, repository_id)
            if not repository:
                log_print(f"❌ 异步更新失败：仓库不存在 {repository_id}", 'API', force=True)
                return

            if repository.type == 'git':
                service = get_git_service(repository)
                success, message = service.clone_or_update_repository()
                log_print(f"Git更新结果: {success}, {message}", 'GIT')
            elif repository.type == 'svn':
                service = get_svn_service(repository)
                success, message = service.checkout_or_update_repository()
                log_print(f"SVN更新结果: {success}, {message}", 'SVN')
            else:
                log_print(f"不支持的仓库类型: {repository.type}", 'API', force=True)
                return

            if success:
                log_print("仓库更新成功，开始触发缓存操作...", 'CACHE')
                commits_added = service.sync_repository_commits(db, Commit)
                log_print(f"{repository.type.upper()} 同步完成，添加了 {commits_added} 个新提交", 'SYNC')
                log_print(f"✅ 仓库 {repository.name} 更新和缓存完成", 'CACHE')
            else:
                log_print(f"❌ 仓库 {repository.name} 更新失败: {message}", 'API', force=True)
    except Exception as e:
        log_print(f"❌ 异步更新和缓存操作异常: {e}", 'API', force=True)
        import traceback
        traceback.print_exc()
@require_admin
def reuse_repository_and_update(repository_id):
    """复用仓库并触发更新和缓存操作的API接口"""
    try:
        data = request.get_json(silent=True) or {}
        action = data.get('action', 'pull_and_cache')
        repository = Repository.query.get_or_404(repository_id)
        log_print(f"🔄 收到仓库复用更新请求: {repository.name} (ID: {repository_id})", 'API')
        update_thread = threading.Thread(target=run_repository_update_and_cache, args=(repository_id,), daemon=True)
        update_thread.start()
        return jsonify({
            'success': True,
            'message': f'仓库 {repository.name} 更新和缓存任务已启动',
            'repository_id': repository_id,
            'action': action
        })
    except Exception as e:
        log_print(f"❌ 仓库更新API异常: {e}", 'API', force=True)
        return jsonify({
            'success': False,
            'message': f'更新失败: {str(e)}'
        }), 500
def check_local_repository_exists(project_code, repository_name, repository_id):
    """检查本地仓库是否存在"""
    try:
        local_path = build_repository_local_path(project_code, repository_name, repository_id, strict=False)
    except (TypeError, ValueError):
        return False

    return os.path.exists(local_path)

def update_commit_status(commit_id):
    """更新提交状态"""
    try:
        data = request.get_json(silent=True) or {}
        status = data.get('status')
        # 兼容历史前端：action=confirm/reject
        if not status:
            action = (data.get('action') or request.form.get('action') or request.form.get('status') or '').strip()
            action_to_status = {
                'confirm': 'confirmed',
                'confirmed': 'confirmed',
                'approve': 'confirmed',
                'reject': 'rejected',
                'rejected': 'rejected',
                'pending': 'pending',
                'reviewed': 'reviewed',
            }
            status = action_to_status.get(action, action)
        if status not in ['pending', 'reviewed', 'confirmed', 'rejected']:
            return jsonify({'status': 'error', 'message': '无效的状态值'}), 400

        commit = Commit.query.get_or_404(commit_id)
        old_status = commit.status
        commit.status = status
        # 记录操作者用户名
        from utils.request_security import _get_current_user
        current_user = _get_current_user()
        if status in ('confirmed', 'rejected'):
            commit.status_changed_by = current_user.username if current_user else None
        elif status == 'pending':
            commit.status_changed_by = None
        db.session.commit()
        # 同步状态到周版本diff
        if old_status != status:
            from services.status_sync_service import StatusSyncService
            sync_service = StatusSyncService(db)
            sync_result = sync_service.sync_commit_to_weekly(commit_id, status)
            log_print(f"提交状态同步结果: {sync_result}", 'SYNC')
        return jsonify({
            'success': True,
            'message': '状态更新成功',
            'status_changed_by': commit.status_changed_by
        })

    except Exception as e:
        app.logger.error(f"更新提交状态失败: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@require_admin
def batch_update_commits_compat():
    """兼容历史前端的批量更新接口（batch-approve/batch-reject 的统一入口）"""
    try:
        data = request.get_json(silent=True) or {}
        commit_ids = data.get('commit_ids') or data.get('ids') or request.form.getlist('ids')
        action = (data.get('action') or request.form.get('action') or '').strip().lower()
        if not commit_ids:
            return jsonify({'status': 'error', 'message': '未选择任何提交'}), 400

        # 标准化commit_ids为int列表
        normalized_ids = []
        for raw_id in commit_ids:
            try:
                normalized_ids.append(int(raw_id))
            except (TypeError, ValueError):
                continue

        if not normalized_ids:
            return jsonify({'status': 'error', 'message': '提交ID无效'}), 400

        if action in {'confirm', 'confirmed', 'approve'}:
            target_status = 'confirmed'
        elif action in {'reject', 'rejected'}:
            target_status = 'rejected'
        else:
            return jsonify({'status': 'error', 'message': '不支持的批量操作'}), 400

        from services.status_sync_service import StatusSyncService
        from utils.request_security import _get_current_user
        sync_service = StatusSyncService(db)
        current_user = _get_current_user()
        updated_count = 0
        sync_results = []
        for commit_id in normalized_ids:
            commit = db.session.get(Commit, commit_id)
            if commit and commit.status != target_status:
                commit.status = target_status
                commit.status_changed_by = current_user.username if current_user else None
                updated_count += 1
                sync_results.append(sync_service.sync_commit_to_weekly(commit_id, target_status))
        db.session.commit()
        total_weekly_updated = sum(r.get('updated_count', 0) for r in sync_results if r.get('success'))
        return jsonify({
            'status': 'success',
            'message': f'已更新 {updated_count} 个提交，同步更新 {total_weekly_updated} 个周版本记录',
            'updated_count': updated_count
        })
    except Exception as e:
        db.session.rollback()
        log_print(f"批量更新提交失败: {str(e)}", 'APP', force=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500

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
# 更新仓库配置 - 表单提交处理


@require_admin
def update_repository(repository_id):
    """处理仓库编辑表单提交"""
    repository = Repository.query.get_or_404(repository_id)
    project_id = repository.project_id
    try:
        # 保存旧的仓库名称，用于重命名目录
        old_name = repository.name
        new_name = (request.form.get('name') or '').strip()
        if not validate_repository_name(new_name):
            flash('仓库名称仅允许字母、数字、点、下划线和短横线', 'error')
            return redirect(url_for('edit_repository', repository_id=repository_id))

        # 保存旧的文件类型过滤器，用于检测是否需要重新筛选
        old_file_type_filter = repository.path_regex if repository.type == 'git' else None
        # 更新仓库信息
        repository.name = new_name
        repository.category = request.form.get('category')
        repository.resource_type = request.form.get('resource_type')
        repository.display_order = int(request.form.get('display_order', 0))
        # 通用字段更新 - 注意：模板中使用的是file_type_filter字段名
        repository.path_regex = request.form.get('file_type_filter') or request.form.get('path_regex')  # 修正字段名
        repository.log_regex = request.form.get('log_regex')
        repository.log_filter_regex = request.form.get('log_filter_regex')
        repository.commit_filter = request.form.get('commit_filter')
        repository.important_tables = request.form.get('important_tables')
        repository.unconfirmed_history = bool(request.form.get('unconfirmed_history'))
        repository.delete_table_alert = bool(request.form.get('delete_table_alert'))
        repository.weekly_version_setting = request.form.get('weekly_version_setting')
        # Table配置字段
        header_rows = request.form.get('header_rows')
        repository.header_rows = int(header_rows) if header_rows else None
        repository.key_columns = request.form.get('key_columns')
        repository.enable_id_confirmation = bool(request.form.get('enable_id_confirmation'))
        repository.show_duplicate_id_warning = bool(request.form.get('show_duplicate_id_warning'))
        repository.tag_selection = request.form.get('tag_selection')
        # 根据仓库类型更新特定字段
        if repository.type == 'git':
            repository.url = request.form.get('url')
            repository.server_url = request.form.get('server_url')
            new_token = (request.form.get('token') or '').strip()
            if new_token:
                repository.token = new_token
            repository.branch = request.form.get('branch')
            repository.enable_webhook = 'enable_webhook' in request.form
            repository.show_latest_id = 'show_latest_id' in request.form
            repository.table_name_column = request.form.get('table_name_column')
            # Git日期范围配置
            current_date = request.form.get('current_date')
            if current_date:
                try:
                    from datetime import datetime
                    repository.start_date = datetime.strptime(current_date, '%Y-%m-%d %H:%M:%S')
                except ValueError:
                    try:
                        repository.start_date = datetime.strptime(current_date, '%Y-%m-%d')
                    except ValueError:
                        flash('日期格式错误，请使用 YYYY-MM-DD HH:MM:SS 或 YYYY-MM-DD 格式', 'error')
                        return redirect(url_for('edit_repository', repository_id=repository_id))

        elif repository.type == 'svn':
            repository.url = request.form.get('url')
            repository.username = request.form.get('username')
            new_password = (request.form.get('password') or '').strip()
            if new_password:
                repository.password = new_password
        # 提交数据库更改
        db.session.commit()
        # 检查是否需要异步触发重新筛选
        need_refilter = repository.type == 'git' and old_file_type_filter != repository.path_regex
        if need_refilter:
            log_print(f"文件类型过滤器已更新: '{old_file_type_filter}' -> '{repository.path_regex}'", 'APP')
            # 在主线程中获取必要的数据，避免跨线程访问SQLAlchemy对象
            repository_id = repository.id
            new_path_regex = repository.path_regex
            # 异步触发重新筛选
            import threading
            def async_refilter():
                try:
                    log_print("开始异步重新筛选仓库内容...", 'APP')
                    with app.app_context():
                        # 重新获取repository对象（在新的应用上下文中）
                        repo = db.session.get(Repository, repository_id)
                        if not repo:
                            log_print(f"❌ 未找到仓库ID: {repository_id}", 'APP', force=True)
                            return

                        # 先清理不符合新过滤规则的数据库记录
                        if new_path_regex:
                            import re
                            try:
                                pattern = re.compile(new_path_regex)
                                log_print(f"开始清理不符合过滤规则的记录: {new_path_regex}", 'APP')
                                # 查找不符合新规则的提交记录
                                all_commits = Commit.query.filter_by(repository_id=repository_id).all()
                                commits_to_delete = []
                                for commit in all_commits:
                                    if commit.path and not pattern.match(commit.path):
                                        commits_to_delete.append(commit)
                                if commits_to_delete:
                                    log_print(f"找到 {len(commits_to_delete)} 个不符合规则的提交记录，开始清理...", 'APP')
                                    # 删除相关的diff缓存
                                    for commit in commits_to_delete:
                                        DiffCache.query.filter_by(
                                            repository_id=repository_id,
                                            commit_id=commit.commit_id,
                                            file_path=commit.path
                                        ).delete()
                                    # 删除提交记录
                                    for commit in commits_to_delete:
                                        db.session.delete(commit)
                                    db.session.commit()
                                    log_print(f"已清理 {len(commits_to_delete)} 个不符合规则的记录", 'APP')
                                else:
                                    log_print("没有找到需要清理的记录", 'APP')
                            except re.error as e:
                                log_print(f"正则表达式编译失败: {e}", 'APP', force=True)
                        # 然后重新同步符合新规则的内容
                        try:
                            from incremental_cache_system import IncrementalCacheManager
                            cache_system = IncrementalCacheManager()
                            success, message = cache_system.force_full_sync(repository_id)
                            if not success:
                                log_print(f"❌ 全量同步失败: {message}", 'APP', force=True)
                            else:
                                log_print("✅ 全量同步成功", 'APP')
                        except Exception as sync_e:
                            log_print(f"❌ 全量同步异常: {str(sync_e)}", 'APP', force=True)
                    log_print("仓库内容重新筛选完成", 'APP')
                except Exception as e:
                    log_print(f"重新筛选仓库内容时出错: {str(e)}", 'APP', force=True)
                    import traceback
                    log_print(f"详细错误信息: {traceback.format_exc()}", 'APP', force=True)
            # 启动后台线程执行重新筛选
            thread = threading.Thread(target=async_refilter, daemon=True)
            thread.start()
            # 立即返回，并在session中设置提示消息
            flash('仓库设置已保存，正在后台重新筛选文件，请稍后查看提交列表。', 'info')
        else:
            flash(f'仓库 "{repository.name}" 更新成功', 'success')
        return redirect(url_for('repository_config', project_id=project_id))

    except Exception as e:
        db.session.rollback()
        flash(f'更新仓库失败: {str(e)}', 'error')
        return redirect(url_for('edit_repository', repository_id=repository_id))

# 更新仓库配置 - API接口


@require_admin
def update_repository_and_cache(repository_id):
    """更新仓库并触发缓存操作的API接口"""
    try:
        data = request.get_json(silent=True) or {}
        action = data.get('action', 'pull_and_cache')
        repository = Repository.query.get_or_404(repository_id)
        log_print(f"🔄 收到仓库更新请求: {repository.name} (ID: {repository_id})", 'API')
        # 启动后台线程执行更新和缓存
        update_thread = threading.Thread(target=run_repository_update_and_cache, args=(repository_id,), daemon=True)
        update_thread.start()
        return jsonify({
            'success': True,
            'message': f'仓库 {repository.name} 更新和缓存任务已启动',
            'repository_id': repository_id,
            'action': action
        })
    except Exception as e:
        log_print(f"❌ 仓库更新API异常: {e}", 'API', force=True)
        return jsonify({
            'success': False,
            'message': f'更新失败: {str(e)}'
        }), 500
# 批量更新仓库凭据


@require_admin
def batch_update_credentials():
    """批量更新项目下的仓库凭据"""
    try:
        data = request.get_json()
        project_id = data.get('project_id')
        repo_type = data.get('repo_type')
        if not project_id or not repo_type:
            return jsonify({'status': 'error', 'message': '缺少必要参数'}), 400

        # 查询项目下指定类型的所有仓库
        repositories = Repository.query.filter_by(project_id=project_id, type=repo_type).all()
        if not repositories:
            return jsonify({'status': 'error', 'message': f'项目下没有找到{repo_type.upper()}仓库'}), 404

        updated_count = 0
        if repo_type == 'git':
            git_token = data.get('git_token')
            if not git_token:
                return jsonify({'status': 'error', 'message': '缺少Git Token'}), 400

            # 更新所有Git仓库的token
            for repo in repositories:
                repo.token = git_token
                updated_count += 1
        elif repo_type == 'svn':
            svn_username = data.get('svn_username')
            svn_password = data.get('svn_password')
            if not svn_username or not svn_password:
                return jsonify({'status': 'error', 'message': '缺少SVN用户名或密码'}), 400

            # 更新所有SVN仓库的用户名和密码
            for repo in repositories:
                repo.username = svn_username
                repo.password = svn_password
                updated_count += 1
        else:
            return jsonify({'status': 'error', 'message': '不支持的仓库类型'}), 400

        # 提交数据库更改
        db.session.commit()
        return jsonify({
            'status': 'success', 
            'message': f'成功更新{updated_count}个{repo_type.upper()}仓库',
            'updated_count': updated_count
        })
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"批量更新仓库凭据失败: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.context_processor
def inject_template_functions():
    """注入模板函数"""
    return dict(
        get_diff_data=get_diff_data,
        generate_commit_diff_url=generate_commit_diff_url,
        generate_excel_diff_data_url=generate_excel_diff_data_url,
        generate_refresh_diff_url=generate_refresh_diff_url
    )
def _migrate_table_columns(table_name, desired_cols):
    """自动为指定表补充模型中新增但数据库尚缺的列（SQLite ALTER TABLE）"""
    try:
        insp = inspect(db.engine)
        if table_name not in insp.get_table_names():
            return  # 表还不存在，create_all() 会负责创建
        existing_cols = {col['name'] for col in insp.get_columns(table_name)}
        added = []
        for col_name, col_ddl in desired_cols.items():
            if col_name not in existing_cols:
                from sqlalchemy import text as sa_text
                db.session.execute(sa_text(f'ALTER TABLE {table_name} ADD COLUMN {col_ddl}'))
                added.append(col_name)
        if added:
            db.session.commit()
            log_print(f"✅ 自动迁移 {table_name} 表，新增列: {', '.join(added)}", 'DB')
        else:
            log_print(f"ℹ️ {table_name} 表列已完整，无需迁移", 'DB')
    except Exception as e:
        log_print(f"⚠️ {table_name} 表自动迁移失败: {e}", 'DB', force=True)
        try:
            db.session.rollback()
        except Exception:
            pass


def _migrate_repository_columns():
    """自动为 repository 表补充模型中新增但数据库尚缺的列"""
    _migrate_table_columns(
        'repository',
        {
            'last_sync_error': 'last_sync_error TEXT',
            'last_sync_error_time': 'last_sync_error_time DATETIME',
        }
    )


def _migrate_commits_log_columns():
    """自动为 commits_log 表补充模型中新增但数据库尚缺的列"""
    _migrate_table_columns(
        'commits_log',
        {
            'status_changed_by': 'status_changed_by VARCHAR(100)',
        }
    )


def _migrate_weekly_version_diff_cache_columns():
    """自动为 weekly_version_diff_cache 表补充模型中新增但数据库尚缺的列"""
    _migrate_table_columns(
        'weekly_version_diff_cache',
        {
            'status_changed_by': 'status_changed_by VARCHAR(100)',
        }
    )


def create_tables():
    """创建数据库表"""
    with app.app_context():
        backend = get_database_backend_from_config(app.config)
        database_uri = str(app.config.get("SQLALCHEMY_DATABASE_URI", "") or "")
        sqlite_db_path = app.config.get("SQLITE_DB_PATH") or get_sqlite_path_from_uri(database_uri)
        if backend == "sqlite" and sqlite_db_path:
            instance_dir = os.path.dirname(sqlite_db_path)
            if instance_dir and not os.path.exists(instance_dir):
                try:
                    os.makedirs(instance_dir, exist_ok=True)
                    log_print(f"✅ 创建instance目录: {os.path.abspath(instance_dir)}", 'DB')
                except Exception as e:
                    log_print(f"❌ 创建instance目录失败: {e}", 'DB', force=True)
                    return

            elif instance_dir:
                log_print(f"ℹ️ instance目录已存在: {os.path.abspath(instance_dir)}", 'DB')
            if not os.path.exists(sqlite_db_path):
                log_print(f"ℹ️ 数据库文件不存在，将创建新数据库: {os.path.abspath(sqlite_db_path)}", 'DB')
        else:
            log_print(
                f"ℹ️ 使用 {backend.upper()} 数据库: {sanitize_database_uri(database_uri)}",
                'DB'
            )
        existing_tables = []
        try:
            existing_tables = inspect(db.engine).get_table_names()
        except Exception as e:
            log_print(f"检查现有表失败: {e}", 'DB', force=True)
        log_print(f"创建前的数据库表: {existing_tables}", 'DB')
        # 创建所有表
        try:
            db.create_all()
            log_print("✅ db.create_all() 执行完成", 'DB')
        except Exception as e:
            log_print(f"❌ 创建表失败: {e}", 'DB', force=True)
            return

        # ---- 自动迁移：为已有表补充缺失列 ----
        _migrate_repository_columns()
        _migrate_commits_log_columns()
        _migrate_weekly_version_diff_cache_columns()

        # 检查创建后的表状态
        try:
            final_tables = inspect(db.engine).get_table_names()
            log_print(f"创建后的数据库表: {final_tables}", 'DB')
            # 验证必需的表
            expected_tables = [
                'project', 'repository', 'commits_log',
                'background_tasks', 'global_repository_counter',
                'diff_cache', 'excel_html_cache', 'weekly_version_config',
                'weekly_version_diff_cache', 'weekly_version_excel_cache',
                'merged_diff_cache', 'operation_log'
            ]
            missing_tables = [t for t in expected_tables if t not in final_tables]
            if missing_tables:
                log_print(f"⚠️ 仍然缺失的表: {missing_tables}", 'DB', force=True)
            else:
                log_print("✅ 所有必需的表都已创建", 'DB')
        except Exception as e:
            log_print(f"检查最终表状态失败: {e}", 'DB', force=True)

        # 启动诊断：记录 SQLite 文件与空闲页占比，帮助快速识别“文件很大但数据为空”场景
        try:
            diag = collect_sqlite_runtime_diagnostics(database_uri)
            if diag.get("backend") == "sqlite":
                def _fmt_mb(num_bytes):
                    try:
                        return f"{(float(num_bytes) / (1024 * 1024)):.2f}MB"
                    except Exception:
                        return "0.00MB"

                log_print(
                    "SQLite诊断: "
                    f"path={diag.get('sqlite_path')}, "
                    f"size={_fmt_mb(diag.get('db_size_bytes', 0))}, "
                    f"wal={_fmt_mb(diag.get('wal_size_bytes', 0))}, "
                    f"journal={diag.get('journal_mode')}, "
                    f"pages={diag.get('page_count')}, "
                    f"free_pages={diag.get('freelist_count')}, "
                    f"free_ratio={float(diag.get('free_ratio', 0.0)):.2%}",
                    'DB',
                    force=True,
                )
                if float(diag.get("free_ratio", 0.0)) >= 0.80:
                    log_print(
                        "⚠️ SQLite空闲页占比超过80%，可能发生过大规模删除且未VACUUM；"
                        "若出现数据缺失请优先核查是否误执行 drop_all/清库脚本。",
                        'DB',
                        force=True,
                    )
            if diag.get("error"):
                log_print(f"SQLite诊断失败: {diag.get('error')}", 'DB', force=True)
        except Exception as e:
            log_print(f"SQLite启动诊断异常: {e}", 'DB', force=True)
def clear_version_mismatch_cache():
    """清理版本不匹配的缓存（自动模式）"""
    try:
        log_print(f"检查并清理版本不匹配的缓存 (当前版本: {DIFF_LOGIC_VERSION})", 'CACHE')
        # 使用服务层批量清理，避免 all()+逐条 delete+sleep 带来的启动期开销
        total_diff_cleaned = excel_cache_service.cleanup_version_mismatch_cache()
        total_html_cleaned = excel_html_cache_service.cleanup_old_version_cache()

        if total_diff_cleaned > 0 or total_html_cleaned > 0:
            log_print(f"清理完成：{total_diff_cleaned} 条数据缓存，{total_html_cleaned} 条HTML缓存", 'CACHE')
        else:
            log_print("无需清理版本不匹配的缓存", 'CACHE')
            log_print("启动成功！", 'APP')
    except Exception as e:
        log_print(f"清理版本不匹配缓存失败: {e}", 'CACHE', force=True)
        try:
            db.session.rollback()
        except:
            pass

# ---------------------------------------------------------------------------
#  配置周版本业务逻辑的运行时依赖
# ---------------------------------------------------------------------------
configure_weekly_version_logic(
    excel_cache_service=excel_cache_service,
    weekly_excel_cache_service=weekly_excel_cache_service,
    excel_html_cache_service=excel_html_cache_service,
    create_weekly_sync_task_func=create_weekly_sync_task,
    get_unified_diff_data_func=get_unified_diff_data,
    get_git_service_func=get_git_service,
    get_svn_service_func=get_svn_service,
    get_file_content_from_git_func=get_file_content_from_git,
    get_file_content_from_svn_func=get_file_content_from_svn,
    generate_merged_diff_data_func=generate_merged_diff_data,
)
_original_print("[TRACE] weekly_version_logic configured")

# ---------------------------------------------------------------------------
# 后台任务工作服务 — 注入运行时依赖
# ---------------------------------------------------------------------------
configure_task_worker(
    app=app,
    db=db,
    excel_cache_service=excel_cache_service,
    BackgroundTask=BackgroundTask,
    Commit=Commit,
    Repository=Repository,
    DiffCache=DiffCache,
    WeeklyVersionConfig=WeeklyVersionConfig,
    active_git_processes=active_git_processes,
    get_git_service=get_git_service,
    get_svn_service=get_svn_service,
    get_unified_diff_data=get_unified_diff_data,
    process_weekly_version_sync=process_weekly_version_sync,
    process_weekly_excel_cache=process_weekly_excel_cache,
    db_retry=db_retry,
)
_original_print("[TRACE] task_worker configured")


def initialize_app():
    """初始化应用，包括数据库和后台任务"""
    global _app_initialized
    if _app_initialized:
        log_print("应用已经初始化过，跳过重复初始化", 'APP')
        return

    try:
        # 创建数据库表
        create_tables()
        log_print("数据库表创建完成", 'APP')
        # 数据库表创建完成后，初始化 Auth 默认数据（首次启动时表不存在会跳过，需要在此补充）
        try:
            from auth.services import init_default_functions, migrate_env_admin_to_db
            with app.app_context():
                func_count = init_default_functions()
                if func_count > 0:
                    log_print(f"Auth: 初始化了 {func_count} 个默认职能", 'AUTH')
                admin_user = migrate_env_admin_to_db()
                if admin_user:
                    log_print(f"Auth: 迁移环境变量管理员到数据库: {admin_user.username}", 'AUTH')
        except Exception as e:
            log_print(f"Auth 默认数据初始化跳过: {e}", 'AUTH')
        # 在应用上下文中启动后台任务工作线程
        with app.app_context():
            start_background_task_worker()
        # 异步清理版本不匹配的缓存（避免阻塞启动）
        import threading
        def async_cache_cleanup():
            try:
                with app.app_context():
                    clear_version_mismatch_cache()
            except Exception as e:
                log_print(f"异步缓存清理失败: {e}", 'APP', force=True)
        cleanup_thread = threading.Thread(target=async_cache_cleanup, daemon=True)
        cleanup_thread.start()
        log_print("异步缓存清理已启动", 'APP')
        # 清理待删除的仓库目录
        cleanup_pending_deletions()
        log_print("应用初始化完成", 'APP')
        _app_initialized = True
    except Exception as e:
        log_print(f"应用初始化失败: {e}", 'APP', force=True)
        raise
def cleanup_app():
    """应用关闭时的清理工作"""
    try:
        # log_print("开始清理应用资源...", 'APP')
        stop_background_task_worker()
        cleanup_git_processes()
        # log_print("应用清理完成", 'APP')
    except Exception as e:
        log_print(f"应用清理过程中出现错误: {e}", 'APP', force=True)
        # 忽略清理过程中的错误，避免阻塞退出
# cache management routes moved to routes/cache_management_routes.py
# 注册清理函数
_original_print("[TRACE] about to register atexit")
atexit.register(cleanup_app)
# 防止重复初始化的标志
_app_initialized = False
_original_print(f"[TRACE] reached if __name__ check, __name__={__name__!r}")


if __name__ == '__main__':
    _original_print("[TRACE] entered __main__")
    import sys
    import os
    import signal
    import threading
    # 禁用Python输出缓冲，确保日志立即显示
    os.environ['PYTHONUNBUFFERED'] = '1'
    # 禁用Werkzeug的访问日志以避免日志输出错误
    # import logging
    # log = logging.getLogger('werkzeug')
    # log.setLevel(logging.ERROR)
    # 设置环境变量避免Windows控制台I/O问题
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    # 强制设置标准输出为无缓冲模式
    _original_print("[TRACE] about to reconfigure stdout")
    try:
        sys.stdout.reconfigure(line_buffering=True)
        _original_print("[TRACE] stdout reconfigured")
    except Exception as e:
        _original_print(f"[TRACE] stdout reconfigure failed: {e}")
    try:
        sys.stderr.reconfigure(line_buffering=True)
        _original_print("[TRACE] stderr reconfigured")
    except Exception as e:
        _original_print(f"[TRACE] stderr reconfigure failed: {e}")
    # 全局标志，用于优雅关闭
    shutdown_flag = threading.Event()
    _original_print("[TRACE] about to call clear_log_file")

    def signal_handler(signum, frame):
        """信号处理器，用于优雅关闭应用"""
        log_print("\n接收到中断信号，正在关闭应用...", 'APP')
        shutdown_flag.set()
        cleanup_app()
        # 强制退出，避免线程异常
        os._exit(0)
    # 注册信号处理器
    signal.signal(signal.SIGINT, signal_handler)
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, signal_handler)
    try:
        # 清空日志文件
        clear_log_file()
        # 初始化应用
        initialize_app()
        # 启动Flask应用
        log_print("正在启动服务器...", 'APP')
        log_print("按 Ctrl+C 停止服务器", 'APP')
        # 禁用debug模式和reloader来避免多进程问题
        _host = os.environ.get("HOST", "0.0.0.0")
        _port = int(os.environ.get("PORT", "8002"))
        app.run(debug=False, host=_host, port=_port, use_reloader=False, threaded=True)
    except KeyboardInterrupt:
        log_print("\n接收到键盘中断，正在关闭应用...", 'APP')
        cleanup_app()
    except SystemExit as se:
        # 记录 SystemExit 以便调试
        import traceback
        _original_print(f"[DEBUG] SystemExit caught: code={se.code}")
        traceback.print_exc()

    except Exception as e:
        import traceback
        _original_print(f"[DEBUG] Exception caught: {type(e).__name__}: {e}")
        traceback.print_exc()
        log_print(f"应用运行异常: {e}", 'APP', force=True)
        cleanup_app()
        sys.exit(1)
    finally:
        # 确保清理工作完成
        if not shutdown_flag.is_set():
            cleanup_app()

