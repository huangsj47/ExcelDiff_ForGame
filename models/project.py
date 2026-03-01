#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
项目模型
"""

from datetime import datetime, timezone
from . import db


class Project(db.Model):
    """项目模型"""
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    department = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    
    # 关系
    repositories = db.relationship('Repository', backref='project', lazy=True, cascade='all, delete-orphan')
    
    def __repr__(self):
        return f'<Project {self.code}: {self.name}>'
    
    def to_dict(self):
        """转换为字典"""
        return {
            'id': self.id,
            'code': self.code,
            'name': self.name,
            'department': self.department,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
