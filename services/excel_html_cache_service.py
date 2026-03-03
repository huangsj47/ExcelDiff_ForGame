"""
Excel HTML缓存服务
提供Excel差异结果的HTML缓存功能，包括HTML内容和CSS样式
"""
import os
import json
import time
import hashlib
import threading
import traceback
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple
from flask import render_template
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
from services.model_loader import get_runtime_models


class ExcelHtmlCacheService:
    """Excel HTML缓存服务类"""
    
    def __init__(self, db, diff_logic_version):
        self.db = db
        self.diff_logic_version = diff_logic_version
        self.current_version = diff_logic_version
        self.processing_cache = set()
        self._lock = threading.Lock()
        # 缓存动态导入的模型引用，避免每个方法都重复调用 get_runtime_models (#38)
        self._models_cache: Dict[str, Any] = {}

    def _log_exception(self, context: str, exc: Exception, category: str = "CACHE"):
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
        """获取并缓存动态模型引用 (#38)"""
        try:
            missing = [n for n in names if n not in self._models_cache]
            if missing:
                models = get_runtime_models(*missing)
                normalized_models = self._normalize_model_results(missing, models)
                for name, model in zip(missing, normalized_models):
                    self._models_cache[name] = model
            if len(names) == 1:
                return self._models_cache[names[0]]
            return tuple(self._models_cache[n] for n in names)
        except Exception as e:
            self._log_exception(f"加载运行时模型失败 names={names}", e)
            raise

    def generate_cache_key(self, repository_id: int, commit_id: str, file_path: str) -> str:
        """生成缓存键（SHA-256）"""
        key_data = f"{repository_id}:{commit_id}:{file_path}:{self.diff_logic_version}"
        return hashlib.sha256(key_data.encode('utf-8')).hexdigest()
    
    def get_cached_html(self, repository_id: int, commit_id: str, file_path: str) -> Optional[Dict[str, Any]]:
        """获取缓存的HTML内容"""
        try:
            ExcelHtmlCache, flask_app = self._get_model("ExcelHtmlCache", "app")
            
            with flask_app.app_context():
                cache_key = self.generate_cache_key(repository_id, commit_id, file_path)

                cache_record = ExcelHtmlCache.query.filter_by(
                    repository_id=repository_id,
                    commit_id=commit_id,
                    file_path=file_path,
                    diff_version=self.diff_logic_version,
                    cache_status='completed'
                ).first()
            
                if cache_record:
                    # 在 session 上下文内提取所有属性，避免 DetachedInstanceError
                    result = {
                        'html_content': cache_record.html_content,
                        'css_content': cache_record.css_content,
                        'js_content': cache_record.js_content,
                        'metadata': json.loads(cache_record.cache_metadata) if cache_record.cache_metadata else {},
                        'created_at': cache_record.created_at,
                        'from_cache': True
                    }
                    return result
                else:
                    return None
                
        except Exception as e:
            self._log_exception(
                f"获取HTML缓存失败 repository_id={repository_id}, commit_id={commit_id}, file_path={file_path}",
                e
            )
            return None
    
    def save_html_cache(self, repository_id: int, commit_id: str, file_path: str, 
                       html_content: str, css_content: str = "", js_content: str = "",
                       metadata: Dict[str, Any] = None) -> bool:
        """保存HTML缓存"""
        try:
            ExcelHtmlCache, flask_app = self._get_model("ExcelHtmlCache", "app")
            
            with flask_app.app_context():
                cache_key = self.generate_cache_key(repository_id, commit_id, file_path)
                
                existing_cache = ExcelHtmlCache.query.filter_by(
                    repository_id=repository_id,
                    commit_id=commit_id,
                    file_path=file_path,
                    diff_version=self.diff_logic_version
                ).first()
            
                if existing_cache:
                    existing_cache.html_content = html_content
                    existing_cache.css_content = css_content
                    existing_cache.js_content = js_content
                    existing_cache.cache_metadata = json.dumps(metadata) if metadata else None
                    existing_cache.cache_status = 'completed'
                    existing_cache.updated_at = datetime.utcnow()
                else:
                    new_cache = ExcelHtmlCache(
                        repository_id=repository_id,
                        commit_id=commit_id,
                        file_path=file_path,
                        cache_key=cache_key,
                        html_content=html_content,
                        css_content=css_content,
                        js_content=js_content,
                        cache_metadata=json.dumps(metadata) if metadata else None,
                        cache_status='completed',
                        diff_version=self.diff_logic_version
                    )
                    self.db.session.add(new_cache)
                    
                self.db.session.commit()
                return True
            
        except Exception as e:
            try:
                self.db.session.rollback()
            except Exception:
                pass
            self._log_exception(
                f"保存HTML缓存失败 repository_id={repository_id}, commit_id={commit_id}, file_path={file_path}",
                e
            )
            return False
    
    def generate_excel_html(self, diff_data: Dict[str, Any]) -> Tuple[str, str, str]:
        """根据Excel差异数据生成HTML内容"""
        try:
            if not diff_data or diff_data.get('type') != 'excel':
                raise ValueError("无效的Excel差异数据")
            
            html_content = self._render_excel_diff_html(diff_data)
            css_content = self._generate_excel_diff_css()
            js_content = self._generate_excel_diff_js()
            
            return html_content, css_content, js_content
            
        except Exception as e:
            self._log_exception("生成Excel HTML失败", e)
            raise
    
    def _render_excel_diff_html(self, diff_data: Dict[str, Any]) -> str:
        """渲染Excel差异HTML模板"""
        try:
            from flask import current_app
            
            with current_app.app_context():
                html_content = render_template(
                    'diff_partials/excel_diff.html',
                    diff_data=diff_data,
                    file_path=diff_data.get('file_path', ''),
                    sheets=diff_data.get('sheets', {}),
                    summary=diff_data.get('summary', {})
                )
                return html_content
                
        except Exception as e:
            self._log_exception("渲染Excel差异模板失败，回退简单HTML", e)
            # 如果模板渲染失败，生成简单的HTML结构
            return self._generate_simple_excel_html(diff_data)
    
    def _generate_simple_excel_html(self, diff_data: Dict[str, Any]) -> str:
        """生成简单的Excel差异HTML结构"""
        html_parts = ['<div class="excel-diff-container">']
        
        file_path = diff_data.get('file_path', '')
        html_parts.append(f'<div class="file-header"><h3>Excel文件差异: {file_path}</h3></div>')
        
        summary = diff_data.get('summary', {})
        if summary:
            html_parts.append('<div class="diff-summary">')
            html_parts.append(f'<span class="added">新增: {summary.get("added", 0)}</span>')
            html_parts.append(f'<span class="removed">删除: {summary.get("removed", 0)}</span>')
            html_parts.append(f'<span class="modified">修改: {summary.get("modified", 0)}</span>')
            html_parts.append('</div>')
        
        sheets = diff_data.get('sheets', {})
        for sheet_name, sheet_data in sheets.items():
            html_parts.append(f'<div class="sheet-container" data-sheet="{sheet_name}">')
            html_parts.append(f'<h4 class="sheet-title">工作表: {sheet_name}</h4>')
            
            if 'rows' in sheet_data and sheet_data['rows']:
                html_parts.append('<div class="table-container">')
                html_parts.append('<table class="excel-diff-table">')
                
                headers = sheet_data.get('headers', [])
                if headers:
                    html_parts.append('<thead><tr>')
                    html_parts.append('<th>行号</th><th>状态</th>')
                    for header in headers:
                        html_parts.append(f'<th>{header}</th>')
                    html_parts.append('</tr></thead>')
                
                html_parts.append('<tbody>')
                for row in sheet_data['rows']:
                    status = row.get('status', 'unchanged')
                    row_number = row.get('row_number', '')
                    data = row.get('data', {})
                    
                    html_parts.append(f'<tr class="row-{status}">')
                    html_parts.append(f'<td>{row_number}</td>')
                    html_parts.append(f'<td class="status-{status}">{status}</td>')
                    
                    for header in headers:
                        value = data.get(header, '')
                        html_parts.append(f'<td>{value}</td>')
                    
                    html_parts.append('</tr>')
                
                html_parts.append('</tbody></table>')
                html_parts.append('</div>')
            
            html_parts.append('</div>')
        
        html_parts.append('</div>')
        return ''.join(html_parts)
    
    def _generate_excel_diff_css(self) -> str:
        """生成Excel差异的CSS样式"""
        return """
        .excel-diff-container {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            margin: 20px 0;
        }
        .file-header h3 {
            color: #333;
            border-bottom: 2px solid #007bff;
            padding-bottom: 10px;
            margin-bottom: 20px;
        }
        .diff-summary {
            background: #f8f9fa;
            padding: 15px;
            border-radius: 5px;
            margin-bottom: 20px;
            display: flex;
            gap: 20px;
        }
        .diff-summary .added { color: #28a745; font-weight: bold; }
        .diff-summary .removed { color: #dc3545; font-weight: bold; }
        .diff-summary .modified { color: #ffc107; font-weight: bold; }
        .sheet-container {
            margin-bottom: 30px;
            border: 1px solid #dee2e6;
            border-radius: 5px;
            overflow: hidden;
        }
        .sheet-title {
            background: #007bff;
            color: white;
            margin: 0;
            padding: 15px;
            font-size: 16px;
        }
        .table-container {
            overflow-x: auto;
            max-height: 600px;
            overflow-y: auto;
        }
        .excel-diff-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 14px;
        }
        .excel-diff-table th,
        .excel-diff-table td {
            border: 1px solid #dee2e6;
            padding: 8px 12px;
            text-align: left;
            vertical-align: top;
        }
        .excel-diff-table th {
            background: #f8f9fa;
            font-weight: 600;
            position: sticky;
            top: 0;
            z-index: 10;
        }
        .row-added { background-color: #d4edda; }
        .row-removed { background-color: #f8d7da; }
        .row-modified { background-color: #fff3cd; }
        .status-added { color: #155724; font-weight: bold; }
        .status-removed { color: #721c24; font-weight: bold; }
        .status-modified { color: #856404; font-weight: bold; }
        .excel-diff-table tr:hover {
            background-color: rgba(0, 123, 255, 0.1);
        }
        .excel-diff-table td {
            max-width: 200px;
            word-wrap: break-word;
            word-break: break-all;
        }
        """
    
    def _generate_excel_diff_js(self) -> str:
        """生成Excel差异的JavaScript代码"""
        return """
        document.addEventListener('DOMContentLoaded', function() {
            const rows = document.querySelectorAll('.excel-diff-table tbody tr');
            rows.forEach(row => {
                row.addEventListener('click', function() {
                    rows.forEach(r => r.classList.remove('selected'));
                    this.classList.add('selected');
                });
            });
            
            const sheetContainers = document.querySelectorAll('.sheet-container');
            if (sheetContainers.length > 1) {
                sheetContainers.forEach((container, index) => {
                    if (index > 0) {
                        container.style.display = 'none';
                    }
                });
                
                const tabContainer = document.createElement('div');
                tabContainer.className = 'sheet-tabs';
                tabContainer.innerHTML = '<style>.sheet-tabs{margin:10px 0;}.sheet-tab{display:inline-block;padding:8px 16px;margin-right:5px;background:#f8f9fa;border:1px solid #dee2e6;cursor:pointer;border-radius:3px;}.sheet-tab.active{background:#007bff;color:white;}</style>';
                
                sheetContainers.forEach((container, index) => {
                    const sheetName = container.getAttribute('data-sheet');
                    const tab = document.createElement('span');
                    tab.className = 'sheet-tab' + (index === 0 ? ' active' : '');
                    tab.textContent = sheetName;
                    tab.addEventListener('click', function() {
                        sheetContainers.forEach(c => c.style.display = 'none');
                        container.style.display = 'block';
                        document.querySelectorAll('.sheet-tab').forEach(t => t.classList.remove('active'));
                        this.classList.add('active');
                    });
                    tabContainer.appendChild(tab);
                });
                
                sheetContainers[0].parentNode.insertBefore(tabContainer, sheetContainers[0]);
            }
        });
        
        const style = document.createElement('style');
        style.textContent = '.excel-diff-table tbody tr.selected { background-color: rgba(0, 123, 255, 0.2) !important; }';
        document.head.appendChild(style);
        """
    
    def cleanup_old_version_cache(self):
        """清理旧版本的HTML缓存"""
        try:
            ExcelHtmlCache, flask_app = self._get_model("ExcelHtmlCache", "app")
            
            with flask_app.app_context():
                count = ExcelHtmlCache.query.filter(
                    ExcelHtmlCache.diff_version != self.current_version
                ).delete(synchronize_session=False)
                
                self.db.session.commit()
                return count
            
        except Exception as e:
            try:
                self.db.session.rollback()
            except Exception:
                pass
            self._log_exception("清理旧版本HTML缓存失败", e)
            return 0
    
    def cleanup_expired_cache(self):
        """清理过期的HTML缓存（基于创建时间，超过7天的缓存）"""
        try:
            ExcelHtmlCache, flask_app = self._get_model("ExcelHtmlCache", "app")
            
            with flask_app.app_context():
                expire_time = datetime.utcnow() - timedelta(days=7)
                
                count = ExcelHtmlCache.query.filter(
                    ExcelHtmlCache.created_at < expire_time
                ).delete(synchronize_session=False)
                
                if count > 0:
                    self.db.session.commit()
                
                return count
            
        except Exception as e:
            try:
                self.db.session.rollback()
            except Exception:
                pass
            self._log_exception("清理过期HTML缓存失败", e)
            return 0
    
    def get_cache_statistics(self, repository_id=None):
        """获取HTML缓存统计信息"""
        try:
            ExcelHtmlCache, flask_app = self._get_model("ExcelHtmlCache", "app")
            
            with flask_app.app_context():
                query = ExcelHtmlCache.query
                if repository_id:
                    query = query.filter(ExcelHtmlCache.repository_id == repository_id)
                
                total_count = query.count()
                completed_count = query.filter(ExcelHtmlCache.cache_status == 'completed').count()
                current_version_count = query.filter(ExcelHtmlCache.diff_version == self.current_version).count()

                completed_query = query.filter(ExcelHtmlCache.cache_status == 'completed')
                total_size = (
                    completed_query.with_entities(
                        func.coalesce(
                            func.sum(
                                func.length(func.coalesce(ExcelHtmlCache.html_content, ''))
                                + func.length(func.coalesce(ExcelHtmlCache.css_content, ''))
                                + func.length(func.coalesce(ExcelHtmlCache.js_content, ''))
                            ),
                            0,
                        )
                    ).scalar()
                    or 0
                )
                
                return {
                    'total_count': total_count,
                    'completed_count': completed_count,
                    'current_version_count': current_version_count,
                    'old_version_count': total_count - current_version_count,
                    'total_size_mb': round(total_size / (1024 * 1024), 2),
                    'current_version': self.current_version
                }
            
        except Exception as e:
            self._log_exception(f"获取HTML缓存统计失败 repository_id={repository_id}", e)
            return {
                'total_count': 0,
                'completed_count': 0,
                'current_version_count': 0,
                'old_version_count': 0,
                'total_size_mb': 0,
                'current_version': self.current_version
            }
    
    def get_cache_statistics_by_repositories(self, repository_ids):
        """获取指定仓库列表的HTML缓存统计信息"""
        try:
            ExcelHtmlCache, flask_app = self._get_model("ExcelHtmlCache", "app")
            
            if not repository_ids:
                return {
                    'total_count': 0,
                    'completed_count': 0,
                    'current_version_count': 0,
                    'old_version_count': 0,
                    'total_size_mb': 0.0,
                    'current_version': self.current_version
                }
            
            with flask_app.app_context():
                query = ExcelHtmlCache.query.filter(ExcelHtmlCache.repository_id.in_(repository_ids))
                
                total_count = query.count()
                completed_count = query.filter(ExcelHtmlCache.cache_status == 'completed').count()
                current_version_count = query.filter(ExcelHtmlCache.diff_version == self.current_version).count()

                completed_query = query.filter(ExcelHtmlCache.cache_status == 'completed')
                total_size = (
                    completed_query.with_entities(
                        func.coalesce(
                            func.sum(
                                func.length(func.coalesce(ExcelHtmlCache.html_content, ''))
                                + func.length(func.coalesce(ExcelHtmlCache.css_content, ''))
                                + func.length(func.coalesce(ExcelHtmlCache.js_content, ''))
                            ),
                            0,
                        )
                    ).scalar()
                    or 0
                )
                
                return {
                    'total_count': total_count,
                    'completed_count': completed_count,
                    'current_version_count': current_version_count,
                    'old_version_count': total_count - current_version_count,
                    'total_size_mb': round(total_size / (1024 * 1024), 2),
                    'current_version': self.current_version
                }
            
        except Exception as e:
            self._log_exception(f"获取仓库列表HTML缓存统计失败 repository_ids={repository_ids}", e)
            return {
                'total_count': 0,
                'completed_count': 0,
                'current_version_count': 0,
                'old_version_count': 0,
                'total_size_mb': 0.0,
                'current_version': self.current_version
            }
    
    def delete_html_cache(self, repository_id: int, commit_id: str, file_path: str) -> int:
        """删除指定的HTML缓存"""
        try:
            ExcelHtmlCache, flask_app = self._get_model("ExcelHtmlCache", "app")
            
            with flask_app.app_context():
                deleted_count = ExcelHtmlCache.query.filter_by(
                    repository_id=repository_id,
                    commit_id=commit_id,
                    file_path=file_path
                ).delete()
                
                if deleted_count > 0:
                    self.db.session.commit()
                
                return deleted_count
                
        except Exception as e:
            try:
                self.db.session.rollback()
            except Exception:
                pass
            self._log_exception(
                f"删除HTML缓存失败 repository_id={repository_id}, commit_id={commit_id}, file_path={file_path}",
                e
            )
            return 0
