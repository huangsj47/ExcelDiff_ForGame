#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据库模型包
"""

from flask_sqlalchemy import SQLAlchemy

# 创建数据库实例
db = SQLAlchemy()

# 导入所有模型
from .project import Project
from .repository import Repository, GlobalRepositoryCounter
from .commit import Commit
from .cache import DiffCache, ExcelHtmlCache, MergedDiffCache
from .task import BackgroundTask
from .weekly_version import WeeklyVersionConfig, WeeklyVersionDiffCache, WeeklyVersionExcelCache
from .operation_log import OperationLog

# 导出所有模型
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
    'OperationLog'
]
