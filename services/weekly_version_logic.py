#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Weekly version business logic and route handlers."""

import json
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from flask import abort, jsonify, render_template, request, url_for
from werkzeug.exceptions import HTTPException

from models import (
    BackgroundTask,
    Commit,
    Project,
    Repository,
    WeeklyVersionConfig,
    WeeklyVersionDiffCache,
    WeeklyVersionExcelCache,
    db,
)
from services.commit_ordering import commit_merge_sort_key
from services.deployment_mode import is_agent_dispatch_mode
from services.diff_render_helpers import render_excel_diff_html, render_git_diff_content, render_new_file_content
from services.diff_service import DiffService
from services.performance_metrics_service import get_perf_metrics_service
from services.repository_ordering import weekly_config_order_key
from services.task_worker_service import TaskWrapper, background_task_queue
from services.task_worker_weekly_handlers import is_weekly_sync_task_enqueued
from services.weekly_deleted_excel_helpers import render_weekly_deleted_excel as _render_weekly_deleted_excel_helper
from services.weekly_deleted_excel_helpers import (
    render_weekly_deleted_excel_notice as _render_weekly_deleted_excel_notice_helper,
)
from services.weekly_deleted_excel_helpers import resolve_primary_operation as _resolve_primary_operation_helper
from services.weekly_deleted_excel_helpers import (
    resolve_weekly_deleted_excel_state as _resolve_weekly_deleted_excel_state_helper,
)
from services.weekly_excel_merge_helpers import (
    extract_excel_diff_from_payload as _extract_excel_diff_from_payload_helper,
)
from services.weekly_excel_merge_helpers import (
    load_weekly_excel_diff_from_cache as _load_weekly_excel_diff_from_cache_helper,
)
from services.weekly_excel_merge_helpers import (
    merge_segmented_excel_diff_payload as _merge_segmented_excel_diff_payload_helper,
)
from services.weekly_file_sync import (
    WeeklyFileSyncResult,
    annotate_same_instant_order,
    describe_weekly_file_totals,
    get_real_base_commit_from_vcs,
    weekly_cache_is_unchanged,
)
from services.weekly_version_files_api_helpers import (
    extract_author_lookup_keys,
    normalize_naive_datetime,
    parse_confirm_usernames,
    parse_json_list,
    parse_json_obj,
    resolve_author_display,
    should_treat_sync_task_as_stale,
)

# 显式结局与「初始缓存遮罩」判定：为什么需要它，见该模块的模块级 docstring。
from services.weekly_version_sync_status import (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_SKIPPED,
    build_partial_failure_outcome,
    collect_target_file_paths,
    completed_outcome,
    config_inactive_outcome,
    config_missing_outcome,
    configs_with_finished_sync,
    initial_cache_readiness,
    latest_weekly_sync_tasks,
    no_commits_outcome,
    should_log_mask_decision,
)
from utils.diff_data_utils import clean_json_data
from utils.logger import log_print
from utils.request_security import _has_project_access
from utils.timezone_utils import beijing_window_to_utc_naive, now_beijing


# 窗口：config 的 start/end 是 naive 北京墙钟，commit_time 是 naive-UTC 墙钟；比较前
# 必须先过 weekly_window_in_utc()，否则窗口偏移 8 小时、窗口前 8 小时的提交被静默丢弃。
def weekly_window_in_utc(config):
    """北京墙钟窗口 → 与 Commit.commit_time 同口径的 (start_utc, end_utc)，均为 naive-UTC。"""
    return beijing_window_to_utc_naive(
        getattr(config, 'start_time', None), getattr(config, 'end_time', None))

# ---------------------------------------------------------------------------
#  运行时依赖 — 由 configure_weekly_version_logic() 注入
# ---------------------------------------------------------------------------
_excel_cache_service = None
_weekly_excel_cache_service = None
_excel_html_cache_service = None

# 延迟导入的函数引用
_create_weekly_sync_task = None
_get_unified_diff_data = None
_get_git_service = None
_get_svn_service = None
_get_file_content_from_git = None
_get_file_content_from_svn = None
_generate_merged_diff_data = None

def configure_weekly_version_logic(
    *,
    excel_cache_service,
    weekly_excel_cache_service,
    excel_html_cache_service,
    create_weekly_sync_task_func,
    get_unified_diff_data_func,
    get_git_service_func,
    get_svn_service_func,
    get_file_content_from_git_func,
    get_file_content_from_svn_func,
    generate_merged_diff_data_func,
):
    """注入运行时依赖。由 app.py 在初始化阶段调用。"""
    global _excel_cache_service, _weekly_excel_cache_service, _excel_html_cache_service
    global _create_weekly_sync_task, _get_unified_diff_data
    global _get_git_service, _get_svn_service
    global _get_file_content_from_git, _get_file_content_from_svn
    global _generate_merged_diff_data

    _excel_cache_service = excel_cache_service
    _weekly_excel_cache_service = weekly_excel_cache_service
    _excel_html_cache_service = excel_html_cache_service
    _create_weekly_sync_task = create_weekly_sync_task_func
    _get_unified_diff_data = get_unified_diff_data_func
    _get_git_service = get_git_service_func
    _get_svn_service = get_svn_service_func
    _get_file_content_from_git = get_file_content_from_git_func
    _get_file_content_from_svn = get_file_content_from_svn_func
    _generate_merged_diff_data = generate_merged_diff_data_func

# ---------------------------------------------------------------------------
#  辅助函数（本模块私有）

def _current_diff_logic_version():
    """当前比较口径版本号；唯一来源 `models.cache.default_diff_logic_version()`。

    不用 `from config import ...`：仓库里有两份字面量，那个函数才解析「真正生效那份」。
    解析不出返回 None（未标注版本）而不猜 —— 猜出来的版本号会让缓存静默失效。
    """
    try:
        from models.cache import default_diff_logic_version

        return default_diff_logic_version()
    except Exception as exc:  # pragma: no cover - 取不到版本不该阻断同步
        log_print(f"⚠️ 取 DIFF_LOGIC_VERSION 失败，本次缓存不标注版本: {exc}", 'WEEKLY', force=True)
        return None

# ---------------------------------------------------------------------------

def _commit_sort_key_for_merge(commit):
    """合并用排序键 —— 判据本体在 `services.commit_ordering`（同刻提交必须两个模块同一个答案）。"""
    return commit_merge_sort_key(commit)


def _get_app_func(name):
    """延迟从 app 模块获取函数引用，避免循环导入。"""
    import sys
    app_mod = sys.modules.get('app')
    if app_mod is None:
        import app as app_mod
    return getattr(app_mod, name)

def _render_project_page_missing(project_id: int, page_label: str):
    return render_template("project_page_missing.html", project_id=project_id, page_label=page_label), 404

def get_real_diff_data_for_merge(commit):
    """代理: 委托给 app.get_real_diff_data_for_merge"""
    return _get_app_func('get_real_diff_data_for_merge')(commit)

def get_commit_pair_diff_internal(current_commit, previous_commit):
    """代理: 委托给 app.get_commit_pair_diff_internal"""
    return _get_app_func('get_commit_pair_diff_internal')(current_commit, previous_commit)

# ---------------------------------------------------------------------------
#  以下为从 app.py 拆分出来的周版本业务逻辑
# ---------------------------------------------------------------------------

def weekly_version_config(project_id):
    """周版本配置页面"""
    project = Project.query.get_or_404(project_id)
    if not _has_project_access(project_id):
        abort(403)
    repositories = Repository.query.filter_by(project_id=project_id).all()
    # 获取分页参数
    page = max(1, request.args.get('page', 1, type=int) or 1)
    requested_per_page = request.args.get('per_page', 20, type=int) or 20
    per_page = min(max(requested_per_page, 1), 200)  # 每页最大200，防止大分页拖垮查询
    # 获取所有配置用于分组
    all_configs = WeeklyVersionConfig.query.filter_by(project_id=project_id).order_by(WeeklyVersionConfig.created_at.desc()).all()
    # 按版本名称和时间范围分组配置
    version_groups = {}
    for config in all_configs:
        # 提取版本基础名称（去掉仓库后缀）
        base_name = config.name
        if ' - ' in config.name:
            base_name = config.name.split(' - ')[0]
        # 创建分组键：版本名称 + 时间范围
        start_time = config.start_time.strftime('%Y-%m-%d %H:%M')
        end_time = config.end_time.strftime('%Y-%m-%d %H:%M')
        group_key = f"{base_name}_{start_time}_{end_time}"
        if group_key not in version_groups:
            version_groups[group_key] = {
                'version_name': base_name,
                'start_time': config.start_time,
                'end_time': config.end_time,
                'configs': [],
                'status': 'active',  # 默认状态
                'cycle_type': config.cycle_type,
                'created_at': config.created_at
            }
        version_groups[group_key]['configs'].append(config)
        # 更新组状态（如果有任何一个配置是completed，则整组为completed）
        if config.status == 'completed':
            version_groups[group_key]['status'] = 'completed'
        elif config.status == 'archived' and version_groups[group_key]['status'] != 'completed':
            version_groups[group_key]['status'] = 'archived'
    # 转换为列表并按优先级排序：活跃版本优先，然后按结束时间倒序
    all_grouped_versions = list(version_groups.values())
    # 判断版本是否活跃（当前时间在版本时间范围内）
    now = now_beijing()
    # 分类版本：活跃版本、未来版本、已结束版本
    active_versions = []    # 当前时间在版本区间内
    future_versions = []    # 开始时间在未来
    ended_versions = []     # 结束时间已过
    for group in all_grouped_versions:
        try:
            # 将now转换为本地时间（无时区）
            now_local = now.replace(tzinfo=None)
            # 确保数据库时间也是无时区的
            start_time = group['start_time']
            end_time = group['end_time']
            if start_time.tzinfo is not None:
                start_time = start_time.replace(tzinfo=None)
            if end_time.tzinfo is not None:
                end_time = end_time.replace(tzinfo=None)
            # 分类逻辑
            if start_time <= now_local <= end_time:
                # 活跃版本：当前时间在版本区间内
                group['category'] = 'active'
                active_versions.append(group)
            elif start_time > now_local:
                # 未来版本：开始时间在未来
                group['category'] = 'future'
                future_versions.append(group)
            else:
                # 已结束版本：结束时间已过
                group['category'] = 'ended'
                ended_versions.append(group)
        except Exception as e:
            log_print(f"时间比较出错: {str(e)}", 'APP', force=True)
            # 如果时间比较出错，默认归类为已结束版本
            group['category'] = 'ended'
            ended_versions.append(group)
    # 各分类内部排序：按结束时间倒序
    active_versions.sort(key=lambda x: -x['end_time'].timestamp())
    future_versions.sort(key=lambda x: -x['end_time'].timestamp())
    ended_versions.sort(key=lambda x: -x['end_time'].timestamp())
    # 合并所有版本：活跃版本 -> 未来版本 -> 已结束版本
    all_grouped_versions = active_versions + future_versions + ended_versions
    # 计算分页信息
    total_groups = len(all_grouped_versions)
    total_pages = (total_groups + per_page - 1) // per_page
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    # 获取当前页的版本组
    grouped_versions = all_grouped_versions[start_idx:end_idx]
    # 分页信息
    pagination = {
        'page': page,
        'per_page': per_page,
        'total': total_groups,
        'total_pages': total_pages,
        'has_prev': page > 1,
        'has_next': page < total_pages,
        'prev_num': page - 1 if page > 1 else None,
        'next_num': page + 1 if page < total_pages else None
    }
    return render_template('weekly_version_config.html',
                         project=project,
                         repositories=repositories,
                         configs=all_configs,  # 保留原始配置用于模态框
                         grouped_versions=grouped_versions,
                         active_versions=active_versions,
                         future_versions=future_versions,
                         ended_versions=ended_versions,
                         # 每个 config 最近一次 weekly_sync 任务：模板据此显示「为什么没有数据」
                         sync_task_by_config=latest_weekly_sync_tasks(BackgroundTask, all_configs),
                         # 「这个 config 有没有过一轮已经出结论的同步」——模板据此区分
                         # 「首轮还没好，请稍候」与「数据是上一轮的，新一轮在排队」。
                         # 少了它，周期性同步会让页面永远显示「排队中，请稍候」。
                         sync_finished_by_config=configs_with_finished_sync(BackgroundTask, all_configs),
                         pagination=pagination)
