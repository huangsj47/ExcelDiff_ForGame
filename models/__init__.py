#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据库模型包。

所有模型定义统一维护在 models/ 子模块中，app.py 通过
``from models import db, Project, ...`` 导入。
db 实例在此处创建（未绑定），app.py 负责调用 db.init_app(app) 完成绑定。
"""

from flask_sqlalchemy import SQLAlchemy
from flask_sqlalchemy.query import Query as FlaskSQLAlchemyQuery


def _install_query_get_compat_patch() -> None:
    """Patch Flask-SQLAlchemy Query.get to avoid SQLAlchemy 2.0 legacy warnings.

    Flask-SQLAlchemy 3.x still routes `get_or_404()` through `Query.get()`.
    On SQLAlchemy 2.0 this emits LegacyAPIWarning. We delegate to
    Session.get() to preserve behavior while removing the warning.
    """

    if getattr(FlaskSQLAlchemyQuery.get, "__name__", "") == "_query_get_compat":
        return

    def _query_get_compat(self, ident):  # noqa: ANN001 - keep Flask-SQLAlchemy signature
        mapper = self._only_full_mapper_zero("get")
        return self.session.get(mapper.class_, ident)

    FlaskSQLAlchemyQuery.get = _query_get_compat


_install_query_get_compat_patch()

# 全局 db 实例，由 app.py 调用 db.init_app(app) 完成绑定
db = SQLAlchemy()

from .project import Project
from .repository import Repository, GlobalRepositoryCounter
from .commit import Commit
from .cache import DiffCache, ExcelHtmlCache, MergedDiffCache
from .task import BackgroundTask
from .weekly_version import WeeklyVersionConfig, WeeklyVersionDiffCache, WeeklyVersionExcelCache
from .operation_log import OperationLog
from .agent import AgentNode, AgentProjectBinding, AgentTask, AgentDefaultAdmin, AgentIncident
from .agent_temp_cache import AgentTempCache
from .ai_analysis import AiProjectApiKey, AiAnalysisRun, AiWeeklyAnalysisState, AiProjectAnalysisConfig

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
        AuthProjectConfirmPermission,
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
        QkitProjectConfirmPermission,
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
    'AgentIncident',
    'AgentTempCache',
    'AiProjectApiKey',
    'AiAnalysisRun',
    'AiWeeklyAnalysisState',
    'AiProjectAnalysisConfig',
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
        'AuthProjectConfirmPermission',
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
        'QkitProjectConfirmPermission',
    ])
