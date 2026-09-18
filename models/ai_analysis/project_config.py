#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Project-level AI analysis configuration."""

from datetime import datetime, timezone

from .. import db

# ---------------------------------------------------------------------------
# 默认值的**唯一事实源**
# ---------------------------------------------------------------------------
# 这些数字此前散落在服务层的模块常量里，示例配置里还有另一份，两处漂移过 8 项
# （旧工具就是这样：改了一处忘了另一处，于是「文档说的默认值」与「实际生效的默认值」
# 不一致）。现在集中在这里，服务层从这里取。
#
# 另一件必须记住的事：`_migrate_table_columns` 用 `ALTER TABLE ... ADD COLUMN` 加列，
# **不会带 DEFAULT 子句**，所以已部署的库里老行的新列是 NULL。因此列的 `default=` 只对
# 「新建的行」有效，读取时必须用 `resolved()` 兜住 NULL —— 不能假设读出来就是默认值。
DEFAULT_AUTO_WEEKLY_ENABLED = True
DEFAULT_WEEKLY_INTERVAL_MINUTES = 60
DEFAULT_MAX_FILES_PER_RUN = 200
DEFAULT_MAX_ANALYSIS_ROUNDS = 8
DEFAULT_MAX_TOOL_REQUESTS = 20
# 与「索取次数 × 单条上限」互相自洽的预算值。
#
# 变更清单现在是**全量列出**的（只有超过 `ai_analysis_service.MAX_LIST_CHARS` 才退化成
# 取样），所以摘要本身的量级从「150 个提交 600 个文件约 39,000 字符」变成了「顶到清单
# 上限、240 个提交时实测 87,055 字符」。加上平台 skill 与项目知识包（约 12,000）与历史
# 结论基线（6,000），再给 20 次 × 11,000 的上下文留足额度：
#
#     12,000 + 87,055 + 6,000 + 20 × 11,000 = 325,055  ≤  360,000
#
# 旧的 200,000 配 20 次索取装不下（差 125,000），后果是模型索要 20 个文件、其中一半被
# 截断，而它分不清是预算不够还是文件就这么大。**改这个值必须同时看
# `DEFAULT_MAX_TOOL_REQUESTS` 与 `services/ai/context_tools.DEFAULT_TOOL_LIMITS`**，
# `test_the_prompt_budget_can_honor_the_request_budget` 会拦住只顾一个的改法。
DEFAULT_PROMPT_CHAR_BUDGET = 360_000
DEFAULT_REQUEST_TIMEOUT_SECONDS = 300
DEFAULT_MIN_SEVERITY = "high"
DEFAULT_MIN_CONFIDENCE = "high"
DEFAULT_MAX_ANOMALIES_PER_RUN = 10

# 取值范围。前后端共用同一组边界：界面上的 min/max 与服务端的校验必须一致，否则会出现
# 「前端允许填、后端悄悄改掉」这种用户看不懂的行为。
MAX_ANALYSIS_ROUNDS_RANGE = (1, 30)
MAX_TOOL_REQUESTS_RANGE = (0, 100)
PROMPT_CHAR_BUDGET_RANGE = (10_000, 2_000_000)
REQUEST_TIMEOUT_RANGE = (10, 3600)
MAX_FILES_PER_RUN_RANGE = (1, 5_000)
WEEKLY_INTERVAL_RANGE = (5, 10_080)
MAX_ANOMALIES_PER_RUN_RANGE = (0, 200)
SEVERITY_CHOICES = ("high", "critical")
CONFIDENCE_CHOICES = ("high", "very_high")


def _int_or(value, fallback: int) -> int:
    """把可能是 NULL / 字符串的值读成整数，读不出来就用默认值。

    数据库里这些列在老库上是 NULL；在某些后端上读回来还可能是字符串。
    """
    if value is None:
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


