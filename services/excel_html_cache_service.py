"""
Excel HTML缂撳瓨鏈嶅姟
鎻愪緵Excel宸紓缁撴灉鐨凥TML缂撳瓨鍔熻兘锛屽寘鎷琀TML鍐呭鍜孋SS鏍峰紡
"""
import os
import json
import time
import hashlib
from datetime import datetime
from typing import Dict, Any, Optional, Tuple
from flask import render_template
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
from services.model_loader import get_runtime_models


class ExcelHtmlCacheService:
    """Excel HTML缂撳瓨鏈嶅姟绫?"""
    
    def __init__(self, db, diff_logic_version):
        self.db = db
        self.diff_logic_version = diff_logic_version
        self.current_version = diff_logic_version  # 娣诲姞current_version灞炴€?        self.processing_cache = set()  # 姝ｅ湪澶勭悊鐨勭紦瀛橀敭闆嗗悎
    
    def generate_cache_key(self, repository_id: int, commit_id: str, file_path: str) -> str:
        """鐢熸垚缂撳瓨閿?"""
        key_data = f"{repository_id}:{commit_id}:{file_path}:{self.diff_logic_version}"
        return hashlib.md5(key_data.encode('utf-8')).hexdigest()
    
    def get_cached_html(self, repository_id: int, commit_id: str, file_path: str) -> Optional[Dict[str, Any]]:
        """鑾峰彇缂撳瓨鐨凥TML鍐呭"""
        try:
            ExcelHtmlCache, flask_app = get_runtime_models("ExcelHtmlCache", "app")
            
            with flask_app.app_context():
                cache_key = self.generate_cache_key(repository_id, commit_id, file_path)
                # 闈欓粯鏌ヨ锛屼笉杈撳嚭鏃ュ織

                
                cache_record = ExcelHtmlCache.query.filter_by(
                    repository_id=repository_id,
                    commit_id=commit_id,
                    file_path=file_path,
                    diff_version=self.diff_logic_version,
                    cache_status='completed'
                ).first()
            
            if cache_record:
                # 缂撳瓨鍛戒腑锛岄潤榛樺鐞?
                return {
                    'html_content': cache_record.html_content,
                    'css_content': cache_record.css_content,
                    'js_content': cache_record.js_content,
                    'metadata': json.loads(cache_record.cache_metadata) if cache_record.cache_metadata else {},
                    'created_at': cache_record.created_at,
                    'from_cache': True
                }
            else:
                # 缂撳瓨鏈懡涓紝闈欓粯澶勭悊

                return None
                
        except Exception as e:
            # 鑾峰彇缂撳瓨澶辫触锛岄潤榛樺鐞嗛敊璇?
            return None
    
    def save_html_cache(self, repository_id: int, commit_id: str, file_path: str, 
                       html_content: str, css_content: str = "", js_content: str = "",
                       metadata: Dict[str, Any] = None) -> bool:
        """淇濆瓨HTML缂撳瓨"""
        try:
            ExcelHtmlCache, flask_app = get_runtime_models("ExcelHtmlCache", "app")
            
            with flask_app.app_context():
                cache_key = self.generate_cache_key(repository_id, commit_id, file_path)
                
                # 妫€鏌ユ槸鍚﹀凡瀛樺湪

                existing_cache = ExcelHtmlCache.query.filter_by(
                    repository_id=repository_id,
                    commit_id=commit_id,
                    file_path=file_path,
                    diff_version=self.diff_logic_version
                ).first()
            
            if existing_cache:
                # 鏇存柊鐜版湁缂撳瓨

                existing_cache.html_content = html_content
                existing_cache.css_content = css_content
                existing_cache.js_content = js_content
                existing_cache.cache_metadata = json.dumps(metadata) if metadata else None
                existing_cache.cache_status = 'completed'
                existing_cache.updated_at = datetime.utcnow()
                # 鏇存柊缂撳瓨锛岄潤榛樺鐞?
            else:
                # 鍒涘缓鏂扮紦瀛?
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
                # 鍒涘缓缂撳瓨锛岄潤榛樺鐞?
                
            self.db.session.commit()
            return True
            
        except Exception as e:
            # 淇濆瓨缂撳瓨澶辫触锛岄潤榛樺鐞嗛敊璇?
            self.db.session.rollback()
            return False
    
    def generate_excel_html(self, diff_data: Dict[str, Any]) -> Tuple[str, str, str]:
        """鏍规嵁Excel宸紓鏁版嵁鐢熸垚HTML鍐呭"""
        try:
            if not diff_data or diff_data.get('type') != 'excel':
                raise ValueError("鏃犳晥鐨凟xcel宸紓鏁版嵁")
            
            # 鐢熸垚HTML鍐呭

            html_content = self._render_excel_diff_html(diff_data)
            
            # 鐢熸垚CSS鏍峰紡

            css_content = self._generate_excel_diff_css()
            
            # 鐢熸垚JavaScript浠ｇ爜

            js_content = self._generate_excel_diff_js()
            
            return html_content, css_content, js_content
            
        except Exception as e:
            # 鐢熸垚HTML澶辫触锛岄潤榛樺鐞嗛敊璇?
            raise
    
    def _render_excel_diff_html(self, diff_data: Dict[str, Any]) -> str:
        """娓叉煋Excel宸紓HTML妯℃澘"""
        try:
            # 浣跨敤Flask鐨剅ender_template娓叉煋Excel宸紓妯℃澘

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
            # 娓叉煋妯℃澘澶辫触锛岄潤榛樺鐞嗛敊璇?            # 濡傛灉妯℃澘娓叉煋澶辫触锛岀敓鎴愮畝鍗曠殑HTML缁撴瀯

            return self._generate_simple_excel_html(diff_data)
    
    def _generate_simple_excel_html(self, diff_data: Dict[str, Any]) -> str:
        """鐢熸垚绠€鍗曠殑Excel宸紓HTML缁撴瀯"""
        html_parts = ['<div class="excel-diff-container">']
        
        # 鏂囦欢淇℃伅

        file_path = diff_data.get('file_path', '')
        html_parts.append(f'<div class="file-header"><h3>Excel鏂囦欢宸紓: {file_path}</h3></div>')
        
        # 姹囨€讳俊鎭?
        summary = diff_data.get('summary', {})
        if summary:
            html_parts.append('<div class="diff-summary">')
            html_parts.append(f'<span class="added">鏂板: {summary.get("added", 0)}</span>')
            html_parts.append(f'<span class="removed">鍒犻櫎: {summary.get("removed", 0)}</span>')
            html_parts.append(f'<span class="modified">淇敼: {summary.get("modified", 0)}</span>')
            html_parts.append('</div>')
        
        # 宸ヤ綔琛ㄥ樊寮?
        sheets = diff_data.get('sheets', {})
        for sheet_name, sheet_data in sheets.items():
            html_parts.append(f'<div class="sheet-container" data-sheet="{sheet_name}">')
            html_parts.append(f'<h4 class="sheet-title">宸ヤ綔琛? {sheet_name}</h4>')
            
            # 琛ㄦ牸鍐呭

            if 'rows' in sheet_data and sheet_data['rows']:
                html_parts.append('<div class="table-container">')
                html_parts.append('<table class="excel-diff-table">')
                
                # 琛ㄥご

                headers = sheet_data.get('headers', [])
                if headers:
                    html_parts.append('<thead><tr>')
                    html_parts.append('<th>琛屽彿</th><th>鐘舵€?/th>')
                    for header in headers:
                        html_parts.append(f'<th>{header}</th>')
                    html_parts.append('</tr></thead>')
                
                # 琛ㄦ牸琛?
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
        """鐢熸垚Excel宸紓鐨凜SS鏍峰紡"""
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
        """鐢熸垚Excel宸紓鐨凧avaScript浠ｇ爜"""
        return """
        // Excel宸紓琛ㄦ牸浜や簰鍔熻兘
        document.addEventListener('DOMContentLoaded', function() {
            // 琛ㄦ牸琛岀偣鍑婚珮浜?            const rows = document.querySelectorAll('.excel-diff-table tbody tr');
            rows.forEach(row => {
                row.addEventListener('click', function() {
                    // 绉婚櫎鍏朵粬琛岀殑楂樹寒
                    rows.forEach(r => r.classList.remove('selected'));
                    // 娣诲姞褰撳墠琛岄珮浜?                    this.classList.add('selected');
                });
            });
            
            // 宸ヤ綔琛ㄥ垏鎹㈠姛鑳?            const sheetContainers = document.querySelectorAll('.sheet-container');
            if (sheetContainers.length > 1) {
                // 濡傛灉鏈夊涓伐浣滆〃锛屾坊鍔犲垏鎹㈠姛鑳?                sheetContainers.forEach((container, index) => {
                    if (index > 0) {
                        container.style.display = 'none';
                    }
                });
                
                // 娣诲姞宸ヤ綔琛ㄥ垏鎹㈡寜閽?                const tabContainer = document.createElement('div');
                tabContainer.className = 'sheet-tabs';
                tabContainer.innerHTML = '<style>.sheet-tabs{margin:10px 0;}.sheet-tab{display:inline-block;padding:8px 16px;margin-right:5px;background:#f8f9fa;border:1px solid #dee2e6;cursor:pointer;border-radius:3px;}.sheet-tab.active{background:#007bff;color:white;}</style>';
                
                sheetContainers.forEach((container, index) => {
                    const sheetName = container.getAttribute('data-sheet');
                    const tab = document.createElement('span');
                    tab.className = 'sheet-tab' + (index === 0 ? ' active' : '');
                    tab.textContent = sheetName;
                    tab.addEventListener('click', function() {
                        // 闅愯棌鎵€鏈夊伐浣滆〃
                        sheetContainers.forEach(c => c.style.display = 'none');
                        // 鏄剧ず閫変腑鐨勫伐浣滆〃
                        container.style.display = 'block';
                        // 鏇存柊鏍囩鐘舵€?                        document.querySelectorAll('.sheet-tab').forEach(t => t.classList.remove('active'));
                        this.classList.add('active');
                    });
                    tabContainer.appendChild(tab);
                });
                
                // 鎻掑叆鍒扮涓€涓伐浣滆〃鍓嶉潰
                sheetContainers[0].parentNode.insertBefore(tabContainer, sheetContainers[0]);
            }
        });
        
        // 娣诲姞閫変腑琛岀殑CSS鏍峰紡
        const style = document.createElement('style');
        style.textContent = '.excel-diff-table tbody tr.selected { background-color: rgba(0, 123, 255, 0.2) !important; }';
        document.head.appendChild(style);
        """
    
    def cleanup_old_version_cache(self):
        """娓呯悊鏃х増鏈殑HTML缂撳瓨"""
        try:
            ExcelHtmlCache, = get_runtime_models("ExcelHtmlCache")
            old_caches = ExcelHtmlCache.query.filter(
                ExcelHtmlCache.diff_version != self.current_version
            ).all()
            
            count = len(old_caches)
            for cache in old_caches:
                self.db.session.delete(cache)
            
            self.db.session.commit()
            # 娓呯悊鏃х増鏈紦瀛樺畬鎴?
            return count
            
        except Exception as e:
            # 娓呯悊鏃х増鏈紦瀛樺け璐?
            self.db.session.rollback()
            return 0
    
    def cleanup_expired_cache(self):
        """娓呯悊杩囨湡鐨凥TML缂撳瓨锛堝熀浜庡垱寤烘椂闂达紝瓒呰繃7澶╃殑缂撳瓨锛?"""
        try:
            ExcelHtmlCache, = get_runtime_models("ExcelHtmlCache")
            from datetime import datetime, timedelta
            
            # HTML缂撳瓨淇濈暀7澶?
            expire_time = datetime.utcnow() - timedelta(days=7)
            
            expired_caches = ExcelHtmlCache.query.filter(
                ExcelHtmlCache.created_at < expire_time
            ).all()
            
            count = len(expired_caches)
            for cache in expired_caches:
                self.db.session.delete(cache)
            
            if count > 0:
                self.db.session.commit()
                # 娓呯悊杩囨湡缂撳瓨瀹屾垚

            
            return count
            
        except Exception as e:
            # 娓呯悊杩囨湡缂撳瓨澶辫触

            self.db.session.rollback()
            return 0
    
    def get_cache_statistics(self, repository_id=None):
        """鑾峰彇HTML缂撳瓨缁熻淇℃伅"""
        try:
            ExcelHtmlCache, flask_app = get_runtime_models("ExcelHtmlCache", "app")
            
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
            # 鑾峰彇缂撳瓨缁熻澶辫触

            return {
                'total_count': 0,
                'completed_count': 0,
                'current_version_count': 0,
                'old_version_count': 0,
                'total_size_mb': 0,
                'current_version': self.current_version
            }
    
    def get_cache_statistics_by_repositories(self, repository_ids):
        """鑾峰彇鎸囧畾浠撳簱鍒楄〃鐨凥TML缂撳瓨缁熻淇℃伅"""
        try:
            ExcelHtmlCache, flask_app = get_runtime_models("ExcelHtmlCache", "app")
            
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
            return {
                'total_count': 0,
                'completed_count': 0,
                'current_version_count': 0,
                'old_version_count': 0,
                'total_size_mb': 0.0,
                'current_version': self.current_version
            }
    
    def delete_html_cache(self, repository_id: int, commit_id: str, file_path: str) -> int:
        """鍒犻櫎鎸囧畾鐨凥TML缂撳瓨"""
        try:
            ExcelHtmlCache, flask_app = get_runtime_models("ExcelHtmlCache", "app")
            
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
            self.db.session.rollback()
            return 0