def weekly_version_config_api(project_id):
    """周版本配置API"""
    project = db.session.get(Project, project_id)
    if not project:
        return jsonify({'success': False, 'message': f'项目不存在: {project_id}'}), 404
    # 权限：本文件另有 9 处接口都做了项目校验（6 处以 config.project_id、3 处以 project_id），
    # 唯独这个**写接口**漏了 —— 下面 POST 分支会创建配置并派发同步任务，normal 角色
    # 用户可枚举 project_id 跨项目读/建配置。
    if not _has_project_access(project_id):
        return jsonify({'success': False, 'message': '无权访问该项目'}), 403
    if request.method == 'GET':
        # 获取配置列表
        configs = WeeklyVersionConfig.query.filter_by(project_id=project_id).all()
        return jsonify({
            'success': True,
            'configs': [{
                'id': config.id,
                'name': config.name,
                'description': config.description,
                'repository_id': config.repository_id,
                'repository_name': config.repository.name,
                'branch': config.branch,
                'start_time': config.start_time.isoformat(),
                'end_time': config.end_time.isoformat(),
                'cycle_type': config.cycle_type,
                'is_active': config.is_active,
                'auto_sync': config.auto_sync,
                'status': config.status,
                'created_at': config.created_at.isoformat()
            } for config in configs]
        })
    elif request.method == 'POST':
        # 创建新配置
        try:
            data = request.get_json()
            # 验证必需字段（repository_id 或 repository_ids 至少提供一个）
            required_fields = ['name', 'branch', 'start_time', 'end_time']
            for field in required_fields:
                if not data.get(field):
                    return jsonify({'success': False, 'message': f'缺少必需字段: {field}'}), 400
            # 验证仓库选择
            if not data.get('repository_id') and not data.get('repository_ids'):
                return jsonify({'success': False, 'message': '缺少必需字段: repository_id 或 repository_ids'}), 400

            # 解析时间并设置默认秒钟
            start_time = datetime.fromisoformat(data['start_time'].replace('T', ' '))
            end_time = datetime.fromisoformat(data['end_time'].replace('T', ' '))
            # 开始时间的秒钟默认为00
            start_time = start_time.replace(second=0, microsecond=0)
            # 结束时间的秒钟默认为59
            end_time = end_time.replace(second=59, microsecond=999999)
            if start_time >= end_time:
                return jsonify({'success': False, 'message': '开始时间必须早于结束时间'}), 400

            created_configs = []
            # 确定需要创建配置的仓库列表
            target_repositories = []
            if data.get('repository_id') == 'all':
                # 全部仓库
                target_repositories = Repository.query.filter_by(project_id=project_id).all()
                if not target_repositories:
                    return jsonify({'success': False, 'message': '该项目下没有仓库'}), 400
            elif data.get('repository_ids'):
                # 多选仓库（ID列表）
                repo_ids = data['repository_ids']
                if isinstance(repo_ids, list):
                    target_repositories = Repository.query.filter(
                        Repository.id.in_(repo_ids),
                        Repository.project_id == project_id
                    ).all()
                    if not target_repositories:
                        return jsonify({'success': False, 'message': '所选仓库不存在或不属于该项目'}), 400
                    if len(target_repositories) != len(repo_ids):
                        return jsonify({'success': False, 'message': '部分仓库不存在或不属于该项目'}), 400
                else:
                    return jsonify({'success': False, 'message': 'repository_ids 必须为数组'}), 400
            else:
                # 单个仓库
                repository = Repository.query.filter_by(id=data['repository_id'], project_id=project_id).first()
                if not repository:
                    return jsonify({'success': False, 'message': '仓库不存在或不属于该项目'}), 400
                target_repositories = [repository]

            # 为每个目标仓库创建配置
            is_multi = len(target_repositories) > 1
            for repository in target_repositories:
                config_name = f"{data['name']} - {repository.name}" if is_multi else data['name']
                config = WeeklyVersionConfig(
                    project_id=project_id,
                    repository_id=repository.id,
                    name=config_name,
                    description=data.get('description', ''),
                    branch=data['branch'],
                    start_time=start_time,
                    end_time=end_time,
                    cycle_type=data.get('cycle_type', 'custom'),
                    is_active=True,
                    auto_sync=True,
                    status='active'
                )
                db.session.add(config)
                created_configs.append(config)
            db.session.commit()
            # 如果启用自动同步，为每个配置创建后台同步任务
            for config in created_configs:
                if config.auto_sync and config.is_active:
                    _create_weekly_sync_task(config.id)
            if is_multi:
                return jsonify({
                    'success': True,
                    'message': f'成功为 {len(created_configs)} 个仓库创建配置',
                    'config_count': len(created_configs)
                })
            else:
                return jsonify({
                    'success': True,
                    'message': '配置创建成功',
                    'config_id': created_configs[0].id
                })
        except Exception as e:
            db.session.rollback()
            log_print(f"创建周版本配置失败: {e}", 'ERROR', force=True)
            return jsonify({'success': False, 'message': f'创建失败: {str(e)}'}), 500

def weekly_version_config_detail_api(project_id, config_id):
    """周版本配置详情API"""
    project = db.session.get(Project, project_id)
    if not project:
        return jsonify({'success': False, 'message': f'项目不存在: {project_id}'}), 404
    # 权限：config 已按 URL 的 project_id 限定，不存在串项目；但从未校验调用者能否
    # 访问该 project —— PUT 会删 WeeklyVersionDiffCache 后重建同步任务，DELETE 会删
    # 配置及其 Excel/diff 缓存与后台任务。
    if not _has_project_access(project_id):
        return jsonify({'success': False, 'message': '无权访问该项目'}), 403
    config = WeeklyVersionConfig.query.filter_by(id=config_id, project_id=project_id).first_or_404()
    if request.method == 'GET':
        # 获取配置详情
        return jsonify({
            'success': True,
            'config': {
                'id': config.id,
                'name': config.name,
                'description': config.description,
                'repository_id': config.repository_id,
                'repository_name': config.repository.name,
                'branch': config.branch,
                'start_time': config.start_time.isoformat(),
                'end_time': config.end_time.isoformat(),
                'cycle_type': config.cycle_type,
                'is_active': config.is_active,
                'auto_sync': config.auto_sync,
                'status': config.status,
                'created_at': config.created_at.isoformat(),
                'updated_at': config.updated_at.isoformat()
            }
        })
    elif request.method == 'PUT':
        # 更新配置
        try:
            data = request.get_json()
            # 检查是否修改了时间范围
            time_changed = data.get('time_changed', False)
            original_start_time = config.start_time
            original_end_time = config.end_time
            # 更新字段
            if 'name' in data:
                config.name = data['name']
            if 'description' in data:
                config.description = data['description']
            if 'branch' in data:
                config.branch = data['branch']
            if 'start_time' in data:
                new_start_time = datetime.fromisoformat(data['start_time'].replace('T', ' '))
                # 开始时间的秒钟默认为00
                new_start_time = new_start_time.replace(second=0, microsecond=0)
                if new_start_time != original_start_time:
                    time_changed = True
                config.start_time = new_start_time
            if 'end_time' in data:
                new_end_time = datetime.fromisoformat(data['end_time'].replace('T', ' '))
                # 结束时间的秒钟默认为59
                new_end_time = new_end_time.replace(second=59, microsecond=999999)
                if new_end_time != original_end_time:
                    time_changed = True
                config.end_time = new_end_time
            if 'cycle_type' in data:
                config.cycle_type = data['cycle_type']
            # 业务约束：周版本配置始终启用自动同步和激活状态，不允许接口层关闭。
            config.is_active = True
            config.auto_sync = True
            if 'status' in data:
                config.status = data['status']
            config.updated_at = datetime.now(timezone.utc)
            # 如果时间范围发生变化，清空所有相关的diff缓存和确认状态
            if time_changed:
                log_print(f"时间范围已变更，清空配置 {config.name} 的所有diff缓存", 'WEEKLY')
                # 删除所有相关的diff缓存
                deleted_count = WeeklyVersionDiffCache.query.filter_by(config_id=config_id).delete()
                log_print(f"已删除 {deleted_count} 条diff缓存记录", 'WEEKLY')
                # 如果启用了自动同步，创建新的同步任务
                if config.auto_sync and config.is_active:
                    _create_weekly_sync_task(config_id)
                    log_print("已创建新的同步任务", 'WEEKLY')
            db.session.commit()
            return jsonify({
                'success': True,
                'message': '配置更新成功',
                'time_changed': time_changed
            })
        except Exception as e:
            db.session.rollback()
            log_print(f"更新周版本配置失败: {e}", 'ERROR', force=True)
            return jsonify({'success': False, 'message': f'更新失败: {str(e)}'}), 500

    elif request.method == 'DELETE':
        try:
            excel_cache_deleted = WeeklyVersionExcelCache.query.filter_by(config_id=config_id).delete()
            log_print(f"删除了 {excel_cache_deleted} 个Excel缓存记录", 'WEEKLY')
            diff_cache_deleted = WeeklyVersionDiffCache.query.filter_by(config_id=config_id).delete()
            log_print(f"删除了 {diff_cache_deleted} 个diff缓存记录", 'WEEKLY')
            task_deleted = BackgroundTask.query.filter(
                BackgroundTask.repository_id == config_id,
                BackgroundTask.task_type.in_(['weekly_excel_cache', 'weekly_sync'])
            ).delete(synchronize_session=False)
            log_print(f"删除了 {task_deleted} 个后台任务", 'WEEKLY')
            db.session.delete(config)
            db.session.commit()
            return jsonify({'success': True, 'message': '配置删除成功'})

        except Exception as e:
            db.session.rollback()
            log_print(f"删除周版本配置失败: {e}", 'ERROR', force=True)
            return jsonify({'success': False, 'message': f'删除失败: {str(e)}'}), 500

