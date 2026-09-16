#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
后台任务模型
"""

from datetime import datetime, timezone

from sqlalchemy import Index

from . import db


class BackgroundTask(db.Model):
    """后台任务模型"""
    __tablename__ = 'background_tasks'

    id = db.Column(db.Integer, primary_key=True)
    task_type = db.Column(db.String(50), nullable=False)  # 'excel_diff', 'cleanup_cache', etc.
    repository_id = db.Column(db.Integer, nullable=True)
    commit_id = db.Column(db.String(100), nullable=True)
    file_path = db.Column(db.Text, nullable=True)
    priority = db.Column(db.Integer, default=10)  # 优先级，数字越小优先级越高
    status = db.Column(db.String(20), default='pending')  # 'pending', 'processing', 'completed', 'failed'
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    retry_count = db.Column(db.Integer, default=0)

    # 只补两个被真实高频查询反复使用的组合索引（对照 models/agent.py 的
    # AgentTask.__table_args__ 写法）。不加那些没有查询形态支撑的索引：
    # 索引本身会拖慢任务写入，而 background_tasks 是写多读多的队列表。
    __table_args__ = (
        # worker 拉取待处理任务：WHERE status='pending'
        # ORDER BY priority ASC, created_at ASC
        # （services/task_worker_service.py::load_pending_tasks）
        Index('idx_background_tasks_status_priority', 'status', 'priority', 'created_at'),
        # 任务去重与清理：WHERE task_type=? AND repository_id=? AND status IN (...)
        # （task_worker_service.add_excel_diff_task / repository_update_form_service）
        Index('idx_background_tasks_type_repo_status', 'task_type', 'repository_id', 'status'),
    )

    def __repr__(self):
        return f'<BackgroundTask {self.id}: {self.task_type} - {self.status}>'
    
    def to_dict(self):
        """转换为字典"""
        return {
            'id': self.id,
            'task_type': self.task_type,
            'repository_id': self.repository_id,
            'commit_id': self.commit_id,
            'file_path': self.file_path,
            'priority': self.priority,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'error_message': self.error_message,
            'retry_count': self.retry_count
        }
