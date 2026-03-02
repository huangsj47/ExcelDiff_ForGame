#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
缓存模型
"""

from datetime import datetime, timezone
from sqlalchemy import Index
from . import db

DIFF_LOGIC_VERSION = "1.8.0"


class DiffCache(db.Model):
    """Excel文件差异缓存表"""
    __tablename__ = 'diff_cache'

    id = db.Column(db.Integer, primary_key=True)
    repository_id = db.Column(db.Integer, db.ForeignKey('repository.id'), nullable=False)
    commit_id = db.Column(db.String(255), nullable=False)
    file_path = db.Column(db.String(500), nullable=False)
    previous_commit_id = db.Column(db.String(255))

    diff_data = db.Column(db.Text)
    file_size = db.Column(db.Integer, default=0)
    processing_time = db.Column(db.Float, default=0.0)
    cache_status = db.Column(db.String(50), default='pending')  # pending, completed, failed
    error_message = db.Column(db.Text)
    diff_version = db.Column(db.String(20), default=DIFF_LOGIC_VERSION)
    commit_time = db.Column(db.DateTime)
    is_long_processing = db.Column(db.Boolean, default=False)
    expire_at = db.Column(db.DateTime)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index('idx_repo_commit_file', 'repository_id', 'commit_id', 'file_path'),
        Index('idx_created_at', 'created_at'),
        Index('idx_cache_status', 'cache_status'),
        Index('idx_diff_version', 'diff_version'),
        Index('idx_expire_at', 'expire_at'),
        Index('idx_is_long_processing', 'is_long_processing'),
    )

    repository = db.relationship('Repository', backref='diff_caches')


class ExcelHtmlCache(db.Model):
    """Excel HTML缓存表"""
    __tablename__ = 'excel_html_cache'

    id = db.Column(db.Integer, primary_key=True)
    repository_id = db.Column(db.Integer, db.ForeignKey('repository.id'), nullable=False)
    commit_id = db.Column(db.String(255), nullable=False)
    file_path = db.Column(db.String(500), nullable=False)
    cache_key = db.Column(db.String(255), nullable=False, unique=True)

    html_content = db.Column(db.Text)
    css_content = db.Column(db.Text)
    js_content = db.Column(db.Text)
    cache_metadata = db.Column(db.Text)

    cache_status = db.Column(db.String(50), default='pending')
    diff_version = db.Column(db.String(20), default=DIFF_LOGIC_VERSION)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index('idx_html_repo_commit_file', 'repository_id', 'commit_id', 'file_path'),
        Index('idx_html_cache_status', 'cache_status'),
        Index('idx_html_cache_key', 'cache_key'),
        Index('idx_html_diff_version', 'diff_version'),
    )

    repository = db.relationship('Repository', backref='excel_html_caches')


class MergedDiffCache(db.Model):
    """合并差异缓存表"""
    __tablename__ = 'merged_diff_cache'

    id = db.Column(db.Integer, primary_key=True)
    repository_id = db.Column(db.Integer, db.ForeignKey('repository.id'), nullable=False)

    cache_key = db.Column(db.String(255), nullable=False, unique=True)
    file_path = db.Column(db.String(500), nullable=False)

    base_commit_id = db.Column(db.String(100))
    target_commit_id = db.Column(db.String(100))
    commit_id_list = db.Column(db.Text)

    merged_diff_data = db.Column(db.Text)
    diff_summary = db.Column(db.Text)

    total_commits = db.Column(db.Integer, default=0)
    added_lines = db.Column(db.Integer, default=0)
    deleted_lines = db.Column(db.Integer, default=0)
    modified_lines = db.Column(db.Integer, default=0)

    cache_status = db.Column(db.String(50), default='pending')
    processing_time = db.Column(db.Float)
    file_size = db.Column(db.Integer)
    diff_version = db.Column(db.String(20), default=DIFF_LOGIC_VERSION)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    expire_at = db.Column(db.DateTime)

    __table_args__ = (
        Index('idx_merged_diff_cache_key', 'cache_key'),
        Index('idx_merged_diff_repo_file', 'repository_id', 'file_path'),
        Index('idx_merged_diff_commits', 'base_commit_id', 'target_commit_id'),
        Index('idx_merged_diff_status', 'cache_status'),
        Index('idx_merged_diff_version', 'diff_version'),
        Index('idx_merged_diff_expire', 'expire_at'),
    )

    repository = db.relationship('Repository', backref='merged_diff_caches')
