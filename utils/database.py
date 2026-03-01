#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据库工具函数
"""

import os
import sqlite3
from config import DATABASE_CONFIG
from utils.safe_print import log_print


def ensure_instance_directory():
    """确保instance目录存在"""
    instance_dir = DATABASE_CONFIG['instance_dir']
    if not os.path.exists(instance_dir):
        os.makedirs(instance_dir)
        log_print(f"✅ 创建instance目录: {instance_dir}", 'DB')
    else:
        log_print(f"ℹ️ instance目录已存在: {instance_dir}", 'DB')


def create_tables(db):
    """创建数据库表"""
    try:
        ensure_instance_directory()
        
        db_path = DATABASE_CONFIG['db_path']
        log_print(f"数据库路径: {db_path}", 'DB')
        
        # 检查创建前的表状态
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            before_tables = [table[0] for table in cursor.fetchall()]
            conn.close()
            
            log_print(f"创建前的数据库表: {before_tables}", 'DB')
        except Exception as e:
            log_print(f"检查创建前表状态失败: {e}", 'DB')
            before_tables = []
        
        # 创建所有表
        log_print("开始创建数据库表...", 'DB')
        db.create_all()
        log_print("✅ db.create_all() 执行完成", 'DB')
        
        # 检查创建后的表状态
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            final_tables = [table[0] for table in cursor.fetchall()]
            conn.close()
            
            log_print(f"创建后的数据库表: {final_tables}", 'DB')
            
            # 验证必需的表
            expected_tables = [
                'project', 'repository', 'commits_log',
                'background_tasks', 'global_repository_counter',
                'diff_cache', 'excel_html_cache', 'weekly_version_config',
                'weekly_version_diff_cache', 'weekly_version_excel_cache',
                'merged_diff_cache', 'operation_log'
            ]
            
            missing_tables = [t for t in expected_tables if t not in final_tables]
            if missing_tables:
                log_print(f"⚠️ 仍然缺失的表: {missing_tables}", 'DB', force=True)
            else:
                log_print("✅ 所有必需的表都已创建", 'DB')
                
        except Exception as e:
            log_print(f"检查创建后表状态失败: {e}", 'DB', force=True)
            
    except Exception as e:
        log_print(f"创建数据库表失败: {e}", 'DB', force=True)
        import traceback
        traceback.print_exc()
        raise


def get_database_info():
    """获取数据库信息"""
    try:
        db_path = DATABASE_CONFIG['db_path']
        if not os.path.exists(db_path):
            return {
                'exists': False,
                'path': db_path,
                'size': 0,
                'tables': []
            }
        
        # 获取文件大小
        size = os.path.getsize(db_path)
        
        # 获取表列表
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [table[0] for table in cursor.fetchall()]
        conn.close()
        
        return {
            'exists': True,
            'path': db_path,
            'size': size,
            'tables': tables
        }
        
    except Exception as e:
        log_print(f"获取数据库信息失败: {e}", 'DB', force=True)
        return {
            'exists': False,
            'path': DATABASE_CONFIG['db_path'],
            'size': 0,
            'tables': [],
            'error': str(e)
        }


def backup_database(backup_path=None):
    """备份数据库"""
    try:
        db_path = DATABASE_CONFIG['db_path']
        if not os.path.exists(db_path):
            log_print("数据库文件不存在，无法备份", 'DB', force=True)
            return False
        
        if not backup_path:
            from datetime import datetime
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_path = f"{db_path}.backup_{timestamp}"
        
        import shutil
        shutil.copy2(db_path, backup_path)
        log_print(f"✅ 数据库备份成功: {backup_path}", 'DB')
        return True
        
    except Exception as e:
        log_print(f"数据库备份失败: {e}", 'DB', force=True)
        return False
