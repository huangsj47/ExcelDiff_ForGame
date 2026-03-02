#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Excel diff cache service extracted from app.py."""

import json
import time
import threading
from datetime import datetime, timedelta, timezone
from sqlalchemy import func


app = None
db = None
DIFF_LOGIC_VERSION = "1.8.0"
DiffCache = None
OperationLog = None
Commit = None
Repository = None
get_unified_diff_data = None


def _noop_log(*args, **kwargs):
    """Fallback no-op logger when log_print is not configured."""
    pass

log_print = _noop_log


def configure_excel_diff_cache_service(*, app_instance, db_instance, diff_logic_version, diff_cache_model, operation_log_model, commit_model, repository_model, log_print_func, unified_diff_func):
    """Wire runtime dependencies for ExcelDiffCacheService."""
    global app, db, DIFF_LOGIC_VERSION, DiffCache, OperationLog, Commit, Repository, log_print, get_unified_diff_data
    app = app_instance
    db = db_instance
    DIFF_LOGIC_VERSION = diff_logic_version
    DiffCache = diff_cache_model
    OperationLog = operation_log_model
    Commit = commit_model
    Repository = repository_model
    log_print = log_print_func
    get_unified_diff_data = unified_diff_func

class ExcelDiffCacheService:
    """Excel文件差异缓存服务"""
    
    def __init__(self):
        self._processing_commits = set()  # 正在处理的提交ID集合
        self._processing_lock = threading.Lock()  # 线程安全锁
        self._log_write_count = 0  # 日志写入计数器（用于控制清理频率）
        self.operation_logs = []  # 操作日志列表ID集合
        self.max_cache_count = 1000  # 最大缓存数量
        self.long_processing_threshold = 10.0  # 长处理时间阈值（秒）
        self.long_processing_expire_days = 90  # 长处理文件缓存保留天数（3个月）
        
    def is_excel_file(self, file_path):
        """检查文件是否为Excel文件"""
        excel_extensions = ['.xlsx', '.xls', '.xlsm']
        return any(file_path.lower().endswith(ext) for ext in excel_extensions)
    
    def log_cache_operation(self, message, log_type='info', repository_id=None, file_path=None):
        """记录缓存操作日志到数据库"""
        try:
            # 保存到数据库
            log_entry = OperationLog(
                log_type=log_type,
                message=message,
                source='excel_cache',
                repository_id=repository_id,
                file_path=file_path
            )
            db.session.add(log_entry)

            # 每50次写入清理一次旧日志 (#34)
            self._log_write_count += 1
            if self._log_write_count % 50 == 0:
                self._cleanup_old_logs()

            db.session.commit()

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
            # 如果数据库操作失败，至少保持内存日志
            from datetime import datetime
            timestamp = datetime.now().strftime('%Y/%m/%d %H:%M:%S')
            memory_log = {
                'time': timestamp,
                'message': message,
                'type': log_type
            }
            self.operation_logs.append(memory_log)
            if len(self.operation_logs) > 100:
                self.operation_logs = self.operation_logs[-100:]
            log_print(f"记录操作日志到数据库失败: {e}", 'ERROR')

    def _cleanup_old_logs(self):
        """清理超过200条的旧日志（批量DELETE，避免逐行删除）"""
        try:
            # 获取当前日志总数
            total_count = OperationLog.query.filter_by(source='excel_cache').count()

            if total_count > 200:
                # 使用子查询批量删除，避免将对象加载到Python内存
                excess_count = total_count - 200
                subquery = (
                    db.session.query(OperationLog.id)
                    .filter_by(source='excel_cache')
                    .order_by(OperationLog.created_at.asc())
                    .limit(excess_count)
                    .subquery()
                )
                OperationLog.query.filter(
                    OperationLog.id.in_(db.session.query(subquery))
                ).delete(synchronize_session=False)

        except Exception as e:
            log_print(f"清理旧操作日志失败: {e}", 'ERROR')
    
    def get_cached_diff(self, repository_id, commit_id, file_path):
        """获取缓存的差异数据，检查版本号匹配"""
        try:
            log_print(f"🔍 查询缓存: repo={repository_id}, commit={commit_id[:8]}, file={file_path}", 'CACHE')

            # 只刷新本次查询命中的对象，避免会话全量失效带来的额外开销
            cache = (
                DiffCache.query
                .populate_existing()
                .filter_by(
                repository_id=repository_id,
                commit_id=commit_id,
                file_path=file_path,
                cache_status='completed',
                diff_version=DIFF_LOGIC_VERSION  # 只返回当前版本的缓存
                )
                .order_by(DiffCache.updated_at.desc())
                .first()
            )
            
            if cache:
                log_print(f"✅ 缓存命中: {file_path} | 版本: {cache.diff_version} | 创建时间: {cache.created_at}", 'CACHE')
                log_print(f"📊 缓存数据大小: {len(cache.diff_data)} 字符 | 处理时间: {cache.processing_time:.2f}秒", 'CACHE')
                return cache
            else:
                # 检查是否存在旧版本的缓存
                old_cache = (
                    DiffCache.query
                    .populate_existing()
                    .filter_by(
                        repository_id=repository_id,
                        commit_id=commit_id,
                        file_path=file_path,
                        cache_status='completed'
                    )
                    .first()
                )
                
                if old_cache and old_cache.diff_version != DIFF_LOGIC_VERSION:
                    log_print(f"⚠️ 发现旧版本缓存: {file_path} | 旧版本: {old_cache.diff_version} → 当前版本: {DIFF_LOGIC_VERSION}", 'CACHE')
                    # 标记旧缓存为过期，稍后清理
                    old_cache.cache_status = 'outdated'
                    db.session.commit()
                    log_print(f"🗑️ 旧版本缓存已标记为过期", 'CACHE')
                else:
                    log_print(f"❌ 缓存未命中: {file_path} | 需要重新计算diff", 'CACHE')
                
            return None
        except Exception as e:
            log_print(f"❌ 获取缓存差异失败: {e}", 'CACHE', force=True)
            return None
    
    def optimize_diff_data(self, diff_data):
        """优化diff数据，只保留有变更的行"""
        if not diff_data or diff_data.get('type') != 'excel':
            return diff_data
        
        try:
            optimized_data = diff_data.copy()
            original_size = 0
            optimized_size = 0
            
            if 'sheets' in optimized_data:
                for sheet_name, sheet_data in optimized_data['sheets'].items():
                    if 'rows' in sheet_data:
                        original_rows = sheet_data['rows']
                        original_size += len(original_rows)
                        
                        # 只保留有变更的行（added, removed, modified）
                        changed_rows = [
                            row for row in original_rows 
                            if row.get('status') in ['added', 'removed', 'modified']
                        ]
                        
                        sheet_data['rows'] = changed_rows
                        optimized_size += len(changed_rows)
                        
                        # 更新统计信息，只统计有变更的行
                        if 'stats' in sheet_data:
                            stats = sheet_data['stats']
                            # 保持原有的统计数据，因为这些是正确的变更统计
            
            log_print(f"🗜️ diff数据优化: {original_size} 行 → {optimized_size} 行 (减少 {original_size - optimized_size} 行)", 'CACHE')
            return optimized_data
            
        except Exception as e:
            log_print(f"❌ diff数据优化失败: {e}", 'CACHE', force=True)
            return diff_data
    
    # diff_data 序列化后最大允许 20MB (#40)
    MAX_DIFF_DATA_BYTES = 20 * 1024 * 1024

    def save_cached_diff(self, repository_id, commit_id, file_path, diff_data, processing_time=0.0, file_size=0, previous_commit_id=None, commit_time=None):
        """保存差异数据到缓存，支持智能缓存策略"""
        try:
            log_print(f"💾 保存缓存: repo={repository_id}, commit={commit_id[:8]}, file={file_path}", 'CACHE')

            # --- #40: 序列化并检查大小限制 ---
            diff_json = json.dumps(diff_data)
            diff_bytes = len(diff_json.encode('utf-8'))
            if diff_bytes > self.MAX_DIFF_DATA_BYTES:
                size_mb = diff_bytes / (1024 * 1024)
                log_print(
                    f"⚠️ diff_data 超出大小限制: {file_path} | "
                    f"{size_mb:.2f}MB > {self.MAX_DIFF_DATA_BYTES / (1024*1024):.0f}MB，"
                    f"仅保存摘要信息", 'CACHE', force=True)
                # 用精简占位数据替代，保留统计但丢弃行详情
                summary_data = {
                    'type': diff_data.get('type', 'excel'),
                    'truncated': True,
                    'original_size_mb': round(size_mb, 2),
                    'error': f'数据过大({size_mb:.1f}MB)，已截断。请在线查看差异。',
                }
                if 'sheets' in diff_data:
                    summary_data['sheets'] = {}
                    for sheet_name, sheet_info in diff_data['sheets'].items():
                        summary_data['sheets'][sheet_name] = {
                            'stats': sheet_info.get('stats', {}),
                            'rows': [],            # 清空行数据
                            'headers': sheet_info.get('headers', []),
                        }
                diff_json = json.dumps(summary_data)
                log_print(f"📦 已替换为摘要数据: {len(diff_json)} 字符", 'CACHE')

            # 判断是否为长处理时间文件
            is_long_processing = processing_time > self.long_processing_threshold
            
            # 计算过期时间
            expire_at = None
            if is_long_processing:
                # 长处理文件保存3个月
                expire_at = datetime.now(timezone.utc) + timedelta(days=self.long_processing_expire_days)
                log_print(f"⏱️ 长处理文件({processing_time:.2f}s)，缓存3个月至: {expire_at.strftime('%Y-%m-%d')}", 'CACHE')
            else:
                # 普通文件根据1000条限制管理，不设置过期时间
                log_print(f"⚡ 普通处理文件({processing_time:.2f}s)，按1000条限制管理", 'CACHE')
            
            # 检查是否已存在
            existing_cache = DiffCache.query.filter_by(
                repository_id=repository_id,
                commit_id=commit_id,
                file_path=file_path
            ).first()
            
            if existing_cache:
                # 更新现有缓存（使用已序列化的 diff_json）
                existing_cache.diff_data = diff_json
                existing_cache.processing_time = processing_time
                existing_cache.file_size = file_size
                existing_cache.cache_status = 'completed'
                existing_cache.diff_version = DIFF_LOGIC_VERSION
                existing_cache.is_long_processing = is_long_processing
                existing_cache.expire_at = expire_at
                existing_cache.updated_at = datetime.now(timezone.utc)
                if commit_time:
                    existing_cache.commit_time = commit_time
                log_print(f"🔄 更新现有缓存: {file_path}", 'CACHE')
            else:
                # 创建新缓存（使用已序列化的 diff_json）
                new_cache = DiffCache(
                    repository_id=repository_id,
                    commit_id=commit_id,
                    file_path=file_path,
                    previous_commit_id=previous_commit_id,
                    diff_data=diff_json,
                    processing_time=processing_time,
                    file_size=file_size,
                    cache_status='completed',
                    diff_version=DIFF_LOGIC_VERSION,
                    commit_time=commit_time,
                    is_long_processing=is_long_processing,
                    expire_at=expire_at
                )
                db.session.add(new_cache)
                log_print(f"💾 创建新缓存: {file_path}", 'CACHE')
            
            db.session.commit()
            
            # 保存后检查是否需要清理旧缓存 (#33: 移除冗余验证查询)
            if not is_long_processing:
                self._cleanup_old_cache(repository_id)
            
            log_print(f"✅ 缓存保存成功: {file_path} | 处理时间: {processing_time:.2f}秒", 'CACHE')
            return True
            
        except Exception as e:
            log_print(f"❌ 保存缓存失败: {e}", 'CACHE', force=True)
            db.session.rollback()
            return False
    
    def cache_diff_error(self, repository_id, commit_id, file_path, error_message):
        """缓存错误信息"""
        try:
            existing_cache = DiffCache.query.filter_by(
                repository_id=repository_id,
                commit_id=commit_id,
                file_path=file_path
            ).first()
            
            if existing_cache:
                existing_cache.cache_status = 'failed'
                existing_cache.error_message = error_message
                existing_cache.updated_at = datetime.now(timezone.utc)
            else:
                cache = DiffCache(
                    repository_id=repository_id,
                    commit_id=commit_id,
                    file_path=file_path,
                    diff_data='{}',
                    cache_status='failed',
                    error_message=error_message
                )
                db.session.add(cache)
            
            db.session.commit()
        except Exception as e:
            log_print(f"缓存错误信息失败: {e}", 'CACHE', force=True)
            db.session.rollback()
    
    def get_recent_excel_commits(self, repository, limit=1000):
        """获取最近的Excel文件提交（从最近1000条提交中筛选）"""
        try:
            # 先获取最近1000条提交记录，应用仓库起始日期过滤
            query = Commit.query.filter(Commit.repository_id == repository.id)
            
            # 应用仓库配置的起始日期过滤
            if repository.start_date:
                query = query.filter(Commit.commit_time >= repository.start_date)
            
            recent_commits = query.order_by(Commit.commit_time.desc()).limit(limit).all()
            
            # 从中筛选出Excel文件
            excel_commits = []
            for commit in recent_commits:
                if (commit.path.endswith('.xlsx') or 
                    commit.path.endswith('.xls') or 
                    commit.path.endswith('.xlsm') or 
                    commit.path.endswith('.xlsb') or 
                    commit.path.endswith('.csv')):
                    excel_commits.append(commit)
            
            commits = excel_commits
            
            # 进一步过滤符合仓库正则条件的文件
            filtered_commits = []
            if repository.path_regex:
                import re
                pattern = re.compile(repository.path_regex)
                for commit in commits:
                    if pattern.search(commit.path):
                        filtered_commits.append(commit)
            else:
                filtered_commits = commits
            
            log_print(f"📊 从最近{limit}条提交中筛选出{len(filtered_commits)}个Excel文件", 'CACHE')
            return filtered_commits
        except Exception as e:
            log_print(f"获取最近Excel提交失败: {e}", 'CACHE', force=True)
            return []
    
    def process_excel_diff_background(self, repository_id, commit_id, file_path):
        """后台处理Excel文件差异"""
        try:
            log_print(f"开始后台处理Excel差异: repo={repository_id}, commit={commit_id}, file={file_path}", 'EXCEL')
            
            # 检查是否已在处理中（线程安全）
            task_key = f"{repository_id}_{commit_id}_{file_path}"
            with self._processing_lock:
                if task_key in self._processing_commits:
                    log_print(f"任务已在处理中，跳过: {task_key}", 'EXCEL')
                    return
                self._processing_commits.add(task_key)
            
            # 确保在Flask应用上下文中执行
            with app.app_context():
                try:
                    repository = db.session.get(Repository, repository_id)
                    if not repository:
                        log_print(f"仓库不存在: {repository_id}", 'EXCEL', force=True)
                        return
                    
                    # 获取提交信息
                    commit = Commit.query.filter_by(
                        repository_id=repository_id,
                        commit_id=commit_id,
                        path=file_path
                    ).first()
                    
                    if not commit:
                        log_print(f"提交不存在: {commit_id}, {file_path}", 'EXCEL', force=True)
                        return
                    
                    # 获取前一个提交
                    previous_commit = None
                    file_commits = Commit.query.filter(
                        Commit.repository_id == repository_id,
                        Commit.path == file_path,
                        Commit.commit_time < commit.commit_time
                    ).order_by(Commit.commit_time.desc()).first()
                    
                    start_time = time.time()
                    
                    # 使用统一差异服务处理
                    diff_data = get_unified_diff_data(commit, file_commits)
                    
                    processing_time = time.time() - start_time
                    
                    if diff_data and diff_data.get('type') == 'excel':
                        # 缓存成功的差异数据
                        self.save_cached_diff(
                            repository_id=repository_id,
                            commit_id=commit_id,
                            file_path=file_path,
                            diff_data=diff_data,
                            previous_commit_id=file_commits.commit_id if file_commits else None,
                            processing_time=processing_time
                        )
                        log_print(f"💾 Excel差异缓存成功: {file_path} | 版本: {DIFF_LOGIC_VERSION} | 耗时: {processing_time:.2f}秒", 'EXCEL')
                        log_print(f"📈 差异数据大小: {len(str(diff_data))} 字符", 'EXCEL')
                        
                        # 记录到操作日志
                        self.log_cache_operation(f"✅ 缓存生成成功: {file_path}", 'success', repository_id=repository_id, file_path=file_path)
                    else:
                        # 缓存错误信息
                        error_msg = diff_data.get('error', '处理失败') if diff_data else '处理返回空结果'
                        self.cache_diff_error(repository_id, commit_id, file_path, error_msg)
                        log_print(f"❌ Excel差异处理失败: {file_path} | 错误: {error_msg}", 'EXCEL', force=True)
                        
                        # 记录到操作日志
                        self.log_cache_operation(f"❌ 缓存生成失败: {file_path} - {error_msg}", 'error', repository_id=repository_id, file_path=file_path)
                        
                except Exception as inner_e:
                    log_print(f"处理Excel差异时出错: {inner_e}", 'EXCEL', force=True)
                    import traceback
                    traceback.print_exc()
                finally:
                    with self._processing_lock:
                        self._processing_commits.discard(task_key)
                
        except Exception as e:
            log_print(f"后台处理Excel差异异常: {e}", 'EXCEL', force=True)
            import traceback
            traceback.print_exc()
    
    def cleanup_old_cache(self, days=30):
        """清理超过指定天数的缓存数据和旧版本缓存（合并OR查询，#39）"""
        try:
            from sqlalchemy import or_
            cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)
            
            # 合并为一条 OR 查询，减少3次独立全表扫描为1次 (#39)
            total_count = DiffCache.query.filter(
                or_(
                    DiffCache.created_at < cutoff_date,                                       # 超期缓存
                    DiffCache.cache_status == 'outdated',                                     # 标记过期
                    db.and_(DiffCache.diff_version != DIFF_LOGIC_VERSION,
                            DiffCache.cache_status == 'completed'),                           # 版本不匹配
                )
            ).delete(synchronize_session=False)
            
            db.session.commit()
            log_print(f"🗑️ 合并清理了 {total_count} 条过期/过旧/版本不匹配缓存")
            return total_count
        except Exception as e:
            log_print(f"清理缓存失败: {e}", 'CACHE', force=True)
            try:
                db.session.rollback()
            except Exception:
                pass
            return 0
    
    def cleanup_version_mismatch_cache(self):
        """专门清理版本号不匹配的缓存（批量DELETE）"""
        try:
            # 批量删除所有版本号不匹配的缓存
            count = DiffCache.query.filter(
                DiffCache.diff_version != DIFF_LOGIC_VERSION
            ).delete(synchronize_session=False)
            
            db.session.commit()
            log_print(f"清理了 {count} 条版本不匹配的缓存", 'CACHE')
            return count
        except Exception as e:
            log_print(f"清理版本不匹配缓存失败: {e}", 'CACHE', force=True)
            db.session.rollback()
            return 0
    
    def _cleanup_old_cache(self, repository_id=None):
        """清理超过1000条的旧缓存（不包括长处理文件）- 使用子查询批量DELETE"""
        try:
            base_filter = [
                DiffCache.cache_status == 'completed',
                DiffCache.is_long_processing == False  # 不清理长处理文件
            ]
            
            if repository_id:
                base_filter.append(DiffCache.repository_id == repository_id)
            
            total_count = DiffCache.query.filter(*base_filter).count()
            if total_count > self.max_cache_count:
                # 用子查询选出要保留的最新N条ID之外的记录
                excess_count = total_count - self.max_cache_count
                subquery = (
                    db.session.query(DiffCache.id)
                    .filter(*base_filter)
                    .order_by(DiffCache.created_at.asc())
                    .limit(excess_count)
                    .subquery()
                )
                
                deleted_count = DiffCache.query.filter(
                    DiffCache.id.in_(db.session.query(subquery))
                ).delete(synchronize_session=False)
                
                db.session.commit()
                log_print(f"🗑️ 清理了 {deleted_count} 条超限缓存", 'CACHE')
                return deleted_count
            else:
                log_print(f"📊 当前缓存数量 {total_count}，无需清理", 'CACHE')
                return 0
                
        except Exception as e:
            log_print(f"❌ 清理旧缓存失败: {e}", 'CACHE', force=True)
            db.session.rollback()
            return 0
    
    def get_cache_statistics(self, repository_id=None):
        """获取缓存统计信息"""
        try:
            query = DiffCache.query
            if repository_id:
                query = query.filter(DiffCache.repository_id == repository_id)
            
            total_count = query.count()
            completed_count = query.filter(DiffCache.cache_status == 'completed').count()
            processing_count = query.filter(DiffCache.cache_status == 'processing').count()
            failed_count = query.filter(DiffCache.cache_status == 'failed').count()
            outdated_count = query.filter(DiffCache.cache_status == 'outdated').count()
            
            # 获取当前版本的缓存数量
            current_version_count = query.filter(
                DiffCache.diff_version == DIFF_LOGIC_VERSION,
                DiffCache.cache_status == 'completed'
            ).count()
            
            # 获取长处理文件数量
            long_processing_count = query.filter(
                DiffCache.is_long_processing == True,
                DiffCache.cache_status == 'completed'
            ).count()
            
            # 计算普通处理文件数量
            normal_processing_count = completed_count - long_processing_count
            
            return {
                'total_count': total_count,
                'completed_count': completed_count,
                'processing_count': processing_count,
                'failed_count': failed_count,
                'outdated_count': outdated_count,
                'current_version_count': current_version_count,
                'long_processing_count': long_processing_count,
                'normal_processing_count': normal_processing_count,
                'version': DIFF_LOGIC_VERSION
            }
        except Exception as e:
            log_print(f"❌ 获取缓存统计失败: {e}", 'CACHE', force=True)
            return {
                'total_count': 0,
                'completed_count': 0,
                'processing_count': 0,
                'failed_count': 0,
                'outdated_count': 0,
                'current_version_count': 0,
                'long_processing_count': 0,
                'normal_processing_count': 0,
                'version': DIFF_LOGIC_VERSION
            }
    
    def get_cache_statistics_by_repositories(self, repository_ids):
        """获取指定仓库列表的缓存统计信息"""
        try:
            log_print(f"🔍 获取仓库缓存统计: repository_ids={repository_ids}", 'CACHE')
            
            if not repository_ids:
                log_print("⚠️ 仓库ID列表为空，返回零值统计", 'CACHE')
                return {
                    'total_count': 0,
                    'completed_count': 0,
                    'processing_count': 0,
                    'failed_count': 0,
                    'outdated_count': 0,
                    'current_version_count': 0,
                    'long_processing_count': 0,
                    'normal_processing_count': 0,
                    'version': DIFF_LOGIC_VERSION
                }
            
            query = DiffCache.query.filter(DiffCache.repository_id.in_(repository_ids))
            
            total_count = query.count()
            completed_count = query.filter(DiffCache.cache_status == 'completed').count()
            processing_count = query.filter(DiffCache.cache_status == 'processing').count()
            failed_count = query.filter(DiffCache.cache_status == 'failed').count()
            outdated_count = query.filter(DiffCache.cache_status == 'outdated').count()
            
            # 获取当前版本的缓存数量
            current_version_count = query.filter(
                DiffCache.diff_version == DIFF_LOGIC_VERSION,
                DiffCache.cache_status == 'completed'
            ).count()
            
            # 获取长处理文件数量
            long_processing_count = query.filter(
                DiffCache.is_long_processing == True,
                DiffCache.cache_status == 'completed'
            ).count()
            
            # 详细调试信息
            log_print(f"📊 缓存统计详细结果:", 'CACHE')
            log_print(f"   - 总缓存数: {total_count}", 'CACHE')
            log_print(f"   - 已完成: {completed_count}", 'CACHE')
            log_print(f"   - 处理中: {processing_count}", 'CACHE')
            log_print(f"   - 失败: {failed_count}", 'CACHE')
            log_print(f"   - 过期: {outdated_count}", 'CACHE')
            log_print(f"   - 当前版本: {current_version_count}", 'CACHE')
            log_print(f"   - 长处理: {long_processing_count}", 'CACHE')
            
            # 查看最近的几条缓存记录
            recent_caches = query.order_by(DiffCache.created_at.desc()).limit(3).all()
            if recent_caches:
                log_print(f"📋 最近的缓存记录:", 'CACHE')
                for cache in recent_caches:
                    log_print(f"   - ID:{cache.id}, 仓库:{cache.repository_id}, 文件:{cache.file_path}, 状态:{cache.cache_status}, 版本:{cache.diff_version}", 'CACHE')
            else:
                log_print(f"❌ 没有找到任何缓存记录", 'CACHE')
            
            # 计算普通处理文件数量
            normal_processing_count = completed_count - long_processing_count
            
            return {
                'total_count': total_count,
                'completed_count': completed_count,
                'processing_count': processing_count,
                'failed_count': failed_count,
                'outdated_count': outdated_count,
                'current_version_count': current_version_count,
                'long_processing_count': long_processing_count,
                'normal_processing_count': normal_processing_count,
                'version': DIFF_LOGIC_VERSION
            }
        except Exception as e:
            log_print(f"❌ 获取仓库缓存统计失败: {e}", 'CACHE', force=True)
            return {
                'total_count': 0,
                'completed_count': 0,
                'processing_count': 0,
                'failed_count': 0,
                'outdated_count': 0,
                'current_version_count': 0,
                'long_processing_count': 0,
                'normal_processing_count': 0,
                'version': DIFF_LOGIC_VERSION
            }


