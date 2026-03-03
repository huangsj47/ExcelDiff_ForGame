#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
提交记录模型
"""

from datetime import datetime, timezone
from . import db


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
    message = db.Column(db.Text)
    status = db.Column(db.String(20), default='pending')  # 'pending', 'confirmed', 'rejected'
    status_changed_by = db.Column(db.String(100))  # 确认/拒绝操作者用户名
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    
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
