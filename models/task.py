#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
后台任务模型
"""

from datetime import datetime, timezone

from sqlalchemy import Index

from . import db
from .big_text import BigText


class BackgroundTask(db.Model):
    """后台任务模型"""
    __tablename__ = 'background_tasks'

    id = db.Column(db.Integer, primary_key=True)
    task_type = db.Column(db.String(50), nullable=False)  # 'excel_diff', 'cleanup_cache', etc.
    repository_id = db.Column(db.Integer, nullable=True)
    commit_id = db.Column(db.String(100), nullable=True)
    file_path = db.Column(BigText, nullable=True)
    priority = db.Column(db.Integer, default=10)  # 优先级，数字越小优先级越高
    status = db.Column(db.String(20), default='pending')  # 'pending', 'processing', 'completed', 'failed'
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    error_message = db.Column(BigText, nullable=True)
    retry_count = db.Column(db.Integer, default=0)

    # ------------------------------------------------------------------
    # 任务身份与所有权（2026-09-21 补，复测文档 AI-P0-04）
    #
    # 这五列此前**只存在于内存里的 `TaskWrapper.task_data` 载荷**：进程一重启、
    # 或者去重命中一条更早创建的任务行，这些信息就没了。实测过的那一幕 ——
    # 手工等待意图 477 登记时库里已有 scheduled 任务 468，队列复用了 468，
    # 于是那次真金白银的分析在库里被记成 `trigger_source=scheduled`，
    # 从用户点击到模型真正开始隔了 13 分 53 秒，而账单上写着「定时」。
    #
    # 落库之后，去重命中时改的是**同一条行**，来源、模式、幂等键都不会丢；
    # 重启后由 `load_pending_tasks` 从列上恢复，不再靠 `group_key + 创建时间` 猜。
    # ------------------------------------------------------------------

    # 这次是谁发起的：'manual'（用户点了按钮）/'scheduled'（调度器排的）。
    # AI 任务与等待意图都读它；服务端与页面的措辞都按它选（`ai_usage_service` 亦同）。
    trigger_source = db.Column(db.String(20), nullable=True)

    # 用户**要求**的执行模式：'incremental'（增量）/ 'full'（全量重跑）。
    # 与 `AiAnalysisRun.scope`（最终**实际**执行的模式）分开记：平台可能因为
    # 变更比例、关键路径等原因把增量请求升级成全量，那件事必须可查
    # （「我点的是增量，为什么跑了全量」不能只能靠日志回答）。
    requested_mode = db.Column(db.String(20), nullable=True)

    # 客户端幂等键。同一个键重复 POST 只认一次 —— 「关掉抽屉再打开又点一下」
    # 不该变成第二次付费调用。与 `AiAnalysisRun.active_key`（输入指纹）不是一回事：
    # 那是「同一份输入不许并发跑两遍」，这是「同一个用户动作只算一次」。
    idempotency_key = db.Column(db.String(120), nullable=True)

    # 这条任务服务于哪个分析 job（`ai_analysis_job.id`）。等待意图与分析任务
    # 靠它建立**显式**关联，不再靠 `group_key + created_at` 猜。
    # 不建外键：这个项目里跨表删除一律走显式清理（见
    # `repository_admin_handlers`），外键只会让删项目时多一处顺序约束。
    job_id = db.Column(db.Integer, nullable=True)

    # 平台侧租约到期时刻。`processing` 且租约过期的任务视为它的执行者已经死了，
    # 由启动恢复/定时清理放回 pending 或置 failed —— 这就是「一小时前的任务
    # 不能无限 pending」的判据。**不设 lease 的老行是 NULL**，读侧按「无租约」处理
    # （宁可让老行进一次恢复，也不要让它永远占着位子）。
    #
    # 注意这与 agent 侧 `AgentTask.lease_expires_at` 是**两套**机制：那一套管的是
    # 「哪个节点领走了这条下发任务」，这一套管的是「本进程的 worker 有没有在跑它」。
    # 两边都要有，因为 platform 部署模式下本进程只派发、不执行。
    lease_expires_at = db.Column(db.DateTime, nullable=True)

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
            # 身份与所有权那五列。**必须一起发出去**：它们的值现在是真的了
            # （见上面那一段），而管理页的任务列表是排查「这次分析是谁要的、
            # 为什么卡住」的第一现场 —— 不发的话，排查只能去翻数据库。
            'trigger_source': self.trigger_source,
            'requested_mode': self.requested_mode,
            'idempotency_key': self.idempotency_key,
            'job_id': self.job_id,
            'lease_expires_at': (
                self.lease_expires_at.isoformat() if self.lease_expires_at else None
            ),
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'error_message': self.error_message,
            'retry_count': self.retry_count
        }