def weekly_version_list(project_id):
    """周版本diff列表页面"""
    project = db.session.get(Project, project_id)
    if not project:
        return _render_project_page_missing(project_id, "周版本列表")
    if not _has_project_access(project_id):
        abort(403)
    repository_id = request.args.get('repository_id', type=int)
    # 获取配置列表
    query = WeeklyVersionConfig.query.filter_by(project_id=project_id)
    if repository_id:
        query = query.filter_by(repository_id=repository_id)
    configs = query.order_by(WeeklyVersionConfig.created_at.desc()).all()
    return render_template('weekly_version_list.html',
                         project=project,
                         configs=configs,
                         selected_repository_id=repository_id)
def merged_project_view(project_id):
    """合并的项目视图：左侧周版本列表，右侧仓库列表"""
    project = db.session.get(Project, project_id)
    if not project:
        return _render_project_page_missing(project_id, "项目合并视图")
    if not _has_project_access(project_id):
        abort(403)
    # P2: 后端可选分页参数（默认不分页，保持兼容）
    repo_page = max(1, request.args.get("repo_page", default=1, type=int) or 1)
    repo_size = max(0, min(100, request.args.get("repo_size", default=0, type=int) or 0))
    active_page = max(1, request.args.get("active_page", default=1, type=int) or 1)
    active_size = max(0, min(100, request.args.get("active_size", default=0, type=int) or 0))
    inactive_page = max(1, request.args.get("inactive_page", default=1, type=int) or 1)
    inactive_size = max(0, min(100, request.args.get("inactive_size", default=0, type=int) or 0))
    # 获取所有周版本配置
    configs = WeeklyVersionConfig.query.filter_by(project_id=project_id).order_by(WeeklyVersionConfig.created_at.desc()).all()
    # 获取仓库（支持可选分页）
    repositories_query = Repository.query.filter_by(project_id=project_id).order_by(Repository.display_order)
    repositories_total_count = repositories_query.count()
    if repo_size > 0:
        repo_offset = (repo_page - 1) * repo_size
        repositories = repositories_query.offset(repo_offset).limit(repo_size).all()
    else:
        repositories = repositories_query.all()
    repo_total_pages = math.ceil(repositories_total_count / repo_size) if repo_size > 0 else 1
    repositories_virtual_enabled = (repo_size == 0 and repositories_total_count >= 50)
    repositories_json = []
    for repo in repositories:
        repositories_json.append(
            {
                "id": repo.id,
                "name": repo.name,
                "type": repo.type,
                "resource_type": repo.resource_type,
                "branch": repo.branch,
                "current_version": repo.current_version,
                "created_date": repo.created_at.strftime("%Y-%m-%d") if repo.created_at else "-",
                "commit_list_url": url_for("commit_list", repository_id=repo.id),
            }
        )
    # 必须用北京墙钟而非 datetime.now()（宿主机本地时间）：在 UTC+8 开发机上碰巧
    # 正确，部署到 UTC 容器后活跃判定会前后各错 8 小时。
    now = now_beijing().replace(tzinfo=None)
    today_date = now.strftime("%Y-%m-%d")
    # 分组逻辑：相同版本基础名称+相同时间范围的配置归为一组
    version_groups = {}
    for config in configs:
        # 提取版本基础名称（去掉仓库后缀），例如 "第一周版本 - qz_client_lua" -> "第一周版本"
        base_name = config.name
        if ' - ' in config.name:
            base_name = config.name.split(' - ')[0]
        # 创建分组键：基础名称 + 开始时间 + 结束时间
        group_key = f"{base_name}_{config.start_time.strftime('%Y%m%d%H%M')}_{config.end_time.strftime('%Y%m%d%H%M')}"
        if group_key not in version_groups:
            version_groups[group_key] = {
                'name': base_name,  # 使用基础名称作为显示名称
                'start_time': config.start_time,
                'end_time': config.end_time,
                'configs': [],
                'is_active': False
            }
        version_groups[group_key]['configs'].append(config)
        # 判断是否为活跃版本（当前时间在版本时间范围内）；统一转成无时区本地时间再比
        try:
            # now 与 config.start_time/end_time 都是 naive 北京墙钟，同口径可直接比较
            start_time = config.start_time.replace(tzinfo=None) if config.start_time and config.start_time.tzinfo else config.start_time
            end_time = config.end_time.replace(tzinfo=None) if config.end_time and config.end_time.tzinfo else config.end_time
            if start_time and end_time and start_time <= now <= end_time:
                version_groups[group_key]['is_active'] = True
        except Exception as e:
            log_print(f"时间比较出错: {str(e)}", 'APP', force=True)
            # 如果时间比较出错，默认为非活跃状态
            pass

    # 分离活跃和非活跃版本
    active_versions = []
    inactive_versions = []
    for group in version_groups.values():
        # 「第一个仓库」的唯一口径（services/repository_ordering.py）：同时决定进入链接的
        # configs[0]、版本卡片的 AI 徽章代表哪个仓库、以及 inactive_versions_json 的顺序。
        group['configs'].sort(key=weekly_config_order_key)
        if group['is_active']:
            active_versions.append(group)
        else:
            inactive_versions.append(group)
    # 按时间排序
    active_versions.sort(key=lambda x: x['start_time'], reverse=True)
    inactive_versions.sort(key=lambda x: x['start_time'], reverse=True)
    active_versions_total_count = len(active_versions)
    inactive_versions_total_count = len(inactive_versions)

    # 可选分页（默认不启用）
    if active_size > 0:
        active_offset = (active_page - 1) * active_size
        active_versions = active_versions[active_offset:active_offset + active_size]
    if inactive_size > 0:
        inactive_offset = (inactive_page - 1) * inactive_size
        inactive_versions = inactive_versions[inactive_offset:inactive_offset + inactive_size]
    active_total_pages = math.ceil(active_versions_total_count / active_size) if active_size > 0 else 1
    inactive_total_pages = math.ceil(inactive_versions_total_count / inactive_size) if inactive_size > 0 else 1

    # 为JavaScript准备序列化的非活跃版本数据
    inactive_versions_json = []
    for version in inactive_versions:
        version_data = {
            'name': version['name'],
            'start_time': version['start_time'].isoformat(),
            'end_time': version['end_time'].isoformat(),
            'is_active': version['is_active'],
            'configs': []
        }
        for config in version['configs']:
            config_data = {
                'id': config.id,
                'branch': config.branch,
                'repository': {
                    'name': config.repository.name,
                    'type': config.repository.type
                }
            }
            version_data['configs'].append(config_data)
        inactive_versions_json.append(version_data)
    return render_template('merged_project_view.html',
                         project=project,
                         today_date=today_date,
                         active_versions=active_versions,
                         active_versions_total_count=active_versions_total_count,
                         active_page=active_page,
                         active_size=active_size,
                         active_total_pages=active_total_pages,
                         inactive_versions=inactive_versions,
                         inactive_versions_total_count=inactive_versions_total_count,
                         inactive_page=inactive_page,
                         inactive_size=inactive_size,
                         inactive_total_pages=inactive_total_pages,
                         inactive_versions_json=inactive_versions_json,
                         repositories=repositories,
                         repositories_json=repositories_json,
                         repositories_virtual_enabled=repositories_virtual_enabled,
                         repositories_total_count=repositories_total_count,
                         repo_page=repo_page,
                         repo_size=repo_size,
                         repo_total_pages=repo_total_pages)
def weekly_version_diff(config_id):
    """周版本diff详情页面 - 聚合显示同一时间段的不同仓库配置"""
    config = WeeklyVersionConfig.query.get_or_404(config_id)
    if not _has_project_access(config.project_id):
        abort(403)
    # 查找同一项目下相同时间段的其他配置
    related_configs = WeeklyVersionConfig.query.filter(
        WeeklyVersionConfig.project_id == config.project_id,
        WeeklyVersionConfig.start_time == config.start_time,
        WeeklyVersionConfig.end_time == config.end_time,
        WeeklyVersionConfig.id != config_id  # 排除当前配置
    ).order_by(WeeklyVersionConfig.repository_id.asc()).all()
    all_configs = [config] + related_configs
    # 标签顺序与「第一个仓库」同一把尺子（services/repository_ordering.py），否则各说各话。
    all_configs.sort(key=weekly_config_order_key)
    return render_template('weekly_version_diff.html',
                         config=config,
                         all_configs=all_configs,
                         current_config_id=config_id)
