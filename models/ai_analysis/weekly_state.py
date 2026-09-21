#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Weekly analysis state tracking for incremental runs.
"""

from datetime import datetime, timezone
from .. import db
from ..big_text import BigText


class AiWeeklyAnalysisState(db.Model):
    __tablename__ = "ai_weekly_analysis_state"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    group_key = db.Column(db.String(200), nullable=False, unique=True)

    base_name = db.Column(db.String(120))
    start_time = db.Column(db.DateTime)
    end_time = db.Column(db.DateTime)

    last_analyzed_at = db.Column(db.DateTime)
    last_analysis_run_id = db.Column(db.Integer)
    last_scope = db.Column(db.String(20))
    last_summary = db.Column(BigText)
    last_triggered_at = db.Column(db.DateTime)
    # 「上一次分析的是哪一份快照」的内容指纹。与 `last_analyzed_at` 的区别是**两者的
    # 判据不同**：时间水位线只在**跑完整了**的 run 上推进（降级的 run 不推进，否则模型
    # 没真读到的变更会被标成「已看过」），而这个指纹**每次跑完都写**（降级也写）——
    # 同一份输入再分析一遍不会有不同的结果，只会白花一次钱。
    # 计算与用途见 `services/ai/scope_sampling.py::weekly_snapshot_digest`。
    last_snapshot_digest = db.Column(db.String(64))

    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    project = db.relationship("Project", backref="ai_weekly_states")

    def __repr__(self):
        return f"<AiWeeklyAnalysisState {self.group_key}>"
