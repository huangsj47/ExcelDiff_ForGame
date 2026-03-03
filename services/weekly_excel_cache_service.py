"""
周版本Excel合并diff缓存服务
提供周版本Excel文件合并diff的HTML缓存功能
"""

import os
import json
import time
import hashlib
import threading
import traceback
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List
from flask import render_template
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
from services.model_loader import get_runtime_models


class WeeklyExcelCacheService:
    """周版本Excel缓存服务类"""

    def __init__(self, db, diff_logic_version):
        self.db = db
        self.diff_logic_version = diff_logic_version
        self.max_cache_count = 1000  # 最多保留1000条缓存
        self.expire_days = 90  # 缓存保留90天
        self.processing_cache = set()  # 正在处理的缓存键集合
        self._processing_lock = threading.Lock()  # 线程安全锁 (#30)
        self.operation_logs = []  # 操作日志列表
        self._log_write_count = 0  # 日志写入计数器，用于控制清理频率 (#34)
        # 缓存动态导入的模型引用 (#38)
        self._models_cache: Dict[str, Any] = {}
        # ===== 日志聚合缓冲区（1分钟窗口）=====
        self._log_buffer = {}       # { project_code: { 'success': count, 'error': count, ... } }
        self._log_buffer_lock = threading.Lock()
        self._LOG_FLUSH_INTERVAL = 60  # 聚合窗口：60秒
        self._project_code_cache = {}  # 项目编号缓存

    def _log_exception(self, context: str, exc: Exception, category: str = "WEEKLY"):
        """统一异常日志：输出异常类型、消息和完整堆栈。"""
        detail = f"{context}: {type(exc).__name__}: {exc}"
        stack = traceback.format_exc()
        try:
            from utils.logger import log_print
            log_print(f"{detail}\n{stack}", category, force=True)
        except Exception:
            print(f"[ERROR] {detail}\n{stack}")

    @staticmethod
    def _normalize_model_results(names, resolved):
        """规范化 get_runtime_models 返回值，兼容单模型 tuple/plain object。"""
        if len(names) == 1:
            if isinstance(resolved, (tuple, list)):
                if len(resolved) != 1:
                    raise RuntimeError(
                        f"get_runtime_models 单对象返回数量异常: expected=1 actual={len(resolved)} names={names}"
                    )
                return (resolved[0],)
            return (resolved,)

        if not isinstance(resolved, (tuple, list)):
            raise RuntimeError(
                f"get_runtime_models 多对象返回类型异常: expected tuple/list actual={type(resolved).__name__} names={names}"
            )
        if len(resolved) != len(names):
            raise RuntimeError(
                f"get_runtime_models 多对象返回数量异常: expected={len(names)} actual={len(resolved)} names={names}"
            )
        return tuple(resolved)

    def _get_model(self, *names):
        """获取并缓存动态模型引用，避免每个方法都重复调用 get_runtime_models (#38)"""
        try:
            missing = [n for n in names if n not in self._models_cache]
            if missing:
                results = get_runtime_models(*missing)
                normalized_results = self._normalize_model_results(missing, results)
                for name, model in zip(missing, normalized_results):
                    self._models_cache[name] = model
            if len(names) == 1:
                return self._models_cache[names[0]]
            return tuple(self._models_cache[n] for n in names)
        except Exception as e:
            self._log_exception(f"加载运行时模型失败 names={names}", e)
            raise

    def generate_cache_key(self, config_id: int, file_path: str, base_commit_id: str, latest_commit_id: str) -> str:
        """生成缓存键（使用 SHA-256 替代 MD5，#36）"""
        key_data = f"{config_id}:{file_path}:{base_commit_id}:{latest_commit_id}:{self.diff_logic_version}"
        return hashlib.sha256(key_data.encode('utf-8')).hexdigest()

    def is_excel_file(self, file_path: str) -> bool:
        """判断是否为Excel文件"""
        excel_extensions = ['.xlsx', '.xls', '.xlsm', '.xlsb', '.csv']
        return any(file_path.lower().endswith(ext) for ext in excel_extensions)

    def _get_project_code(self, repository_id):
        """根据 repository_id 获取项目编号（如 G119），带简易缓存"""
        if repository_id is None:
            return None
        if repository_id in self._project_code_cache:
            return self._project_code_cache[repository_id]
        try:
            Repository = self._get_model("Repository")
            repo = self.db.session.get(Repository, repository_id)
            if repo and repo.project:
                code = repo.project.code
                self._project_code_cache[repository_id] = code
                return code
        except Exception as e:
            self._log_exception(f"获取项目编号失败 repository_id={repository_id}", e)
        self._project_code_cache[repository_id] = None
        return None

    def _flush_log_buffer(self, project_code, force=False):
        """刷新指定项目的日志缓冲区，将聚合摘要写入数据库"""
        with self._log_buffer_lock:
            buf = self._log_buffer.get(project_code)
            if not buf:
                return
            now = time.time()
            elapsed = now - buf.get('last_flush', 0)
            if not force and elapsed < self._LOG_FLUSH_INTERVAL:
                return  # 窗口未到，不刷新
            # 构建聚合摘要
            success_cnt = buf.get('success', 0)
            error_cnt = buf.get('error', 0)
            total = success_cnt + error_cnt
            if total == 0:
                return
            prefix = f"【{project_code}】" if project_code else ""
            summary_parts = []
            if success_cnt > 0:
                summary_parts.append(f"成功 {success_cnt} 个")
            if error_cnt > 0:
                summary_parts.append(f"失败 {error_cnt} 个")
            summary_msg = f"{prefix}周版本缓存批量统计: {'，'.join(summary_parts)}（共 {total} 个文件）"
            log_type = 'success' if error_cnt == 0 else ('error' if success_cnt == 0 else 'warning')
            saved_repo_id = buf.get('repository_id')
            # 重置缓冲区
            self._log_buffer[project_code] = {
                'success': 0, 'error': 0, 'files': set(),
                'last_flush': now, 'repository_id': saved_repo_id
            }
        # 在锁外写入数据库
        try:
            OperationLog = self._get_model("OperationLog")
            log_entry = OperationLog(
                log_type=log_type,
                message=summary_msg,
                source='weekly_excel_cache',
                repository_id=saved_repo_id,
                file_path=None
            )
            self.db.session.add(log_entry)
            self._log_write_count += 1
            if self._log_write_count % 50 == 0:
                self._cleanup_old_logs()
            self.db.session.commit()
            # 内存日志
            from utils.timezone_utils import now_beijing
            timestamp = now_beijing().strftime('%Y/%m/%d %H:%M:%S')
            self.operation_logs.append({'time': timestamp, 'message': summary_msg, 'type': log_type})
            if len(self.operation_logs) > 100:
                self.operation_logs = self.operation_logs[-100:]
        except Exception as e:
            self._log_exception(f"刷新周版本聚合日志失败 project_code={project_code}", e)

    def log_cache_operation(self, message, log_type='info', repository_id=None, config_id=None, file_path=None):
        """记录缓存操作日志到数据库。
        对于高频的 success/error 类型日志，使用 1 分钟聚合窗口批量写入。
        其他类型（info/warning）立即写入并附带项目编号前缀。
        """
        try:
            OperationLog = self._get_model("OperationLog")

            # 获取项目编号前缀
            project_code = self._get_project_code(repository_id)
            prefix = f"【{project_code}】" if project_code else ""

            # 高频日志聚合：success 和 error 类型进入缓冲区
            if log_type in ('success', 'error') and repository_id is not None:
                buf_key = project_code or f"repo_{repository_id}"
                with self._log_buffer_lock:
                    if buf_key not in self._log_buffer:
                        self._log_buffer[buf_key] = {
                            'success': 0, 'error': 0, 'files': set(),
                            'last_flush': time.time(), 'repository_id': repository_id
                        }
                    buf = self._log_buffer[buf_key]
                    buf[log_type] = buf.get(log_type, 0) + 1
                    if file_path:
                        buf['files'].add(file_path)
                # 检查是否需要刷新
                self._flush_log_buffer(buf_key)
                return

            # 非聚合日志：立即写入，添加项目编号前缀
            prefixed_message = f"{prefix}{message}" if prefix else message

            log_entry = OperationLog(
                log_type=log_type,
                message=prefixed_message,
                source='weekly_excel_cache',
                repository_id=repository_id,
                config_id=config_id,
                file_path=file_path
            )
            self.db.session.add(log_entry)

            # 每50次写入清理一次旧日志 (#34)
            self._log_write_count += 1
            if self._log_write_count % 50 == 0:
                self._cleanup_old_logs()

            self.db.session.commit()

            # 同时保持内存中的日志（用于向后兼容），使用北京时间
            from utils.timezone_utils import now_beijing
            timestamp = now_beijing().strftime('%Y/%m/%d %H:%M:%S')
            memory_log = {
                'time': timestamp,
                'message': prefixed_message,
                'type': log_type
            }
            self.operation_logs.append(memory_log)
            # 保持最大100条内存日志
            if len(self.operation_logs) > 100:
                self.operation_logs = self.operation_logs[-100:]

        except Exception as e:
            # 如果数据库操作失败，至少保持内存日志，使用北京时间
            try:
                from utils.timezone_utils import now_beijing
                timestamp = now_beijing().strftime('%Y/%m/%d %H:%M:%S')
                memory_log = {
                    'time': timestamp,
                    'message': message,
                    'type': log_type
                }
                self.operation_logs.append(memory_log)
                if len(self.operation_logs) > 100:
                    self.operation_logs = self.operation_logs[-100:]
            except Exception:
                pass  # 如果连内存日志都失败，就忽略

            self._log_exception("记录缓存操作日志失败", e)

    def _cleanup_old_logs(self):
        """清理超过200条的旧日志（批量DELETE，避免逐行删除）"""
        try:
            OperationLog = self._get_model("OperationLog")

            # 获取当前日志总数
            total_count = OperationLog.query.filter_by(source='weekly_excel_cache').count()

            if total_count > 200:
                # 使用子查询批量删除，避免将对象加载到Python内存
                excess_count = total_count - 200
                subquery = (
                    self.db.session.query(OperationLog.id)
                    .filter_by(source='weekly_excel_cache')
                    .order_by(OperationLog.created_at.asc())
                    .limit(excess_count)
                    .subquery()
                )
                OperationLog.query.filter(
                    OperationLog.id.in_(self.db.session.query(subquery))
                ).delete(synchronize_session=False)

        except Exception as e:
            self._log_exception("清理旧周版本操作日志失败", e)

    def needs_merged_diff_cache(self, config_id: int, file_path: str) -> bool:
        """判断是否需要合并Diff缓存（只有多次连续提交的Excel文件才需要）"""
        try:
            if not self.is_excel_file(file_path):
                return False

            WeeklyVersionDiffCache, WeeklyVersionExcelCache = self._get_model(
                "WeeklyVersionDiffCache",
                "WeeklyVersionExcelCache"
            )

            latest_diff_cache = (
                WeeklyVersionDiffCache.query
                .filter_by(config_id=config_id, file_path=file_path, cache_status='completed')
                .order_by(WeeklyVersionDiffCache.updated_at.desc())
                .first()
            )
            if not latest_diff_cache or not latest_diff_cache.latest_commit_id:
                return False

            base_commit_id = latest_diff_cache.base_commit_id or ''
            existing_cache = WeeklyVersionExcelCache.query.filter_by(
                config_id=config_id,
                file_path=file_path,
                base_commit_id=base_commit_id,
                latest_commit_id=latest_diff_cache.latest_commit_id,
                diff_version=self.diff_logic_version,
                cache_status='completed'
            ).first()
            return existing_cache is None

        except Exception as e:
            self._log_exception(
                f"needs_merged_diff_cache 执行失败 config_id={config_id}, file_path={file_path}",
                e
            )
            return False

    def get_cached_html(self, config_id: int, file_path: str, base_commit_id: str, latest_commit_id: str) -> Optional[Dict[str, Any]]:
        """获取缓存的HTML内容"""
        try:
            WeeklyVersionExcelCache = self._get_model("WeeklyVersionExcelCache")

            cache_key = self.generate_cache_key(config_id, file_path, base_commit_id, latest_commit_id)

            cache_record = WeeklyVersionExcelCache.query.filter_by(
                config_id=config_id,
                file_path=file_path,
                base_commit_id=base_commit_id,
                latest_commit_id=latest_commit_id,
                diff_version=self.diff_logic_version,
                cache_status='completed'
            ).first()

            if cache_record:
                return {
                    'html_content': cache_record.html_content,
                    'css_content': cache_record.css_content,
                    'js_content': cache_record.js_content,
                    'metadata': json.loads(cache_record.cache_metadata) if cache_record.cache_metadata else {},
                    'created_at': cache_record.created_at,
                    'from_cache': True
                }
            else:
                return None

        except Exception as e:
            self._log_exception(
                f"获取周版本Excel缓存失败 config_id={config_id}, file_path={file_path}",
                e
            )
            return None

    def save_html_cache(self, config_id: int, repository_id: int, file_path: str,
                       base_commit_id: str, latest_commit_id: str, commit_count: int,
                       html_content: str, css_content: str = "", js_content: str = "",
                       metadata: Dict[str, Any] = None, processing_time: float = 0) -> bool:
        """保存HTML缓存"""
        try:
            WeeklyVersionExcelCache = self._get_model("WeeklyVersionExcelCache")

            cache_key = self.generate_cache_key(config_id, file_path, base_commit_id, latest_commit_id)

            # 检查是否已存在
            existing_cache = WeeklyVersionExcelCache.query.filter_by(
                config_id=config_id,
                file_path=file_path,
                base_commit_id=base_commit_id,
                latest_commit_id=latest_commit_id,
                diff_version=self.diff_logic_version
            ).first()

            if existing_cache:
                # 更新现有缓存
                existing_cache.html_content = html_content
                existing_cache.css_content = css_content
                existing_cache.js_content = js_content
                existing_cache.cache_metadata = json.dumps(metadata) if metadata else None
                existing_cache.cache_status = 'completed'
                existing_cache.processing_time = processing_time
                existing_cache.updated_at = datetime.now(timezone.utc)
            else:
                # 创建新缓存
                new_cache = WeeklyVersionExcelCache(
                    config_id=config_id,
                    repository_id=repository_id,
                    file_path=file_path,
                    cache_key=cache_key,
                    base_commit_id=base_commit_id,
                    latest_commit_id=latest_commit_id,
                    commit_count=commit_count,
                    html_content=html_content,
                    css_content=css_content,
                    js_content=js_content,
                    cache_metadata=json.dumps(metadata) if metadata else None,
                    cache_status='completed',
                    diff_version=self.diff_logic_version,
                    processing_time=processing_time
                )
                self.db.session.add(new_cache)

            self.db.session.commit()
            return True

        except Exception as e:
            self.db.session.rollback()
            self._log_exception(
                f"保存周版本Excel缓存失败 config_id={config_id}, repository_id={repository_id}, file_path={file_path}",
                e
            )
            return False

    def cleanup_expired_cache(self) -> int:
        """清理过期缓存（超过90天）- 使用批量DELETE"""
        try:
            WeeklyVersionExcelCache, flask_app = self._get_model("WeeklyVersionExcelCache", "app")

            with flask_app.app_context():
                expire_date = datetime.now(timezone.utc) - timedelta(days=self.expire_days)

                # 批量DELETE，不加载ORM对象到内存
                count = WeeklyVersionExcelCache.query.filter(
                    WeeklyVersionExcelCache.created_at < expire_date
                ).delete(synchronize_session=False)

                self.db.session.commit()
                return count

        except Exception as e:
            self.db.session.rollback()
            self._log_exception("清理过期周版本Excel缓存失败", e)
            return 0

    def cleanup_old_cache(self) -> int:
        """清理超过1000条的旧缓存 - 使用子查询批量DELETE"""
        try:
            WeeklyVersionExcelCache, flask_app = self._get_model("WeeklyVersionExcelCache", "app")

            with flask_app.app_context():
                total_count = WeeklyVersionExcelCache.query.count()

                if total_count <= self.max_cache_count:
                    return 0

                # 用子查询选出要删除的ID，再批量DELETE
                excess_count = total_count - self.max_cache_count
                subquery = (
                    self.db.session.query(WeeklyVersionExcelCache.id)
                    .order_by(WeeklyVersionExcelCache.created_at.asc())
                    .limit(excess_count)
                    .subquery()
                )

                count = WeeklyVersionExcelCache.query.filter(
                    WeeklyVersionExcelCache.id.in_(self.db.session.query(subquery))
                ).delete(synchronize_session=False)

                self.db.session.commit()
                return count

        except Exception as e:
            self.db.session.rollback()
            self._log_exception("清理超限周版本Excel缓存失败", e)
            return 0

    def get_cache_stats(self) -> Dict[str, Any]:
        """获取缓存统计信息"""
        try:
            WeeklyVersionExcelCache = self._get_model("WeeklyVersionExcelCache")

            total_count = WeeklyVersionExcelCache.query.count()
            completed_count = WeeklyVersionExcelCache.query.filter_by(cache_status='completed').count()
            processing_count = WeeklyVersionExcelCache.query.filter_by(cache_status='processing').count()
            failed_count = WeeklyVersionExcelCache.query.filter_by(cache_status='failed').count()

            # 在数据库层面计算缓存大小，避免将大字段加载到Python内存
            total_size = 0
            try:
                size_result = self.db.session.query(
                    func.sum(func.length(WeeklyVersionExcelCache.html_content))
                ).filter_by(cache_status='completed').scalar()
                total_size = size_result or 0
            except Exception as e:
                self._log_exception("计算周版本Excel缓存总大小失败", e)
                total_size = 0

            return {
                'total_count': total_count,
                'completed_count': completed_count,
                'processing_count': processing_count,
                'failed_count': failed_count,
                'total_size': total_size,
                'max_cache_count': self.max_cache_count,
                'expire_days': self.expire_days
            }

        except Exception as e:
            self._log_exception("获取周版本Excel缓存统计失败", e)
            return {
                'total_count': 0,
                'completed_count': 0,
                'processing_count': 0,
                'failed_count': 0,
                'total_size': 0,
                'max_cache_count': self.max_cache_count,
                'expire_days': self.expire_days
            }

    def get_cache_stats_by_project(self, project_id: int) -> Dict[str, Any]:
        """获取指定项目的周版本Excel缓存统计信息"""
        try:
            WeeklyVersionExcelCache, WeeklyVersionConfig = self._get_model("WeeklyVersionExcelCache", "WeeklyVersionConfig")

            # 获取该项目下所有周版本配置的ID
            config_ids = [config.id for config in WeeklyVersionConfig.query.join(
                WeeklyVersionConfig.repository
            ).filter_by(project_id=project_id).all()]

            if not config_ids:
                return {
                    'total_count': 0,
                    'completed_count': 0,
                    'processing_count': 0,
                    'failed_count': 0,
                    'total_size': 0,
                    'max_cache_count': self.max_cache_count,
                    'expire_days': self.expire_days
                }

            # 统计该项目下的周版本Excel缓存
            total_count = WeeklyVersionExcelCache.query.filter(
                WeeklyVersionExcelCache.config_id.in_(config_ids)
            ).count()

            completed_count = WeeklyVersionExcelCache.query.filter(
                WeeklyVersionExcelCache.config_id.in_(config_ids),
                WeeklyVersionExcelCache.cache_status == 'completed'
            ).count()

            processing_count = WeeklyVersionExcelCache.query.filter(
                WeeklyVersionExcelCache.config_id.in_(config_ids),
                WeeklyVersionExcelCache.cache_status == 'processing'
            ).count()

            failed_count = WeeklyVersionExcelCache.query.filter(
                WeeklyVersionExcelCache.config_id.in_(config_ids),
                WeeklyVersionExcelCache.cache_status == 'failed'
            ).count()

            # 在数据库层面计算缓存大小，避免将大字段加载到Python内存
            total_size = 0
            try:
                size_result = self.db.session.query(
                    func.sum(func.length(WeeklyVersionExcelCache.html_content))
                ).filter(
                    WeeklyVersionExcelCache.config_id.in_(config_ids),
                    WeeklyVersionExcelCache.cache_status == 'completed'
                ).scalar()
                total_size = size_result or 0
            except Exception as e:
                self._log_exception(f"计算项目周版本Excel缓存总大小失败 project_id={project_id}", e)
                total_size = 0

            return {
                'total_count': total_count,
                'completed_count': completed_count,
                'processing_count': processing_count,
                'failed_count': failed_count,
                'total_size': total_size,
                'max_cache_count': self.max_cache_count,
                'expire_days': self.expire_days
            }

        except Exception as e:
            self._log_exception(f"获取项目周版本Excel缓存统计失败 project_id={project_id}", e)
            return {
                'total_count': 0,
                'completed_count': 0,
                'processing_count': 0,
                'failed_count': 0,
                'total_size': 0,
                'max_cache_count': self.max_cache_count,
                'expire_days': self.expire_days
            }

    def clear_all_cache(self) -> int:
        """清理所有缓存"""
        try:
            WeeklyVersionExcelCache = self._get_model("WeeklyVersionExcelCache")

            count = WeeklyVersionExcelCache.query.count()
            WeeklyVersionExcelCache.query.delete()
            self.db.session.commit()
            print(f"✅ 已清理 {count} 条周版本Excel缓存")
            return count

        except Exception as e:
            self._log_exception("清理全部周版本Excel缓存失败", e)
            self.db.session.rollback()
            return 0
