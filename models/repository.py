#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
仓库模型
"""

from datetime import datetime, timezone
from . import db
from .big_text import BigText
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
    path_regex = db.Column(BigText)
    log_regex = db.Column(BigText)
    log_filter_regex = db.Column(BigText)
    commit_filter = db.Column(BigText)
    # 界面上那一栏「重点表名」：管理员声明的**本项目重点表**，逗号分隔（中英文逗号、
    # 分号、换行都收，见 `services.ai.project_facts.declared_important_tables`）。
    #
    # 语义与读法：命中改动路径的**表名/文件名**即算「关键路径」，会把本次周版本分析从
    # 增量升级为全量（`ai_analysis_service._decide_scope` 的 `critical_path_detected`）。
    # 匹配忽略大小写，按 basename、去扩展名的 stem、整条路径三种写法比，另允许
    # 「声明名出现在文件名里且落在名字分量的起点」（于是填「道具表」能命中
    # `[30]道具表_CfgItem.xlsx`）。判据与理由见 `project_facts.important_table_hit`。
    #
    # **这一栏以前只写不读**：界面上让管理员填，全仓没有一处读它，于是「我明明填了
    # 重点表，分析还是按增量跑」这件事既没有效果也没有解释。
    important_tables = db.Column(BigText)
    unconfirmed_history = db.Column(db.Boolean, default=False)
    delete_table_alert = db.Column(db.Boolean, default=False)
    weekly_version_setting = db.Column(db.String(100))
    clone_status = db.Column(db.String(20), default='pending')
    clone_error = db.Column(BigText)
    display_order = db.Column(db.Integer, default=0)
    last_sync_commit_id = db.Column(db.String(100))
    last_sync_time = db.Column(db.DateTime)
    cache_version = db.Column(db.String(20))
    sync_mode = db.Column(db.String(20), default='full')
    
    # Table配置字段
    header_rows = db.Column(db.Integer)
    # 列名取表头块里的第几行（1 或空 = 第 1 行，与历史上一致）。配 2 用于
    # 「第 1 行是大标题、第 2 行才是字段名」的表 —— 读取仍旧是 header=0，
    # 引擎在比较之前把列名换成这一行的取值（见 DiffService._plan_name_row）。
    header_name_row = db.Column(db.Integer)
    key_columns = db.Column(db.String(200))
    # 一个仓库里**并存多种表头格式**时的选用规则（JSON 文本；空 = 只用上面那三个标量）。
    #
    # 形状：`{"profiles": [{key,label,header_rows,header_name_row,key_columns,marker_column}],
    #        "bindings": [{match,value,profile}]}`，解析与匹配全在
    # `services/excel_header_profiles.py`。四种匹配方式：固定文件名 / 目录前缀 /
    # 路径正则 / 表头特征（只有最后一种会去读文件）。
    #
    # 为什么需要它：上面那三个标量是**仓库级**的，而一个配表仓库里可以并存多种表头
    # （实测某仓库里有 5 行自描述表头、4 行编辑器表头、1 行简化表头三种，**A 列的
    # 语义三种都不同**）—— 一个标量表达不了，按列号硬编码的解析器在它们之间必然错列。
    #
    # 为什么是文本列而不是 `db.JSON`：平台里没有任何 JSON 列，先例是
    # `AiProjectAnalysisConfig.model_price_table`（JSON 文本 + 行编辑器 UI）。
    # 行编辑器与 JSON 文本框编辑的是**同一份文本**，谁最后被编辑就以谁为准 ——
    # 两边各存一份状态的写法必然漂移，而漂移的方向是「界面上显示 A、存下去的是 B」。
    #
    # **空值 = 今天的行为**，逐字不变。平台里绝大多数仓库不会配它。
    header_profiles = db.Column(BigText)
    enable_id_confirmation = db.Column(db.Boolean, default=False)
    show_duplicate_id_warning = db.Column(db.Boolean, default=False)

    # Git特定字段
    tag_selection = db.Column(db.String(500))
    start_date = db.Column(db.DateTime)

    # 同步状态字段
    last_sync_error = db.Column(BigText)        # 最近一次同步失败的错误信息（成功后清空）
    last_sync_error_time = db.Column(db.DateTime)  # 最近一次同步失败的时间
    
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # 关系
    commits = db.relationship('Commit', backref='repository', lazy=True, cascade='all, delete-orphan')

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
            'display_order': self.display_order,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