def weekly_version_config_info_api(config_id):
    """获取周版本配置信息API"""
    try:
        config = WeeklyVersionConfig.query.get_or_404(config_id)
        if not _has_project_access(config.project_id):
            abort(403)
        repository = config.repository
        if not repository:
            return jsonify({'success': False, 'message': '周版本关联仓库不存在'}), 404
        return jsonify({
            'success': True,
            'config': {
                'id': config.id,
                'name': config.name,
                'repository': {
                    'id': repository.id,
                    'name': repository.name,
                    'type': repository.type,
                    'resource_type': getattr(repository, 'resource_type', None),
                }
            }
        })
    except HTTPException:
        raise
    except Exception as e:
        log_print(f"获取周版本配置信息失败: {e}", 'ERROR', force=True)
        return jsonify({'success': False, 'message': str(e)}), 500

def weekly_version_files_api(config_id):
    """获取周版本文件列表API"""
    try:
        config = WeeklyVersionConfig.query.get_or_404(config_id)
        if not _has_project_access(config.project_id):
            abort(403)
        repository = config.repository
        if not repository:
            return jsonify({'success': False, 'message': '周版本关联仓库不存在'}), 404
        # 获取该配置的所有diff缓存
        diff_caches = WeeklyVersionDiffCache.query.filter_by(config_id=config_id).all()
        sync_task_id = None
        sync_task_status = None
        sync_triggered = False
        sync_blocking = False
        initial_sync_waiting = False
        initial_sync_window = timedelta(minutes=20)
        config_is_active = bool(getattr(config, 'is_active', True))
        config_auto_sync = bool(getattr(config, 'auto_sync', True))
        now_local = normalize_naive_datetime(now_beijing()) or datetime.utcnow()

        latest_sync_task = BackgroundTask.query.filter(
            BackgroundTask.task_type == 'weekly_sync',
            BackgroundTask.commit_id == str(config_id),
        ).order_by(BackgroundTask.id.desc()).first()
        if latest_sync_task:
            sync_task_id = latest_sync_task.id
            sync_task_status = latest_sync_task.status

        # 无论是否已有部分缓存，都先探测当前正在执行中的同步任务状态。
        existing_sync_task = BackgroundTask.query.filter(
            BackgroundTask.task_type == 'weekly_sync',
            BackgroundTask.commit_id == str(config_id),
            BackgroundTask.status.in_(['pending', 'processing']),
        ).order_by(BackgroundTask.id.desc()).first()
        # **还在内存队列里的 pending 不算陈旧**，理由与边界（只对 pending 生效）见
        # `should_treat_sync_task_as_stale`。页面每轮询一次就把排着队的任务置 failed、
        # 调度器紧接着又建一条新的，是「重置→重建」那个循环的一半。
        if should_treat_sync_task_as_stale(
            existing_sync_task, now_local, is_enqueued=is_weekly_sync_task_enqueued
        ):
            stale_task_id = getattr(existing_sync_task, 'id', None)
            stale_task_status = getattr(existing_sync_task, 'status', None)
            log_print(
                f"检测到陈旧周版本同步任务，按非阻塞处理: config_id={config_id}, task_id={stale_task_id}, status={stale_task_status}",
                'WEEKLY',
                force=True,
            )
            try:
                existing_sync_task.status = 'failed'
                if not getattr(existing_sync_task, 'error_message', None):
                    existing_sync_task.error_message = '任务长时间未推进，已自动标记为失败并解除页面阻塞'
                db.session.commit()
                sync_task_id = stale_task_id
                sync_task_status = 'failed'
            except Exception as stale_update_error:
                db.session.rollback()
                log_print(f"更新陈旧周版本任务状态失败: {stale_update_error}", 'WEEKLY', force=True)
            existing_sync_task = None

        if existing_sync_task:
            sync_task_id = existing_sync_task.id
            sync_task_status = existing_sync_task.status

        config_created_at = normalize_naive_datetime(getattr(config, 'created_at', None))
        is_recent_config = (
            config_created_at is not None
            and now_local >= config_created_at
            and (now_local - config_created_at) <= initial_sync_window
        )

        if is_recent_config and config_is_active and config_auto_sync:
            # 「首轮同步已有结论」= completed 或 skipped（窗口内无提交、明确跳过）。
            # skipped 必须算数，否则无数据的周版本会被判成「首轮未结束」，每次轮询都重派任务。
            completed_sync_task = BackgroundTask.query.filter(
                BackgroundTask.task_type == 'weekly_sync',
                BackgroundTask.commit_id == str(config_id),
                BackgroundTask.status.in_((TASK_STATUS_COMPLETED, TASK_STATUS_SKIPPED)),
            ).order_by(BackgroundTask.id.desc()).first()
            # 目标文件 = 窗口内有提交的文件。缓存行是同步过程中逐个创建的，只看缓存行会
            # 漏掉「还没来得及建缓存的文件」—— 那正是「大部分文件还没缓存时页面就放行」的成因。
            target_paths = set()
            try:
                target_paths = collect_target_file_paths(
                    Commit, repository.id, *weekly_window_in_utc(config))
            except Exception as target_error:
                # 枚举失败时降级为只按已有缓存行判定，避免页面永久卡在加载态。
                log_print(f"枚举周版本目标文件失败，降级为按已有缓存判定: {target_error}", 'WEEKLY')
            initial_cache_ready, mask_detail = initial_cache_readiness(target_paths, diff_caches)
            if should_log_mask_decision(config_id, initial_cache_ready):
                log_print(f"周版本初始缓存遮罩: config_id={config_id}, 放行={initial_cache_ready}, {mask_detail}", 'WEEKLY')
            # 要求「全部目标文件都成功」才放行：旧实现是 any()，第一个文件成功就解锁。
            initial_sync_waiting = (completed_sync_task is None) and (not initial_cache_ready)

        # 新建周版本在首轮同步完成前一律展示“处理中”，避免先看到不完整数据。
        should_trigger_sync = (
            not existing_sync_task
            and config_is_active
            and config_auto_sync
            and (initial_sync_waiting or not diff_caches)
            and callable(_create_weekly_sync_task)
        )
        if should_trigger_sync:
            created_task_id = _create_weekly_sync_task(config_id)
            if created_task_id:
                sync_task_id = created_task_id
                sync_task_status = 'pending'
                sync_triggered = True

        # 仅在“首轮同步尚未完成”时阻塞页面，避免后续周期性同步导致反复卡加载态。
        if initial_sync_waiting and (existing_sync_task or sync_triggered):
            sync_blocking = True

        all_confirm_usernames = set()
        all_author_keys = set()
        for cache in diff_caches:
            all_confirm_usernames.update(parse_confirm_usernames(cache.status_changed_by))
            commit_authors = parse_json_list(cache.commit_authors)
            for commit_author in commit_authors:
                all_author_keys.update(extract_author_lookup_keys(commit_author))

        username_to_display_name = {}
        username_to_display_name_lower = {}
        email_prefix_to_display_name = {}
        if all_confirm_usernames or all_author_keys:
            try:
                from auth import get_auth_backend

                if get_auth_backend() == "qkit":
                    from qkit_auth.models import QkitAuthUser as _UserModel
                else:
                    from auth.models import AuthUser as _UserModel

                from sqlalchemy import func as sa_func
                from sqlalchemy import or_

                user_query_conditions = []
                if all_confirm_usernames:
                    user_query_conditions.append(
                        sa_func.lower(_UserModel.username).in_([username.lower() for username in all_confirm_usernames])
                    )
                if all_author_keys:
                    user_query_conditions.append(sa_func.lower(_UserModel.username).in_(list(all_author_keys)))
                    user_query_conditions.extend(
                        sa_func.lower(_UserModel.email).like(f"{author_key}@%")
                        for author_key in all_author_keys
                        if author_key
                    )

                users = _UserModel.query.filter(or_(*user_query_conditions)).all() if user_query_conditions else []
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
                log_print(f"加载周版本确认用户姓名映射失败，回退为用户名显示: {e}", 'WEEKLY')

        files = []
        authors = set()
        for cache in diff_caches:
            # 解析提交者信息
            commit_authors = parse_json_list(cache.commit_authors)
            commit_messages = parse_json_list(cache.commit_messages)
            commit_times = parse_json_list(cache.commit_times)
            mapped_commit_authors = [
                resolve_author_display(
                    author,
                    username_to_display_name_lower=username_to_display_name_lower,
                    email_prefix_to_display_name=email_prefix_to_display_name,
                )
                for author in commit_authors
                if str(author or '').strip()
            ]
            authors.update(mapped_commit_authors)

            confirm_usernames = parse_confirm_usernames(cache.status_changed_by)
            confirm_display_names = [
                username_to_display_name.get(username)
                or username_to_display_name_lower.get(username.lower(), username)
                for username in confirm_usernames
            ]
            confirm_user_display = ''
            confirm_user_title = ''
            if cache.overall_status in ('confirmed', 'rejected') and confirm_usernames:
                confirm_user_display = ', '.join(confirm_display_names)
                # title 保留用户名，便于定位账号
                confirm_user_title = ', '.join(confirm_usernames)

            # 解析合并diff数据以获取文件操作信息
            file_operations = []
            if cache.merged_diff_data:
                try:
                    merged_data = parse_json_obj(cache.merged_diff_data)
                    file_operations = merged_data.get('operations', [])
                except Exception:
                    pass

            # 确定文件的主要操作类型（用于颜色编码）
            # 跟**窗口内的最终状态**走，而不是「操作里出现过 D 就标红」：
            # 文件被删掉又建回来时，旧口径会把它标成红色的「删除文件」，而它的
            # diff 页显示的是新增内容（线上 奖励模式_CfgRewardMode.xlsx）。
            primary_operation = _resolve_primary_operation_helper(file_operations)
            files.append({
                'file_path': cache.file_path,
                'commit_count': cache.commit_count,
                'commit_authors': json.dumps(mapped_commit_authors, ensure_ascii=False),
                'commit_messages': json.dumps(commit_messages, ensure_ascii=False),  # 添加提交日志
                'commit_times': json.dumps(commit_times, ensure_ascii=False),        # 添加提交时间
                'overall_status': cache.overall_status,
                'status_changed_by': cache.status_changed_by,  # 操作者用户名
                'confirm_user_display': confirm_user_display,
                'confirm_user_title': confirm_user_title,
                'confirmation_status': cache.confirmation_status,
                'last_sync_time': cache.last_sync_time.isoformat() if cache.last_sync_time else None,
                'operations': file_operations,  # 所有操作
                'primary_operation': primary_operation  # 主要操作类型
            })
        return jsonify({
            'success': True,
            'files': files,
            'authors': list(authors),
            'total_files': len(files),
            'repository_name': repository.name,
            'enable_id_confirmation': bool(repository.enable_id_confirmation),
            'sync_task_id': sync_task_id,
            'sync_task_status': sync_task_status,
            'sync_triggered': sync_triggered,
            'sync_blocking': sync_blocking,
        })
    except HTTPException:
        raise
    except Exception as e:
        log_print(f"获取周版本文件列表失败: {e}", 'ERROR', force=True)
        return jsonify({'success': False, 'message': str(e)}), 500

