#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
缓存模型
"""

from datetime import datetime, timezone
from sqlalchemy import Index
from . import db
from .big_text import BigText


def default_diff_logic_version():
    """diff_version 列的默认值：**懒惰取唯一版本源**，不再自带一份字面量。

    历史问题：这里曾写死 `DIFF_LOGIC_VERSION = "1.8.0"`，而真正驱动缓存失效的是
    app.py 的 DIFF_LOGIC_VERSION（当前 1.9.0）。凡是没显式传 diff_version 就落库的
    缓存记录（例如 cache_diff_error 写的失败记录、脚本/测试直接构造的记录）都会带着
    一个**过期版本号**出生，随后被「版本不匹配」逻辑当成旧缓存清掉 —— 一个
    没有任何运行时信号、只能靠人去比字面量才能发现的静默不一致。

    改成按行插入时惰性解析（SQLAlchemy 的列默认值是在 INSERT 时求值的），
    全仓库就只剩 app.py 那一份字面量（config.py 那份只做界面展示，由
    tests/test_diff_logic_version_single_source.py 锁定两者一致）。
    解析不出来时返回 None（= 未标注版本），由写入方显式赋值，绝不猜一个版本号。
    """
    try:
        from services.model_loader import get_runtime_model
        version = get_runtime_model("DIFF_LOGIC_VERSION")
        if version:
            return str(version)
    except Exception:
        pass
    return None


class DiffCache(db.Model):
    """Excel文件差异缓存表"""
    __tablename__ = 'diff_cache'

    id = db.Column(db.Integer, primary_key=True)
    repository_id = db.Column(db.Integer, db.ForeignKey('repository.id'), nullable=False)
    commit_id = db.Column(db.String(255), nullable=False)
    file_path = db.Column(db.String(500), nullable=False)
    previous_commit_id = db.Column(db.String(255))

    diff_data = db.Column(BigText)
    file_size = db.Column(db.Integer, default=0)
    processing_time = db.Column(db.Float, default=0.0)
    cache_status = db.Column(db.String(50), default='pending')  # pending, completed, failed
    error_message = db.Column(BigText)
    diff_version = db.Column(db.String(20), default=default_diff_logic_version)
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

    html_content = db.Column(BigText)
    css_content = db.Column(BigText)
    js_content = db.Column(BigText)
    cache_metadata = db.Column(BigText)

    cache_status = db.Column(db.String(50), default='pending')
    diff_version = db.Column(db.String(20), default=default_diff_logic_version)

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
    commit_id_list = db.Column(BigText)

    merged_diff_data = db.Column(BigText)
    diff_summary = db.Column(BigText)

    total_commits = db.Column(db.Integer, default=0)
    added_lines = db.Column(db.Integer, default=0)
    deleted_lines = db.Column(db.Integer, default=0)
    modified_lines = db.Column(db.Integer, default=0)

    cache_status = db.Column(db.String(50), default='pending')
    processing_time = db.Column(db.Float)
    file_size = db.Column(db.Integer)
    diff_version = db.Column(db.String(20), default=default_diff_logic_version)

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
