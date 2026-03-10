#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Weekly analysis state tracking for incremental runs.
"""

from datetime import datetime, timezone
from .. import db


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
    last_summary = db.Column(db.Text)
    last_triggered_at = db.Column(db.DateTime)

    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    project = db.relationship("Project", backref="ai_weekly_states")

    def __repr__(self):
        return f"<AiWeeklyAnalysisState {self.group_key}>"
