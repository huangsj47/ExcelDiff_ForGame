"""
周版本Excel合并diff缓存服务
提供周版本Excel文件合并diff的HTML缓存功能
"""
import os
import json
import time
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List
from flask import render_template
from flask_sqlalchemy import SQLAlchemy


class WeeklyExcelCacheService:
    """周版本Excel缓存服务类"""

    def __init__(self, db, diff_logic_version):
        self.db = db
        self.diff_logic_version = diff_logic_version
        self.max_cache_count = 1000  # 最多保留1000条缓存
        self.expire_days = 90  # 缓存保留90天
        self.processing_cache = set()  # 正在处理的缓存键集合
        self.operation_logs = []  # 操作日志列表
    
    def generate_cache_key(self, config_id: int, file_path: str, base_commit_id: str, latest_commit_id: str) -> str:
        """生成缓存键"""
        key_data = f"{config_id}:{file_path}:{base_commit_id}:{latest_commit_id}:{self.diff_logic_version}"
        return hashlib.md5(key_data.encode('utf-8')).hexdigest()
    
    def is_excel_file(self, file_path: str) -> bool:
        """判断是否为Excel文件"""
        excel_extensions = ['.xlsx', '.xls', '.csv']
        return any(file_path.lower().endswith(ext) for ext in excel_extensions)

    def log_cache_operation(self, message, log_type='info', repository_id=None, config_id=None, file_path=None):
        """记录缓存操作日志到数据库"""
        try:
            # 避免循环导入，直接使用已有的数据库连接
            # 不再重复创建应用上下文，使用当前上下文

            # 动态导入，避免循环导入问题
            import importlib
            app_module = importlib.import_module('app')
            OperationLog = app_module.OperationLog

            # 保存到数据库（不创建新的应用上下文）
            log_entry = OperationLog(
                log_type=log_type,
                message=message,
                source='weekly_excel_cache',
                repository_id=repository_id,
                config_id=config_id,
                file_path=file_path
            )
            self.db.session.add(log_entry)

            # 清理超过200条的旧日志
            self._cleanup_old_logs()

            self.db.session.commit()

            # 同时保持内存中的日志（用于向后兼容），使用北京时间
            from utils.timezone_utils import now_beijing
            timestamp = now_beijing().strftime('%Y/%m/%d %H:%M:%S')
            memory_log = {
                'time': timestamp,
                'message': message,
                'type': log_type
            }
            self.operation_logs.append(memory_log)
            # 保持最多100条内存日志
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
            except:
                pass  # 如果连内存日志都失败，就忽略

            # 使用print而不是log_print，避免循环导入
            print(f"[ERROR] 记录缓存操作日志失败: {e}")

    def _cleanup_old_logs(self):
        """清理超过200条的旧日志"""
        try:
            # 动态导入，避免循环导入问题
            import importlib
            app_module = importlib.import_module('app')
            OperationLog = app_module.OperationLog

            # 获取当前日志总数
            total_count = OperationLog.query.filter_by(source='weekly_excel_cache').count()

            if total_count > 200:
                # 删除最旧的日志，保留最新的200条
                excess_count = total_count - 200
                old_logs = OperationLog.query.filter_by(source='weekly_excel_cache').order_by(OperationLog.created_at.asc()).limit(excess_count).all()

                for log in old_logs:
                    self.db.session.delete(log)

        except Exception as e:
            # 使用print而不是log_print，避免循环导入
            print(f"[ERROR] 清理旧周版本操作日志失败: {e}")
    
    def needs_merged_diff_cache(self, config_id: int, file_path: str) -> bool:
        """判断是否需要合并diff缓存（只有多次连续提交的Excel文件才需要）"""
        try:
            # 检查是否为Excel文件
            if not self.is_excel_file(file_path):
                print(f"Debug: {file_path} is not an Excel file")
                return False

            print(f"Debug: {file_path} is an Excel file, checking diff cache...")

            # 暂时简化逻辑，直接返回True让所有Excel文件都重建缓存
            # 这样可以避免复杂的数据库查询导致的卡死问题
            print(f"Debug: Returning True for {file_path} (simplified logic)")
            return True

        except Exception as e:
            # 添加错误日志
            print(f"Error in needs_merged_diff_cache: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def get_cached_html(self, config_id: int, file_path: str, base_commit_id: str, latest_commit_id: str) -> Optional[Dict[str, Any]]:
        """获取缓存的HTML内容"""
        try:
            # 动态导入，避免循环导入问题
            import importlib
            app_module = importlib.import_module('app')
            WeeklyVersionExcelCache = app_module.WeeklyVersionExcelCache

            # 不创建新的应用上下文，使用当前上下文
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
            # 静默处理错误，避免日志污染
            return None
    
    def save_html_cache(self, config_id: int, repository_id: int, file_path: str,
                       base_commit_id: str, latest_commit_id: str, commit_count: int,
                       html_content: str, css_content: str = "", js_content: str = "",
                       metadata: Dict[str, Any] = None, processing_time: float = 0) -> bool:
        """保存HTML缓存"""
        try:
            # 动态导入，避免循环导入问题
            import importlib
            app_module = importlib.import_module('app')
            WeeklyVersionExcelCache = app_module.WeeklyVersionExcelCache

            # 不创建新的应用上下文，使用当前上下文
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
            return False
    
    def cleanup_expired_cache(self) -> int:
        """清理过期缓存（超过90天）"""
        try:
            from app import WeeklyVersionExcelCache, app
            
            with app.app_context():
                expire_date = datetime.now(timezone.utc) - timedelta(days=self.expire_days)
                
                expired_caches = WeeklyVersionExcelCache.query.filter(
                    WeeklyVersionExcelCache.created_at < expire_date
                ).all()
                
                count = len(expired_caches)
                for cache in expired_caches:
                    self.db.session.delete(cache)
                
                self.db.session.commit()
                return count
                
        except Exception as e:
            self.db.session.rollback()
            return 0
    
    def cleanup_old_cache(self) -> int:
        """清理超过1000条的旧缓存"""
        try:
            from app import WeeklyVersionExcelCache, app
            
            with app.app_context():
                total_count = WeeklyVersionExcelCache.query.count()
                
                if total_count <= self.max_cache_count:
                    return 0
                
                # 删除最旧的缓存，保留最新的1000条
                old_caches = WeeklyVersionExcelCache.query.order_by(
                    WeeklyVersionExcelCache.created_at.asc()
                ).limit(total_count - self.max_cache_count).all()
                
                count = len(old_caches)
                for cache in old_caches:
                    self.db.session.delete(cache)
                
                self.db.session.commit()
                return count
                
        except Exception as e:
            self.db.session.rollback()
            return 0
    
    def get_cache_stats(self) -> Dict[str, Any]:
        """获取缓存统计信息"""
        try:
            # 动态导入，避免循环导入问题
            import importlib
            app_module = importlib.import_module('app')
            WeeklyVersionExcelCache = app_module.WeeklyVersionExcelCache

            # 不需要 app_context，因为在Flask请求中已经有了
            total_count = WeeklyVersionExcelCache.query.count()
            completed_count = WeeklyVersionExcelCache.query.filter_by(cache_status='completed').count()
            processing_count = WeeklyVersionExcelCache.query.filter_by(cache_status='processing').count()
            failed_count = WeeklyVersionExcelCache.query.filter_by(cache_status='failed').count()

            # 计算缓存大小（估算）- 使用更快的方法
            total_size = 0
            try:
                # 只计算已完成的缓存的大小，避免长时间查询
                completed_caches = WeeklyVersionExcelCache.query.filter_by(cache_status='completed').limit(100).all()
                for cache in completed_caches:
                    if cache.html_content:
                        total_size += len(cache.html_content.encode('utf-8'))

                # 如果有更多缓存，按比例估算
                if completed_count > 100:
                    total_size = int(total_size * (completed_count / 100))

            except Exception:
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
            from app import WeeklyVersionExcelCache, WeeklyVersionConfig

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

            # 计算缓存大小（估算）- 使用更快的方法
            total_size = 0
            try:
                # 只计算已完成的缓存的大小，避免长时间查询
                completed_caches = WeeklyVersionExcelCache.query.filter(
                    WeeklyVersionExcelCache.config_id.in_(config_ids),
                    WeeklyVersionExcelCache.cache_status == 'completed'
                ).limit(100).all()

                for cache in completed_caches:
                    if cache.html_content:
                        total_size += len(cache.html_content.encode('utf-8'))

                # 如果有更多缓存，按比例估算
                if completed_count > 100:
                    total_size = int(total_size * (completed_count / 100))

            except Exception:
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
            # 动态导入，避免循环导入问题
            import importlib
            app_module = importlib.import_module('app')
            WeeklyVersionExcelCache = app_module.WeeklyVersionExcelCache

            # 不需要额外的app_context，因为这个方法已经在Flask请求上下文中运行
            count = WeeklyVersionExcelCache.query.count()
            WeeklyVersionExcelCache.query.delete()
            self.db.session.commit()
            print(f"✅ 已清理 {count} 条周版本Excel缓存")
            return count

        except Exception as e:
            print(f"❌ 清理周版本Excel缓存失败: {e}")
            import traceback
            traceback.print_exc()
            self.db.session.rollback()
            return 0
