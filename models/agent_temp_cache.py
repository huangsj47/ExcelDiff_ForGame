#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent 临时加速缓存模型（平台侧，仅用于可过期加速，不作为主业务数据存储）。"""

from datetime import datetime, timezone

from sqlalchemy import Index

from . import db


class AgentTempCache(db.Model):
    __tablename__ = "agent_temp_cache"

    id = db.Column(db.Integer, primary_key=True)
    cache_key = db.Column(db.String(255), nullable=False, unique=True)

    task_type = db.Column(db.String(50), nullable=True)
    cache_kind = db.Column(db.String(50), nullable=True)
    project_id = db.Column(db.Integer, nullable=True)
    repository_id = db.Column(db.Integer, nullable=True)
    commit_id = db.Column(db.String(255), nullable=True)
    file_path = db.Column(db.String(500), nullable=True)

    payload_json = db.Column(db.Text, nullable=True)
    payload_hash = db.Column(db.String(128), nullable=True)
    payload_size = db.Column(db.Integer, default=0)

    source_agent_id = db.Column(db.Integer, nullable=True)
    source_task_id = db.Column(db.Integer, nullable=True)
    expire_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("idx_agent_temp_cache_key", "cache_key"),
        Index("idx_agent_temp_cache_expire", "expire_at"),
        Index("idx_agent_temp_cache_project", "project_id"),
        Index("idx_agent_temp_cache_repo", "repository_id"),
    )