def weekly_version_file_diff_api(config_id):
    """获取单个文件的diff内容"""
    try:
        config = WeeklyVersionConfig.query.get_or_404(config_id)
        if not _has_project_access(config.project_id):
            abort(403)
        file_path = request.args.get('file_path')
        if not file_path:
            return "缺少文件路径参数", 400

        # 获取该文件的diff缓存
        diff_cache = WeeklyVersionDiffCache.query.filter_by(
            config_id=config_id, file_path=file_path).first()
        if not diff_cache:
            return "<div class='alert alert-warning'>未找到该文件的diff数据</div>"

        diff_html = generate_weekly_git_diff_html(config, diff_cache, file_path)
        return diff_html

    except HTTPException:
        raise
    except Exception as e:
        log_print(f"获取文件diff失败: {e}", 'ERROR', force=True)
        return f"<div class='alert alert-danger'>加载diff失败: {str(e)}</div>"

def weekly_version_file_full_diff(config_id):
    """周版本文件完整diff页面 - 优化版本，先显示页面框架"""
    try:
        config = WeeklyVersionConfig.query.get_or_404(config_id)
        if not _has_project_access(config.project_id):
            abort(403)
        file_path = request.args.get('file_path')
        if not file_path:
            return "缺少文件路径参数", 400

        # 获取该文件的diff缓存基本信息
        diff_cache = WeeklyVersionDiffCache.query.filter_by(
            config_id=config_id, file_path=file_path).first()
        if not diff_cache:
            return render_template('error.html',
                                 error_message="未找到该文件的diff数据",
                                 back_url=url_for('weekly_version_diff', config_id=config_id))
        # 只准备基本的静态数据，不进行耗时的Git操作
        template_data = {
            'config': config,
            'file_path': file_path,
            'diff_cache': diff_cache,
            'base_commit_id': diff_cache.base_commit_id,
            'latest_commit_id': diff_cache.latest_commit_id,
            # 基本的提交信息（从缓存中获取，不需要Git操作）
            'commit_authors': json.loads(diff_cache.commit_authors) if diff_cache.commit_authors else [],
            'commit_messages': json.loads(diff_cache.commit_messages) if diff_cache.commit_messages else [],
            'commit_times': json.loads(diff_cache.commit_times) if diff_cache.commit_times else []
        }
        return render_template('weekly_version_full_diff.html', **template_data)

    except HTTPException:
        raise
    except Exception as e:
        log_print(f"显示周版本完整diff失败: {e}", 'ERROR', force=True)
        return render_template('error.html',
                             error_message=f"加载失败: {str(e)}",
                             back_url=url_for('weekly_version_diff', config_id=config_id))
def weekly_version_file_full_diff_data(config_id):
    """异步加载周版本文件完整diff数据"""
    try:
        config = WeeklyVersionConfig.query.get_or_404(config_id)
        if not _has_project_access(config.project_id):
            abort(403)
        file_path = request.args.get('file_path')
        if not file_path:
            return jsonify({'success': False, 'message': '缺少文件路径参数'}), 400

        # 获取该文件的diff缓存
        diff_cache = WeeklyVersionDiffCache.query.filter_by(
            config_id=config_id, file_path=file_path).first()
        if not diff_cache:
            return jsonify({'success': False, 'message': '未找到该文件的diff数据'}), 404

        # 获取基准版本的详细信息（这是耗时操作）
        base_commit_info = None
        if diff_cache.base_commit_id:
            try:
                # 重新获取Repository对象，避免SQLAlchemy会话问题
                repository = db.session.get(Repository, config.repository_id)
                if not repository:
                    log_print(f"异步获取基准版本信息失败: 仓库不存在 {config.repository_id}", 'ERROR', force=True)
                else:
                    # 根据仓库类型选择合适的服务
                    if repository.type == 'git':
                        service = _get_git_service(repository)
                    else:  # SVN仓库
                        service = _get_svn_service(repository)
                    base_commit_info = service.get_commit_info(diff_cache.base_commit_id)
                    log_print(f"异步获取基准版本信息: {base_commit_info}", 'WEEKLY')
            except Exception as e:
                log_print(f"异步获取基准版本信息失败: {e}", 'ERROR', force=True)
        # 检查文件类型
        from services.diff_service import DiffService
        diff_service = DiffService()
        file_type = diff_service.get_file_type(file_path)
        # 生成diff HTML内容（固定走标准路径，不支持页面级手动重算）
        diff_html = generate_weekly_git_diff_html(config, diff_cache, file_path)
        return jsonify({
            'success': True,
            'diff_html': diff_html,
            'base_commit_info': base_commit_info,
            'file_type': file_type,
            'recalculated': False
        })
    except HTTPException:
        raise
    except Exception as e:
        log_print(f"异步加载周版本diff数据失败: {e}", 'ERROR', force=True)
        return jsonify({'success': False, 'message': f'加载失败: {str(e)}'}), 500

def generate_weekly_git_diff_html(config, diff_cache, file_path, force_recalculate=False):
    """生成周版本的真实Git diff HTML内容"""
    try:
        repository = config.repository
        # 获取基准commit和最新commit
        base_commit_id = diff_cache.base_commit_id
        latest_commit_id = diff_cache.latest_commit_id
        if not latest_commit_id:
            return "<div class='alert alert-warning'>未找到最新提交记录</div>"

        # 检查是否为Excel文件
        from services.diff_service import DiffService
        diff_service = DiffService()
        file_type = diff_service.get_file_type(file_path)
        if file_type == 'excel':
            # Excel文件使用合并diff逻辑
            return generate_weekly_excel_merged_diff_html(config, diff_cache, file_path, force_recalculate=force_recalculate)

        # 原先这里解析 diff_cache 的 commit_authors / commit_messages / commit_times
        # 拼一个版本信息头部；完整 diff 页已有那一块，重复渲染只会让人以为两处应当
        # 一致，而解析结果没有任何读者。删掉（顺带去掉「缓存 JSON 坏掉即抛异常」的失败面）。
        # 使用现有的Git服务获取真实的diff内容
        try:
            from services.threaded_git_service import ThreadedGitService
            git_service = ThreadedGitService(
                repository.url,
                repository.root_directory,
                repository.username,
                repository.token,
                repository
            )
            # 获取两个commit之间的diff
            if base_commit_id:
                log_print(f"获取周版本diff: {base_commit_id[:8]} -> {latest_commit_id[:8]}, 文件: {file_path}", 'WEEKLY')
                diff_result = git_service.get_commit_range_diff(base_commit_id, latest_commit_id, file_path)
                if diff_result and 'patch' in diff_result:
                    diff_content = diff_result['patch']
                    log_print(f"获取到diff内容，长度: {len(diff_content)} 字符", 'WEEKLY')
                    log_print(f"diff内容预览: {diff_content[:200]}...", 'WEEKLY')
                    # 使用现有的diff渲染函数
                    diff_html = render_git_diff_content(diff_content, file_path, base_commit_id, latest_commit_id, config, diff_cache)
                else:
                    log_print(f"未获取到diff内容，diff_result: {diff_result}", 'WEEKLY')
                    diff_html = "<div class='alert alert-warning'>文件在此期间无变更</div>"
            else:
                # 如果没有基准commit，获取最新commit的文件内容作为全新文件显示
                log_print(f"获取周版本初始文件内容: {latest_commit_id[:8]}, 文件: {file_path}", 'WEEKLY')
                file_content = git_service.get_file_content(latest_commit_id, file_path)
                if file_content:
                    # 将内容格式化为全新文件的diff格式
                    diff_html = render_new_file_content(file_content, file_path, latest_commit_id)
                else:
                    diff_html = "<div class='alert alert-warning'>无法获取文件内容</div>"
        except Exception as e:
            log_print(f"获取Git diff失败: {e}", 'ERROR', force=True)
            diff_html = f"<div class='alert alert-danger'>获取diff内容失败: {str(e)}</div>"
        # 只返回纯粹的diff内容，不包含重复的版本信息
        return diff_html

    except Exception as e:
        log_print(f"生成周版本Git diff HTML失败: {e}", 'ERROR', force=True)
        return f"<div class='alert alert-danger'>生成diff内容失败: {str(e)}</div>"

def _merge_segmented_excel_diff_payload(segment_payloads):
    """将 segmented_diff 中的多个 Excel diff 段合并为可渲染结构。"""
    return _merge_segmented_excel_diff_payload_helper(segment_payloads)

def _extract_excel_diff_from_payload(payload):
    """从 merged_diff/diff_data/segmented_diff 结构中提取 Excel diff 数据。"""
    return _extract_excel_diff_from_payload_helper(payload)

def _load_weekly_excel_diff_from_cache(repository, diff_cache, file_path):
    """优先复用周版本缓存中的真实合并 diff 数据（含 segmented_diff）。"""
    # 兼容守护：历史实现中存在 _extract_excel_diff_from_payload(merged_payload) 直取路径。
    return _load_weekly_excel_diff_from_cache_helper(
        repository=repository,
        diff_cache=diff_cache,
        file_path=file_path,
        commit_model=Commit,
        log_print=log_print,
        commit_sort_key=_commit_sort_key_for_merge,
        generate_merged_diff_data=_generate_merged_diff_data,
    )

