#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
缓存模型
"""

from datetime import datetime, timezone
from sqlalchemy import Index
from . import db


class DiffCache(db.Model):
    """Excel文件差异缓存表"""
    __tablename__ = 'diff_cache'
    
    id = db.Column(db.Integer, primary_key=True)
    repository_id = db.Column(db.Integer, db.ForeignKey('repository.id'), nullable=False)
    commit_id = db.Column(db.String(100), nullable=False)
    file_path = db.Column(db.Text, nullable=False)
    
    # 差异数据
    diff_data = db.Column(db.Text)  # JSON格式的差异数据
    
    # 缓存元数据
    file_size = db.Column(db.Integer)
    processing_time = db.Column(db.Float)
    diff_version = db.Column(db.String(20))
    expire_at = db.Column(db.DateTime)
    
    # 时间戳
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    # 索引
    __table_args__ = (
        Index('idx_diff_cache_repo_commit', 'repository_id', 'commit_id'),
        Index('idx_diff_cache_file', 'file_path'),
        Index('idx_diff_cache_expire', 'expire_at'),
    )


class ExcelHtmlCache(db.Model):
    """Excel HTML缓存表"""
    __tablename__ = 'excel_html_cache'

    id = db.Column(db.Integer, primary_key=True)
    repository_id = db.Column(db.Integer, db.ForeignKey('repository.id'), nullable=False)
    commit_id = db.Column(db.String(255), nullable=False)
    file_path = db.Column(db.String(500), nullable=False)
    cache_key = db.Column(db.String(255), nullable=False, unique=True)

    # HTML内容和样式
    html_content = db.Column(db.Text)  # 渲染好的HTML内容
    css_content = db.Column(db.Text)   # CSS样式
    js_content = db.Column(db.Text)    # JavaScript代码
    cache_metadata = db.Column(db.Text)  # JSON格式的元数据（文件信息、统计等）

    # 缓存状态
    cache_status = db.Column(db.String(50))  # 'pending', 'processing', 'completed', 'failed'
    diff_version = db.Column(db.String(20))  # diff逻辑版本号

    # 时间戳
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # 索引（使用现有的索引名称）
    __table_args__ = (
        Index('idx_html_repo_commit_file', 'repository_id', 'commit_id', 'file_path'),
        Index('idx_html_cache_status', 'cache_status'),
        Index('idx_html_cache_key', 'cache_key'),
        Index('idx_html_diff_version', 'diff_version'),
    )


class MergedDiffCache(db.Model):
    """合并差异缓存表"""
    __tablename__ = 'merged_diff_cache'
    
    id = db.Column(db.Integer, primary_key=True)
    repository_id = db.Column(db.Integer, db.ForeignKey('repository.id'), nullable=False)
    commit_id = db.Column(db.String(100), nullable=False)
    file_path = db.Column(db.Text, nullable=False)
    
    # 合并差异数据
    merged_diff_data = db.Column(db.Text)  # JSON格式的合并差异数据
    
    # 缓存元数据
    processing_time = db.Column(db.Float)
    diff_version = db.Column(db.String(20))
    
    # 时间戳
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    # 索引
    __table_args__ = (
        Index('idx_merged_diff_cache_repo_commit', 'repository_id', 'commit_id'),
        Index('idx_merged_diff_cache_file', 'file_path'),
    )