class AiProjectAnalysisConfig(db.Model):
    __tablename__ = "ai_project_analysis_config"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), unique=True, nullable=False)

    auto_weekly_enabled = db.Column(db.Boolean, default=DEFAULT_AUTO_WEEKLY_ENABLED)
    weekly_interval_minutes = db.Column(db.Integer, default=DEFAULT_WEEKLY_INTERVAL_MINUTES)
    max_files_per_run = db.Column(db.Integer, default=DEFAULT_MAX_FILES_PER_RUN)
    # 语义已调整为「项目级补充指令」：整个提示词由平台内置 skill 承载，这一栏只做补充。
    # 历史项目里存过的自定义提示词照旧生效（降级为补充指令），不会丢配置。
    prompt_template = db.Column(db.Text)

    # --- 连接配置（OpenAI 兼容端点）---
    api_base_url = db.Column(db.String(500))
    api_model = db.Column(db.String(200))

    # --- 分析策略 ---
    max_analysis_rounds = db.Column(db.Integer, default=DEFAULT_MAX_ANALYSIS_ROUNDS)
    max_tool_requests = db.Column(db.Integer, default=DEFAULT_MAX_TOOL_REQUESTS)
    prompt_char_budget = db.Column(db.Integer, default=DEFAULT_PROMPT_CHAR_BUDGET)
    request_timeout_seconds = db.Column(db.Integer, default=DEFAULT_REQUEST_TIMEOUT_SECONDS)

    # --- 告警门槛（规则侧，改这里不用改提示词）---
    min_severity = db.Column(db.String(20), default=DEFAULT_MIN_SEVERITY)
    min_confidence = db.Column(db.String(20), default=DEFAULT_MIN_CONFIDENCE)
    max_anomalies_per_run = db.Column(db.Integer, default=DEFAULT_MAX_ANOMALIES_PER_RUN)

    # --- 项目补充知识（在项目知识包之外追加，只补充不覆盖）---
    project_knowledge = db.Column(db.Text)

    # --- 模型单价表（算费用用，JSON 文本）---
    # 留空 = 用平台的默认表（`services/ai/pricing.py::DEFAULT_PRICE_TABLE`，出厂是空的）。
    # **单价必须由用户提供**：接口只回 token 数、不回金额，编一份单价出来会被当成真钱看。
    model_price_table = db.Column(db.Text)

    updated_by = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    project = db.relationship("Project", backref="ai_analysis_config")

    def resolved(self) -> dict:
        """把这一行读成「实际生效的配置」，NULL 用默认值补齐。

        **所有读取路径都必须走这里**。直接读 `config.min_severity` 在老库上会拿到
        `None`，而下游拿 `None` 当门槛的后果是「什么都过滤不掉」—— 报出一堆低置信度的
        条目，而且没有任何报错。
        """
        return {
            "auto_weekly_enabled": (
                DEFAULT_AUTO_WEEKLY_ENABLED
                if self.auto_weekly_enabled is None
                else bool(self.auto_weekly_enabled)
            ),
            "weekly_interval_minutes": _int_or(
                self.weekly_interval_minutes, DEFAULT_WEEKLY_INTERVAL_MINUTES
            ),
            "max_files_per_run": _int_or(self.max_files_per_run, DEFAULT_MAX_FILES_PER_RUN),
            "prompt_template": self.prompt_template or "",
            "api_base_url": (self.api_base_url or "").strip(),
            "api_model": (self.api_model or "").strip(),
            "max_analysis_rounds": _int_or(
                self.max_analysis_rounds, DEFAULT_MAX_ANALYSIS_ROUNDS
            ),
            "max_tool_requests": _int_or(self.max_tool_requests, DEFAULT_MAX_TOOL_REQUESTS),
            "prompt_char_budget": _int_or(self.prompt_char_budget, DEFAULT_PROMPT_CHAR_BUDGET),
            "request_timeout_seconds": _int_or(
                self.request_timeout_seconds, DEFAULT_REQUEST_TIMEOUT_SECONDS
            ),
            "min_severity": (self.min_severity or DEFAULT_MIN_SEVERITY).strip().lower(),
            "min_confidence": (self.min_confidence or DEFAULT_MIN_CONFIDENCE).strip().lower(),
            "max_anomalies_per_run": _int_or(
                self.max_anomalies_per_run, DEFAULT_MAX_ANOMALIES_PER_RUN
            ),
            "project_knowledge": self.project_knowledge or "",
            # 空串 = 没有项目单价表（用平台默认表）。**不是「免费」**，读取侧据此返回
            # 「还没有配置价格表」而不是 0（见 services/ai/usage.py 的口径）。
            "model_price_table": self.model_price_table or "",
        }

    def __repr__(self):
        return f"<AiProjectAnalysisConfig project={self.project_id}>"