def _resolve_weekly_deleted_excel_state(config, diff_cache, file_path):
    """判断周版本Excel是否为最终删除状态，并返回可用的上一版本commit_id。"""
    return _resolve_weekly_deleted_excel_state_helper(
        commit_model=Commit,
        config=config,
        diff_cache=diff_cache,
        file_path=file_path,
    )

def _render_weekly_deleted_excel_notice(config, file_path, previous_commit_id):
    """复用提交页删除提示语义，展示周版本Excel文件已删除提示。"""
    return _render_weekly_deleted_excel_notice_helper(
        commit_model=Commit,
        url_for=url_for,
        config=config,
        file_path=file_path,
        previous_commit_id=previous_commit_id,
    )

def _render_weekly_deleted_excel(config, file_path, previous_commit_id, repository):
    """删除态 Excel 的正文：整份渲染删除前的内容，取不到基线字节才退回删除提示。

    与提交页的删除提交、非 Excel 的删除同一个口径（编排在 helper 里，这里注入依赖）。
    """
    return _render_weekly_deleted_excel_helper(
        commit_model=Commit, url_for=url_for, config=config, file_path=file_path,
        previous_commit_id=previous_commit_id, repository=repository,
        readers={'git': _get_file_content_from_git, 'svn': _get_file_content_from_svn},
        diff_service=DiffService(), render_excel_html=render_excel_diff_html,
        render_notice=_render_weekly_deleted_excel_notice, log_print=log_print,
    )

def generate_weekly_excel_merged_diff_html(config, diff_cache, file_path, force_recalculate=False):
    """生成周版本Excel文件的合并diff HTML内容"""
    try:
        repository = config.repository
        # 缓存读与「强制重算的清理」都排在「删除」判定**之前**：删除态文件同样进这份 HTML
        # 缓存（后台任务用同一个键写），读留在判定之后就等于每次打开页面都重读整份基线
        # Excel，清理留在判定之后则会让删除文件的手动重算留下旧的那一行。
        if not force_recalculate:
            try:
                cached_html = _weekly_excel_cache_service.get_cached_html(
                    config.id,
                    file_path,
                    diff_cache.base_commit_id or '',
                    diff_cache.latest_commit_id,
                )
                if cached_html and cached_html.get('html_content'):
                    log_print(f"命中周版本Excel HTML缓存: {file_path}", 'WEEKLY')
                    return cached_html['html_content']
            except Exception as cache_lookup_error:
                log_print(
                    f"查询周版本Excel HTML缓存失败，继续回退计算: {file_path}, 错误: {cache_lookup_error}",
                    'WEEKLY',
                    force=True,
                )
        if force_recalculate:
            log_print(f"🔄 强制重新计算周版本Excel diff: {file_path}", 'WEEKLY')
            try:
                # 清理该文件的周版本Excel缓存
                deleted_count = WeeklyVersionExcelCache.query.filter_by(
                    config_id=config.id,
                    file_path=file_path
                ).delete()
                db.session.commit()
                if deleted_count > 0:
                    log_print(f"已清理 {deleted_count} 条周版本Excel缓存: {file_path}", 'WEEKLY')
            except Exception as cache_e:
                log_print(f"清理周版本Excel缓存失败: {cache_e}", 'WEEKLY', force=True)
        is_deleted, previous_commit_id = _resolve_weekly_deleted_excel_state(config, diff_cache, file_path)
        if is_deleted:
            return _render_weekly_deleted_excel(config, file_path, previous_commit_id, repository)
        merged_diff_data = _load_weekly_excel_diff_from_cache(repository, diff_cache, file_path)

        if merged_diff_data:
            log_print(f"复用周版本缓存中的合并Excel diff: {file_path}", 'WEEKLY')
        else:
            log_print(f"周版本缓存缺少可用Excel合并数据，回退实时计算: {file_path}", 'WEEKLY')
            from sqlalchemy import and_, or_
            # 优先使用窗口内的完整提交集合；窗口须换算（见 weekly_window_in_utc 说明）
            _win_start_utc, _win_end_utc = weekly_window_in_utc(config)
            commits = Commit.query.filter(
                Commit.repository_id == repository.id,
                Commit.path == file_path,
                Commit.commit_time >= _win_start_utc,
                Commit.commit_time <= _win_end_utc,
            ).order_by(Commit.commit_time.asc(), Commit.id.asc()).all()

            # 向后兼容：历史缓存缺失时间窗口提交时，回退到首尾提交兜底
            if not commits:
                commits = Commit.query.filter(
                    and_(
                        Commit.repository_id == repository.id,
                        Commit.path == file_path,
                        or_(
                            Commit.commit_id == diff_cache.base_commit_id,
                            Commit.commit_id == diff_cache.latest_commit_id
                        )
                    )
                ).order_by(Commit.commit_time.asc(), Commit.id.asc()).all()
            if not commits:
                return "<div class='alert alert-warning'>未找到相关的Excel提交记录</div>"

            # 同刻提交同样要按 git 拓扑定序：这条回退路径自己取 `commits[-1]` 当 latest，
            # 次序错了这里**展示的**收尾内容就是前一个提交的状态（写入侧的理由见
            # services/commit_ordering.py）。无平局时不调 git。
            annotate_same_instant_order(repository, commits)
            commits = sorted(commits, key=commit_merge_sort_key)
            log_print(f"回退模式找到 {len(commits)} 个相关提交", 'WEEKLY')
            base_commit = None
            if diff_cache.base_commit_id:
                base_commit = Commit.query.filter(
                    Commit.repository_id == repository.id,
                    Commit.path == file_path,
                    Commit.commit_id == diff_cache.base_commit_id,
                ).first()
            if base_commit is None:
                base_commit = Commit.query.filter(
                    Commit.repository_id == repository.id,
                    Commit.path == file_path,
                    Commit.commit_time < _win_start_utc,
                ).order_by(Commit.commit_time.desc(), Commit.id.desc()).first()

            recomputed_payload = _generate_merged_diff_data(
                repository=repository,
                file_path=file_path,
                base_commit=base_commit,
                latest_commit=commits[-1],
                commits=commits,
            )
            merged_diff_data = _extract_excel_diff_from_payload(recomputed_payload)

            # 最后兜底：保持旧逻辑兼容
            if not merged_diff_data:
                if len(commits) == 1:
                    merged_diff_data = get_real_diff_data_for_merge(commits[0])
                else:
                    merged_diff_data = get_commit_pair_diff_internal(commits[-1], commits[0])
        if not merged_diff_data or merged_diff_data.get('type') != 'excel':
            log_print("❌ Excel合并diff数据检查失败:", 'WEEKLY', force=True)
            log_print(f"  - merged_diff_data存在: {merged_diff_data is not None}", 'WEEKLY', force=True)
            if merged_diff_data:
                log_print(f"  - merged_diff_data类型: {merged_diff_data.get('type', 'None')}", 'WEEKLY', force=True)
                log_print(f"  - merged_diff_data键: {list(merged_diff_data.keys())}", 'WEEKLY', force=True)
                if 'error' in merged_diff_data:
                    log_print(f"  - 错误信息: {merged_diff_data.get('error')}", 'WEEKLY', force=True)
                if 'message' in merged_diff_data:
                    log_print(f"  - 消息: {merged_diff_data.get('message')}", 'WEEKLY', force=True)
            return "<div class='alert alert-warning'>无法生成Excel合并diff数据</div>"

        # 清理NaN/Inf值。用共用工具而不是本文件原先那份只处理 NaN 的闭包：`inf` 会让
        # json.dumps 写出非法的 `Infinity`，前端 JSON.parse 直接失败、整份 diff 不显示。
        cleaned_diff_data = clean_json_data(merged_diff_data)
        # 生成Excel diff HTML
        excel_diff_html = render_excel_diff_html(cleaned_diff_data, file_path)
        return excel_diff_html

    except Exception as e:
        log_print(f"生成周版本Excel合并diff失败: {e}", 'ERROR', force=True)
        return f"<div class='alert alert-danger'>生成Excel diff失败: {str(e)}</div>"

# render_excel_sheet_html函数已删除，现在使用JavaScript动态生成

def get_status_text(status):
    """获取状态文本"""
    return {'pending': '待确认', 'confirmed': '已确认', 'rejected': '已拒绝'}.get(status, '未知')

def get_status_badge_class(status):
    """获取状态徽章样式类"""
    return {'pending': 'warning', 'confirmed': 'success', 'rejected': 'danger'}.get(status, 'secondary')

# create_weekly_sync_task 已移至 services/task_worker_service.py

