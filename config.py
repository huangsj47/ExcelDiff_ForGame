#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
应用配置文件
"""

import os
import sys

# 设置控制台输出编码为UTF-8
if sys.platform == 'win32':
    import codecs
    import io
    # 设置UTF-8编码并启用错误处理
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    # 设置控制台代码页为UTF-8
    os.system('chcp 65001 >nul 2>&1')

# 设置窗口标题
from os import system
system("title SEOTool - diff-confirmation-platform")

# Diff逻辑版本号 - 当diff算法或逻辑发生变化时需要更新此版本号
DIFF_LOGIC_VERSION = "1.8.0"

# 日志级别配置
LOG_LEVEL = {
    'APP_VERBOSE': True,      # 应用主要日志
    'GIT_VERBOSE': True,     # Git操作详细日志
    'CACHE_VERBOSE': True,   # 缓存操作详细日志
    'DIFF_VERBOSE': True,    # Diff计算详细日志
    'SVN_VERBOSE': True,     # SVN操作详细日志
    'EXCEL_VERBOSE': True,   # Excel处理详细日志
    'LOGGING_VERBOSE': True   # 通用日志输出（重载print函数使用）
}

# Flask配置
class Config:
    SECRET_KEY = 'your-secret-key-here'
    SQLALCHEMY_DATABASE_URI = f'sqlite:///{os.path.abspath("instance/diff_platform.db")}'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    # 服务器配置
    HOST = '0.0.0.0'
    PORT = 8002
    DEBUG = False
    USE_RELOADER = False
    THREADED = True

# 数据库配置
DATABASE_CONFIG = {
    'db_path': os.path.abspath("instance/diff_platform.db"),
    'instance_dir': os.path.abspath("instance"),
}

# 后台任务配置
BACKGROUND_TASK_CONFIG = {
    'max_workers': 4,
    'queue_timeout': 30,
    'retry_limit': 3,
    'cleanup_interval': 3600,  # 1小时
}

# 缓存配置
CACHE_CONFIG = {
    'long_processing_threshold': 10.0,  # 秒
    'long_processing_expire_days': 90,  # 天
    'max_cache_entries': 1000,
    'cleanup_batch_size': 100,
}

# 定时任务配置
SCHEDULE_CONFIG = {
    'cleanup_time': "04:00",  # 每天4点清理
    'weekly_sync_interval': 2,  # 每2分钟检查周版本同步
    'repo_sync_interval': 10,  # 每10分钟自动同步仓库新提交
}
