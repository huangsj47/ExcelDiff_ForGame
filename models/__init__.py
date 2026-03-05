#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据库模型包。

所有模型定义统一维护在 models/ 子模块中，app.py 通过
``from models import db, Project, ...`` 导入。
db 实例在此处创建（未绑定），app.py 负责调用 db.init_app(app) 完成绑定。
"""

from flask_sqlalchemy import SQLAlchemy

# 全局 db 实例，由 app.py 调用 db.init_app(app) 完成绑定
db = SQLAlchemy()

from .project import Project
from .repository import Repository, GlobalRepositoryCounter
from .commit import Commit
from .cache import DiffCache, ExcelHtmlCache, MergedDiffCache
from .task import BackgroundTask
from .weekly_version import WeeklyVersionConfig, WeeklyVersionDiffCache, WeeklyVersionExcelCache
from .operation_log import OperationLog
from .agent import AgentNode, AgentProjectBinding, AgentTask, AgentDefaultAdmin
from .agent_temp_cache import AgentTempCache

# 导入 auth 模块的模型，确保 db.create_all() 能创建对应的表
try:
    from auth.models import (
        AuthUser,
        AuthFunction,
        AuthUserFunction,
        AuthUserProject,
        AuthProjectJoinRequest,
        AuthProjectCreateRequest,
        AuthProjectPreAssignment,
    )
    _AUTH_MODELS_LOADED = True
except ImportError:
    _AUTH_MODELS_LOADED = False

# 导入 qkit_auth 模块模型（AUTH_BACKEND=qkit 时启用）
try:
    from qkit_auth.models import (
        QkitAuthUser,
        QkitAuthUserProject,
        QkitAuthProjectJoinRequest,
        QkitAuthProjectCreateRequest,
        QkitAuthProjectPreAssignment,
        QkitAuthProjectImportConfig,
        QkitAuthUserImportToken,
        QkitAuthImportBlock,
    )
    _QKIT_AUTH_MODELS_LOADED = True
except ImportError:
    _QKIT_AUTH_MODELS_LOADED = False

__all__ = [
    'db',
    'Project',
    'Repository',
    'GlobalRepositoryCounter',
    'Commit',
    'DiffCache',
    'ExcelHtmlCache',
    'MergedDiffCache',
    'BackgroundTask',
    'WeeklyVersionConfig',
    'WeeklyVersionDiffCache',
    'WeeklyVersionExcelCache',
    'OperationLog',
    'AgentNode',
    'AgentProjectBinding',
    'AgentTask',
    'AgentDefaultAdmin',
    'AgentTempCache',
]

if _AUTH_MODELS_LOADED:
    __all__.extend([
        'AuthUser',
        'AuthFunction',
        'AuthUserFunction',
        'AuthUserProject',
        'AuthProjectJoinRequest',
        'AuthProjectCreateRequest',
        'AuthProjectPreAssignment',
    ])

if _QKIT_AUTH_MODELS_LOADED:
    __all__.extend([
        'QkitAuthUser',
        'QkitAuthUserProject',
        'QkitAuthProjectJoinRequest',
        'QkitAuthProjectCreateRequest',
        'QkitAuthProjectPreAssignment',
        'QkitAuthProjectImportConfig',
        'QkitAuthUserImportToken',
        'QkitAuthImportBlock',
    ])
