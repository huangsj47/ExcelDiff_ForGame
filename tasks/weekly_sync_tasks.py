#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
周版本同步任务
"""

from datetime import datetime, timezone
from utils.safe_print import log_print


def create_weekly_sync_task(config_id):
    """创建周版本同步任务"""
    try:
        from .background_tasks import add_task_to_queue
        
        task_data = {
            'type': 'weekly_sync',
            'data': {
                'config_id': config_id
            }
        }
        
        add_task_to_queue(task_data, priority=5)  # 中等优先级
        log_print(f"📋 已创建周版本同步任务: config_id={config_id}", 'WEEKLY')
        
    except Exception as e:
        log_print(f"创建周版本同步任务失败: {e}", 'WEEKLY', force=True)


def schedule_weekly_sync_tasks():
    """调度周版本同步任务"""
    try:
        from models import WeeklyVersionConfig
        
        # 查找需要同步的活跃配置
        active_configs = WeeklyVersionConfig.query.filter_by(
            is_active=True,
            auto_sync=True,
            status='active'
        ).all()
        
        scheduled_count = 0
        for config in active_configs:
            # 检查是否需要同步（这里可以添加更复杂的逻辑）
            if should_sync_config(config):
                create_weekly_sync_task(config.id)
                scheduled_count += 1
        
        if scheduled_count > 0:
            log_print(f"📋 已调度 {scheduled_count} 个周版本同步任务", 'WEEKLY')
        
    except Exception as e:
        log_print(f"调度周版本同步任务失败: {e}", 'WEEKLY', force=True)


def should_sync_config(config):
    """判断配置是否需要同步"""
    try:
        # 这里可以添加更复杂的同步逻辑
        # 例如：检查最后同步时间、检查是否有新的提交等
        
        # 简单实现：如果配置是活跃的且启用了自动同步，就需要同步
        return config.is_active and config.auto_sync
        
    except Exception as e:
        log_print(f"判断配置同步需求失败: {e}", 'WEEKLY', force=True)
        return False


def process_weekly_sync_task(task_data):
    """处理周版本同步任务"""
    try:
        config_id = task_data.get('data', {}).get('config_id')
        if not config_id:
            log_print("周版本同步任务缺少config_id", 'WEEKLY', force=True)
            return False
        
        from models import WeeklyVersionConfig
        config = WeeklyVersionConfig.query.get(config_id)
        if not config:
            log_print(f"未找到周版本配置: {config_id}", 'WEEKLY', force=True)
            return False
        
        log_print(f"🔄 开始同步周版本配置: {config.name}", 'WEEKLY')
        
        # 执行同步逻辑
        success = sync_weekly_version_config(config)
        
        if success:
            log_print(f"✅ 周版本配置同步完成: {config.name}", 'WEEKLY')
        else:
            log_print(f"❌ 周版本配置同步失败: {config.name}", 'WEEKLY', force=True)
        
        return success
        
    except Exception as e:
        log_print(f"处理周版本同步任务失败: {e}", 'WEEKLY', force=True)
        return False


def sync_weekly_version_config(config):
    """同步周版本配置"""
    try:
        # 这里实现具体的同步逻辑
        # 例如：获取仓库的最新提交、生成diff、更新缓存等
        
        log_print(f"同步配置: {config.name} (仓库: {config.repository.name})", 'WEEKLY')
        
        # 模拟同步过程
        import time
        time.sleep(0.1)  # 模拟处理时间
        
        # 更新最后同步时间
        config.updated_at = datetime.now(timezone.utc)
        
        from models import db
        db.session.commit()
        
        return True
        
    except Exception as e:
        log_print(f"同步周版本配置失败: {e}", 'WEEKLY', force=True)
        return False
