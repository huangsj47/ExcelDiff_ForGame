#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI analysis run records.
"""

from datetime import datetime, timezone
from .. import db


class AiAnalysisRun(db.Model):
    __tablename__ = "ai_analysis_run"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)

    target_type = db.Column(db.String(20), nullable=False)  # commit / weekly
    target_id = db.Column(db.Integer, nullable=True)  # commit_id or config_id
    target_key = db.Column(db.String(200), nullable=True)  # weekly group key

    status = db.Column(db.String(20), default="pending")
    response_mode = db.Column(db.String(20), default="streaming")
    scope = db.Column(db.String(20), default="full")  # full / incremental
    trigger_source = db.Column(db.String(20), default="manual")  # manual / scheduled

    trace_id = db.Column(db.String(80))
    request_payload = db.Column(db.Text)
    delta_summary = db.Column(db.Text)
    response_payload = db.Column(db.Text)
    response_text = db.Column(db.Text)
    error_message = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    project = db.relationship("Project", backref="ai_analysis_runs")

    def __repr__(self):
        return f"<AiAnalysisRun {self.id} {self.target_type}:{self.target_id or self.target_key}>"