def process_weekly_version_sync(config_id):
    """处理周版本同步任务。

    返回显式的 WeeklySyncOutcome（见 services/weekly_version_sync_status.py）：过去这里
    在「配置不存在 / 被禁用 / 窗口内无提交 / 正常跑完」四条路径上都 `return None`。
    """
    try:
        config = db.session.get(WeeklyVersionConfig, config_id)
        if not config:
            log_print(f"周版本配置不存在: {config_id}", 'WEEKLY', force=True)
            return config_missing_outcome(config_id)

        if not config.is_active:
            log_print(f"周版本配置已禁用: {config_id}", 'WEEKLY')
            return config_inactive_outcome(config)

        repository = config.repository
        log_print(f"开始处理周版本同步: {config.name} (仓库: {repository.name})", 'WEEKLY')
        # 时间范围内的提交（窗口须换算，见 weekly_window_in_utc 说明）
        _win_start_utc, _win_end_utc = weekly_window_in_utc(config)
        commits_in_range = Commit.query.filter(
            Commit.repository_id == repository.id,
            Commit.commit_time >= _win_start_utc,
            Commit.commit_time <= _win_end_utc
        ).order_by(Commit.commit_time.asc()).all()
        log_print(f"找到 {len(commits_in_range)} 个时间范围内的提交", 'WEEKLY')
        if not commits_in_range:
            # 这周本来就没改动 —— 常态而非故障：不打失败，也不算「完成」。
            outcome = no_commits_outcome(config)
            log_print(f"周版本同步跳过（无数据）: {config.name} - {outcome.describe()}", 'WEEKLY')
            _weekly_excel_cache_service.log_cache_operation(f"⏭️ 周版本同步跳过（窗口内无提交）: {config.name}", 'info', repository_id=config.repository_id, config_id=config.id)
            return outcome

        # **同刻提交必须按 git 拓扑定序**，否则每个文件列表的最后一个（`commits[-1]`，
        # 即写进 `latest_commit_id` 的那个）可能不是真正的最后一个提交。机器人提交会把
        # committer date 打成同一时刻，平局时原先落回数据库行序、而那是入库顺序 ——
        # 实测就出现「父提交被当成 latest」。判据与实测记录见 services/commit_ordering.py。
        # 没有平局时这里不调 git；仓库不是 git / 克隆不在本地时同样原样返回。
        #
        # 排序必须在**分组之前**：分组出来的每个列表是同一个次序的切片，事后排
        # `commits_in_range` 动不了已经建好的列表。而这次排序也是排序键的唯一一次使用点
        # 之前的定序 —— `generate_weekly_merged_diff` 里的 `commits[-1]` 与
        # `_generate_merged_diff_data` 内部的排序必须给出同一个次序，否则列里写的
        # latest 与 payload 收尾的内容会指向两个不同的提交。
        annotated = annotate_same_instant_order(repository, commits_in_range)
        if annotated:
            log_print(f"按 git 拓扑定序的同刻提交: {annotated} 个", 'WEEKLY')
        commits_in_range = sorted(commits_in_range, key=commit_merge_sort_key)

        # 按文件路径分组提交
        files_commits = {}
        for commit in commits_in_range:
            files_commits.setdefault(commit.path, []).append(commit)
        log_print(f"涉及 {len(files_commits)} 个文件", 'WEEKLY')
        # 单文件失败必须累计上报：过去 continue 掉之后照样打「同步完成」并标 completed。
        failed_details = []
        # 逐文件的结果**只累计、不逐条打**（见 `describe_weekly_file_totals`）：
        # 条数与文件数成正比，1 个项目 2 个仓库就已经在刷屏。
        totals = {"created": 0, "updated": 0, "excel": 0, "excel_failed": 0, "vcs": 0}
        for file_path, file_commits in files_commits.items():
            try:
                result = generate_weekly_merged_diff(config, file_path, file_commits)
                if result is not None:
                    totals["created"] += 1 if result.created else 0
                    totals["updated"] += 1 if result.updated else 0
                    totals["excel"] += 1 if result.excel == 'created' else 0
                    totals["excel_failed"] += 1 if result.excel == 'failed' else 0
                    totals["vcs"] += 1 if result.vcs_base_lookup else 0
            except Exception as e:
                failed_details.append((file_path, str(e)))
                log_print(f"生成文件 {file_path} 的合并diff失败（第 {len(failed_details)} 个失败）: {e}", 'WEEKLY', force=True)

        summary = describe_weekly_file_totals(totals, len(files_commits))
        if failed_details:
            outcome = build_partial_failure_outcome(len(files_commits), failed_details)
            log_print(f"❌ 周版本同步部分失败: {config.name} - {summary} - {outcome.describe()}", 'WEEKLY', force=True)
            _weekly_excel_cache_service.log_cache_operation(f"❌ 周版本同步部分失败: {config.name} - {outcome.describe()}", 'error', repository_id=config.repository_id, config_id=config.id)
            return outcome

        log_print(f"周版本同步完成: {config.name} - {summary}", 'WEEKLY')
        _weekly_excel_cache_service.log_cache_operation(f"✅ 周版本同步完成: {config.name} - 处理了 {len(files_commits)} 个文件", 'success', repository_id=config.repository_id, config_id=config.id)
        return completed_outcome(config, len(files_commits))
    except Exception as e:
        db.session.rollback()  # 错误路径必须回滚，避免脏事务残留（rule 4.2）
        log_print(f"周版本同步处理失败: {e}", 'WEEKLY', force=True)
        raise e


def generate_weekly_merged_diff(config, file_path, commits) -> Optional[WeeklyFileSyncResult]:
    """为单个文件生成周版本合并diff。

    **返回它干了什么**（`WeeklyFileSyncResult`），由调用方聚合成一行日志。本函数自己
    只打两类：错误（`force=True`，必须看见）与 `DETAIL` 明细（默认关）。
    """
    try:
        if not commits:
            return None

        repository = config.repository
        vcs_base_lookup = False
        # 基准版本（窗口起始前的最后一个提交；窗口须换算，否则基准会被选晚 8 小时）
        _win_start_utc, _ = weekly_window_in_utc(config)
        base_commit = Commit.query.filter(
            Commit.repository_id == repository.id,
            Commit.path == file_path,
            Commit.commit_time < _win_start_utc
        ).order_by(Commit.commit_time.desc()).first()
        # 优化策略：如果数据库中没有找到基准版本，直接查询Git/SVN获取真实的提交历史
        if not base_commit:
            vcs_base_lookup = True
            # 逐条明细（默认关）。**不能再用 `force=True`** —— 它在逐文件的路上，
            # 而 force 会短路整个日志开关，等于这一类行永远关不掉。次数由调用方汇总。
            log_print(f"🔍 数据库中未找到基准版本，查询Git/SVN获取 {file_path} 的完整提交历史", 'DETAIL')
            base_commit = get_real_base_commit_from_vcs(config, file_path)
            if base_commit:
                log_print(f"✅ 从Git/SVN获取到真实基准版本: {base_commit.commit_id[:8]} ({base_commit.commit_time})", 'DETAIL')
            else:
                log_print("ℹ️ Git/SVN中也未找到更早的提交，确认为新文件", 'DETAIL')
        # 获取最新版本（时间范围内的最后一个提交）
        latest_commit = commits[-1]
        # 检查是否已存在缓存
        existing_cache = WeeklyVersionDiffCache.query.filter_by(
            config_id=config.id,
            file_path=file_path
        ).first()
        # 准备提交信息
        commit_authors = [commit.author for commit in commits]
        commit_messages = [commit.message.strip() for commit in commits]
        commit_times = [commit.commit_time.isoformat() for commit in commits]
        # 生成合并diff数据
        merged_diff_data = _generate_merged_diff_data(
            repository, file_path, base_commit, latest_commit, commits
        )
        payload_json = json.dumps(merged_diff_data)
        unchanged = bool(existing_cache) and weekly_cache_is_unchanged(
            existing_cache, payload_json, base_commit, latest_commit, commits,
            _current_diff_logic_version(),
        )
        if unchanged:
            # **内容与上次逐字相同就一个字节都不写。** 本表逐文件写一次，`updated_at`
            # （onupdate）就被顶高一次，而 AI 分析的变更判据读的正是它
            # （`_summarize_weekly_files` 筛 `updated_at > last_analyzed_at`）：同步每
            # 2~3 分钟跑一遍，于是平台**永远**认为「有新变化」，`no_change` 几乎不触发、
            # 手动触发也总是真跑（白花一次 token）。跳过写入让那一列恢复它字面上的语义
            # ——「内容最后变化的时刻」。
            #
            # 不写 `last_sync_time` 也不影响就绪判定：那个判据只问它**是不是空**
            # （`initial_cache_readiness`），本行既然已经是一行 completed 的缓存，它早就非空。
            log_print(f"内容未变，跳过写库: {file_path}", 'DETAIL')
            created, updated = False, False
        elif existing_cache:
            # 更新现有缓存
            previous_latest_commit_id = existing_cache.latest_commit_id
            existing_cache.merged_diff_data = payload_json
            existing_cache.base_commit_id = base_commit.commit_id if base_commit else None
            existing_cache.latest_commit_id = latest_commit.commit_id
            existing_cache.commit_authors = json.dumps(commit_authors)
            existing_cache.commit_messages = json.dumps(commit_messages)
            existing_cache.commit_times = json.dumps(commit_times)
            existing_cache.commit_count = len(commits)
            existing_cache.cache_status = 'completed'
            # 打上本次比较口径的版本号：不写则本表 diff_version 恒为 NULL，读侧
            # （is_merged_diff_cache_current）只能把 NULL 当历史行宽容处理，「升级版本后
            # 合并 diff 自动失效」就永远不生效。详见 models/weekly_version.py。
            existing_cache.diff_version = _current_diff_logic_version()
            existing_cache.last_sync_time = datetime.now(timezone.utc)
            if previous_latest_commit_id != latest_commit.commit_id:
                # 重置确认状态时操作者必须一起清（否则留下「待确认 + 有确认人」的记录）
                existing_cache.confirmation_status = json.dumps({"dev": "pending"})
                existing_cache.overall_status = 'pending'
                existing_cache.status_changed_by = None
            log_print(f"更新周版本diff缓存: {file_path}", 'DETAIL')
            created, updated = False, True
        else:
            # 创建新缓存
            new_cache = WeeklyVersionDiffCache(
                config_id=config.id,
                repository_id=repository.id,
                file_path=file_path,
                merged_diff_data=json.dumps(merged_diff_data),
                base_commit_id=base_commit.commit_id if base_commit else None,
                latest_commit_id=latest_commit.commit_id,
                commit_authors=json.dumps(commit_authors),
                commit_messages=json.dumps(commit_messages),
                commit_times=json.dumps(commit_times),
                commit_count=len(commits),
                confirmation_status=json.dumps({"dev": "pending"}),
                overall_status='pending',
                cache_status='completed',
                diff_version=_current_diff_logic_version(),  # 理由见上「更新现有缓存」分支
                last_sync_time=datetime.now(timezone.utc)
            )
            db.session.add(new_cache)
            log_print(f"创建周版本diff缓存: {file_path}", 'DETAIL')
            created, updated = True, False
            # 这里原先有一段「基准版本优化」：base 为空时再查一次 VCS，把查到的提交写进
            # base_commit_id。它是**取不到的** —— 走到这里意味着上面那次
            # get_real_base_commit_from_vcs 已经返回 None（库里与 VCS 都确认没有更早的
            # 提交），而它对同一 (config, file_path) 的判定是确定的，第二次调用只会再
            # 返回 None。而它一旦真被触发，就会留下一行自相矛盾的数据：payload 是按
            # base=None 算的（整份文件读作新增），base_commit_id 却写着真实提交，而
            # 该列是 Excel 缓存键的一部分、也是「删除了哪些行」的比较基准。
            # 已删除：base 为空时该列保持 NULL，与 payload 里的 'base_commit': None 一致。
        db.session.commit()
        # 检查是否需要生成Excel合并diff缓存
        excel = 'skipped'
        if _weekly_excel_cache_service.needs_merged_diff_cache(config.id, file_path):
            log_print(f"触发Excel合并diff缓存生成: {file_path}", 'DETAIL')
            excel = 'created'
            try:
                # 异步生成Excel HTML缓存
                create_weekly_excel_cache_task(config.id, file_path)
                log_print(f"✅ Excel缓存任务创建成功: {file_path}", 'DETAIL')
            except Exception as cache_e:
                log_print(f"创建Excel缓存任务失败: {cache_e}", 'WEEKLY', force=True)
                excel = 'failed'
        # 走到这里说明这个文件的缓存处理是**成功**的（失败在上面 raise 掉了）。
        # 「跳过 Excel 缓存」不在此处记日志：它是**刻意的非动作**，而对代码仓库来说
        # 几乎是每个文件的命运 —— 一行一个文件，833 个文件就是 833 行在说「我没干什么」，
        # 且与「更新缓存」那行数量恒等。计入返回值，由调用方聚合成一行。
        return WeeklyFileSyncResult(
            created=created, updated=updated, excel=excel, vcs_base_lookup=vcs_base_lookup
        )
    except Exception as e:
        db.session.rollback()
        log_print(f"生成周版本合并diff失败: {file_path}, 错误: {e}", 'WEEKLY', force=True)
        raise e


