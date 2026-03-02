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
]
