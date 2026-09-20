#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
提交记录模型
"""

from datetime import datetime, timezone

from sqlalchemy import Index

from . import db
from .big_text import BigText


class Commit(db.Model):
    """提交记录模型"""
    __tablename__ = 'commits_log'

    id = db.Column(db.Integer, primary_key=True)
    repository_id = db.Column(db.Integer, db.ForeignKey('repository.id'), nullable=False)
    commit_id = db.Column(db.String(100), nullable=False)
    path = db.Column(db.String(500))
    version = db.Column(db.String(50))
    operation = db.Column(db.String(10))  # 'A', 'M', 'D'
    author = db.Column(db.String(100))
    commit_time = db.Column(db.DateTime)
    message = db.Column(BigText)
    status = db.Column(db.String(20), default='pending')  # 'pending', 'confirmed', 'rejected'
    status_changed_by = db.Column(db.String(100))  # 确认/拒绝操作者用户名
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # commits_log 是全平台最大的表（提交列表 / diff / 周版本 / 缓存回查都按
    # repository_id / commit_id / commit_time / status 过滤），此前一个索引都没有。
    # 这里只加索引：不改字段、不改默认值、不加唯一约束，因此不改变任何查询语义。
    #
    # 命名与 models/agent.py 的 AgentTask.__table_args__ 保持一致（idx_<表>_<列...>）。
    __table_args__ = (
        # 提交列表分页：WHERE repository_id = ? ORDER BY commit_time DESC
        # （services/commit_list_page_service.py）
        Index('idx_commits_log_repo_commit_time', 'repository_id', 'commit_time'),
        # 按 (仓库, commit) 点查：diff 生成、状态回查、缓存回查
        # （services/excel_diff_cache_service.py、commit_diff_logic.py）
        Index('idx_commits_log_repo_commit_id', 'repository_id', 'commit_id'),
        # 状态筛选 / 批量确认：WHERE repository_id = ? AND status = ?
        Index('idx_commits_log_repo_status', 'repository_id', 'status'),
        # 不带 repository_id 的时间窗口扫描（跨仓库的提交时间范围查询、
        # 以及 (repository_id, commit_time) 复合索引覆盖不到的前缀无关场景）
        Index('idx_commits_log_commit_time', 'commit_time'),
    )

    def __repr__(self):
        return f'<Commit {self.commit_id[:8]} - {self.path}>'
    
    def to_dict(self):
        """转换为字典"""
        return {
            'id': self.id,
            'repository_id': self.repository_id,
            'commit_id': self.commit_id,
            'path': self.path,
            'version': self.version,
            'operation': self.operation,
            'author': self.author,
            'commit_time': self.commit_time.isoformat() if self.commit_time else None,
            'message': self.message,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
