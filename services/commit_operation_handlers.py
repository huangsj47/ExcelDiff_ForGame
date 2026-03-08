"""Commit operation handlers extracted from app.py."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime

from flask import abort, flash, jsonify, redirect, render_template, request, url_for
from sqlalchemy import func, or_
from sqlalchemy.exc import SQLAlchemyError

from services.commit_diff_input_models import CommitDiffQueryInput, MergeDiffRefreshInput
from models import Commit, DiffCache, ExcelHtmlCache, db
from services.api_response_service import json_error, json_success
from services.agent_commit_diff_dispatch import dispatch_or_get_commit_diff, is_agent_dispatch_mode
from services.model_loader import get_runtime_model
from utils.request_security import (
    _has_project_access,
    can_current_user_operate_project_confirmation,
    require_admin,
)
from utils.timezone_utils import format_beijing_time


class _RuntimeProxy:
    def __init__(self, name: str):
        self._name = name

    def _target(self):
        return get_runtime_model(self._name)

    def __getattr__(self, item):
        return getattr(self._target(), item)

    def __call__(self, *args, **kwargs):
        return self._target()(*args, **kwargs)


log_print = _RuntimeProxy("log_print")
excel_cache_service = _RuntimeProxy("excel_cache_service")
add_excel_diff_task = _RuntimeProxy("add_excel_diff_task")
background_task_queue = _RuntimeProxy("background_task_queue")
get_unified_diff_data = _RuntimeProxy("get_unified_diff_data")
get_merged_diff_data = _RuntimeProxy("get_merged_diff_data")
build_smart_display_list = _RuntimeProxy("build_smart_display_list")
resolve_previous_commit = _RuntimeProxy("resolve_previous_commit")


_AUTHOR_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})")


def _extract_author_lookup_keys(raw_author):
    """提取作者可匹配的账号键（用户名、邮箱前缀）。"""
    text = str(raw_author or "").strip()
    if not text:
        return []

    keys = []
    lower_text = text.lower()
    if all(symbol not in lower_text for symbol in ("@", "<", ">", " ")):
        keys.append(lower_text)

    if "@" in lower_text and "<" not in lower_text and ">" not in lower_text:
        email_prefix = lower_text.split("@", 1)[0].strip()
        if email_prefix and email_prefix not in keys:
            keys.append(email_prefix)

    for email in _AUTHOR_EMAIL_RE.findall(text):
        email_prefix = email.lower().split("@", 1)[0].strip()
        if email_prefix and email_prefix not in keys:
            keys.append(email_prefix)
    return keys


def _get_auth_user_models():
    """按优先级返回可用账号模型（当前后端优先，另一套作为兜底）。"""
    backend = None
    try:
        from auth import get_auth_backend

        backend = (get_auth_backend() or "").strip().lower()
    except Exception:
        backend = None

    source_order = ["qkit", "local"] if backend == "qkit" else ["local", "qkit"]
    models = []
    seen_tables = set()

    for source in source_order:
        try:
            if source == "qkit":
                from qkit_auth.models import QkitAuthUser as user_model
            else:
                from auth.models import AuthUser as user_model
        except Exception:
            continue

        table_name = str(getattr(user_model, "__tablename__", "") or "")
        if table_name and table_name in seen_tables:
            continue
        if table_name:
            seen_tables.add(table_name)
        models.append(user_model)

    return models


def _resolve_author_display(raw_author, username_to_display_name_lower, email_prefix_to_display_name):
    text = str(raw_author or "").strip()
    if not text:
        return ""
    for author_key in _extract_author_lookup_keys(text):
        mapped_name = (
            username_to_display_name_lower.get(author_key)
            or email_prefix_to_display_name.get(author_key)
        )
        if mapped_name:
            return mapped_name
    return text


def _attach_author_display(commits):
    if not commits:
        return
    user_models = _get_auth_user_models()
    if not user_models:
        for commit in commits:
            commit.author_display = str(commit.author or "").strip() or "未知"
        return

    author_keys = set()
    for commit in commits:
        author_keys.update(_extract_author_lookup_keys(getattr(commit, "author", "")))
    if not author_keys:
        for commit in commits:
            commit.author_display = str(commit.author or "").strip() or "未知"
        return

    username_to_display_name_lower = {}
    email_prefix_to_display_name = {}
    for user_model in user_models:
        try:
            conditions = [
                func.lower(user_model.username).in_(list(author_keys)),
            ]
            conditions.extend(
                func.lower(user_model.email).like(f"{author_key}@%")
                for author_key in author_keys
                if author_key
            )
            users = user_model.query.filter(or_(*conditions)).all() if conditions else []
            for user in users:
                username = (getattr(user, "username", "") or "").strip()
                if not username:
                    continue
                display_name = (getattr(user, "display_name", "") or "").strip() or username
                username_key = username.lower()
                if username_key not in username_to_display_name_lower:
                    username_to_display_name_lower[username_key] = display_name
                email = (getattr(user, "email", "") or "").strip().lower()
                if email and "@" in email:
                    email_prefix = email.split("@", 1)[0]
                    if email_prefix not in email_prefix_to_display_name:
                        email_prefix_to_display_name[email_prefix] = display_name
        except Exception as exc:
            log_print(f"加载作者姓名映射失败({getattr(user_model, '__tablename__', 'unknown')}): {exc}", "APP")

    for commit in commits:
        commit.author_display = _resolve_author_display(
            getattr(commit, "author", ""),
            username_to_display_name_lower,
            email_prefix_to_display_name,
        ) or (str(getattr(commit, "author", "")).strip() or "未知")


def _commit_project_id(commit):
    repository = getattr(commit, "repository", None)
    return getattr(repository, "project_id", None)


def _ensure_commit_project_access(commit):
    project_id = _commit_project_id(commit)
    if not project_id:
        return False, "提交关联项目不存在"
    if not _has_project_access(project_id):
        return False, "当前账号无权访问该项目"
    return True, ""


def approve_all_files(commit_id):
    """批量确认提交的所有文件"""
    try:
        commit = Commit.query.get_or_404(commit_id)
        project_id = commit.repository.project_id if commit.repository else None
        allowed, message = can_current_user_operate_project_confirmation(project_id, "confirm")
        if not allowed:
            return jsonify({'status': 'error', 'message': message}), 403
        log_print(f"批量确认: 当前提交ID={commit.id}, commit_id={commit.commit_id}, repository_id={commit.repository_id}", 'INFO')
        # 获取同一次提交的所有文件（通过commit_id匹配）
        related_commits = Commit.query.filter_by(
            repository_id=commit.repository_id,
            commit_id=commit.commit_id
        ).all()
        log_print(f"找到 {len(related_commits)} 个相关提交:")
        for rc in related_commits:
            log_print(f"  - ID={rc.id}, path={rc.path}, 当前状态={rc.status}", 'INFO')
        # 将所有相关提交状态设为已确认
        updated_count = 0
        for related_commit in related_commits:
            if related_commit.status != 'confirmed':
                related_commit.status = 'confirmed'
                updated_count += 1
                log_print(f"  更新提交 {related_commit.id} 状态为 confirmed", 'INFO')
        db.session.commit()
        log_print(f"批量确认完成，更新了 {updated_count} 个文件", 'INFO')
        return jsonify({
            'status': 'success', 
            'message': f'已确认 {len(related_commits)} 个文件 (更新了 {updated_count} 个)'
        })
    except SQLAlchemyError as e:
        db.session.rollback()
        log_print(f"批量确认数据库失败: {str(e)}")
        return jsonify({'status': 'error', 'message': '数据库操作失败，请稍后重试'}), 500
    except (TypeError, ValueError, RuntimeError) as e:
        log_print(f"批量确认失败: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

def batch_approve_commits():
    """批量通过选中的提交"""
    try:
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({'status': 'error', 'message': '请求体必须为JSON对象'}), 400
        commit_ids = data.get('commit_ids', [])
        if not commit_ids:
            return jsonify({'status': 'error', 'message': '未选择任何提交'}), 400

        from services.status_sync_service import StatusSyncService
        sync_service = StatusSyncService(db)
        updated_count = 0
        sync_results = []
        permission_cache = {}
        for commit_id in commit_ids:
            commit = db.session.get(Commit, commit_id)
            if not commit:
                continue
            project_id = commit.repository.project_id if commit.repository else None
            if project_id not in permission_cache:
                permission_cache[project_id] = can_current_user_operate_project_confirmation(project_id, "confirm")
            allowed, message = permission_cache[project_id]
            if not allowed:
                return jsonify({'status': 'error', 'message': message}), 403

            if commit.status != 'confirmed':
                old_status = commit.status
                commit.status = 'confirmed'
                updated_count += 1
                # 同步状态到周版本diff
                sync_result = sync_service.sync_commit_to_weekly(commit_id, 'confirmed')
                sync_results.append(sync_result)
        db.session.commit()
        # 统计同步结果
        total_weekly_updated = sum(r.get('updated_count', 0) for r in sync_results if r.get('success'))
        return jsonify({
            'status': 'success',
            'message': f'已通过 {updated_count} 个提交，同步更新了 {total_weekly_updated} 个周版本记录'
        })
    except SQLAlchemyError as e:
        db.session.rollback()
        log_print(f"批量通过数据库失败: {str(e)}")
        return jsonify({'status': 'error', 'message': '数据库操作失败，请稍后重试'}), 500
    except (TypeError, ValueError, RuntimeError) as e:
        log_print(f"批量通过失败: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

def batch_reject_commits():
    """批量拒绝选中的提交"""
    try:
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({'status': 'error', 'message': '请求体必须为JSON对象'}), 400
        commit_ids = data.get('commit_ids', [])
        if not commit_ids:
            return jsonify({'status': 'error', 'message': '未选择任何提交'}), 400

        from services.status_sync_service import StatusSyncService
        sync_service = StatusSyncService(db)
        updated_count = 0
        sync_results = []
        permission_cache = {}
        for commit_id in commit_ids:
            commit = db.session.get(Commit, commit_id)
            if not commit:
                continue
            project_id = commit.repository.project_id if commit.repository else None
            if project_id not in permission_cache:
                permission_cache[project_id] = can_current_user_operate_project_confirmation(project_id, "reject")
            allowed, message = permission_cache[project_id]
            if not allowed:
                return jsonify({'status': 'error', 'message': message}), 403

            if commit.status != 'rejected':
                old_status = commit.status
                commit.status = 'rejected'
                updated_count += 1
                # 同步状态到周版本diff
                sync_result = sync_service.sync_commit_to_weekly(commit_id, 'rejected')
                sync_results.append(sync_result)
        db.session.commit()
        # 统计同步结果
        total_weekly_updated = sum(r.get('updated_count', 0) for r in sync_results if r.get('success'))
        return jsonify({
            'status': 'success',
            'message': f'已拒绝 {updated_count} 个提交，同步更新了 {total_weekly_updated} 个周版本记录'
        })
    except SQLAlchemyError as e:
        db.session.rollback()
        log_print(f"批量拒绝数据库失败: {str(e)}")
        return jsonify({'status': 'error', 'message': '数据库操作失败，请稍后重试'}), 500
    except (TypeError, ValueError, RuntimeError) as e:
        log_print(f"批量拒绝失败: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

def reject_commit():
    """拒绝单个提交"""
    try:
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({'status': 'error', 'message': '请求体必须为JSON对象'}), 400
        commit_id = data.get('commit_id')
        if not commit_id:
            return jsonify({'status': 'error', 'message': '未指定提交ID'}), 400

        commit = db.session.get(Commit, commit_id)
        if not commit:
            return jsonify({'status': 'error', 'message': '提交不存在'}), 404
        project_id = commit.repository.project_id if commit.repository else None
        allowed, message = can_current_user_operate_project_confirmation(project_id, "reject")
        if not allowed:
            return jsonify({'status': 'error', 'message': message}), 403

        if commit.status != 'rejected':
            commit.status = 'rejected'
            db.session.commit()
            return jsonify({
                'status': 'success',
                'message': '提交已拒绝'
            })
        else:
            return jsonify({
                'status': 'error',
                'message': '提交已经是拒绝状态'
            })
    except SQLAlchemyError as e:
        db.session.rollback()
        log_print(f"拒绝提交数据库失败: {str(e)}")
        return jsonify({'status': 'error', 'message': '数据库操作失败，请稍后重试'}), 500
    except (TypeError, ValueError, RuntimeError) as e:
        log_print(f"拒绝提交失败: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

def request_priority_diff(commit_id):
    """请求优先处理指定提交的diff"""
    try:
        commit = Commit.query.get_or_404(commit_id)
        allowed, message = _ensure_commit_project_access(commit)
        if not allowed:
            return jsonify({'success': False, 'message': message}), 403
        repository = commit.repository
        # 检查是否为Excel文件
        if not excel_cache_service.is_excel_file(commit.path):
            return jsonify({
                'success': False, 
                'message': '该文件不是Excel文件，无需优先处理'
            })
        # 检查是否已有缓存
        cached_diff = excel_cache_service.get_cached_diff(
            repository.id, commit.commit_id, commit.path
        )
        if cached_diff:
            return jsonify({
                'success': True, 
                'message': '该文件已有缓存，无需重新处理',
                'cached': True
            })
        # 添加到高优先级队列
        add_excel_diff_task(repository.id, commit.commit_id, commit.path, priority=1)
        return jsonify({
            'success': True, 
            'message': f'已将 {commit.path} 添加到高优先级处理队列',
            'cached': False,
            'queue_size': background_task_queue.qsize()
        })
    except SQLAlchemyError as e:
        db.session.rollback()
        log_print(f"请求优先处理数据库失败: {e}", 'INFO')
        return jsonify({
            'success': False,
            'message': '请求失败: 数据库操作异常'
        }), 500
    except (TypeError, ValueError, RuntimeError) as e:
        log_print(f"请求优先处理失败: {e}", 'INFO')
        return jsonify({
            'success': False, 
            'message': f'请求失败: {str(e)}'
        })

def request_priority_diff_with_path(project_code, repository_name, commit_id):
    """请求优先处理指定提交的diff (带路径版本)"""
    return request_priority_diff(commit_id)

# 合并diff重新计算路由

def get_commit_diff_data(commit_id):
    """异步获取单个提交的diff数据。platform+agent 模式仅调度 Agent。"""
    try:
        start_time = time.time()
        commit = db.session.get(Commit, commit_id)
        if not commit:
            return json_error(
                jsonify=jsonify,
                message="提交不存在",
                error_type="commit_not_found",
                http_status=404,
                commit_id=commit_id,
            )
        allowed, message = _ensure_commit_project_access(commit)
        if not allowed:
            return json_error(
                jsonify=jsonify,
                message=message,
                error_type="forbidden",
                http_status=403,
                commit_id=commit_id,
            )

        if is_agent_dispatch_mode():
            query_input = CommitDiffQueryInput.from_request(request)
            dispatch_result = dispatch_or_get_commit_diff(commit, force_retry=query_input.force_retry)
            dispatch_status = str(dispatch_result.get("status") or "").strip().lower()

            if dispatch_status == "ready":
                payload = dispatch_result.get("payload") or {}
                diff_data = payload.get("diff_data")
                previous_commit = payload.get("previous_commit")
                return json_success(
                    jsonify=jsonify,
                    message="Agent diff 已就绪",
                    status="ready",
                    commit_id=commit_id,
                    diff_data=diff_data,
                    previous_commit=previous_commit,
                    source="agent",
                )

            if dispatch_status == "unbound":
                return json_error(
                    jsonify=jsonify,
                    message=dispatch_result.get("message") or "项目未绑定 Agent",
                    error_type="agent_unbound",
                    status="unbound",
                    http_status=409,
                    commit_id=commit_id,
                )

            if dispatch_status in {"pending", "pending_offline"}:
                return json_success(
                    jsonify=jsonify,
                    message=dispatch_result.get("message") or "Agent 正在处理diff",
                    status=dispatch_status,
                    http_status=202,
                    pending=True,
                    commit_id=commit_id,
                    retry_after_seconds=dispatch_result.get("retry_after_seconds") or 60,
                    task_id=dispatch_result.get("task_id"),
                )

            return json_error(
                jsonify=jsonify,
                message=dispatch_result.get("message") or "Agent diff 获取失败",
                error_type="agent_dispatch_failed",
                status="error",
                http_status=500,
                commit_id=commit_id,
            )

        repository = commit.repository
        is_excel = excel_cache_service.is_excel_file(commit.path)
        file_commits = (
            Commit.query.filter(
                Commit.repository_id == commit.repository_id,
                Commit.path == commit.path,
            )
            .order_by(Commit.commit_time.desc(), Commit.id.desc())
            .all()
        )
        previous_commit = resolve_previous_commit(commit, file_commits=file_commits)
        diff_data = None
        if is_excel:
            log_print(f"🔍 合并diff异步请求Excel文件: {commit.path}", 'CACHE')
            cached_diff = excel_cache_service.get_cached_diff(
                repository.id, commit.commit_id, commit.path
            )
            if cached_diff:
                log_print(f"✅ 缓存命中，避免重复计算: {commit.path} | 耗时: {time.time() - start_time:.2f}秒", 'CACHE')
                try:
                    diff_data = json.loads(cached_diff.diff_data)
                except Exception as parse_error:
                    log_print(f"❌ 缓存数据解析失败: {parse_error}", 'CACHE')
                    diff_data = None
            else:
                diff_data = get_unified_diff_data(commit, previous_commit)
        else:
            diff_data = get_unified_diff_data(commit, previous_commit)

        if diff_data:
            if previous_commit:
                _attach_author_display([previous_commit])
            import math

            def sanitize_data(obj):
                if isinstance(obj, dict):
                    return {k: sanitize_data(v) for k, v in obj.items()}
                if isinstance(obj, list):
                    return [sanitize_data(item) for item in obj]
                if isinstance(obj, float):
                    if math.isnan(obj) or math.isinf(obj):
                        return None
                    return obj
                return obj

            diff_data = sanitize_data(diff_data)
            total_time = time.time() - start_time
            log_print(f"✅ 合并diff异步请求完成: {commit.path} | 总耗时: {total_time:.2f}秒", 'PERF')
            return json_success(
                jsonify=jsonify,
                message="diff数据就绪",
                status="ready",
                commit_id=commit_id,
                diff_data=diff_data,
                previous_commit={
                    'commit_id': previous_commit.commit_id[:8] if previous_commit else 'N/A',
                    'commit_time': format_beijing_time(previous_commit.commit_time, '%Y-%m-%d %H:%M:%S') if previous_commit and previous_commit.commit_time else 'N/A',
                    'author': (getattr(previous_commit, 'author_display', None) or previous_commit.author) if previous_commit else 'N/A',
                    'message': previous_commit.message if previous_commit else 'N/A'
                } if previous_commit else None,
            )

        total_time = time.time() - start_time
        log_print(f"❌ 合并diff异步请求失败: {commit.path} | 耗时: {total_time:.2f}秒", 'PERF')
        return json_error(
            jsonify=jsonify,
            message="无法获取diff数据",
            error_type="diff_unavailable",
            http_status=500,
            commit_id=commit_id,
        )
    except SQLAlchemyError as e:
        db.session.rollback()
        total_time = time.time() - start_time if 'start_time' in locals() else 0
        log_print(f"❌ 获取提交 {commit_id} 的diff数据数据库失败: {e} | 耗时: {total_time:.2f}秒", 'ERROR')
        return json_error(
            jsonify=jsonify,
            message="获取diff数据失败: 数据库操作异常",
            error_type="database_error",
            http_status=500,
            commit_id=commit_id,
        )
    except (ValueError, TypeError, RuntimeError) as e:
        total_time = time.time() - start_time if 'start_time' in locals() else 0
        log_print(f"❌ 获取提交 {commit_id} 的diff数据失败: {e} | 耗时: {total_time:.2f}秒", 'ERROR')
        return json_error(
            jsonify=jsonify,
            message=f"获取diff数据失败: {str(e)}",
            error_type="runtime_error",
            http_status=500,
            commit_id=commit_id,
        )
    except Exception as e:
        total_time = time.time() - start_time if 'start_time' in locals() else 0
        log_print(f"❌ 获取提交 {commit_id} 的diff数据未知失败: {e} | 耗时: {total_time:.2f}秒", 'ERROR')
        return json_error(
            jsonify=jsonify,
            message=f"获取diff数据失败: {str(e)}",
            error_type="unexpected_error",
            http_status=500,
            commit_id=commit_id,
        )

def refresh_merge_diff():
    """重新计算合并diff数据，绕过缓存"""
    try:
        log_print("🔄 开始处理合并diff重新计算请求", 'APP')
        try:
            refresh_input = MergeDiffRefreshInput.from_request_json(request)
            commit_ids = refresh_input.commit_ids
        except ValueError as input_error:
            return json_error(
                jsonify=jsonify,
                message=str(input_error),
                error_type="invalid_request",
                http_status=400,
            )
        log_print(f"📋 收到提交ID: {commit_ids}", 'APP')

        commits = []
        for commit_id in commit_ids:
            commit = db.session.get(Commit, commit_id)
            if commit:
                allowed, message = _ensure_commit_project_access(commit)
                if not allowed:
                    return json_error(
                        jsonify=jsonify,
                        message=message,
                        error_type="forbidden",
                        http_status=403,
                    )
                commits.append(commit)
                log_print(f"✅ 找到提交: {commit_id} - {commit.path}", 'INFO')
        if not commits:
            log_print("❌ 未找到有效的提交记录", 'INFO')
            return json_error(
                jsonify=jsonify,
                message="未找到有效的提交记录",
                error_type="not_found",
                http_status=404,
            )

        # 临时暂停后台缓存任务，避免冲突
        log_print("🔄 临时暂停后台缓存任务处理...", 'INFO')
        from services.background_task_service import pause_background_tasks
        pause_background_tasks()
        # 优化的批量缓存清理逻辑
        cleared_count = 0
        cache_clear_start = time.time()
        cache_clear_time = 0.0
        # 批量收集所有需要删除的缓存条件
        diff_cache_conditions = []
        html_cache_conditions = []
        for commit in commits:
            if excel_cache_service.is_excel_file(commit.path):
                log_print(f"🔄 准备清除缓存: {commit.path}", 'INFO')
                diff_cache_conditions.append((commit.repository_id, commit.commit_id, commit.path))
                html_cache_conditions.append((commit.repository_id, commit.commit_id, commit.path))
                cleared_count += 1
        if diff_cache_conditions:
            # 批量删除diff缓存
            total_diff_deleted = 0
            for repo_id, commit_id, file_path in diff_cache_conditions:
                deleted_count = DiffCache.query.filter(
                    DiffCache.repository_id == repo_id,
                    DiffCache.commit_id == commit_id,
                    DiffCache.file_path == file_path
                ).delete(synchronize_session=False)
                total_diff_deleted += deleted_count
            # 批量删除HTML缓存 - 直接在这里执行，避免函数调用开销
            total_html_deleted = 0
            for repo_id, commit_id, file_path in html_cache_conditions:
                html_deleted_count = ExcelHtmlCache.query.filter_by(
                    repository_id=repo_id,
                    commit_id=commit_id,
                    file_path=file_path
                ).delete(synchronize_session=False)
                total_html_deleted += html_deleted_count
            # 一次性提交所有删除操作
            db.session.commit()
            cache_clear_time = time.time() - cache_clear_start
            log_print(f"✅ 批量清除缓存完成: diff={total_diff_deleted}, html={total_html_deleted} | 耗时: {cache_clear_time:.2f}秒", 'INFO')
        else:
            cache_clear_time = time.time() - cache_clear_start
            log_print(f"ℹ️ 没有找到需要清除的Excel文件缓存", 'INFO')
        # 恢复后台缓存任务处理
        log_print("🔄 恢复后台缓存任务处理...", 'INFO')
        from services.background_task_service import resume_background_tasks
        resume_background_tasks()
        return json_success(
            jsonify=jsonify,
            message=f'已清除 {cleared_count} 个文件的缓存，缓存清理耗时 {cache_clear_time:.2f} 秒，请刷新页面查看重新计算的结果',
            status="ready",
            cleared_count=cleared_count,
            cache_clear_time=cache_clear_time,
        )
    except SQLAlchemyError as e:
        db.session.rollback()
        log_print(f"重新计算合并diff数据库失败: {e}", 'INFO')
        return json_error(
            jsonify=jsonify,
            message="重新计算失败: 数据库操作异常",
            error_type="database_error",
            http_status=500,
        )
    except (ValueError, TypeError, RuntimeError) as e:
        log_print(f"重新计算合并diff失败: {e}", 'INFO')
        return json_error(
            jsonify=jsonify,
            message=f"重新计算失败: {str(e)}",
            error_type="runtime_error",
            http_status=500,
        )
    except Exception as e:
        log_print(f"重新计算合并diff失败: {e}", 'INFO')
        return json_error(
            jsonify=jsonify,
            message=f"重新计算失败: {str(e)}",
            error_type="unexpected_error",
            http_status=500,
        )

def merge_diff():
    """合并选中条目的diff显示页面"""
    log_print("🚨🚨🚨 ROUTE CALLED! /commits/merge-diff 🚨🚨🚨", 'APP')
    try:
        commit_ids = request.args.getlist('ids')
        if not commit_ids:
            flash('未选择任何提交', 'error')
            return redirect(request.referrer or url_for('index'))

        commits = []
        for commit_id in commit_ids:
            commit = db.session.get(Commit, commit_id)
            if commit:
                allowed, _message = _ensure_commit_project_access(commit)
                if not allowed:
                    abort(403)
                commits.append(commit)
        if not commits:
            flash('未找到有效的提交记录', 'error')
            return redirect(request.referrer or url_for('index'))

        # 按提交时间排序
        commits.sort(key=lambda x: x.commit_time or datetime.min, reverse=False)  # 升序排列，最早的在前
        _attach_author_display(commits)
        # 获取项目和仓库信息
        repository = commits[0].repository
        project = repository.project
        # 检查是否为同一文件的连续提交
        log_print(f"=== 开始调用get_merged_diff_data ===", 'APP')
        log_print(f"📋 提交ID列表: {commit_ids}", 'APP')
        log_print(f"📊 提交数量: {len(commits)}", force=True)
        for i, commit in enumerate(commits):
            log_print(f"  {i+1}. {commit.commit_id[:8]} - {commit.path}", 'APP')
        merged_diff_data = None
        if is_agent_dispatch_mode():
            # platform+agent 模式下不在平台本地计算合并diff，统一走前端异步逐项拉取 Agent diff。
            log_print("platform+agent 模式：跳过平台本地 get_merged_diff_data 计算", "APP")
        else:
            try:
                merged_diff_data = get_merged_diff_data(commits)
                log_print(f"=== get_merged_diff_data调用完成 ===", 'APP')
                log_print(f"merged_diff_data结果: {merged_diff_data is not None}", 'INFO')
                if merged_diff_data:
                    log_print(f"merged_diff_data类型: {merged_diff_data.get('type', 'INFO')}")
                    log_print(f"merged_diff_data键: {list(merged_diff_data.keys())}", 'INFO')
                log_print(f"=== 结束get_merged_diff_data调试 ===", 'APP')
            except Exception as merge_error:
                log_print(f"❌ get_merged_diff_data处理失败: {str(merge_error)}", 'INFO', force=True)
                import traceback
                traceback.print_exc()
                flash(f'合并diff处理失败: {str(merge_error)}', 'error')
                return redirect(request.referrer or url_for('index'))

    except Exception as route_error:
        log_print(f"❌ 合并diff路由处理失败: {str(route_error)}", force=True)
        import traceback
        traceback.print_exc()
        flash(f'页面处理失败: {str(route_error)}', 'error')
        return redirect(request.referrer or url_for('index'))

    log_print(f"合并diff页面调试信息:", 'INFO')
    log_print(f"- 提交数量: {len(commits)}")
    log_print(f"- 提交ID: {[c.id for c in commits]}", 'INFO')
    log_print(f"- 文件路径: {[c.path for c in commits]}", 'INFO')
    commit_times = []
    for c in commits:
        try:
            if c.commit_time:
                commit_times.append(format_beijing_time(c.commit_time, '%Y-%m-%d %H:%M:%S'))
            else:
                commit_times.append('None')
        except Exception as e:
            commit_times.append(f'Error: {str(e)}')
    log_print(f"- 提交时间: {commit_times}", 'INFO')
    log_print(f"- 合并diff数据: {merged_diff_data is not None}", 'INFO')
    if merged_diff_data:
        log_print(f"- 合并diff类型: {merged_diff_data.get('type', 'INFO')}")
        log_print(f"- 合并diff键: {list(merged_diff_data.keys())}", 'INFO')
        if merged_diff_data.get('type') == 'excel':
            log_print(f"- Excel工作表数量: {len(merged_diff_data.get('sheets', {}))}", 'INFO')
            if merged_diff_data.get('sheets'):
                for sheet_name, sheet_data in merged_diff_data.get('sheets', {}).items():
                    log_print(f"  - 工作表 '{sheet_name}': {sheet_data.get('status', 'unknown')}, 行数: {len(sheet_data.get('rows', []))}")
                    if sheet_data.get('rows'):
                        log_print(f"    - 第一行示例: {sheet_data['rows'][0] if sheet_data['rows'] else 'None'}", 'INFO')
        else:
            log_print(f"- hunks数量: {len(merged_diff_data.get('hunks', []))}", 'INFO')
    else:
        log_print("- 合并diff数据为None，将使用传统逐个显示方式", 'INFO')
    # 对于多文件或同文件非连续提交（segmented），改为分段UI逐项显示，避免单块合并区误报“无变更”。
    merged_type = ""
    if isinstance(merged_diff_data, dict):
        merged_type = str(merged_diff_data.get("type") or "").strip().lower()
    if merged_type in {"multiple_files", "segmented_diff", "segmented"}:
        log_print(f"检测到 {merged_type}，切换为逐项diff展示模式", "INFO")
        merged_diff_data = None

    # 智能构建显示列表：合并连续提交，分离不同文件
    commits_with_diff = build_smart_display_list(commits)
    # 计算缓存状态
    cache_status_summary = {'cached': 0, 'uncached': 0, 'total': len(commits_with_diff)}
    for item in commits_with_diff:
        if item.get('cache_available', False):
            cache_status_summary['cached'] += 1
        else:
            cache_status_summary['uncached'] += 1
    log_print(f"commits_with_diff 数量: {len(commits_with_diff)} (异步加载模式)")
    log_print(f"缓存状态: {cache_status_summary['cached']}/{cache_status_summary['total']} 已缓存", 'CACHE')
    return render_template('merge_diff.html',
                         commits=commits,
                         commits_with_diff=commits_with_diff,
                         merged_diff_data=merged_diff_data,
                         project=project,
                         repository=repository,
                         commit_ids=commit_ids)
# 编辑仓库页面

@require_admin
def update_commit_fields_route():
    """更新现有提交记录中缺失的version和operation字段"""
    try:
        # 查找version或operation为None的记录
        commits_to_update = Commit.query.filter(
            (Commit.version.is_(None)) | (Commit.operation.is_(None))
        ).all()
        updated_count = 0
        for commit in commits_to_update:
            # 更新version字段（使用commit_id的前8位）
            if commit.version is None:
                commit.version = commit.commit_id[:8] if commit.commit_id else 'unknown'
            # 更新operation字段（默认为修改）
            if commit.operation is None:
                commit.operation = 'M'  # 默认为修改
            updated_count += 1
        # 提交更改
        db.session.commit()
        return jsonify({
            'success': True,
            'message': f'成功更新 {updated_count} 条提交记录',
            'updated_count': updated_count
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({
            'success': False,
            'message': f'更新失败: {str(e)}'
        }), 500

