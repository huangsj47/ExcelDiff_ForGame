#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
仓库路由
"""

from flask import render_template, request, jsonify
from . import repository_bp
from models import Repository
from utils.safe_print import log_print


@repository_bp.route('/api/repositories/<int:repository_id>/info')
def repository_info(repository_id):
    """获取仓库信息API"""
    try:
        repository = Repository.query.get_or_404(repository_id)
        return jsonify({
            'success': True,
            'data': repository.to_dict()
        })
    except Exception as e:
        log_print(f"获取仓库信息失败: {e}", 'REPO', force=True)
        return jsonify({
            'success': False,
            'message': str(e)
        }), 500
