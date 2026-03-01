#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
周版本路由
"""

from flask import render_template, request, jsonify
from . import weekly_version_bp
from models import WeeklyVersionConfig
from utils.safe_print import log_print


@weekly_version_bp.route('/weekly-version-config/<int:config_id>/stats')
def weekly_version_stats(config_id):
    """获取周版本统计信息"""
    try:
        config = WeeklyVersionConfig.query.get_or_404(config_id)
        
        # 这里可以添加统计逻辑
        stats = {
            'config_id': config.id,
            'name': config.name,
            'repository_name': config.repository.name if config.repository else 'Unknown',
            'status': config.status,
            'is_active': config.is_active
        }
        
        return jsonify({
            'success': True,
            'data': stats
        })
        
    except Exception as e:
        log_print(f"获取周版本统计失败: {e}", 'WEEKLY', force=True)
        return jsonify({
            'success': False,
            'message': str(e)
        }), 500
