#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每轮分析的 trace。

## 为什么不写文件

旧工具把每轮的状态写成 JSON/CSV 文件，于是：多进程会互相覆盖、容器重启就丢、
并发写没有锁、通知与落盘之间有窗口。这个平台是单进程多线程跑（Flask 自带服务器），
但**重启丢 trace 这件事与进程数无关** —— 出问题的时候恰恰最需要回看 trace，而那时
往往已经重启过了。所以入库，走正常事务。

## 为什么要存「原始回答」

解析失败的时候，「模型到底返回了什么」是唯一能定位问题的证据。只记一个「解析失败」
的结论，事后只能靠猜（是超时截断了？还是它把 JSON 包在围栏里？还是它压根没按协议来？）。
原始回答可能很长，所以按 run 维度留存、并给上层一个清理入口。
"""

from datetime import datetime, timezone

from .. import db
from ..big_text import BigText

# 一轮的结束方式。与 protocol.STATUSES 不同 —— 这里描述的是**我们观察到的结果**，
# 包含「没走到解析」的几种情形。
TRACE_OUTCOMES = (
    "need_more_context",
    "final",
    "protocol_error",
    "transport_error",
    "budget_exhausted",
    "degraded_markdown",
)


class AiAnalysisTrace(db.Model):
    __tablename__ = "ai_analysis_trace"

    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(
        db.Integer, db.ForeignKey("ai_analysis_run.id"), nullable=False, index=True
    )
    round_index = db.Column(db.Integer, nullable=False)
    # 这一轮是**谁**跑的。NULL/空 = 常规的单代理运行（也是子代理模式里主代理自己那几轮）；
    # `S1`/`S2`… = 第几个分片代理（见 `services/ai/subagent.py`）。
    agent = db.Column(db.String(20))
    # 这一轮在**那个成员内部**的序号（`S1` 的第 2 轮就是 2）。`round_index` 是**整个家族
    # 内全局递增**的序号 —— 一家子只落一条 `ai_analysis_run`，两个成员的「第 1 轮」必须
    # 在库里区分得开，否则会撞上 `uq_ai_trace_run_round`。界面显示「S1 · 第 2/4 轮」用的
    # 是这个列，不是 `round_index`。
    agent_round = db.Column(db.Integer)

    outcome = db.Column(db.String(30))
    parsed_ok = db.Column(db.Boolean, default=False)

    # 提示词与回答。`request_chars` 只记长度，完整提示词不入库（体积不划算，
    # 而且提示词里含变更数据，与 AiAnalysisRun.request_payload 重复）。
    request_chars = db.Column(db.Integer)
    response_text = db.Column(BigText)
    error = db.Column(BigText)
    correction_hint = db.Column(BigText)

    # 模型索要的 / 实际执行的 / 被丢弃的，各自存 JSON 文本。
    requests_json = db.Column(BigText)
    executed_json = db.Column(BigText)
    dropped_json = db.Column(BigText)
    budget_notes = db.Column(BigText)

    context_chars = db.Column(db.Integer)
    tokens_input = db.Column(db.Integer)
    tokens_output = db.Column(db.Integer)
    # 逐轮的 prompt cache 账目。`None` = 上游这一轮没报（**不是「没命中」**）。
    # 逐轮记的理由：输入 token 是**累计值**（每轮把上一轮的上下文重发一遍），
    # 所以「钱花在第几轮」只有逐轮列出来才看得出。
    cache_read_tokens = db.Column(db.Integer)
    cache_write_tokens = db.Column(db.Integer)
    duration_ms = db.Column(db.Integer)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    run = db.relationship("AiAnalysisRun", backref="traces")

    # 一轮一行。这个唯一约束是安全的：轮次在一次分析内顺序递增，引擎每轮只写一次。
    # 它挡住的是「重试逻辑不小心把同一轮写了两遍」这种 bug —— 那种情况下 trace 会出现
    # 两条自相矛盾的记录（一条说解析失败、一条说成功），比直接报错难查得多。
    #
    # **子代理模式没有为它做任何迁移**：一家子只落一条运行，成员各自的轮次在
    # `aggregate_outcomes` 里被重编成家族内全局递增的序号（顺序执行 → 确定），
    # 所以「run_id + round_index」在这里依然是唯一的那一对。
    __table_args__ = (
        db.UniqueConstraint("run_id", "round_index", name="uq_ai_trace_run_round"),
    )

    def __repr__(self):
        return f"<AiAnalysisTrace run={self.run_id} round={self.round_index} {self.outcome}>"
