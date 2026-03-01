#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
清理任务
"""

import os
import shutil
from utils.safe_print import log_print


def cleanup_pending_deletions():
    """清理待删除的仓库目录"""
    try:
        # 检查是否有待删除的目录标记文件
        pending_deletions_file = "pending_deletions.txt"
        
        if not os.path.exists(pending_deletions_file):
            log_print("ℹ️ 没有发现需要自动分析的仓库", 'CLEANUP')
            return
        
        with open(pending_deletions_file, 'r', encoding='utf-8') as f:
            directories = [line.strip() for line in f.readlines() if line.strip()]
        
        if not directories:
            os.remove(pending_deletions_file)
            return
        
        deleted_count = 0
        failed_deletions = []
        
        for directory in directories:
            try:
                if os.path.exists(directory):
                    shutil.rmtree(directory)
                    log_print(f"✅ 已删除目录: {directory}", 'CLEANUP')
                    deleted_count += 1
                else:
                    log_print(f"ℹ️ 目录不存在，跳过: {directory}", 'CLEANUP')
            except Exception as e:
                log_print(f"❌ 删除目录失败: {directory}, 错误: {e}", 'CLEANUP', force=True)
                failed_deletions.append(directory)
        
        # 更新待删除列表
        if failed_deletions:
            with open(pending_deletions_file, 'w', encoding='utf-8') as f:
                for directory in failed_deletions:
                    f.write(f"{directory}\n")
            log_print(f"⚠️ {len(failed_deletions)} 个目录删除失败，将在下次重试", 'CLEANUP')
        else:
            os.remove(pending_deletions_file)
            log_print(f"✅ 所有待删除目录已清理完成，共删除 {deleted_count} 个目录", 'CLEANUP')
        
    except Exception as e:
        log_print(f"清理待删除目录失败: {e}", 'CLEANUP', force=True)


def schedule_cleanup_task():
    """调度清理任务"""
    try:
        from .background_tasks import add_task_to_queue
        
        task_data = {
            'type': 'cleanup_cache',
            'data': {
                'cleanup_type': 'general'
            }
        }
        
        add_task_to_queue(task_data, priority=8)  # 较低优先级
        log_print("📋 已调度缓存清理任务", 'CLEANUP')
        
    except Exception as e:
        log_print(f"调度清理任务失败: {e}", 'CLEANUP', force=True)


def cleanup_temp_files():
    """清理临时文件"""
    try:
        temp_dirs = ['temp', 'tmp', 'cache']
        cleaned_count = 0
        
        for temp_dir in temp_dirs:
            if os.path.exists(temp_dir):
                for root, dirs, files in os.walk(temp_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        try:
                            # 检查文件是否超过1天
                            import time
                            if time.time() - os.path.getmtime(file_path) > 86400:  # 24小时
                                os.remove(file_path)
                                cleaned_count += 1
                        except Exception as e:
                            log_print(f"删除临时文件失败: {file_path}, 错误: {e}", 'CLEANUP')
        
        if cleaned_count > 0:
            log_print(f"✅ 清理了 {cleaned_count} 个临时文件", 'CLEANUP')
        
    except Exception as e:
        log_print(f"清理临时文件失败: {e}", 'CLEANUP', force=True)


def cleanup_log_files():
    """清理旧的日志文件"""
    try:
        log_dir = "logs"
        if not os.path.exists(log_dir):
            return
        
        import time
        current_time = time.time()
        cleaned_count = 0
        
        for file in os.listdir(log_dir):
            if file.endswith('.log') and file != 'runlog.log':
                file_path = os.path.join(log_dir, file)
                try:
                    # 删除超过7天的日志文件
                    if current_time - os.path.getmtime(file_path) > 604800:  # 7天
                        os.remove(file_path)
                        cleaned_count += 1
                except Exception as e:
                    log_print(f"删除日志文件失败: {file_path}, 错误: {e}", 'CLEANUP')
        
        if cleaned_count > 0:
            log_print(f"✅ 清理了 {cleaned_count} 个旧日志文件", 'CLEANUP')
        
    except Exception as e:
        log_print(f"清理日志文件失败: {e}", 'CLEANUP', force=True)
