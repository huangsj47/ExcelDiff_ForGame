#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Project-bound API key storage for AI analysis.
"""

from datetime import datetime, timezone
from .. import db


class AiProjectApiKey(db.Model):
    __tablename__ = "ai_project_api_key"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False, unique=True)
    encrypted_key = db.Column(db.Text, nullable=False)
    updated_by = db.Column(db.String(100))

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    project = db.relationship(
        "Project",
        backref=db.backref("ai_api_key", uselist=False),
    )

    def __repr__(self):
        return f"<AiProjectApiKey project_id={self.project_id}>"