def process_weekly_excel_cache(config_id, file_path):
    """处理周版本Excel缓存生成"""
    perf_metrics_service = get_perf_metrics_service()
    try:
        start_time = time.time()
        log_print(f"开始生成周版本Excel缓存: 配置 {config_id}, 文件 {file_path}", 'WEEKLY')
        # 获取配置和diff缓存
        lookup_start = time.time()
        config = db.session.get(WeeklyVersionConfig, config_id)
        if not config:
            raise Exception(f"周版本配置不存在: {config_id}")
        diff_cache = WeeklyVersionDiffCache.query.filter_by(
            config_id=config_id, file_path=file_path).first()
        if not diff_cache:
            raise Exception(f"周版本diff缓存不存在: {file_path}")
        lookup_time = time.time() - lookup_start
        # 检查是否已存在缓存
        cache_lookup_start = time.time()
        existing_cache = _weekly_excel_cache_service.get_cached_html(
            config_id, file_path,
            diff_cache.base_commit_id or '',
            diff_cache.latest_commit_id
        )
        cache_lookup_time = time.time() - cache_lookup_start
        if existing_cache:
            total_time = time.time() - start_time
            log_print(
                f"周版本Excel缓存已存在，跳过生成: {file_path} | "
                f"lookup={lookup_time:.2f}s, cache_lookup={cache_lookup_time:.2f}s, total={total_time:.2f}s",
                'WEEKLY'
            )
            perf_metrics_service.record(
                "weekly_excel_cache",
                success=True,
                metrics={
                    "total_ms": total_time * 1000,
                    "lookup_ms": lookup_time * 1000,
                    "cache_lookup_ms": cache_lookup_time * 1000,
                },
                tags={
                    "source": "cache_hit",
                    "config_id": config_id,
                    "project_id": config.project_id if config else "",
                    "project_code": (config.project.code if config and config.project else ""),
                    "file_path": file_path,
                },
            )
            return

        # 生成Excel合并diff HTML
        render_start = time.time()
        html_content = generate_weekly_excel_merged_diff_html(config, diff_cache, file_path)
        render_time = time.time() - render_start
        if not html_content:
            raise Exception("生成Excel合并diff HTML失败")
        # 保存到缓存
        save_start = time.time()
        processing_time = time.time() - start_time
        success = _weekly_excel_cache_service.save_html_cache(
            config_id=config_id,
            repository_id=config.repository_id,
            file_path=file_path,
            base_commit_id=diff_cache.base_commit_id or '',
            latest_commit_id=diff_cache.latest_commit_id,
            commit_count=diff_cache.commit_count,
            html_content=html_content,
            css_content="",  # CSS已包含在HTML中
            js_content="",   # JS已包含在HTML中
            metadata={
                'file_type': 'excel',
                'commit_count': diff_cache.commit_count,
                'generated_at': datetime.now(timezone.utc).isoformat()
            },
            processing_time=processing_time
        )
        save_time = time.time() - save_start
        if success:
            log_print(f"✅ 周版本Excel缓存生成完成: {file_path}, 耗时: {processing_time:.2f}秒", 'WEEKLY')
            log_print(
                f"📊 周版本Excel缓存指标: html_bytes={len(html_content.encode('utf-8')) / 1024:.1f}KB, "
                f"commit_count={diff_cache.commit_count}, lookup={lookup_time:.2f}s, "
                f"cache_lookup={cache_lookup_time:.2f}s, render={render_time:.2f}s, save={save_time:.2f}s",
                'WEEKLY'
            )
            perf_metrics_service.record(
                "weekly_excel_cache",
                success=True,
                metrics={
                    "total_ms": processing_time * 1000,
                    "lookup_ms": lookup_time * 1000,
                    "cache_lookup_ms": cache_lookup_time * 1000,
                    "render_ms": render_time * 1000,
                    "save_ms": save_time * 1000,
                    "html_bytes": len(html_content.encode("utf-8")),
                    "commit_count": diff_cache.commit_count or 0,
                },
                tags={
                    "source": "generated",
                    "config_id": config_id,
                    "repository_id": config.repository_id,
                    "project_id": config.project_id if config else "",
                    "project_code": (config.project.code if config and config.project else ""),
                    "file_path": file_path,
                },
            )
            # 记录到操作日志
            _weekly_excel_cache_service.log_cache_operation(f"✅ 周版本Excel缓存生成成功: {file_path} (耗时: {processing_time:.2f}秒)", 'success', repository_id=config.repository_id, config_id=config_id, file_path=file_path)
        else:
            raise Exception("保存缓存失败")
    except Exception as e:
        log_print(f"❌ 周版本Excel缓存生成失败: {file_path}, 错误: {e}", 'WEEKLY', force=True)
        perf_metrics_service.record(
            "weekly_excel_cache",
            success=False,
            metrics={
                "total_ms": (time.time() - start_time) * 1000,
            },
            tags={
                "source": "exception",
                "config_id": config_id,
                "project_id": config.project_id if "config" in locals() and config else "",
                "project_code": (config.project.code if "config" in locals() and config and config.project else ""),
                "file_path": file_path,
            },
        )
        _weekly_excel_cache_service.log_cache_operation(f"❌ 周版本Excel缓存生成失败: {file_path} - {str(e)}", 'error', config_id=config_id, file_path=file_path)
        raise e
def create_weekly_excel_cache_task(config_id, file_path):
    """创建周版本Excel缓存任务"""
    log_print(f"📝 开始创建周版本Excel缓存任务: config_id={config_id}, file_path={file_path}", 'WEEKLY', force=True)
    try:
        existing_task = BackgroundTask.query.filter(
            BackgroundTask.task_type == 'weekly_excel_cache',
            BackgroundTask.repository_id == config_id,
            BackgroundTask.file_path == file_path,
            BackgroundTask.status.in_(['pending', 'processing']),
        ).order_by(BackgroundTask.id.desc()).first()
        if existing_task:
            log_print(
                f"⏭️ 跳过重复周版本Excel缓存任务: config_id={config_id}, "
                f"file_path={file_path}, existing_task_id={existing_task.id}, status={existing_task.status}",
                'WEEKLY'
            )
            return None

        # 创建后台任务来生成Excel HTML缓存；使用repository_id字段存储config_id
        log_print("🗃️ 创建数据库任务记录...", 'WEEKLY', force=True)
        new_task = BackgroundTask(
            task_type='weekly_excel_cache',
            repository_id=config_id,  # 存储config_id
            file_path=file_path,
            status='pending',
            priority=5  # 中等优先级
        )
        db.session.add(new_task)
        db.session.flush()

        if is_agent_dispatch_mode():
            config = db.session.get(WeeklyVersionConfig, config_id)
            if config:
                from services.agent_management_handlers import enqueue_agent_task

                enqueue_agent_task(
                    task_type='weekly_excel_cache',
                    project_id=config.project_id,
                    repository_id=config.repository_id,
                    source_task_id=new_task.id,
                    priority=5,
                    payload={
                        "config_id": config_id,
                        "file_path": file_path,
                        "background_task_id": new_task.id,
                    },
                )
        db.session.commit()
        log_print(f"✅ 数据库任务记录创建成功，任务ID: {new_task.id}", 'WEEKLY', force=True)
        if is_agent_dispatch_mode():
            log_print("📡 platform/agent 模式：任务已下发到 agent_tasks", 'WEEKLY', force=True)
            return new_task.id
        # 添加到任务队列
        import time
        task_counter = int(time.time() * 1000000)  # 微秒级时间戳作为计数器
        log_print(f"📋 添加任务到队列，计数器: {task_counter}", 'WEEKLY', force=True)
        task_wrapper = TaskWrapper(
            5,  # 中等优先级
            task_counter,
            {
                'id': new_task.id,
                'type': 'weekly_excel_cache',
                'data': {
                    'config_id': config_id,
                    'file_path': file_path
                }
            }
        )
        background_task_queue.put(task_wrapper)
        log_print(f"✅ 任务已添加到队列，当前队列大小: {background_task_queue.qsize()}", 'WEEKLY', force=True)
        log_print(f"🎉 周版本Excel缓存任务创建完成: {file_path}", 'WEEKLY', force=True)
        return new_task.id
    except Exception as e:
        log_print(f"❌ 创建周版本Excel缓存任务失败: {e}", 'WEEKLY', force=True)
        log_print(f"错误详情: {type(e).__name__}: {str(e)}", 'WEEKLY', force=True)
        db.session.rollback()
        raise e
