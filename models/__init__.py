#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据库模型包。

优先复用 app.py 中的模型定义，尽量避免双轨模型漂移。
若 app.py 因环境依赖无法导入，再回退到本地 models/* 定义。
"""

import importlib
from flask_sqlalchemy import SQLAlchemy

USING_APP_MODELS = False

try:
    app_module = importlib.import_module("app")
    db = app_module.db
    Project = app_module.Project
    Repository = app_module.Repository
    GlobalRepositoryCounter = app_module.GlobalRepositoryCounter
    Commit = app_module.Commit
    DiffCache = app_module.DiffCache
    ExcelHtmlCache = app_module.ExcelHtmlCache
    MergedDiffCache = app_module.MergedDiffCache
    BackgroundTask = app_module.BackgroundTask
    WeeklyVersionConfig = app_module.WeeklyVersionConfig
    WeeklyVersionDiffCache = app_module.WeeklyVersionDiffCache
    WeeklyVersionExcelCache = app_module.WeeklyVersionExcelCache
    OperationLog = app_module.OperationLog
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
