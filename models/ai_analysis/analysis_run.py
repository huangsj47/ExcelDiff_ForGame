#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI analysis run records.
"""

from datetime import datetime, timedelta, timezone

from .. import db
from ..big_text import BigText

# 运行状态。原实现只有 pending/running/succeeded 三种，**没有 failed**，于是
# 「进程中断」「模型返回不可用」这类情况只能留下一条永远 running 的僵尸记录，
# `error_message` 列存在但从未被写入过 —— 用户看到的是「一直在分析中」，无从判断。
RUN_STATUSES = ("pending", "running", "succeeded", "failed")

# 超过这个时长仍是 running 的记录，视为僵尸（进程被杀 / 容器重启留下的）。
# 判定放在读取侧而不是靠定时清理：定时任务本身也会被杀，而读取侧判断是幂等的、
# 不依赖任何后台组件。
STALE_RUNNING_SECONDS = 3600


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
    request_payload = db.Column(BigText)
    delta_summary = db.Column(BigText)
    response_payload = db.Column(BigText)
    response_text = db.Column(BigText)
    error_message = db.Column(BigText)

    # --- 幂等与可复现标识 ---
    # 输入内容哈希 + 版本标识合成，决定「这次能不能复用上一次的结果」。
    # 这几个字段分开存而不是只存一个合成值：出问题时第一个要回答的问题是
    # 「是提示词变了、规则变了，还是只是模型换了」。
    analysis_revision = db.Column(db.String(80))
    model = db.Column(db.String(200))
    prompt_version = db.Column(db.String(80))
    skill_version = db.Column(db.String(80))
    rules_version = db.Column(db.String(80))

    # --- 本轮消耗（账要算清楚，「为什么这次慢/贵」才答得上来）---
    rounds_used = db.Column(db.Integer)
    tool_requests_used = db.Column(db.Integer)
    tokens_input = db.Column(db.Integer)
    tokens_output = db.Column(db.Integer)

    # --- prompt cache 的账（「命中缓存能省多少」只有这里答得上来）---
    # 这两列是**上游 provider 报的**命中/写入 token，与「工具结果在本次分析内的内存缓存
    # 命中」（`EngineOutcome.cache_hits`）是两件完全不同的事，界面上不许都叫「缓存」。
    #
    # `None` = 上游没报这个字段，`0` = 报了且确实是 0。这个区分必须保住：把「没报」当成
    # 「没命中」，界面就会显示一个用户会当真的 0%，而它其实只是未知。
    cache_read_tokens = db.Column(db.Integer)
    cache_write_tokens = db.Column(db.Integer)
    # 这两个数是从哪种字段形态读来的（各家命名不一，见 llm_client._extract_cache_usage）。
    # 留着它才能回答「为什么这个端点从来不上报缓存」——是端点不支持，还是形态没认出来。
    cache_source = db.Column(db.String(40))

    # 整次分析的墙钟耗时（毫秒）。逐轮的耗时刻在 trace 上。
    duration_ms = db.Column(db.Integer)
    # 按工具类型的记账（JSON 文本，键见 services/ai/context_tools.py::_STAT_COUNTERS）。
    # 用 JSON 列而不是新表：它只在「看某一次运行」时被整体读出来，没有按类型查询的需求，
    # 建表只多一次 join。
    tool_stats_json = db.Column(BigText)

    # --- 产出与裁剪记账 ---
    anomalies_found = db.Column(db.Integer)
    # 被丢弃 / 合并 / 因封顶砍掉的条数合计。**必须记账**：只报「发现 3 条」而不说
    # 「另外 5 条被门槛过滤了」，用户没法判断门槛是不是设得太严。
    dropped_count = db.Column(db.Integer)
    context_chars = db.Column(db.Integer)

    # 这条费用是按哪一版价格表算的（services/ai/pricing.py 的 PRICE_TABLE_VERSION）。
    # 价格会变，而库里存的是**金额**不是「当时的单价」——不记版本号，一年后就没人能
    # 解释那一行数字是怎么来的。价格表为空（默认）时这一列保持 NULL。
    pricing_version = db.Column(db.String(40))

    # --- 子代理模式（services/ai/subagent.py）---
    # `subagent_mode` 为 NULL = 这次没开子代理（也是所有老行的情形）；开了写 `subagents`。
    #
    # 一家子（n 个分片 + 1 次汇总）只落**这一条**运行行：它的 tokens / 轮次 / 耗时是
    # **一家子的合计**（约为单代理的 n+1 倍），逐成员的账在 trace 的 `agent` 列与
    # `response_payload["subagents"]` 里。所以预算闸门读到的「已用」天然是诚实的，
    # 运行条数也不会被灌水 —— 这两件事是「不建子运行行」的主要理由。
    subagent_mode = db.Column(db.String(20))
    subagent_count = db.Column(db.Integer)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    project = db.relationship("Project", backref="ai_analysis_runs")

    __table_args__ = (
        # 运行历史列表：WHERE project_id = ? ORDER BY created_at DESC
        db.Index("idx_ai_run_project_created", "project_id", "created_at"),
    )

    @property
    def is_stale_running(self) -> bool:
        """进程被杀留下的僵尸 running 记录。"""
        if self.status != "running":
            return False
        reference = self.started_at or self.created_at
        if reference is None:
            return False
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - reference > timedelta(seconds=STALE_RUNNING_SECONDS)

    @property
    def effective_status(self) -> str:
        """给界面用的状态：僵尸 running 显示成 failed，而不是永远转圈。

        不改库里的值 —— 那个进程可能只是慢，还没死。只是不给用户看一个永远停在
        「分析中」的界面。
        """
        return "failed" if self.is_stale_running else (self.status or "pending")

    def to_dict(self):
        return {
            "id": self.id,
            "project_id": self.project_id,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "target_key": self.target_key,
            "status": self.effective_status,
            "stored_status": self.status,
            "scope": self.scope,
            "trigger_source": self.trigger_source,
            "analysis_revision": self.analysis_revision,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "skill_version": self.skill_version,
            "rules_version": self.rules_version,
            "rounds_used": self.rounds_used,
            "tool_requests_used": self.tool_requests_used,
            "tokens_input": self.tokens_input,
            "tokens_output": self.tokens_output,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cache_source": self.cache_source,
            "duration_ms": self.duration_ms,
            "tool_stats_json": self.tool_stats_json,
            "anomalies_found": self.anomalies_found,
            "dropped_count": self.dropped_count,
            "context_chars": self.context_chars,
            "pricing_version": self.pricing_version,
            "subagent_mode": self.subagent_mode,
            "subagent_count": self.subagent_count,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }

    def __repr__(self):
        return f"<AiAnalysisRun {self.id} {self.target_type}:{self.target_id or self.target_key}>"
