#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 分析产出的单条异常，以及它的人工处置状态。

## 为什么要单独一张表

原来异常只是 `ai_analysis_run.response_payload` 里的一段 JSON。那样的东西**没法被处置**：
「已确认 / 已忽略」没有地方存，跟进的人只能对着一段文本自己记。单独一行一条之后，
处置状态、处置人、处置时间都能落下，而且「已忽略的条目在后续重跑中不再提示」才有依据。

## 为什么 disposition 存英文码

存 `pending` / `confirmed` / `ignored` 而不是中文：这些值会进筛选条件与 URL 查询参数，
ASCII 码值在任何后端、任何编码设置下都不会出问题，中文标签只出现在界面上
（`DISPOSITION_LABELS`）。
"""

from datetime import datetime, timezone

from .. import db
from ..big_text import BigText

# 处置状态。**改这里就要同步改 `DISPOSITION_LABELS`**（有一条测试盯着这件事）。
DISPOSITIONS = ("pending", "confirmed", "ignored")
DISPOSITION_LABELS = {
    "pending": "待确认",
    "confirmed": "已确认",
    "ignored": "已忽略",
}
DEFAULT_DISPOSITION = "pending"


class AiAnalysisAnomaly(db.Model):
    __tablename__ = "ai_analysis_anomaly"

    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(
        db.Integer, db.ForeignKey("ai_analysis_run.id"), nullable=False, index=True
    )
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False, index=True)

    # 异常的身份指纹（见 services/ai/rules.py::anomaly_fingerprint）。
    # 用于把上一轮的处置继承到这一轮：模型每次重跑措辞都会略有不同，靠标题原文匹配不上。
    fingerprint = db.Column(db.String(32), index=True)

    title = db.Column(db.String(500), nullable=False)
    category = db.Column(db.String(50))
    severity = db.Column(db.String(20))
    confidence = db.Column(db.String(20))
    # 证据留存为 JSON 数组文本（与 AiAnalysisRun.request_payload 等保持一致的存法）。
    evidence = db.Column(BigText)
    commit_ref = db.Column(db.String(100))
    file_path = db.Column(db.String(500))
    impact = db.Column(BigText)
    suggestion = db.Column(BigText)

    disposition = db.Column(db.String(20), default=DEFAULT_DISPOSITION, index=True)
    disposition_by = db.Column(db.String(100))
    disposition_at = db.Column(db.DateTime)
    disposition_note = db.Column(BigText)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    run = db.relationship("AiAnalysisRun", backref="anomalies")

    # 注意这里**没有**给 (run_id, fingerprint) 加唯一约束。
    # 去重是 services/ai/rules.py 的职责，且它有自己的记账（「哪条被合并了」）。再加一道
    # 唯一约束并不会让去重更正确，却会让「去重逻辑回归」从「报告里多一条重复项」升级成
    # 「整个分析在写库时 IntegrityError 失败」—— 用一个更严重的故障去兜一个更轻的问题。
    __table_args__ = (db.Index("idx_ai_anomaly_run_disposition", "run_id", "disposition"),)

    def to_dict(self):
        return {
            "id": self.id,
            "run_id": self.run_id,
            "fingerprint": self.fingerprint,
            "title": self.title,
            "category": self.category,
            "severity": self.severity,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "commit_ref": self.commit_ref,
            "file_path": self.file_path,
            "impact": self.impact,
            "suggestion": self.suggestion,
            "disposition": self.disposition,
            "disposition_by": self.disposition_by,
            "disposition_at": self.disposition_at.isoformat() if self.disposition_at else None,
            "disposition_note": self.disposition_note,
        }

    def __repr__(self):
        return f"<AiAnalysisAnomaly {self.id} {self.severity} {self.disposition}>"
