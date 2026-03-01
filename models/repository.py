#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
仓库模型
"""

from datetime import datetime, timezone
from . import db
from utils.security_utils import decrypt_credential, encrypt_credential


class GlobalRepositoryCounter(db.Model):
    """全局仓库ID计数器表"""
    __tablename__ = 'global_repository_counter'
    
    id = db.Column(db.Integer, primary_key=True)
    max_repository_id = db.Column(db.Integer, default=0, nullable=False)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class Repository(db.Model):
    """仓库模型"""
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('project.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    type = db.Column(db.String(10), nullable=False)  # 'svn' or 'git'
    category = db.Column(db.String(50))
    url = db.Column(db.String(500), nullable=False)
    server_url = db.Column(db.String(500))
    root_directory = db.Column(db.String(500))
    username = db.Column(db.String(100))
    _password = db.Column('password', db.String(512))
    _token = db.Column('token', db.String(512))
    branch = db.Column(db.String(100))
    resource_type = db.Column(db.String(20))  # 'table', 'res', 'code'
    current_version = db.Column(db.String(50))
    path_regex = db.Column(db.Text)
    log_regex = db.Column(db.Text)
    log_filter_regex = db.Column(db.Text)
    commit_filter = db.Column(db.Text)
    important_tables = db.Column(db.Text)
    unconfirmed_history = db.Column(db.Boolean, default=False)
    delete_table_alert = db.Column(db.Boolean, default=False)
    weekly_version_setting = db.Column(db.String(100))
    clone_status = db.Column(db.String(20))
    clone_error = db.Column(db.Text)
    display_order = db.Column(db.Integer)
    last_sync_commit_id = db.Column(db.String(100))
    last_sync_time = db.Column(db.DateTime)
    cache_version = db.Column(db.String(20))
    sync_mode = db.Column(db.String(20))
    
    # Table配置字段
    header_rows = db.Column(db.Integer)
    key_columns = db.Column(db.String(200))
    enable_id_confirmation = db.Column(db.Boolean)
    show_duplicate_id_warning = db.Column(db.Boolean)

    # Git特定字段
    tag_selection = db.Column(db.String(500))
    start_date = db.Column(db.DateTime)
    
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    @property
    def password(self):
        return decrypt_credential(self._password)

    @password.setter
    def password(self, value):
        self._password = encrypt_credential(value)

    @property
    def token(self):
        return decrypt_credential(self._token)

    @token.setter
    def token(self, value):
        self._token = encrypt_credential(value)
    
    def __repr__(self):
        return f'<Repository {self.name} ({self.type})>'
    
    def to_dict(self):
        """转换为字典"""
        return {
            'id': self.id,
            'project_id': self.project_id,
            'name': self.name,
            'type': self.type,
            'category': self.category,
            'url': self.url,
            'server_url': self.server_url,
            'root_directory': self.root_directory,
            'username': self.username,
            'branch': self.branch,
            'resource_type': self.resource_type,
            'current_version': self.current_version,
            'clone_status': self.clone_status,
            'clone_progress': self.clone_progress,
            'display_order': self.display_order,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
