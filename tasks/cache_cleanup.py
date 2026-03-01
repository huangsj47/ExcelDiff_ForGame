#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
缓存清理任务
"""

from datetime import datetime, timezone, timedelta
from utils.safe_print import log_print
from config import DIFF_LOGIC_VERSION, CACHE_CONFIG


def clear_version_mismatch_cache():
    """清理版本不匹配的缓存"""
    try:
        log_print(f"检查并清理版本不匹配的缓存 (当前版本: {DIFF_LOGIC_VERSION})", 'CACHE')
        
        from models import db, DiffCache, ExcelHtmlCache, MergedDiffCache
        
        # 清理DiffCache中版本不匹配的记录
        diff_cache_count = DiffCache.query.filter(
            DiffCache.diff_version != DIFF_LOGIC_VERSION
        ).count()
        
        if diff_cache_count > 0:
            DiffCache.query.filter(
                DiffCache.diff_version != DIFF_LOGIC_VERSION
            ).delete()
            log_print(f"清理了 {diff_cache_count} 个版本不匹配的DiffCache记录", 'CACHE')
        
        # 清理ExcelHtmlCache中版本不匹配的记录
        excel_cache_count = ExcelHtmlCache.query.filter(
            ExcelHtmlCache.diff_version != DIFF_LOGIC_VERSION
        ).count()
        
        if excel_cache_count > 0:
            ExcelHtmlCache.query.filter(
                ExcelHtmlCache.diff_version != DIFF_LOGIC_VERSION
            ).delete()
            log_print(f"清理了 {excel_cache_count} 个版本不匹配的ExcelHtmlCache记录", 'CACHE')
        
        # 清理MergedDiffCache中版本不匹配的记录
        merged_cache_count = MergedDiffCache.query.filter(
            MergedDiffCache.diff_version != DIFF_LOGIC_VERSION
        ).count()
        
        if merged_cache_count > 0:
            MergedDiffCache.query.filter(
                MergedDiffCache.diff_version != DIFF_LOGIC_VERSION
            ).delete()
            log_print(f"清理了 {merged_cache_count} 个版本不匹配的MergedDiffCache记录", 'CACHE')
        
        total_cleaned = diff_cache_count + excel_cache_count + merged_cache_count
        if total_cleaned > 0:
            db.session.commit()
            log_print(f"✅ 版本不匹配缓存清理完成，共清理 {total_cleaned} 条记录", 'CACHE')
        else:
            log_print("无需清理版本不匹配的缓存", 'CACHE')
        
    except Exception as e:
        log_print(f"清理版本不匹配缓存失败: {e}", 'CACHE', force=True)
        import traceback
        traceback.print_exc()


def cleanup_old_cache_entries():
    """清理过期的缓存条目"""
    try:
        from models import db, DiffCache
        
        now = datetime.now(timezone.utc)
        
        # 清理过期的缓存
        expired_count = DiffCache.query.filter(
            DiffCache.expire_at < now
        ).count()
        
        if expired_count > 0:
            DiffCache.query.filter(
                DiffCache.expire_at < now
            ).delete()
            
            db.session.commit()
            log_print(f"✅ 清理了 {expired_count} 个过期的缓存条目", 'CACHE')
        else:
            log_print("没有过期的缓存条目需要清理", 'CACHE')
        
        # 清理超出数量限制的缓存
        cleanup_excess_cache_entries()
        
    except Exception as e:
        log_print(f"清理过期缓存失败: {e}", 'CACHE', force=True)


def cleanup_excess_cache_entries():
    """清理超出数量限制的缓存条目"""
    try:
        from models import db, DiffCache
        
        max_entries = CACHE_CONFIG['max_cache_entries']
        current_count = DiffCache.query.count()
        
        if current_count > max_entries:
            excess_count = current_count - max_entries
            
            # 删除最旧的缓存条目
            old_entries = DiffCache.query.order_by(
                DiffCache.created_at.asc()
            ).limit(excess_count).all()
            
            for entry in old_entries:
                db.session.delete(entry)
            
            db.session.commit()
            log_print(f"✅ 清理了 {excess_count} 个超出限制的缓存条目", 'CACHE')
        
    except Exception as e:
        log_print(f"清理超出限制的缓存失败: {e}", 'CACHE', force=True)


def process_cleanup_task(task_data):
    """处理清理任务"""
    try:
        cleanup_type = task_data.get('data', {}).get('cleanup_type', 'general')
        
        if cleanup_type == 'version_mismatch':
            clear_version_mismatch_cache()
        elif cleanup_type == 'expired':
            cleanup_old_cache_entries()
        else:
            # 通用清理
            clear_version_mismatch_cache()
            cleanup_old_cache_entries()
        
        return True
        
    except Exception as e:
        log_print(f"处理清理任务失败: {e}", 'CACHE', force=True)
        return False
