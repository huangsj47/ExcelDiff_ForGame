#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
后台任务管理服务 - 用于暂停和恢复后台缓存任务
"""

import threading
import time

# 全局任务控制变量
_task_paused = False
_pause_lock = threading.Lock()

def pause_background_tasks():
    """暂停后台缓存任务"""
    global _task_paused
    with _pause_lock:
        _task_paused = True
        print("⏸️ 后台缓存任务已暂停")

def resume_background_tasks():
    """恢复后台缓存任务"""
    global _task_paused
    with _pause_lock:
        _task_paused = False
        print("▶️ 后台缓存任务已恢复")

def is_tasks_paused():
    """检查任务是否被暂停"""
    with _pause_lock:
        return _task_paused

def wait_if_paused():
    """如果任务被暂停，等待恢复"""
    while True:
        with _pause_lock:
            if not _task_paused:
                break
        time.sleep(0.1)  # 等待100ms后重新检查
