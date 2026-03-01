"""
鍛ㄧ増鏈珽xcel鍚堝苟diff缂撳瓨鏈嶅姟
鎻愪緵鍛ㄧ増鏈珽xcel鏂囦欢鍚堝苟diff鐨凥TML缂撳瓨鍔熻兘
"""
import os
import json
import time
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List
from flask import render_template
from flask_sqlalchemy import SQLAlchemy
from services.model_loader import get_runtime_models


class WeeklyExcelCacheService:
    """鍛ㄧ増鏈珽xcel缂撳瓨鏈嶅姟绫?"""

    def __init__(self, db, diff_logic_version):
        self.db = db
        self.diff_logic_version = diff_logic_version
        self.max_cache_count = 1000  # 鏈€澶氫繚鐣?000鏉＄紦瀛?
        self.expire_days = 90  # 缂撳瓨淇濈暀90澶?
        self.processing_cache = set()  # 姝ｅ湪澶勭悊鐨勭紦瀛橀敭闆嗗悎
        self.operation_logs = []  # 鎿嶄綔鏃ュ織鍒楄〃
    
    def generate_cache_key(self, config_id: int, file_path: str, base_commit_id: str, latest_commit_id: str) -> str:
        """鐢熸垚缂撳瓨閿?"""
        key_data = f"{config_id}:{file_path}:{base_commit_id}:{latest_commit_id}:{self.diff_logic_version}"
        return hashlib.md5(key_data.encode('utf-8')).hexdigest()
    
    def is_excel_file(self, file_path: str) -> bool:
        """鍒ゆ柇鏄惁涓篍xcel鏂囦欢"""
        excel_extensions = ['.xlsx', '.xls', '.csv']
        return any(file_path.lower().endswith(ext) for ext in excel_extensions)

    def log_cache_operation(self, message, log_type='info', repository_id=None, config_id=None, file_path=None):
        """璁板綍缂撳瓨鎿嶄綔鏃ュ織鍒版暟鎹簱"""
        try:
            # 閬垮厤寰幆瀵煎叆锛岀洿鎺ヤ娇鐢ㄥ凡鏈夌殑鏁版嵁搴撹繛鎺?
            # 涓嶅啀閲嶅鍒涘缓搴旂敤涓婁笅鏂囷紝浣跨敤褰撳墠涓婁笅鏂?

            # 鍔ㄦ€佸鍏ワ紝閬垮厤寰幆瀵煎叆闂
            OperationLog, = get_runtime_models("OperationLog")

            # 淇濆瓨鍒版暟鎹簱锛堜笉鍒涘缓鏂扮殑搴旂敤涓婁笅鏂囷級
            log_entry = OperationLog(
                log_type=log_type,
                message=message,
                source='weekly_excel_cache',
                repository_id=repository_id,
                config_id=config_id,
                file_path=file_path
            )
            self.db.session.add(log_entry)

            # 娓呯悊瓒呰繃200鏉＄殑鏃ф棩蹇?
            self._cleanup_old_logs()

            self.db.session.commit()

            # 鍚屾椂淇濇寔鍐呭瓨涓殑鏃ュ織锛堢敤浜庡悜鍚庡吋瀹癸級锛屼娇鐢ㄥ寳浜椂闂?
            from utils.timezone_utils import now_beijing
            timestamp = now_beijing().strftime('%Y/%m/%d %H:%M:%S')
            memory_log = {
                'time': timestamp,
                'message': message,
                'type': log_type
            }
            self.operation_logs.append(memory_log)
            # 淇濇寔鏈€澶?00鏉″唴瀛樻棩蹇?
            if len(self.operation_logs) > 100:
                self.operation_logs = self.operation_logs[-100:]

        except Exception as e:
            # 濡傛灉鏁版嵁搴撴搷浣滃け璐ワ紝鑷冲皯淇濇寔鍐呭瓨鏃ュ織锛屼娇鐢ㄥ寳浜椂闂?
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
                pass  # 濡傛灉杩炲唴瀛樻棩蹇楅兘澶辫触锛屽氨蹇界暐

            # 浣跨敤print鑰屼笉鏄痩og_print锛岄伩鍏嶅惊鐜鍏?
            print(f"[ERROR] 璁板綍缂撳瓨鎿嶄綔鏃ュ織澶辫触: {e}")

    def _cleanup_old_logs(self):
        """娓呯悊瓒呰繃200鏉＄殑鏃ф棩蹇?"""
        try:
            # 鍔ㄦ€佸鍏ワ紝閬垮厤寰幆瀵煎叆闂
            OperationLog, = get_runtime_models("OperationLog")

            # 鑾峰彇褰撳墠鏃ュ織鎬绘暟
            total_count = OperationLog.query.filter_by(source='weekly_excel_cache').count()

            if total_count > 200:
                # 鍒犻櫎鏈€鏃х殑鏃ュ織锛屼繚鐣欐渶鏂扮殑200鏉?
                excess_count = total_count - 200
                old_logs = OperationLog.query.filter_by(source='weekly_excel_cache').order_by(OperationLog.created_at.asc()).limit(excess_count).all()

                for log in old_logs:
                    self.db.session.delete(log)

        except Exception as e:
            # 浣跨敤print鑰屼笉鏄痩og_print锛岄伩鍏嶅惊鐜鍏?
            print(f"[ERROR] 娓呯悊鏃у懆鐗堟湰鎿嶄綔鏃ュ織澶辫触: {e}")
    
    def needs_merged_diff_cache(self, config_id: int, file_path: str) -> bool:
        """鍒ゆ柇鏄惁闇€瑕佸悎骞禿iff缂撳瓨锛堝彧鏈夊娆¤繛缁彁浜ょ殑Excel鏂囦欢鎵嶉渶瑕侊級"""
        try:
            # 妫€鏌ユ槸鍚︿负Excel鏂囦欢
            if not self.is_excel_file(file_path):
                print(f"Debug: {file_path} is not an Excel file")
                return False

            print(f"Debug: {file_path} is an Excel file, checking diff cache...")

            # 鏆傛椂绠€鍖栭€昏緫锛岀洿鎺ヨ繑鍥濼rue璁╂墍鏈塃xcel鏂囦欢閮介噸寤虹紦瀛?
            # 杩欐牱鍙互閬垮厤澶嶆潅鐨勬暟鎹簱鏌ヨ瀵艰嚧鐨勫崱姝婚棶棰?
            print(f"Debug: Returning True for {file_path} (simplified logic)")
            return True

        except Exception as e:
            # 娣诲姞閿欒鏃ュ織
            print(f"Error in needs_merged_diff_cache: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def get_cached_html(self, config_id: int, file_path: str, base_commit_id: str, latest_commit_id: str) -> Optional[Dict[str, Any]]:
        """鑾峰彇缂撳瓨鐨凥TML鍐呭"""
        try:
            # 鍔ㄦ€佸鍏ワ紝閬垮厤寰幆瀵煎叆闂
            WeeklyVersionExcelCache, = get_runtime_models("WeeklyVersionExcelCache")

            # 涓嶅垱寤烘柊鐨勫簲鐢ㄤ笂涓嬫枃锛屼娇鐢ㄥ綋鍓嶄笂涓嬫枃
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
            # 闈欓粯澶勭悊閿欒锛岄伩鍏嶆棩蹇楁薄鏌?
            return None
    
    def save_html_cache(self, config_id: int, repository_id: int, file_path: str,
                       base_commit_id: str, latest_commit_id: str, commit_count: int,
                       html_content: str, css_content: str = "", js_content: str = "",
                       metadata: Dict[str, Any] = None, processing_time: float = 0) -> bool:
        """淇濆瓨HTML缂撳瓨"""
        try:
            # 鍔ㄦ€佸鍏ワ紝閬垮厤寰幆瀵煎叆闂
            WeeklyVersionExcelCache, = get_runtime_models("WeeklyVersionExcelCache")

            # 涓嶅垱寤烘柊鐨勫簲鐢ㄤ笂涓嬫枃锛屼娇鐢ㄥ綋鍓嶄笂涓嬫枃
            cache_key = self.generate_cache_key(config_id, file_path, base_commit_id, latest_commit_id)

            # 妫€鏌ユ槸鍚﹀凡瀛樺湪
            existing_cache = WeeklyVersionExcelCache.query.filter_by(
                config_id=config_id,
                file_path=file_path,
                base_commit_id=base_commit_id,
                latest_commit_id=latest_commit_id,
                diff_version=self.diff_logic_version
            ).first()

            if existing_cache:
                # 鏇存柊鐜版湁缂撳瓨
                existing_cache.html_content = html_content
                existing_cache.css_content = css_content
                existing_cache.js_content = js_content
                existing_cache.cache_metadata = json.dumps(metadata) if metadata else None
                existing_cache.cache_status = 'completed'
                existing_cache.processing_time = processing_time
                existing_cache.updated_at = datetime.now(timezone.utc)
            else:
                # 鍒涘缓鏂扮紦瀛?
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
        """娓呯悊杩囨湡缂撳瓨锛堣秴杩?0澶╋級"""
        try:
            WeeklyVersionExcelCache, flask_app = get_runtime_models("WeeklyVersionExcelCache", "app")
            
            with flask_app.app_context():
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
        """娓呯悊瓒呰繃1000鏉＄殑鏃х紦瀛?"""
        try:
            WeeklyVersionExcelCache, flask_app = get_runtime_models("WeeklyVersionExcelCache", "app")
            
            with flask_app.app_context():
                total_count = WeeklyVersionExcelCache.query.count()
                
                if total_count <= self.max_cache_count:
                    return 0
                
                # 鍒犻櫎鏈€鏃х殑缂撳瓨锛屼繚鐣欐渶鏂扮殑1000鏉?
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
        """鑾峰彇缂撳瓨缁熻淇℃伅"""
        try:
            # 鍔ㄦ€佸鍏ワ紝閬垮厤寰幆瀵煎叆闂
            WeeklyVersionExcelCache, = get_runtime_models("WeeklyVersionExcelCache")

            # 涓嶉渶瑕?app_context锛屽洜涓哄湪Flask璇锋眰涓凡缁忔湁浜?
            total_count = WeeklyVersionExcelCache.query.count()
            completed_count = WeeklyVersionExcelCache.query.filter_by(cache_status='completed').count()
            processing_count = WeeklyVersionExcelCache.query.filter_by(cache_status='processing').count()
            failed_count = WeeklyVersionExcelCache.query.filter_by(cache_status='failed').count()

            # 璁＄畻缂撳瓨澶у皬锛堜及绠楋級- 浣跨敤鏇村揩鐨勬柟娉?
            total_size = 0
            try:
                # 鍙绠楀凡瀹屾垚鐨勭紦瀛樼殑澶у皬锛岄伩鍏嶉暱鏃堕棿鏌ヨ
                completed_caches = WeeklyVersionExcelCache.query.filter_by(cache_status='completed').limit(100).all()
                for cache in completed_caches:
                    if cache.html_content:
                        total_size += len(cache.html_content.encode('utf-8'))

                # 濡傛灉鏈夋洿澶氱紦瀛橈紝鎸夋瘮渚嬩及绠?
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
        """鑾峰彇鎸囧畾椤圭洰鐨勫懆鐗堟湰Excel缂撳瓨缁熻淇℃伅"""
        try:
            WeeklyVersionExcelCache, WeeklyVersionConfig = get_runtime_models("WeeklyVersionExcelCache", "WeeklyVersionConfig")

            # 鑾峰彇璇ラ」鐩笅鎵€鏈夊懆鐗堟湰閰嶇疆鐨処D
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

            # 缁熻璇ラ」鐩笅鐨勫懆鐗堟湰Excel缂撳瓨
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

            # 璁＄畻缂撳瓨澶у皬锛堜及绠楋級- 浣跨敤鏇村揩鐨勬柟娉?
            total_size = 0
            try:
                # 鍙绠楀凡瀹屾垚鐨勭紦瀛樼殑澶у皬锛岄伩鍏嶉暱鏃堕棿鏌ヨ
                completed_caches = WeeklyVersionExcelCache.query.filter(
                    WeeklyVersionExcelCache.config_id.in_(config_ids),
                    WeeklyVersionExcelCache.cache_status == 'completed'
                ).limit(100).all()

                for cache in completed_caches:
                    if cache.html_content:
                        total_size += len(cache.html_content.encode('utf-8'))

                # 濡傛灉鏈夋洿澶氱紦瀛橈紝鎸夋瘮渚嬩及绠?
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
        """娓呯悊鎵€鏈夌紦瀛?"""
        try:
            # 鍔ㄦ€佸鍏ワ紝閬垮厤寰幆瀵煎叆闂
            WeeklyVersionExcelCache, = get_runtime_models("WeeklyVersionExcelCache")

            # 涓嶉渶瑕侀澶栫殑app_context锛屽洜涓鸿繖涓柟娉曞凡缁忓湪Flask璇锋眰涓婁笅鏂囦腑杩愯
            count = WeeklyVersionExcelCache.query.count()
            WeeklyVersionExcelCache.query.delete()
            self.db.session.commit()
            print(f"鉁?宸叉竻鐞?{count} 鏉″懆鐗堟湰Excel缂撳瓨")
            return count

        except Exception as e:
            print(f"鉂?娓呯悊鍛ㄧ増鏈珽xcel缂撳瓨澶辫触: {e}")
            import traceback
            traceback.print_exc()
            self.db.session.rollback()
            return 0






