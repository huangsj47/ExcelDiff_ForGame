#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
操作日志模型
"""

from datetime import datetime, timezone
from . import db


class OperationLog(db.Model):
    """操作日志模型"""
    __tablename__ = 'operation_log'
    
    id = db.Column(db.Integer, primary_key=True)
    log_type = db.Column(db.String(20), nullable=False)  # 'info', 'success', 'error', 'warning'
    message = db.Column(db.Text, nullable=False)  # 日志消息
    source = db.Column(db.String(50), nullable=False)  # 'excel_cache', 'weekly_excel_cache'
    repository_id = db.Column(db.Integer, db.ForeignKey('repository.id'), nullable=True)
    config_id = db.Column(db.Integer, nullable=True)
    file_path = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    repository = db.relationship('Repository', backref='operation_logs', lazy=True)
    
    def __repr__(self):
        return f'<OperationLog {self.log_type} - {self.source}>'
    
    def to_dict(self):
        """转换为字典"""
        return {
            'id': self.id,
            'log_type': self.log_type,
            'message': self.message,
            'source': self.source,
            'repository_id': self.repository_id,
            'config_id': self.config_id,
            'file_path': self.file_path,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
