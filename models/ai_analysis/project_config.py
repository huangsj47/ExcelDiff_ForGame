#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Project-level AI analysis configuration."""

from datetime import datetime, timezone
from .. import db


class AiProjectAnalysisConfig(db.Model):
    __tablename__ = "ai_project_analysis_config"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), unique=True, nullable=False)

    auto_weekly_enabled = db.Column(db.Boolean, default=True)
    weekly_interval_minutes = db.Column(db.Integer, default=60)
    max_files_per_run = db.Column(db.Integer, default=200)
    prompt_template = db.Column(db.Text)

    updated_by = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    project = db.relationship("Project", backref="ai_analysis_config")

    def __repr__(self):
        return f"<AiProjectAnalysisConfig project={self.project_id}>"
