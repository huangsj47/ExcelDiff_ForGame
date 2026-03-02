#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
周版本模型
"""

from datetime import datetime, timezone
from sqlalchemy import Index
from . import db


class WeeklyVersionConfig(db.Model):
    """周版本diff配置表"""
    __tablename__ = 'weekly_version_config'

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('project.id'), nullable=False)
    repository_id = db.Column(db.Integer, db.ForeignKey('repository.id'), nullable=False)

    # 配置基本信息
    name = db.Column(db.String(100), nullable=False)  # 配置名称，如"第42周版本"
    description = db.Column(db.Text)  # 配置描述
    branch = db.Column(db.String(100), nullable=False)  # 分支名称

    # 时间配置
    start_time = db.Column(db.DateTime, nullable=False)  # 版本开始时间
    end_time = db.Column(db.DateTime, nullable=False)    # 版本结束时间
    cycle_type = db.Column(db.String(20), default='custom')  # 'weekly', 'biweekly', 'custom'

    # 状态配置
    is_active = db.Column(db.Boolean, default=True)     # 是否启用
    auto_sync = db.Column(db.Boolean, default=True)     # 是否自动同步
    status = db.Column(db.String(20), default='active') # 'active', 'completed', 'archived'

    # 时间戳
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # 关系
    project = db.relationship('Project', backref='weekly_version_configs')
    repository = db.relationship('Repository', backref='weekly_version_configs')

    def __repr__(self):
        return f'<WeeklyVersionConfig {self.name}>'


class WeeklyVersionDiffCache(db.Model):
    """周版本diff缓存表"""
    __tablename__ = 'weekly_version_diff_cache'

    id = db.Column(db.Integer, primary_key=True)
    config_id = db.Column(db.Integer, db.ForeignKey('weekly_version_config.id'), nullable=False)
    repository_id = db.Column(db.Integer, db.ForeignKey('repository.id'), nullable=False)
    file_path = db.Column(db.String(500), nullable=False)
    file_type = db.Column(db.String(50))  # 文件类型：'code', 'table', 'res', etc.

    # 差异数据
    merged_diff_data = db.Column(db.Text)  # JSON格式的合并diff数据
    base_commit_id = db.Column(db.String(100))  # 基准版本的commit_id
    latest_commit_id = db.Column(db.String(100))  # 最新版本的commit_id

    # 提交信息
    commit_authors = db.Column(db.Text)  # JSON格式的提交者列表
    commit_messages = db.Column(db.Text)  # JSON格式的提交消息列表
    commit_times = db.Column(db.Text)    # JSON格式的提交时间列表
    commit_count = db.Column(db.Integer, default=0)  # 涉及的提交数量

    # 确认状态 - 支持多角色确认
    confirmation_status = db.Column(db.Text)  # JSON格式：{"dev": "pending", "qa": "confirmed", "pm": "pending"}
    overall_status = db.Column(db.String(20), default='pending')  # 'pending', 'confirmed', 'rejected'

    # 缓存状态
    cache_status = db.Column(db.String(50), default='pending')  # pending, completed, failed
    processing_time = db.Column(db.Float)  # 处理时间（秒）
    file_size = db.Column(db.Integer)      # 文件大小（字节）

    # 时间戳
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    last_sync_time = db.Column(db.DateTime)  # 最后同步时间

    # 关系
    config = db.relationship('WeeklyVersionConfig', backref='diff_caches')
    repository = db.relationship('Repository', backref='weekly_diff_caches')

    # 添加索引
    __table_args__ = (
        Index('idx_weekly_diff_config_file', 'config_id', 'file_path'),
        Index('idx_weekly_diff_repo', 'repository_id'),
        Index('idx_weekly_diff_status', 'overall_status'),
        Index('idx_weekly_diff_cache_status', 'cache_status'),
        Index('idx_weekly_diff_sync_time', 'last_sync_time'),
    )


class WeeklyVersionExcelCache(db.Model):
    """周版本Excel缓存表"""
    __tablename__ = 'weekly_version_excel_cache'

    id = db.Column(db.Integer, primary_key=True)
    config_id = db.Column(db.Integer, db.ForeignKey('weekly_version_config.id'), nullable=False)
    repository_id = db.Column(db.Integer, db.ForeignKey('repository.id'), nullable=False)
    file_path = db.Column(db.String(500), nullable=False)
    cache_key = db.Column(db.String(255), nullable=False, unique=True)

    # 提交信息
    base_commit_id = db.Column(db.String(100))  # 基准版本的commit_id
    latest_commit_id = db.Column(db.String(100))  # 最新版本的commit_id
    commit_count = db.Column(db.Integer, default=0)  # 提交数量

    # HTML内容和样式
    html_content = db.Column(db.Text)  # 渲染好的HTML内容
    css_content = db.Column(db.Text)   # CSS样式
    js_content = db.Column(db.Text)    # JavaScript代码
    cache_metadata = db.Column(db.Text)  # JSON格式的元数据

    # 缓存状态
    cache_status = db.Column(db.String(20), default='pending')
    diff_version = db.Column(db.String(20))
    processing_time = db.Column(db.Float)

    # 时间戳
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # 关系
    config = db.relationship('WeeklyVersionConfig', backref='excel_caches')
    repository = db.relationship('Repository', backref='weekly_excel_caches')

    # 索引
    __table_args__ = (
        Index('idx_weekly_excel_config_file', 'config_id', 'file_path'),
        Index('idx_weekly_excel_repo', 'repository_id'),
        Index('idx_weekly_excel_status', 'cache_status'),
        Index(
            'idx_weekly_excel_lookup',
            'config_id',
            'file_path',
            'base_commit_id',
            'latest_commit_id',
            'diff_version',
            'cache_status',
        ),
    )
