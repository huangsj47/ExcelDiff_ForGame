#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
路由包
"""

from flask import Blueprint

# 创建蓝图
main_bp = Blueprint('main', __name__)
project_bp = Blueprint('project', __name__, url_prefix='/projects')
repository_bp = Blueprint('repository', __name__)
weekly_version_bp = Blueprint('weekly_version', __name__)
status_sync_bp = Blueprint('status_sync', __name__, url_prefix='/status-sync')

# 导入所有路由模块
from . import main_routes
from . import project_routes
from . import repository_routes
from . import weekly_version_routes
from . import status_sync_routes

def register_blueprints(app):
    """注册所有蓝图"""
    app.register_blueprint(main_bp)
    app.register_blueprint(project_bp)
    app.register_blueprint(repository_bp)
    app.register_blueprint(weekly_version_bp)
    app.register_blueprint(status_sync_bp)
