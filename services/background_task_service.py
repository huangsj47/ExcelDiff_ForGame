#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""后台任务管理服务 - 用于暂停和恢复后台缓存任务

## 这个开关是给谁用的

「重新计算合并 diff」那条路（`services/commit_operation_handlers.refresh_merge_diff`）
要在删掉一批 Excel 缓存之后让页面重算，而**后台工作线程正好也在算同一批文件**：
它每算完一个就把结果写回缓存，于是刚删掉的行被原样写回来，用户刷新看到的还是旧结果 ——
「点了重新计算，什么都没变」。

所以那里在删缓存前后设/清这个标志（`pause_background_tasks` / `resume_background_tasks`），
真正读它的是工作线程主循环（`services/task_worker_service.background_task_worker`，
每轮领任务之前 `is_tasks_paused()`）。

**曾经这里只有写没有读。** `pause_background_tasks()` 只改了一个变量、
`wait_if_paused()` 零调用者，工作线程从来没看过它 —— 于是日志里印着
「🔄 临时暂停后台缓存任务处理...」，后台照旧在跑。这是一条**读日志看不出来**的缺陷：
两边的话都说了，只有中间那条线没接上。

## 为什么是「跳过这一轮」而不是「在工作线程里阻塞等待」

`wait_if_paused()` 那种 `while True: sleep(0.1)` 的无限等待**不要在领到任务之后调**：
工作线程一阻塞，心跳与任务租约的续期就一起停了，节点会被判离线、
任务会被别的节点领走（见 `agent/task_heartbeat.py` 存在的理由）。所以工作线程用
「这一轮什么都不领、睡一下再回来」的形态，而 `wait_if_paused()` 留给
「已经拿着任务、明确知道自己该等」的调用方。
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
