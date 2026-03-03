#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agent 节点模型
"""

from datetime import datetime, timezone

from sqlalchemy import Index, UniqueConstraint

from . import db


class AgentNode(db.Model):
    """Agent 节点信息"""

    __tablename__ = "agent_nodes"

    id = db.Column(db.Integer, primary_key=True)
    agent_code = db.Column(db.String(100), unique=True, nullable=False)
    agent_name = db.Column(db.String(200), nullable=False)
    host = db.Column(db.String(255))
    port = db.Column(db.Integer)
    default_admin_username = db.Column(db.String(100))
    agent_token = db.Column(db.String(128), nullable=False)
    capabilities = db.Column(db.Text)

    status = db.Column(db.String(20), default="offline")  # online/offline
    last_heartbeat = db.Column(db.DateTime)
    last_error = db.Column(db.Text)
    cpu_cores = db.Column(db.Integer)
    cpu_usage_percent = db.Column(db.Float)
    memory_total_bytes = db.Column(db.BigInteger)
    memory_available_bytes = db.Column(db.BigInteger)
    disk_free_bytes = db.Column(db.BigInteger)
    os_name = db.Column(db.String(100))
    os_version = db.Column(db.String(200))
    os_platform = db.Column(db.String(300))
    metrics_updated_at = db.Column(db.DateTime)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    project_bindings = db.relationship(
        "AgentProjectBinding",
        backref="agent",
        lazy=True,
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("idx_agent_nodes_status", "status"),
        Index("idx_agent_nodes_last_heartbeat", "last_heartbeat"),
    )


class AgentProjectBinding(db.Model):
    """项目与 Agent 绑定关系（一个项目只能绑定一个 Agent）"""

    __tablename__ = "agent_project_bindings"

    id = db.Column(db.Integer, primary_key=True)
    agent_id = db.Column(db.Integer, db.ForeignKey("agent_nodes.id"), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    project_code = db.Column(db.String(50), nullable=False)
    assigned_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    project = db.relationship("Project", backref="agent_binding")

    __table_args__ = (
        UniqueConstraint("project_id", name="uq_agent_project_bindings_project_id"),
        UniqueConstraint("agent_id", "project_id", name="uq_agent_project_bindings_agent_project"),
        Index("idx_agent_project_bindings_agent_id", "agent_id"),
        Index("idx_agent_project_bindings_project_code", "project_code"),
    )


class AgentTask(db.Model):
    """分配给 Agent 执行的任务。"""

    __tablename__ = "agent_tasks"

    id = db.Column(db.Integer, primary_key=True)
    task_type = db.Column(db.String(50), nullable=False)  # excel_diff/auto_sync/weekly_sync
    priority = db.Column(db.Integer, default=10, nullable=False)

    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    repository_id = db.Column(db.Integer, db.ForeignKey("repository.id"), nullable=True)
    source_task_id = db.Column(db.Integer, db.ForeignKey("background_tasks.id"), nullable=True)

    payload = db.Column(db.Text)  # JSON
    status = db.Column(db.String(20), default="pending")  # pending/processing/completed/failed
    retry_count = db.Column(db.Integer, default=0)
    error_message = db.Column(db.Text)

    assigned_agent_id = db.Column(db.Integer, db.ForeignKey("agent_nodes.id"), nullable=True)
    lease_expires_at = db.Column(db.DateTime, nullable=True)
    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    result_summary = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    project = db.relationship("Project", backref="agent_tasks", lazy=True)
    repository = db.relationship("Repository", backref="agent_tasks", lazy=True)
    source_task = db.relationship("BackgroundTask", backref="agent_tasks", lazy=True)
    assigned_agent = db.relationship("AgentNode", backref="running_tasks", lazy=True)

    __table_args__ = (
        Index("idx_agent_tasks_status_priority", "status", "priority", "created_at"),
        Index("idx_agent_tasks_project_status", "project_id", "status"),
        Index("idx_agent_tasks_assigned_agent", "assigned_agent_id"),
        Index("idx_agent_tasks_lease", "lease_expires_at"),
    )
