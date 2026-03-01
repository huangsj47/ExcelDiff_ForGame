#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据库初始化脚本 - 确保所有表都被正确创建
"""

import sqlite3
import os
from services.model_loader import get_runtime_models

def check_and_create_all_tables():
    """检查并创建所有必需的数据库表"""
    app, db = get_runtime_models("app", "db")

    # 确保instance目录存在
    instance_dir = 'instance'
    if not os.path.exists(instance_dir):
        try:
            os.makedirs(instance_dir)
            print(f'✅ 创建instance目录: {os.path.abspath(instance_dir)}')
        except Exception as e:
            print(f'❌ 创建instance目录失败: {e}')
            return False
    else:
        print(f'ℹ️ instance目录已存在: {os.path.abspath(instance_dir)}')

    # 先检查当前数据库状态
    db_path = 'instance/diff_platform.db'
    if os.path.exists(db_path):
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM `sqlite_master` WHERE `type`='table'")
        existing_tables = [table[0] for table in cursor.fetchall()]
        print(f'当前数据库表: {existing_tables}')
        conn.close()
    else:
        print(f'数据库文件不存在，将创建新数据库: {os.path.abspath(db_path)}')
        existing_tables = []
    
    # 应该存在的表
    expected_tables = [
        'project',                    # 项目表
        'repository',                 # 仓库表
        'commits_log',                # 提交记录表 (改名避免SQL保留字冲突)
        'background_tasks',           # 后台任务表 (注意表名是复数)
        'global_repository_counter',  # 全局仓库ID计数器表
        'diff_cache',                # Excel差异缓存表
        'excel_html_cache',          # Excel HTML缓存表
        'weekly_version_config',     # 周版本配置表
        'weekly_version_diff_cache', # 周版本diff缓存表
        'merged_diff_cache'          # 合并diff缓存表
    ]
    
    missing_tables = [t for t in expected_tables if t not in existing_tables]
    if missing_tables:
        print(f'缺失的表: {missing_tables}')
    else:
        print('所有必需的表都存在')
    
    # 在应用上下文中创建所有表
    try:
        with app.app_context():
            print('正在执行 db.create_all()...')
            db.create_all()
            print('✅ db.create_all() 执行完成')
    except Exception as e:
        print(f'❌ 创建表失败: {e}')
        return False
    
    # 重新检查数据库状态
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM `sqlite_master` where `type`='table'")
    final_tables = [table[0] for table in cursor.fetchall()]
    print(f'最终数据库表: {final_tables}')
    
    # 检查每个表的记录数
    print('\n=== 表记录统计 ===')
    for table in final_tables:
        try:
            # 对于commits_log表使用标准查询
            cursor.execute(f'SELECT COUNT(*) FROM {table}')
            count = cursor.fetchone()[0]
            print(f'{table}: {count} 条记录')
        except Exception as e:
            print(f'{table}: 查询失败 - {e}')
    
    conn.close()
    
    # 最终验证
    still_missing = [t for t in expected_tables if t not in final_tables]
    if still_missing:
        print(f'\n❌ 仍然缺失的表: {still_missing}')
        return False
    else:
        print('\n✅ 所有必需的表都已成功创建')
        return True

if __name__ == "__main__":
    success = check_and_create_all_tables()
    if success:
        print('\n🎉 数据库初始化完成!')
    else:
        print('\n💥 数据库初始化失败!')
