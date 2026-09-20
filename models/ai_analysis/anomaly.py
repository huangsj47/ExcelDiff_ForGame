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

import json
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
        """这一行的对外形态（读接口直接回它）。

        `evidence` 出成**数组**而不是库里那个 JSON 文本：它在库里是 `_json_dumps`
        存下的字符串，原样给界面，界面就得自己 `JSON.parse` —— 而解析失败时它只能
        要么丢掉证据（静默），要么显示一段带转义符的原文。归一到这一层，
        两处读法就只有一份口径。

        解析失败回空数组而不是抛：这是读路径，一条证据存坏了不该让整次读取 500
        —— 那一行其余的字段（标题、严重度、文件）都还是好的，而它们才是用户要的。

        ## 为什么中文名与时间显示在这里算（而不是界面自己映射）

        与本仓库既有的口径一致（见 `conclusion_view` 的 `scope_label` / `risk_label`）：
        **中文口径在服务端算，界面不自己映射**。两处各写一份映射表，改一处必然漏另一处，
        而漏掉的那一处显示的是英文码值 —— 它看起来像正经标识符，不会有人发现。

        时间同理，而且更硬：`disposition_at` 是**库里的 naive-UTC**，
        界面自己 `new Date()` 会按浏览器时区渲染（本机 UTC+8 时与旁边那张
        `created_at_display` 的北京时间差 8 小时），而仓库另有测试明令禁止前端做时区换算
        （见 `tests/test_ai_analysis_time_display.py`）。所以给一个算好的
        `disposition_at_display`，界面直接印。
        """
        try:
            evidence = json.loads(self.evidence) if self.evidence else []
        except (TypeError, ValueError):
            evidence = []
        if not isinstance(evidence, list):
            evidence = []
        from services.ai.report_document import (
            beijing_display,
            confidence_label,
            severity_label,
        )

        return {
            "id": self.id,
            "run_id": self.run_id,
            "fingerprint": self.fingerprint,
            "title": self.title,
            "category": self.category,
            "severity": self.severity,
            # 中文名（`严重` / `高`）。认不出的码值原样返回 —— 与
            # `report_document._label` 同一条兜底：不猜、也不显示成空。
            "severity_label": severity_label(self.severity),
            "confidence": self.confidence,
            "confidence_label": confidence_label(self.confidence),
            "evidence": evidence,
            "commit_ref": self.commit_ref,
            "file_path": self.file_path,
            "impact": self.impact,
            "suggestion": self.suggestion,
            "disposition": self.disposition,
            "disposition_by": self.disposition_by,
            "disposition_at": self.disposition_at.isoformat() if self.disposition_at else None,
            "disposition_at_display": beijing_display(self.disposition_at),
            "disposition_note": self.disposition_note,
        }

    def __repr__(self):
        return f"<AiAnalysisAnomaly {self.id} {self.severity} {self.disposition}>"
