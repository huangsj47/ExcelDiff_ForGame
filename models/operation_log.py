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
    operation_type = db.Column(db.String(50), nullable=False)  # 操作类型
    target_type = db.Column(db.String(50))  # 目标类型 (repository, commit, etc.)
    target_id = db.Column(db.Integer)  # 目标ID
    description = db.Column(db.Text)  # 操作描述
    details = db.Column(db.Text)  # 详细信息 (JSON格式)
    user_info = db.Column(db.Text)  # 用户信息 (JSON格式)
    ip_address = db.Column(db.String(45))  # IP地址
    user_agent = db.Column(db.Text)  # 用户代理
    status = db.Column(db.String(20), default='success')  # 操作状态
    error_message = db.Column(db.Text)  # 错误信息
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    
    def __repr__(self):
        return f'<OperationLog {self.operation_type} - {self.status}>'
    
    def to_dict(self):
        """转换为字典"""
        return {
            'id': self.id,
            'operation_type': self.operation_type,
            'target_type': self.target_type,
            'target_id': self.target_id,
            'description': self.description,
            'details': self.details,
            'user_info': self.user_info,
            'ip_address': self.ip_address,
            'user_agent': self.user_agent,
            'status': self.status,
            'error_message': self.error_message,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
