#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据库模型包。

优先复用 app.py 中的模型定义，尽量避免双轨模型漂移。
若 app.py 因环境依赖无法导入，再回退到本地 models/* 定义。
"""

from flask_sqlalchemy import SQLAlchemy

USING_APP_MODELS = False

try:
    from app import (  # type: ignore
        db,
        Project,
        Repository,
        GlobalRepositoryCounter,
        Commit,
        DiffCache,
        ExcelHtmlCache,
        MergedDiffCache,
        BackgroundTask,
        WeeklyVersionConfig,
        WeeklyVersionDiffCache,
        WeeklyVersionExcelCache,
        OperationLog,
    )
    USING_APP_MODELS = True
except Exception:
    db = SQLAlchemy()
    from .project import Project
    from .repository import Repository, GlobalRepositoryCounter
    from .commit import Commit
    from .cache import DiffCache, ExcelHtmlCache, MergedDiffCache
    from .task import BackgroundTask
    from .weekly_version import WeeklyVersionConfig, WeeklyVersionDiffCache, WeeklyVersionExcelCache
    from .operation_log import OperationLog

__all__ = [
    'db',
    'USING_APP_MODELS',
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
]
