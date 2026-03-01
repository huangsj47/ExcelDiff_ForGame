#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
后台任务管理
"""

import threading
import queue
import time
from datetime import datetime, timezone
from utils.safe_print import log_print
from config import BACKGROUND_TASK_CONFIG


# 全局变量
task_queue = queue.PriorityQueue()
background_task_running = False
background_task_thread = None


class TaskWrapper:
    """任务包装器，用于优先级队列"""
    def __init__(self, priority, counter, task):
        self.priority = priority
        self.counter = counter
        self.task = task
    
    def __lt__(self, other):
        # 优先级数字越小，优先级越高
        if self.priority != other.priority:
            return self.priority < other.priority
        # 优先级相同时，按计数器排序（先进先出）
        return self.counter < other.counter


def add_task_to_queue(task_data, priority=10):
    """添加任务到队列"""
    try:
        import time
        counter = int(time.time() * 1000000)  # 微秒级时间戳作为计数器
        
        task_wrapper = TaskWrapper(priority, counter, task_data)
        task_queue.put(task_wrapper)
        
        log_print(f"📋 任务已添加到队列: {task_data.get('type', 'unknown')} (优先级: {priority})", 'TASK')
        return True
        
    except Exception as e:
        log_print(f"添加任务到队列失败: {e}", 'TASK', force=True)
        return False


def process_background_tasks():
    """处理后台任务的工作线程"""
    global background_task_running
    
    log_print("后台任务工作线程启动", 'TASK')
    log_print(f"初始队列大小: {task_queue.qsize()}", 'TASK')
    
    while background_task_running:
        try:
            # 从队列获取任务，超时时间为30秒
            task_wrapper = task_queue.get(timeout=BACKGROUND_TASK_CONFIG['queue_timeout'])
            task_data = task_wrapper.task
            
            log_print(f"🔄 开始处理任务: {task_data.get('type', 'unknown')}", 'TASK')
            
            # 根据任务类型处理任务
            success = process_single_task(task_data)
            
            if success:
                log_print(f"✅ 任务处理完成: {task_data.get('type', 'unknown')}", 'TASK')
            else:
                log_print(f"❌ 任务处理失败: {task_data.get('type', 'unknown')}", 'TASK', force=True)
            
            # 标记任务完成
            task_queue.task_done()
            
        except queue.Empty:
            # 队列为空，继续等待
            continue
        except Exception as e:
            log_print(f"处理后台任务异常: {e}", 'TASK', force=True)
            import traceback
            traceback.print_exc()
    
    log_print("后台任务工作线程停止", 'TASK')


def process_single_task(task_data):
    """处理单个任务"""
    try:
        task_type = task_data.get('type', '')
        
        if task_type == 'excel_diff':
            from .excel_tasks import process_excel_diff_task
            return process_excel_diff_task(task_data)
        elif task_type == 'cleanup_cache':
            from .cache_cleanup import process_cleanup_task
            return process_cleanup_task(task_data)
        elif task_type == 'weekly_sync':
            from .weekly_sync_tasks import process_weekly_sync_task
            return process_weekly_sync_task(task_data)
        elif task_type == 'weekly_excel_cache':
            from .weekly_excel_tasks import process_weekly_excel_cache_task
            return process_weekly_excel_cache_task(task_data)
        else:
            log_print(f"未知任务类型: {task_type}", 'TASK', force=True)
            return False
            
    except Exception as e:
        log_print(f"处理任务失败: {e}", 'TASK', force=True)
        import traceback
        traceback.print_exc()
        return False


def start_background_task_worker():
    """启动后台任务工作线程"""
    global background_task_running, background_task_thread
    
    if background_task_running:
        log_print("后台任务工作线程已在运行", 'TASK')
        return
    
    try:
        # 从数据库加载待处理任务
        load_pending_tasks_from_db()
        
        background_task_running = True
        background_task_thread = threading.Thread(target=process_background_tasks, daemon=True)
        background_task_thread.start()
        
        log_print("后台任务工作线程已启动", 'TASK')
        
    except Exception as e:
        log_print(f"启动后台任务工作线程失败: {e}", 'TASK', force=True)
        background_task_running = False


def stop_background_task_worker():
    """停止后台任务工作线程"""
    global background_task_running, background_task_thread
    
    if not background_task_running:
        return
    
    background_task_running = False
    
    # 等待线程结束
    if background_task_thread and background_task_thread.is_alive():
        background_task_thread.join(timeout=5)


def load_pending_tasks_from_db():
    """从数据库加载待处理任务"""
    try:
        from models import BackgroundTask
        
        pending_tasks = BackgroundTask.query.filter_by(status='pending').all()
        loaded_count = 0
        
        for task in pending_tasks:
            task_data = {
                'id': task.id,
                'type': task.task_type,
                'data': {
                    'repository_id': task.repository_id,
                    'commit_id': task.commit_id,
                    'file_path': task.file_path
                }
            }
            
            if add_task_to_queue(task_data, task.priority):
                loaded_count += 1
        
        log_print(f"从数据库加载了 {loaded_count} 个待处理任务到队列", 'TASK')
        
    except Exception as e:
        log_print(f"从数据库加载任务失败: {e}", 'TASK', force=True)
