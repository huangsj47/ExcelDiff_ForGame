"""
Excel HTML缓存服务
提供Excel差异结果的HTML缓存功能，包括HTML内容和CSS样式
"""
import os
import json
import time
import hashlib
from datetime import datetime
from typing import Dict, Any, Optional, Tuple
from flask import render_template
from flask_sqlalchemy import SQLAlchemy


class ExcelHtmlCacheService:
    """Excel HTML缓存服务类"""
    
    def __init__(self, db, diff_logic_version):
        self.db = db
        self.diff_logic_version = diff_logic_version
        self.current_version = diff_logic_version  # 添加current_version属性
        self.processing_cache = set()  # 正在处理的缓存键集合
    
    def generate_cache_key(self, repository_id: int, commit_id: str, file_path: str) -> str:
        """生成缓存键"""
        key_data = f"{repository_id}:{commit_id}:{file_path}:{self.diff_logic_version}"
        return hashlib.md5(key_data.encode('utf-8')).hexdigest()
    
    def get_cached_html(self, repository_id: int, commit_id: str, file_path: str) -> Optional[Dict[str, Any]]:
        """获取缓存的HTML内容"""
        try:
            from app import ExcelHtmlCache, app
            
            with app.app_context():
                cache_key = self.generate_cache_key(repository_id, commit_id, file_path)
                # 静默查询，不输出日志
                
                cache_record = ExcelHtmlCache.query.filter_by(
                    repository_id=repository_id,
                    commit_id=commit_id,
                    file_path=file_path,
                    diff_version=self.diff_logic_version,
                    cache_status='completed'
                ).first()
            
            if cache_record:
                # 缓存命中，静默处理
                return {
                    'html_content': cache_record.html_content,
                    'css_content': cache_record.css_content,
                    'js_content': cache_record.js_content,
                    'metadata': json.loads(cache_record.cache_metadata) if cache_record.cache_metadata else {},
                    'created_at': cache_record.created_at,
                    'from_cache': True
                }
            else:
                # 缓存未命中，静默处理
                return None
                
        except Exception as e:
            # 获取缓存失败，静默处理错误
            return None
    
    def save_html_cache(self, repository_id: int, commit_id: str, file_path: str, 
                       html_content: str, css_content: str = "", js_content: str = "",
                       metadata: Dict[str, Any] = None) -> bool:
        """保存HTML缓存"""
        try:
            from app import ExcelHtmlCache, app
            
            with app.app_context():
                cache_key = self.generate_cache_key(repository_id, commit_id, file_path)
                
                # 检查是否已存在
                existing_cache = ExcelHtmlCache.query.filter_by(
                    repository_id=repository_id,
                    commit_id=commit_id,
                    file_path=file_path,
                    diff_version=self.diff_logic_version
                ).first()
            
            if existing_cache:
                # 更新现有缓存
                existing_cache.html_content = html_content
                existing_cache.css_content = css_content
                existing_cache.js_content = js_content
                existing_cache.cache_metadata = json.dumps(metadata) if metadata else None
                existing_cache.cache_status = 'completed'
                existing_cache.updated_at = datetime.utcnow()
                # 更新缓存，静默处理
            else:
                # 创建新缓存
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
                # 创建缓存，静默处理
                
            self.db.session.commit()
            return True
            
        except Exception as e:
            # 保存缓存失败，静默处理错误
            self.db.session.rollback()
            return False
    
    def generate_excel_html(self, diff_data: Dict[str, Any]) -> Tuple[str, str, str]:
        """根据Excel差异数据生成HTML内容"""
        try:
            if not diff_data or diff_data.get('type') != 'excel':
                raise ValueError("无效的Excel差异数据")
            
            # 生成HTML内容
            html_content = self._render_excel_diff_html(diff_data)
            
            # 生成CSS样式
            css_content = self._generate_excel_diff_css()
            
            # 生成JavaScript代码
            js_content = self._generate_excel_diff_js()
            
            return html_content, css_content, js_content
            
        except Exception as e:
            # 生成HTML失败，静默处理错误
            raise
    
    def _render_excel_diff_html(self, diff_data: Dict[str, Any]) -> str:
        """渲染Excel差异HTML模板"""
        try:
            # 使用Flask的render_template渲染Excel差异模板
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
            # 渲染模板失败，静默处理错误
            # 如果模板渲染失败，生成简单的HTML结构
            return self._generate_simple_excel_html(diff_data)
    
    def _generate_simple_excel_html(self, diff_data: Dict[str, Any]) -> str:
        """生成简单的Excel差异HTML结构"""
        html_parts = ['<div class="excel-diff-container">']
        
        # 文件信息
        file_path = diff_data.get('file_path', '')
        html_parts.append(f'<div class="file-header"><h3>Excel文件差异: {file_path}</h3></div>')
        
        # 汇总信息
        summary = diff_data.get('summary', {})
        if summary:
            html_parts.append('<div class="diff-summary">')
            html_parts.append(f'<span class="added">新增: {summary.get("added", 0)}</span>')
            html_parts.append(f'<span class="removed">删除: {summary.get("removed", 0)}</span>')
            html_parts.append(f'<span class="modified">修改: {summary.get("modified", 0)}</span>')
            html_parts.append('</div>')
        
        # 工作表差异
        sheets = diff_data.get('sheets', {})
        for sheet_name, sheet_data in sheets.items():
            html_parts.append(f'<div class="sheet-container" data-sheet="{sheet_name}">')
            html_parts.append(f'<h4 class="sheet-title">工作表: {sheet_name}</h4>')
            
            # 表格内容
            if 'rows' in sheet_data and sheet_data['rows']:
                html_parts.append('<div class="table-container">')
                html_parts.append('<table class="excel-diff-table">')
                
                # 表头
                headers = sheet_data.get('headers', [])
                if headers:
                    html_parts.append('<thead><tr>')
                    html_parts.append('<th>行号</th><th>状态</th>')
                    for header in headers:
                        html_parts.append(f'<th>{header}</th>')
                    html_parts.append('</tr></thead>')
                
                # 表格行
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
        
        .diff-summary .added {
            color: #28a745;
            font-weight: bold;
        }
        
        .diff-summary .removed {
            color: #dc3545;
            font-weight: bold;
        }
        
        .diff-summary .modified {
            color: #ffc107;
            font-weight: bold;
        }
        
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
        
        .row-added {
            background-color: #d4edda;
        }
        
        .row-removed {
            background-color: #f8d7da;
        }
        
        .row-modified {
            background-color: #fff3cd;
        }
        
        .status-added {
            color: #155724;
            font-weight: bold;
        }
        
        .status-removed {
            color: #721c24;
            font-weight: bold;
        }
        
        .status-modified {
            color: #856404;
            font-weight: bold;
        }
        
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
        // Excel差异表格交互功能
        document.addEventListener('DOMContentLoaded', function() {
            // 表格行点击高亮
            const rows = document.querySelectorAll('.excel-diff-table tbody tr');
            rows.forEach(row => {
                row.addEventListener('click', function() {
                    // 移除其他行的高亮
                    rows.forEach(r => r.classList.remove('selected'));
                    // 添加当前行高亮
                    this.classList.add('selected');
                });
            });
            
            // 工作表切换功能
            const sheetContainers = document.querySelectorAll('.sheet-container');
            if (sheetContainers.length > 1) {
                // 如果有多个工作表，添加切换功能
                sheetContainers.forEach((container, index) => {
                    if (index > 0) {
                        container.style.display = 'none';
                    }
                });
                
                // 添加工作表切换按钮
                const tabContainer = document.createElement('div');
                tabContainer.className = 'sheet-tabs';
                tabContainer.innerHTML = '<style>.sheet-tabs{margin:10px 0;}.sheet-tab{display:inline-block;padding:8px 16px;margin-right:5px;background:#f8f9fa;border:1px solid #dee2e6;cursor:pointer;border-radius:3px;}.sheet-tab.active{background:#007bff;color:white;}</style>';
                
                sheetContainers.forEach((container, index) => {
                    const sheetName = container.getAttribute('data-sheet');
                    const tab = document.createElement('span');
                    tab.className = 'sheet-tab' + (index === 0 ? ' active' : '');
                    tab.textContent = sheetName;
                    tab.addEventListener('click', function() {
                        // 隐藏所有工作表
                        sheetContainers.forEach(c => c.style.display = 'none');
                        // 显示选中的工作表
                        container.style.display = 'block';
                        // 更新标签状态
                        document.querySelectorAll('.sheet-tab').forEach(t => t.classList.remove('active'));
                        this.classList.add('active');
                    });
                    tabContainer.appendChild(tab);
                });
                
                // 插入到第一个工作表前面
                sheetContainers[0].parentNode.insertBefore(tabContainer, sheetContainers[0]);
            }
        });
        
        // 添加选中行的CSS样式
        const style = document.createElement('style');
        style.textContent = '.excel-diff-table tbody tr.selected { background-color: rgba(0, 123, 255, 0.2) !important; }';
        document.head.appendChild(style);
        """
    
    def cleanup_old_version_cache(self):
        """清理旧版本的HTML缓存"""
        try:
            from app import ExcelHtmlCache
            old_caches = ExcelHtmlCache.query.filter(
                ExcelHtmlCache.diff_version != self.current_version
            ).all()
            
            count = len(old_caches)
            for cache in old_caches:
                self.db.session.delete(cache)
            
            self.db.session.commit()
            # 清理旧版本缓存完成
            return count
            
        except Exception as e:
            # 清理旧版本缓存失败
            self.db.session.rollback()
            return 0
    
    def cleanup_expired_cache(self):
        """清理过期的HTML缓存（基于创建时间，超过7天的缓存）"""
        try:
            from app import ExcelHtmlCache
            from datetime import datetime, timedelta
            
            # HTML缓存保留7天
            expire_time = datetime.utcnow() - timedelta(days=7)
            
            expired_caches = ExcelHtmlCache.query.filter(
                ExcelHtmlCache.created_at < expire_time
            ).all()
            
            count = len(expired_caches)
            for cache in expired_caches:
                self.db.session.delete(cache)
            
            if count > 0:
                self.db.session.commit()
                # 清理过期缓存完成
            
            return count
            
        except Exception as e:
            # 清理过期缓存失败
            self.db.session.rollback()
            return 0
    
    def get_cache_statistics(self, repository_id=None):
        """获取HTML缓存统计信息"""
        try:
            from app import ExcelHtmlCache, app
            from typing import Dict, Any
            
            with app.app_context():
                query = ExcelHtmlCache.query
                if repository_id:
                    query = query.filter(ExcelHtmlCache.repository_id == repository_id)
                
                total_count = query.count()
                completed_count = query.filter(ExcelHtmlCache.cache_status == 'completed').count()
                current_version_count = query.filter(ExcelHtmlCache.diff_version == self.current_version).count()
                
                # 计算总缓存大小（估算）
                total_size = 0
                for cache in query.filter(ExcelHtmlCache.cache_status == 'completed').all():
                    if cache.html_content:
                        total_size += len(cache.html_content.encode('utf-8'))
                    if cache.css_content:
                        total_size += len(cache.css_content.encode('utf-8'))
                    if cache.js_content:
                        total_size += len(cache.js_content.encode('utf-8'))
            
            return {
                'total_count': total_count,
                'completed_count': completed_count,
                'current_version_count': current_version_count,
                'old_version_count': total_count - current_version_count,
                'total_size_mb': round(total_size / (1024 * 1024), 2),
                'current_version': self.current_version
            }
            
        except Exception as e:
            # 获取缓存统计失败
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
            from app import ExcelHtmlCache, app
            
            if not repository_ids:
                return {
                    'total_count': 0,
                    'completed_count': 0,
                    'current_version_count': 0,
                    'old_version_count': 0,
                    'total_size_mb': 0.0,
                    'current_version': self.current_version
                }
            
            with app.app_context():
                query = ExcelHtmlCache.query.filter(ExcelHtmlCache.repository_id.in_(repository_ids))
                
                total_count = query.count()
                completed_count = query.filter(ExcelHtmlCache.cache_status == 'completed').count()
                current_version_count = query.filter(ExcelHtmlCache.diff_version == self.current_version).count()
                
                # 计算总缓存大小（估算）
                total_size = 0
                for cache in query.filter(ExcelHtmlCache.cache_status == 'completed').all():
                    if cache.html_content:
                        total_size += len(cache.html_content.encode('utf-8'))
                    if cache.css_content:
                        total_size += len(cache.css_content.encode('utf-8'))
                    if cache.js_content:
                        total_size += len(cache.js_content.encode('utf-8'))
            
            return {
                'total_count': total_count,
                'completed_count': completed_count,
                'current_version_count': current_version_count,
                'old_version_count': total_count - current_version_count,
                'total_size_mb': round(total_size / (1024 * 1024), 2),
                'current_version': self.current_version
            }
            
        except Exception as e:
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
            from app import ExcelHtmlCache, app
            
            with app.app_context():
                deleted_count = ExcelHtmlCache.query.filter_by(
                    repository_id=repository_id,
                    commit_id=commit_id,
                    file_path=file_path
                ).delete()
                
                if deleted_count > 0:
                    self.db.session.commit()
                
                return deleted_count
                
        except Exception as e:
            self.db.session.rollback()
            return 0
