#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
主要路由
"""

from flask import render_template, request, jsonify
from . import main_bp
from models import Project
from utils.safe_print import log_print


@main_bp.route('/test')
def test():
    """测试路由"""
    return "服务器正常工作！"


@main_bp.route('/')
def index():
    """主页路由"""
    try:
        log_print("访问主页路由", 'APP')
        projects = Project.query.order_by(Project.created_at.desc()).all()
        log_print(f"找到 {len(projects)} 个项目", 'APP')
        return render_template('index.html', projects=projects)
    except Exception as e:
        log_print(f"主页路由错误: {str(e)}", 'APP', force=True)
        import traceback
        traceback.print_exc()
        return f"主页加载错误: {str(e)}", 500


@main_bp.route('/api/system/info')
def system_info():
    """系统信息API"""
    try:
        from utils.database import get_database_info
        from config import DIFF_LOGIC_VERSION
        import sys
        import platform
        
        db_info = get_database_info()
        
        system_info = {
            'version': DIFF_LOGIC_VERSION,
            'python_version': sys.version,
            'platform': platform.platform(),
            'database': db_info,
            'status': 'running'
        }
        
        return jsonify({
            'success': True,
            'data': system_info
        })
        
    except Exception as e:
        log_print(f"获取系统信息失败: {e}", 'APP', force=True)
        return jsonify({
            'success': False,
            'message': str(e)
        }), 500
